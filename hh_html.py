"""Сбор вакансий hh.ru из HTML страниц поиска (ADR-015).

Публичный `GET /vacancies` закрыт с апреля 2026: всем неавторизованным прилетает 403.
Поэтому данные берём из того же JSON, который hh.ru отдаёт браузеру внутри страницы.

Важное свойство кода ниже: он не знает точной структуры страницы и не опирается на
один жёсткий путь. Сначала извлекается любой найденный JSON состояния, потом по нему
идёт обход в поисках объектов, похожих на вакансию, и только затем — резервный разбор
разметки. При редизайне шанс выжить выше, а диагностика понятнее (probe_hh.py).
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import random
import re
import time
from typing import Any, Iterator, Sequence

import httpx

from hh import Vacancy, strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://hh.ru/search/vacancy"
VACANCY_PREFIX = "https://hh.ru/vacancy/"

# hh.ru не возвращает результаты глубже 2000 позиций.
HH_MAX_SEARCH_RESULTS = 2000

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


class BlockedError(RuntimeError):
    """hh.ru показал капчу или забанил запросы."""


class ExtractionError(RuntimeError):
    """Страница пришла, но вакансии из неё достать не удалось."""


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
        published_at=_name_of(_first(node, "publicationTime", "creationTime", "publishedAt")),
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
    """Один поток, паузы с дрожанием, собственный backoff. Скромность дешевле бана."""

    def __init__(
        self,
        pause: float = 2.0,
        timeout: float = 30.0,
        cookie: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self.pause = pause
        headers = dict(BROWSER_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy,
        )

    def __enter__(self) -> "HHHtmlClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _sleep(self) -> None:
        time.sleep(self.pause + random.uniform(0, 1.0))

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
                    raise BlockedError(
                        "hh.ru требует капчу или блокирует запросы. Открой hh.ru в браузере, "
                        "пройди капчу и положи свежие cookie в HH_COOKIE, либо увеличь HH_PAUSE"
                    )
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            self._sleep()
            return body
        raise RuntimeError("unreachable")

    def search(
        self,
        text: str,
        area: int | Sequence[int] | None = None,
        period: int = 7,
        per_page: int = 50,
        max_pages: int = 3,
        extra: dict[str, Any] | None = None,
    ) -> Iterator[Vacancy]:
        # hh.ru сообщает об ошибке при запросе за пределами первых 2000 результатов.
        # Не отправляем такой запрос: считаем этот предел концом выдачи.
        max_search_pages = (HH_MAX_SEARCH_RESULTS + per_page - 1) // per_page
        pages_to_fetch = min(max_pages, max_search_pages)
        if max_pages > pages_to_fetch:
            log.info(
                "hh.ru: конец доступной выдачи на %s-й странице (лимит %s результатов)",
                pages_to_fetch,
                HH_MAX_SEARCH_RESULTS,
            )

        for page in range(pages_to_fetch):
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

            body = self.fetch(SEARCH_URL, params)
            try:
                nodes = find_vacancy_nodes(extract_state(body))
                vacancies = [node_to_vacancy(n) for n in nodes if _first(n, "vacancyId", "id")]
            except ExtractionError as exc:
                log.warning("JSON состояния не найден (%s), иду по разметке", exc)
                vacancies = parse_cards_fallback(body)

            vacancies = [v for v in vacancies if v.external_id and v.title]
            log.info("страница %s: вакансий %s", page, len(vacancies))
            yield from vacancies
            if len(vacancies) < per_page:
                break

    def vacancy(self, vacancy_id: str) -> dict[str, Any]:
        """Карточка вакансии со страницы: описание и навыки полностью."""
        body = self.fetch(VACANCY_PREFIX + str(vacancy_id))
        try:
            state = extract_state(body)
        except ExtractionError:
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
        }
