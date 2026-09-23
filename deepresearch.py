"""Глубокий ресёрч по одной компании: суды, долги, банкротство, новости.

Зачем это отдельно от dossier.py. Досье отвечает на вопрос «что пишут бывшие
сотрудники» и ходит по закрытому списку отзовиков. Здесь другое: что про
контору говорят реестр, суд, приставы и новости. Эти источники отвечают на
«стоит ли связываться» точнее десяти отзывов, но стоят времени, поэтому ресёрч
запускается кнопкой по выбранной компании, а не в ночном прогоне [CORE-016].

Как устроен обход.

1. Круг первый — детерминированные шаблоны запросов по темам
   (deepresearch_rules.TOPICS). Модель не участвует нигде [CORE-015].
2. Из страницы реестра регулярками вынимаются ИНН и дата регистрации.
3. Круг второй идёт только от факта: есть ИНН — спрашиваем суды, долги и
   банкротство по ИНН. Запросов «из головы» здесь нет по построению.

Границы, которые здесь соблюдаются:

- капча и защита от ботов не обходятся: источник помечается недоступным, обход
  идёт к следующему [CORE-017];
- ограничения только по времени: число запросов и страниц не режется, дедлайн
  проверяется перед каждым походом в сеть (решение владельца 19.09.2026);
- собираем про юрлицо, а не про людей: ФИО из страниц не извлекаются
  [CORE-012], [CORE-013];
- находка без ссылки и даты не сохраняется [HRD-003];
- страница засчитывается компании, только если её название (или ИНН) реально
  встретилось в тексте: одноимённых контор в реестрах много.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

import company_key
import deepresearch_rules as R
import deepresearch_store as store
import reviewpage
import settings
import websearch

log = logging.getLogger("fuckhr")

CACHE_DAYS = 30
TIMEOUT = 20.0


@dataclass(frozen=True)
class DeepOptions:
    """Настройки ресёрча. Потолков по запросам и страницам нет — только время."""

    enabled: bool
    seconds: float
    ttl_days: int
    in_score: bool


def options() -> DeepOptions:
    return DeepOptions(
        enabled=settings.flag("DEEP_ENABLED"),
        seconds=max(30.0, settings.as_float(settings.get("DEEP_TIME_BUDGET", "300"), 300.0)),
        ttl_days=max(0, settings.as_int(settings.get("DEEP_TTL_DAYS", "30"), 30)),
        in_score=settings.flag("DEEP_IN_SCORE"),
    )


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def trust_of(domain: str) -> float:
    for suffix, value in R.DOMAIN_TRUST:
        if domain == suffix or domain.endswith("." + suffix):
            return value
    return R.DEFAULT_TRUST


def blocked_page(text: str) -> bool:
    low = text[:2000].lower()
    return any(marker in low for marker in R.BLOCK_MARKERS)


def quote_around(text: str, marker: str) -> str:
    """Кусок текста вокруг маркера. Цитата, а не пересказ: пересказ — работа
    модели, а модель здесь не участвует."""
    low = text.lower()
    at = low.find(marker)
    if at < 0:
        return ""
    start = max(0, at - R.QUOTE_CHARS // 2)
    piece = " ".join(text[start : at + R.QUOTE_CHARS // 2].split())
    return piece.strip(" -—|")


def mentions(text: str, company: str, inn: str = "") -> bool:
    """Страница действительно про эту контору?

    ИНН — точный признак. Названию верим по самому длинному значащему слову:
    «ООО «Ромашка»» на странице про «Ромашка-Строй» — другая компания, но
    полное совпадение строки в реестрах встречается редко.
    """
    low = text.lower()
    if inn and inn in low:
        return True
    words = [w for w in company_key.normalize(company).split() if len(w) > 3]
    if not words:
        return False
    return max(words, key=len) in low


class Researcher:
    """Один ресёрч. Дедлайн общий на всю работу, кэш страниц — в базе."""

    def __init__(
        self,
        conn,
        provider: websearch.SearchProvider,
        seconds: float,
        transport: Callable[[str], str] | None = None,
        cache_days: int = CACHE_DAYS,
        pause: float = R.PAUSE,
    ) -> None:
        self.conn = conn
        self.provider = provider
        self.deadline = time.monotonic() + max(1.0, seconds)
        self.transport = transport
        self.cache_days = cache_days
        self.pause = pause if transport is None else 0.0
        self.queries = 0
        self.pages = 0
        self.blocked: list[str] = []
        # Тексты этого прогона: реквизиты вынимаются из них, а не из кэша —
        # кэш может быть выключен, а второй круг всё равно должен состояться.
        self.texts: dict[str, str] = {}
        store.ensure_schema(conn)

    # —— время ——

    @property
    def out_of_time(self) -> bool:
        return time.monotonic() >= self.deadline

    # —— сеть ——

    def _http_get(self, url: str) -> str:
        import httpx

        response = httpx.get(
            url,
            headers=reviewpage.BROWSER_HEADERS,
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    def fetch(self, url: str) -> str:
        """Текст страницы. Никогда не бросает исключение [CORE-017]."""
        cached = store.page_get(self.conn, url, self.cache_days)
        if cached is not None:
            self.texts[url] = cached
            return cached
        domain = domain_of(url)
        if domain in self.blocked:
            return ""
        try:
            raw = (self.transport or self._http_get)(url)
        except Exception as exc:  # noqa: BLE001 — недоступный источник не срывает ресёрч
            log.warning("страница не открылась (%s): %s", url, exc)
            self._block(domain, "не отвечает")
            return ""
        self.pages += 1
        if blocked_page(raw):
            log.warning("капча или защита на %s — идём к следующему источнику", domain)
            self._block(domain, "капча")
            return ""
        text = reviewpage.strip_tags(raw)[: R.MAX_PAGE_CHARS]
        self.texts[url] = text
        store.page_put(self.conn, url, text)
        if self.pause:
            time.sleep(self.pause)
        return text

    def _block(self, domain: str, why: str) -> None:
        if domain and domain not in self.blocked:
            self.blocked.append(domain)
            log.info("источник %s пропущен: %s", domain, why)

    def links(self, query: str) -> list[str]:
        self.queries += 1
        out: list[str] = []
        for hit in self.provider.search(query, R.HITS_PER_QUERY):
            domain = domain_of(hit.url)
            if not domain or domain in self.blocked:
                continue
            if any(domain == s or domain.endswith("." + s) for s in R.STOP_DOMAINS):
                continue
            out.append(hit.url)
        return out

    # —— темы ——

    def topic(
        self, company: str, spec: R.Topic, inn: str = ""
    ) -> list[store.Finding]:
        found: list[store.Finding] = []
        seen: set[str] = set()
        for template in spec.queries:
            if self.out_of_time:
                break
            query = template.format(company=company, inn=inn)
            for url in self.links(query):
                if self.out_of_time:
                    break
                if url in seen:
                    continue
                seen.add(url)
                text = self.fetch(url)
                if not text or not mentions(text, company, inn):
                    continue
                low = text.lower()
                marker = next((m for m in spec.markers if m in low), "")
                if not marker:
                    continue
                found.append(
                    store.Finding(
                        code=spec.code,
                        url=url,
                        domain=domain_of(url),
                        quote=quote_around(text, marker),
                        trust=trust_of(domain_of(url)),
                        observed_at=datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                    )
                )
        return found

    def requisites(self, findings: list[store.Finding]) -> tuple[str, str]:
        """ИНН и дата регистрации из страниц реестра. Только регулярками
        [CORE-019]: число, которое назвала модель, — не число."""
        inn = ""
        registered = ""
        for item in findings:
            text = self.texts.get(item.url, "")
            if not inn:
                match = R.INN_RE.search(text)
                if match:
                    inn = match.group(1)
            if not registered:
                match = R.REG_DATE_RE.search(text)
                if match:
                    registered = match.group(1)
        return inn, registered

    def run(self, company: str) -> store.Report:
        started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        store.start(self.conn, company)
        findings: list[store.Finding] = []
        registry: list[store.Finding] = []

        for spec in R.TOPICS:
            if spec.round != 1:
                continue
            if self.out_of_time:
                break
            log.info("тема: %s", spec.title)
            items = self.topic(company, spec)
            findings += items
            if spec.lookup:
                registry += items

        inn, registered = self.requisites(registry)
        if inn:
            log.info("реквизиты: ИНН %s, регистрация %s", inn, registered or "?")
            for spec in R.TOPICS:
                if spec.round != 2 or self.out_of_time:
                    continue
                log.info("тема: %s", spec.title)
                findings += self.topic(company, spec, inn=inn)
        elif not self.out_of_time:
            log.info("ИНН не нашёлся — второй круг не запускаем")

        status = "время вышло" if self.out_of_time else "готово"
        if not self.provider.enabled:
            status = "поиск выключен"
        report = store.Report(
            company=company,
            status=status,
            started_at=started,
            finished_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            queries=self.queries,
            pages=self.pages,
            blocked=tuple(self.blocked),
            inn=inn,
            registered_at=registered,
            note="",
            findings=tuple(findings),
        )
        store.save(self.conn, report)
        log.info(
            "ресёрч по «%s»: находок %s, запросов %s, страниц %s, пропущено источников %s",
            company,
            len(findings),
            self.queries,
            self.pages,
            len(self.blocked),
        )
        return report


def research(conn, company: str, seconds: float | None = None) -> store.Report:
    """Ресёрч по компании с настройками из .env."""
    opts = options()
    provider = websearch.SearchProvider.from_env(conn)
    if not provider.enabled:
        log.warning("внешний поиск выключен: %s", provider.disabled_reason)
    worker = Researcher(conn, provider, seconds if seconds is not None else opts.seconds)
    return worker.run(company)


__all__ = (
    "DeepOptions",
    "Researcher",
    "blocked_page",
    "domain_of",
    "mentions",
    "options",
    "quote_around",
    "research",
    "trust_of",
)
