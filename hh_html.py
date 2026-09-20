"""Сбор вакансий hh.ru из HTML страниц поиска (ADR-015).

Публичный `GET /vacancies` закрыт с апреля 2026: всем неавторизованным прилетает 403.
Поэтому данные берём из того же JSON, который hh.ru отдаёт браузеру внутри страницы.

Важное свойство кода ниже: он не знает точной структуры страницы и не опирается на
один жёсткий путь. Сначала извлекается любой найденный JSON состояния, потом по нему
идёт обход в поисках объектов, похожих на вакансию, и только затем — резервный разбор
разметки. При редизайне шанс выжить выше, а диагностика понятнее (probe_hh.py).

Когда разбор всё-таки ломается, сырая страница падает в data/failures/. Без неё
починка парсера превращается в гадание: к следующему запуску hh.ru уже отдаст
другую верстку, и воспроизвести сбой нечем.
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import httpx

from hh import Vacancy, normalize_published_at, strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://hh.ru/search/vacancy"
VACANCY_PREFIX = "https://hh.ru/vacancy/"
FAILURE_DIR = "data/failures"
FAILURE_KEEP = 5

# Обычные браузерные заголовки. Без Accept-Language hh.ru охотнее показывает капчу.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

STATE_PATTERNS = (
    # Основной вариант hh.ru: состояние в <template id="HH-Lux-InitialState">.
    re.compile(
        r'<template[^>]+id="HH-Lux-InitialState"[^>]*>(?P<json>.*?)</template>',
        re.DOTALL,
    ),
    re.compile(
        r'<template[^>]+id="HH-Lux-(?:Redux-)?InitialState"[^>]*>(?P<json>.*?)</template>',
        re.DOTALL,
    ),
    re.compile(r'window\.__INITIAL_STATE__\s*=\s*(?P<json>\{.*?\})\s*;?\s*</script>', re.DOTALL),
    re.compile(r'id="__NEXT_DATA__"[^>]*>(?P<json>\{.*?\})</script>', re.DOTALL),
)

CAPTCHA_MARKERS = ("captcha", "подтвердите, что вы не робот", "вы не робот")

# В сохраняемой странице могут оказаться сессионные токены. Файл лежит в data/
# (она в .gitignore), но владелец может прислать его в issue — лучше вырезать сразу.
SECRET_RE = re.compile(
    r"(hhtoken|hhuid|_xsrf|xsrf|sessid|GMT|crypted_id)=([^;\"'\s<>]{6,})", re.IGNORECASE
)


class BlockedError(RuntimeError):
    """hh.ru показал капчу или забанил запросы."""


class ExtractionError(RuntimeError):
    """Страница пришла, но вакансии из неё достать не удалось."""


def scrub(page: str, secrets: Iterable[str] = ()) -> str:
    """Убирает из страницы наши cookie и похожие на токены значения."""
    for secret in secrets:
        if secret:
            page = page.replace(secret, "***")
    return SECRET_RE.sub(lambda m: f"{m.group(1)}=***", page)


def prune_failures(directory: str | Path, keep: int = FAILURE_KEEP) -> list[Path]:
    """Оставляет только `keep` самых свежих дампов: страница hh.ru — это ~1 МБ."""
    files = sorted(Path(directory).glob("*.html"))
    removed: list[Path] = []
    for path in files[: max(0, len(files) - keep)]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            log.debug("не смог удалить старый дамп %s", path)
    return removed


def dump_failure(
    page: str,
    reason: str,
    directory: str | Path = FAILURE_DIR,
    secrets: Iterable[str] = (),
    keep: int = FAILURE_KEEP,
) -> Path:
    """Кладёт сырую страницу на диск и возвращает путь к файлу."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-")[:40] or "failure"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"{stamp}-{slug}.html"
    path.write_text(scrub(page, secrets), encoding="utf-8", errors="replace")
    prune_failures(directory, keep)
    return path


def extract_state(page: str) -> dict[str, Any]:
    """Вытаскивает JSON состояния страницы первым сработавшим способом."""
    for index, pattern in enumerate(STATE_PATTERNS):
        match = pattern.search(page)
        if not match:
            continue
        raw = html_mod.unescape(match.group("json").strip())
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.debug("стратегия %s нашла блок, но JSON не разобрался: %s", index, exc)
            continue
        if isinstance(state, dict):
            log.debug(
                "состояние извлечено стратегией %s, корневые ключи: %s", index, list(state)[:12]
            )
            return state
    raise ExtractionError(
        "не нашёл JSON состояния на странице; запусти probe_hh.py и пришли его вывод"
    )


def _looks_like_vacancy(node: dict[str, Any]) -> bool:
    has_id = any(k in node for k in ("vacancyId", "id"))
    has_name = isinstance(node.get("name"), str) and node["name"].strip() != ""
    has_context = any(
        k in node
        for k in ("compensation", "salary", "company", "employer", "area", "links", "workSchedule")
    )
    return has_id and has_name and has_context


def find_vacancy_nodes(state: Any, limit: int = 500) -> list[dict[str, Any]]:
    """Обходит состояние в ширину и собирает всё, что похоже на вакансию.

    Специально не привязываемся к конкретному пути вида vacancySearchResult.vacancies:
    hh.ru переименовывал эти ключи не раз.
    """
    found: dict[str, dict[str, Any]] = {}
    queue: list[Any] = [state]
    seen = 0
    while queue and len(found) < limit:
        node = queue.pop(0)
        seen += 1
        if seen > 200_000:
            break
        if isinstance(node, dict):
            if _looks_like_vacancy(node):
                key = str(node.get("vacancyId") or node.get("id"))
                found.setdefault(key, node)
            else:
                queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return list(found.values())


def _first(node: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _name_of(value: Any) -> str | None:
    """Поле может быть строкой, либо словарём с name/title/$."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "title", "text", "$", "trl"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def node_to_vacancy(node: dict[str, Any]) -> Vacancy:
    """Приводит сырой узел страницы к той же модели Vacancy, что была у API."""
    vacancy_id = str(_first(node, "vacancyId", "id") or "")
    compensation = _first(node, "compensation", "salary", "salaryRange") or {}
    if not isinstance(compensation, dict):
        compensation = {}
    company = _first(node, "company", "employer") or {}
    if not isinstance(company, dict):
        company = {"name": _name_of(company)}
    links = node.get("links") if isinstance(node.get("links"), dict) else {}

    url = (
        _first(node, "alternateUrl", "alternate_url", "url")
        or (links.get("desktop") if isinstance(links, dict) else None)
        or VACANCY_PREFIX + str(vacancy_id)
    )
    if isinstance(url, str) and url.startswith("/"):
        url = "https://hh.ru" + url

    snippet_parts: list[str] = []
    snippet = node.get("snippet")
    if isinstance(snippet, dict):
        snippet_parts += [
            strip_html(snippet.get(part)) for part in ("requirement", "responsibility", "text")
        ]
    for key in ("workExperienceText", "description", "descriptionText"):
        if isinstance(node.get(key), str):
            snippet_parts.append(strip_html(node[key]))
    for key in ("keySkills", "key_skills", "skills"):
        raw_skills = node.get(key)
        if isinstance(raw_skills, dict):
            raw_skills = raw_skills.get("keySkill") or raw_skills.get("items")
        if isinstance(raw_skills, list):
            skills = [s for s in (_name_of(item) for item in raw_skills) if s]
            break
    else:
        skills = []

    gross = compensation.get("gross")
    currency = _first(compensation, "currencyCode", "currency", "code")

    # Дата публикации приходит в десятке форматов — нормализация живёт в hh.py.
    published_raw = _first(
        node,
        "publicationTime",
        "publicationDate",
        "creationTime",
        "publishedAt",
        "published_at",
        "publicationTimeText",
    )

    return Vacancy(
        source="hh.ru",
        external_id=vacancy_id,
        url=url,
        title=_name_of(node.get("name")) or "",
        company=_name_of(company.get("name")) or _name_of(company),
        company_id=str(company["id"]) if company.get("id") else None,
        area=_name_of(_first(node, "area", "region", "city")),
        salary_from=_first(compensation, "from", "salaryFrom"),
        salary_to=_first(compensation, "to", "salaryTo"),
        currency=_name_of(currency),
        gross=bool(gross) if gross is not None else None,
        schedule=_name_of(_first(node, "workSchedule", "schedule", "workFormat")),
        experience=_name_of(_first(node, "workExperience", "experience")),
        employment=_name_of(_first(node, "employment", "employmentForm")),
        skills=skills,
        description=" ".join(p for p in snippet_parts if p).strip(),
        published_at=normalize_published_at(published_raw),
    )


CARD_LINK_RE = re.compile(r'href="(https://[^"]*?/vacancy/(\d+)[^"]*)"[^>]*>(?P<title>[^<]{3,200})<')


def parse_cards_fallback(page: str) -> list[Vacancy]:
    """Грубый резерв: только ссылка и заголовок из разметки.

    Скоринг без вилки и описания будет бедным, но прогон не умрёт целиком:
    детали потом доберутся со страницы вакансии.
    """
    out: dict[str, Vacancy] = {}
    for match in CARD_LINK_RE.finditer(page):
        url, vacancy_id, title = match.group(1), match.group(2), match.group("title")
        title = html_mod.unescape(title).strip()
        if vacancy_id in out or not title:
            continue
        out[vacancy_id] = Vacancy(external_id=vacancy_id, url=url.split("?")[0], title=title)
    return list(out.values())


class HHHtmlClient:
    """Один поток, паузы с дрожанием, собственный backoff. Скромность дешевле бана.

    Клиент сам считает признаки нездоровья (pages_fetched, fallback_pages, empty_pages,
    blocked, failures) — по ним прогон решает, писать ли владельцу (canary.py).
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
    ) -> None:
        # Пауза адаптивная: HH_PAUSE — верхняя граница и точка возврата, а не
        # постоянная величина. На чистых ответах она снижается до HH_PAUSE_MIN,
        # на 403/429/капче возвращается к максимуму. Раньше каждый из тысячи
        # запросов ждал одинаковые «безопасные» 2–3 секунды.
        self.pause_max = max(0.0, float(pause))
        self.pause_min = max(0.0, min(float(pause_min), self.pause_max))
        self.pause = self.pause_max
        self.failure_dir = failure_dir
        self.pages_fetched = 0
        self.fallback_pages = 0
        self.empty_pages = 0
        # Кэш страниц выдачи на прогон (hh_pages.PageCache). Профили часто ищут
        # одно и то же; без кэша каждый платит за страницу заново (B-15).
        self.cache = cache
        # Сколько раз обход остановился на полностью известной странице.
        self.known_stops = 0
        # Дошли ли до конца выдачи в последнем search: по нему прогон
        # отличает «вакансии кончились» от «упёрлись в свой потолок».
        self.exhausted = False
        self.blocked = False
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

    def _sleep(self) -> None:
        time.sleep(self.pause + random.uniform(0, 1.0))

    def _ease(self) -> None:
        """Ответ чистый — идём чуть быстрее, но не быстрее нижней границы."""
        self.pause = max(self.pause_min, self.pause * 0.85)

    def _back_off(self) -> None:
        """Ответ подозрительный — сразу к верхней границе, без полумер."""
        self.pause = self.pause_max

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
            response.raise_for_status()
            self.pages_fetched += 1
            self._ease()
            self._sleep()
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
        страницы отдавали половину. Обход всё равно конечен — hh.ru отдаёт
        пустую страницу, а при зацикливании выдачи страница приходит без единого
        нового id, и это тоже конец.

        `known_page` — третий конец (B-15): страница целиком уже в базе. Выдача
        отсортирована по дате публикации, значит дальше лежит только более старое,
        и платить за него паузами незачем. Вакансии со страницы всё равно отдаются:
        «видна в выдаче» — факт, который нужен истории (ADR-009).
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
                body = self.fetch(SEARCH_URL, params)
                try:
                    nodes = find_vacancy_nodes(extract_state(body))
                    vacancies = [node_to_vacancy(n) for n in nodes if _first(n, "vacancyId", "id")]
                except ExtractionError as exc:
                    log.warning("JSON состояния не найден (%s), иду по разметке", exc)
                    self.fallback_pages += 1
                    self._back_off()
                    self._dump(body, "no-state")
                    vacancies = parse_cards_fallback(body)

                vacancies = [v for v in vacancies if v.external_id and v.title]
                if not vacancies and page == 0:
                    # Пустая первая страница по широкому запросу — повод посмотреть глазами.
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
        """Карточка вакансии со страницы: описание и навыки полностью."""
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
            return {"description": description, "key_skills": []}

        nodes = find_vacancy_nodes(state)
        best: dict[str, Any] = {}
        for node in nodes:
            if str(_first(node, "vacancyId", "id")) == str(vacancy_id):
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
        raw_skills = _first(best, "keySkills", "key_skills", "skills")
        if isinstance(raw_skills, dict):
            raw_skills = raw_skills.get("keySkill") or raw_skills.get("items")
        if isinstance(raw_skills, list):
            skills = [s for s in (_name_of(item) for item in raw_skills) if s]

        return {
            "description": description,
            "key_skills": [{"name": s} for s in skills],
            "schedule": {"name": _name_of(_first(best, "workSchedule", "schedule"))},
            "employer": {"name": _name_of(_first(best, "company", "employer"))},
            "published_at": _first(
                best, "publicationTime", "publicationDate", "creationTime", "publishedAt"
            ),
        }
