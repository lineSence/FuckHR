"""Сбор вакансий hh.ru из HTML страниц поиска (ADR-015).

Публичный `GET /vacancies` закрыт с апреля 2026: неавторизованным 403. Данные
берём из того же JSON, который hh.ru отдаёт браузеру внутри страницы.

Код не знает точной структуры страницы: сначала извлекается любой найденный
JSON состояния, потом обход в поисках объектов, похожих на вакансию, и только
затем резервный разбор разметки. При редизайне шанс выжить выше, а диагностика
понятнее (probe_hh.py). Сломавшаяся страница падает в data/failures/: без неё
починка парсера — гадание, hh.ru к следующему запуску отдаст другую вёрстку.

Сам разбор живёт в `hh_parse.py`, здесь — клиент и его темп.
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import httpx

import net_rate

from hh import Vacancy, strip_html
from hh_parse import (  # noqa: F401 — публичные имена остаются у hh_html
    BROWSER_HEADERS,
    CAPTCHA_MARKERS,
    CARD_LINK_RE,
    FAILURE_DIR,
    FAILURE_KEEP,
    HOST,
    MAX_RESULTS,
    SEARCH_URL,
    SECRET_RE,
    SNIPPET_KEYS,
    STATE_PATTERNS,
    VACANCY_PREFIX,
    BlockedError,
    ExtractionError,
    MissingPageError,
    dump_failure,
    extract_state,
    find_vacancy_nodes,
    node_to_vacancy,
    first_of,
    name_of,
    parse_cards_fallback,
    schedule_of,
    prune_failures,
    scrub,
)

log = logging.getLogger(__name__)

class HHHtmlClient:
    """Клиент hh.ru: темп держит общий бакет, поэтому потоков может быть много.

    Клиент считает признаки нездоровья (pages_fetched, fallback_pages,
    empty_pages, blocked, failures) — по ним прогон решает, писать ли
    владельцу (canary.py).
    """

    def __init__(
        self,
        pause: float = 2.0,
        pause_min: float = 0.8,
        timeout: float = 30.0,
        cookie: str | None = None,
        proxy: str | None = None,
        failure_dir: str | Path | None = FAILURE_DIR,
        cache: Any | None = None,
        bucket: "net_rate.Bucket | None" = None,
    ) -> None:
        # Пауза адаптивная: HH_PAUSE — верхняя граница и точка возврата. На
        # чистых ответах она снижается до HH_PAUSE_MIN, на 403/429/капче
        # возвращается к максимуму. Хранит её общий на процесс бакет
        # (net_rate): темп считается на хост, а не на клиента, поэтому
        # карточки можно качать пулом, не ускоряя обращения к hh.ru.
        self.pause_max = max(0.0, float(pause))
        self.pause_min = max(0.0, min(float(pause_min), self.pause_max))
        self.bucket = bucket if bucket is not None else net_rate.bucket(
            HOST, self.pause_max, self.pause_min
        )
        self.failure_dir = failure_dir
        self.pages_fetched = 0
        self.fallback_pages = 0
        self.empty_pages = 0
        # Кэш страниц выдачи на прогон (hh_pages.PageCache). Профили часто ищут
        # одно и то же; без кэша каждый платит за страницу заново (B-15).
        self.cache = cache
        # Сколько раз обход остановился на полностью известной странице.
        self.known_stops = 0
        # Дошли ли до конца выдачи в последнем search: отличает
        # «вакансии кончились» от «упёрлись в свой потолок».
        self.exhausted = False
        self.blocked = False
        # Сколько секунд простояли в очереди к hh.ru: по этому числу видно,
        # держит ли темп бакет или запросы и так редкие.
        self.waited = 0.0
        self.failures: list[str] = []
        self._cookie = cookie
        headers = dict(BROWSER_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        self._client = self._open(headers, timeout, proxy)

    @staticmethod
    def _open(headers: dict[str, str], timeout: float, proxy: str | None) -> httpx.Client:
        """HTTP/2 экономит на рукопожатиях при сотнях запросов к одному хосту.

        Он требует пакет h2. Его может не быть в старом окружении, и ронять из-за
        этого прогон нельзя: сбор работал и без HTTP/2 [CORE-017].
        """
        common = {
            "headers": headers,
            "timeout": timeout,
            "follow_redirects": True,
            "proxy": proxy,
        }
        try:
            return httpx.Client(http2=True, **common)
        except ImportError:
            log.info("пакет h2 не установлен, идём к hh.ru по HTTP/1.1")
            return httpx.Client(**common)

    def __enter__(self) -> "HHHtmlClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def pause(self) -> float:
        """Текущий интервал между запросами. Живёт в бакете хоста."""
        return self.bucket.interval

    def _wait_turn(self) -> None:
        """Очередь к hh.ru. Ждём до запроса, а не после: иначе пауза одного
        потока не перекрывается ожиданием ответа у другого."""
        self.waited += self.bucket.take()

    def _ease(self) -> None:
        """Ответ чистый — идём чуть быстрее, но не быстрее нижней границы."""
        self.bucket.ease()

    def _back_off(self) -> None:
        """Ответ подозрительный — сразу к верхней границе, без полумер."""
        self.bucket.back_off()

    def _dump(self, body: str, reason: str) -> None:
        """Сохраняет страницу для разбора. Ошибка записи не должна рвать прогон."""
        if not self.failure_dir:
            return
        try:
            path = dump_failure(
                body,
                reason,
                self.failure_dir,
                secrets=[self._cookie] if self._cookie else [],
            )
        except OSError as exc:
            log.warning("не смог сохранить страницу сбоя: %s", exc)
            return
        self.failures.append(str(path))
        log.warning("сырая страница сохранена: %s (%s)", path, reason)

    def fetch(self, url: str, params: dict[str, Any] | None = None, attempts: int = 3) -> str:
        delay = 5.0
        for attempt in range(1, attempts + 1):
            self._wait_turn()
            response = self._client.get(url, params=params)
            body = response.text
            lowered = body[:4000].lower()
            if response.status_code in (403, 429) or any(m in lowered for m in CAPTCHA_MARKERS):
                log.warning(
                    "hh.ru ответил %s на %s, попытка %s/%s",
                    response.status_code,
                    url,
                    attempt,
                    attempts,
                )
                if attempt == attempts:
                    self.blocked = True
                    self._dump(body, f"blocked-{response.status_code}")
                    raise BlockedError(
                        "hh.ru требует капчу или блокирует запросы. Открой hh.ru в браузере, "
                        "пройди капчу и положи свежие cookie в HH_COOKIE, либо увеличь HH_PAUSE"
                    )
                self._back_off()
                time.sleep(delay)
                delay *= 2
                continue
            if response.status_code in (404, 410):
                # Нет страницы: конец выдачи, снятая вакансия, битая ссылка.
                raise MissingPageError(f"hh.ru: страницы нет ({response.status_code}) {url}")
            response.raise_for_status()
            self.pages_fetched += 1
            self._ease()
            return body
        raise RuntimeError("unreachable")

    def search(
        self,
        text: str,
        area: int | Sequence[int] | None = None,
        period: int = 7,
        per_page: int = 50,
        max_pages: int = 0,
        extra: dict[str, Any] | None = None,
        known_page: Callable[[Sequence[Vacancy]], bool] | None = None,
    ) -> Iterator[Vacancy]:
        """max_pages=0 — идти до конца выдачи.

        Потолка страниц по умолчанию нет: при лимите в тысячу вакансий три
        страницы отдавали половину. Обход конечен — концом считается пустая
        страница, страница без единого нового id (выдача пошла по кругу),
        404 за последней страницей и `known_page` (B-15): страница целиком в
        базе, а выдача отсортирована по дате, значит дальше только старее.
        Вакансии со страницы всё равно отдаются: «видна в выдаче» — факт для
        истории (ADR-009).
        """
        from hh_pages import cache_key  # локально: hh_pages тянет настройки

        seen_ids: set[str] = set()
        self.exhausted = False
        page = 0
        while not max_pages or page < max_pages:
            params: dict[str, Any] = {
                "text": text,
                "search_period": period,
                "items_on_page": per_page,
                "page": page,
                "order_by": "publication_time",
                "search_field": ["name", "company_name", "description"],
                "disableBrowserCache": "true",
            }
            if area is not None:
                params["area"] = area if isinstance(area, int) else list(area)
            if extra:
                params.update(extra)

            key = cache_key(params)
            cached = self.cache.get(key, page) if self.cache is not None else None
            if cached is not None:
                log.info("страница %s: взята из кэша прогона", page)
                vacancies = list(cached)
            else:
                try:
                    body = self.fetch(SEARCH_URL, params)
                except MissingPageError:
                    # hh.ru отдаёт 404 на странице за последней — выдача кончилась.
                    log.info("страница %s за концом выдачи (404), дальше нечего брать", page)
                    self.exhausted = True
                    break
                try:
                    nodes = find_vacancy_nodes(extract_state(body))
                    vacancies = [node_to_vacancy(n) for n in nodes if first_of(n, "vacancyId", "id")]
                except ExtractionError as exc:
                    log.warning("JSON состояния не найден (%s), иду по разметке", exc)
                    self.fallback_pages += 1
                    self._back_off()
                    self._dump(body, "no-state")
                    vacancies = parse_cards_fallback(body)

                vacancies = [v for v in vacancies if v.external_id and v.title]
                if not vacancies and page == 0:
                    # Пустая первая страница — повод посмотреть глазами.
                    self.empty_pages += 1
                    self._dump(body, "empty-search")
                if self.cache is not None:
                    self.cache.put(key, page, vacancies)

            fresh = [v for v in vacancies if v.external_id not in seen_ids]
            seen_ids.update(v.external_id for v in fresh)
            log.info("страница %s: вакансий %s", page, len(vacancies))
            yield from fresh
            if not vacancies:
                self.exhausted = True
                break
            if not fresh:
                # hh.ru после последней страницы повторяет предыдущую.
                log.info("выдача пошла по кругу на странице %s, дальше нечего брать", page)
                self.exhausted = True
                break
            if (page + 1) * per_page >= MAX_RESULTS:
                log.info(
                    "страница %s — потолок выдачи hh.ru в %s результатов, дальше её нет",
                    page,
                    MAX_RESULTS,
                )
                self.exhausted = True
                break
            if known_page is not None and known_page(vacancies):
                self.known_stops += 1
                log.info(
                    "страница %s целиком известна, дальше только старше — останавливаемся",
                    page,
                )
                self.exhausted = True
                break
            page += 1
        else:
            self.exhausted = False

    def vacancy(self, vacancy_id: str) -> dict[str, Any]:
        """Карточка вакансии: описание, навыки и адрес из одного состояния."""
        import geo  # noqa: PLC0415 — точка из того же состояния [CORE-016]

        body = self.fetch(VACANCY_PREFIX + str(vacancy_id))
        try:
            state = extract_state(body)
        except ExtractionError:
            self._dump(body, "vacancy-no-state")
            description = ""
            match = re.search(
                r'data-qa="vacancy-description"[^>]*>(?P<html>.*?)</div>', body, re.DOTALL
            )
            if match:
                description = strip_html(match.group("html"))
            return {"description": description, "key_skills": [], "address": None}

        nodes = find_vacancy_nodes(state)
        best: dict[str, Any] = {}
        for node in nodes:
            if str(first_of(node, "vacancyId", "id")) == str(vacancy_id):
                best = node
                break
        if not best and nodes:
            best = nodes[0]

        description = ""
        for key in ("description", "descriptionText", "branded_description"):
            value = best.get(key)
            if isinstance(value, str) and value.strip():
                description = strip_html(value)
                break

        skills: list[str] = []
        raw_skills = first_of(best, "keySkills", "key_skills", "skills")
        if isinstance(raw_skills, dict):
            raw_skills = raw_skills.get("keySkill") or raw_skills.get("items")
        if isinstance(raw_skills, list):
            skills = [s for s in (name_of(item) for item in raw_skills) if s]

        return {
            "description": description,
            "key_skills": [{"name": s} for s in skills],
            "address": geo.from_state(state, vacancy_id),
            "schedule": {"name": schedule_of(best)},
            "experience": {"name": name_of(first_of(best, "workExperience", "experience"))},
            "employment": {
                "name": name_of(first_of(best, "employment", "employmentForm", "employmentType"))
            },
            "employer": {"name": name_of(first_of(best, "company", "employer"))},
            "published_at": first_of(
                best, "publicationTime", "publicationDate", "creationTime", "publishedAt"
            ),
        }
