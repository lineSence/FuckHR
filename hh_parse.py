"""Разбор страниц hh.ru: состояние, узлы вакансий, резервный разбор разметки.

Вынесено из `hh_html.py`, чтобы оба файла держались в пределах 25 КБ
[CORE-024]. Здесь нет сети: только строки страницы на входе и вакансии на
выходе — так разбор проверяется тестами без запросов.
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hh import Vacancy, normalize_published_at, strip_html

log = logging.getLogger(__name__)

SEARCH_URL = "https://hh.ru/search/vacancy"
# hh.ru отдаёт не больше 2000 результатов на запрос: страницы за этой границей
# либо повторяют предыдущую, либо отвечают 404. Перебирать их — чистая трата
# пауз, а на частом запросе это десятки запросов в хвосте (docs/performance.md).
MAX_RESULTS = 2000
HOST = "hh.ru"  # ключ бакета темпа: один на процесс, а не на клиента
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


# Ключи сниппета: длинные у hh.ru, короткие (req/resp/cond) у zarplata.ru на
# том же движке. Без коротких описание внешней площадки терялось целиком, а
# без описания вакансия не добирала до порога профиля.
SNIPPET_KEYS = ("requirement", "responsibility", "text", "req", "resp", "cond")


class BlockedError(RuntimeError):
    """hh.ru показал капчу или забанил запросы."""


class ExtractionError(RuntimeError):
    """Страница пришла, но вакансии из неё достать не удалось."""


class MissingPageError(RuntimeError):
    """hh.ru ответил 404/410: страницы нет. За концом выдачи это норма."""


def scrub(page: str, secrets: Iterable[str] = ()) -> str:
    """Убирает из страницы наши cookie и похожие на токены значения."""
    for secret in secrets:
        if secret:
            page = page.replace(secret, "***")
    return SECRET_RE.sub(lambda m: f"{m.group(1)}=***", page)


def prune_failures(directory: str | Path, keep: int = FAILURE_KEEP) -> list[Path]:
    """Оставляет `keep` свежих дампов: страница hh.ru — это ~1 МБ."""
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

    К пути вида vacancySearchResult.vacancies не привязываемся: hh.ru
    переименовывал эти ключи не раз.
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


def first_of(node: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def name_of(value: Any) -> str | None:
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
    vacancy_id = str(first_of(node, "vacancyId", "id") or "")
    compensation = first_of(node, "compensation", "salary", "salaryRange") or {}
    if not isinstance(compensation, dict):
        compensation = {}
    company = first_of(node, "company", "employer") or {}
    if not isinstance(company, dict):
        company = {"name": name_of(company)}
    links = node.get("links") if isinstance(node.get("links"), dict) else {}

    url = (
        first_of(node, "alternateUrl", "alternate_url", "url")
        or (links.get("desktop") if isinstance(links, dict) else None)
        or VACANCY_PREFIX + str(vacancy_id)
    )
    if isinstance(url, str) and url.startswith("/"):
        url = "https://hh.ru" + url

    snippet_parts: list[str] = []
    snippet = node.get("snippet")
    if isinstance(snippet, dict):
        snippet_parts += [strip_html(snippet.get(part)) for part in SNIPPET_KEYS]
    for key in ("workExperienceText", "description", "descriptionText"):
        if isinstance(node.get(key), str):
            snippet_parts.append(strip_html(node[key]))
    for key in ("keySkills", "key_skills", "skills"):
        raw_skills = node.get(key)
        if isinstance(raw_skills, dict):
            raw_skills = raw_skills.get("keySkill") or raw_skills.get("items")
        if isinstance(raw_skills, list):
            skills = [s for s in (name_of(item) for item in raw_skills) if s]
            break
    else:
        skills = []

    gross = compensation.get("gross")
    currency = first_of(compensation, "currencyCode", "currency", "code")

    # Дата публикации приходит в десятке форматов — нормализация живёт в hh.py.
    published_raw = first_of(
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
        title=name_of(node.get("name")) or "",
        company=name_of(company.get("name")) or name_of(company),
        company_id=str(company["id"]) if company.get("id") else None,
        area=name_of(first_of(node, "area", "region", "city")),
        salary_from=first_of(compensation, "from", "salaryFrom"),
        salary_to=first_of(compensation, "to", "salaryTo"),
        currency=name_of(currency),
        gross=bool(gross) if gross is not None else None,
        schedule=name_of(first_of(node, "workSchedule", "schedule", "workFormat")),
        experience=name_of(first_of(node, "workExperience", "experience")),
        employment=name_of(first_of(node, "employment", "employmentForm")),
        skills=skills,
        description=" ".join(p for p in snippet_parts if p).strip(),
        published_at=normalize_published_at(published_raw),
    )


CARD_LINK_RE = re.compile(r'href="(https://[^"]*?/vacancy/(\d+)[^"]*)"[^>]*>(?P<title>[^<]{3,200})<')


def parse_cards_fallback(page: str) -> list[Vacancy]:
    """Грубый резерв: только ссылка и заголовок из разметки.

    Скоринг без вилки будет бедным, но прогон не умрёт: детали доберутся
    потом со страницы вакансии.
    """
    out: dict[str, Vacancy] = {}
    for match in CARD_LINK_RE.finditer(page):
        url, vacancy_id, title = match.group(1), match.group(2), match.group("title")
        title = html_mod.unescape(title).strip()
        if vacancy_id in out or not title:
            continue
        out[vacancy_id] = Vacancy(external_id=vacancy_id, url=url.split("?")[0], title=title)
    return list(out.values())
