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


# Ключи поддеревьев, где лежит именно выдача поиска. Проверяется вхождением:
# hh.ru и площадки на том же движке зовут их по-разному и переименовывают.
SEARCH_KEYS = ("vacancysearchresult", "vacancysearch", "searchvacancy", "searchresult")


def _subtrees(state: Any, limit: int = 200_000) -> list[Any]:
    """Поддеревья с результатами поиска. Пусто — таких ключей на странице нет."""
    out: list[Any] = []
    queue: list[Any] = [state]
    seen = 0
    while queue:
        node = queue.pop(0)
        seen += 1
        if seen > limit:
            break
        if isinstance(node, dict):
            for key, value in node.items():
                if any(part in str(key).lower() for part in SEARCH_KEYS):
                    out.append(value)
                else:
                    queue.append(value)
        elif isinstance(node, list):
            queue.extend(node)
    return out


def _walk(root: Any, limit: int) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    queue: list[Any] = [root]
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
    return found


def find_vacancy_nodes(state: Any, limit: int = 500) -> list[dict[str, Any]]:
    """Вакансии из выдачи поиска.

    Раньше собиралось всё, что похоже на вакансию, в любом месте состояния. На
    той же странице живут «Похожие вакансии», «Вакансии дня» и рекламные
    подборки — и они приезжали в базу наравне с выдачей. По запросу «Ревизор»
    так набирались повара и упаковщики: искали одно, собиралось другое.

    Поэтому сначала берутся поддеревья, чей ключ похож на результат поиска
    (`SEARCH_KEYS`), и вакансии ищутся только внутри них. К точному пути
    по-прежнему не привязываемся: hh.ru переименовывал эти ключи не раз. Если
    подходящего ключа нет вовсе — обходится всё состояние, как раньше, чтобы
    смена разметки не оставила прогон без вакансий [CORE-017].
    """
    found: dict[str, dict[str, Any]] = {}
    for subtree in _subtrees(state):
        for key, node in _walk(subtree, limit - len(found)).items():
            found.setdefault(key, node)
        if len(found) >= limit:
            break
    if found:
        return list(found.values())
    log.info("выдачи поиска в состоянии не нашлось, смотрю всю страницу")
    return list(_walk(state, limit).values())


def first_of(node: dict[str, Any], *keys: str) -> Any:
    """Первое непустое значение. Ключ проверяется и с решёткой перед ним.

    В состоянии страницы часть полей лежит под именем с `@`: `@workSchedule`,
    `@responseLetterRequired`. Это внутренняя пометка hh.ru, а не другое поле,
    и знать о ней должен один этот хелпер, а не каждое место разбора.
    """
    for key in keys:
        for name in (key, "@" + key) if not key.startswith("@") else (key,):
            value = node.get(name)
            if value not in (None, "", [], {}):
                return value
    return None


# Идентификаторы графика и занятости hh.ru. Если в состоянии лежит id, а не
# название, показывать владельцу «fullDay» нельзя, а скоринг ищет в этой строке
# признак удалёнки по-русски [CORE-015].
HH_NAMES = {
    "fullDay": "Полный день",
    "shift": "Сменный график",
    "flexible": "Гибкий график",
    "remote": "Удалённая работа",
    "flyInFlyOut": "Вахтовый метод",
    "REMOTE": "Удалённая работа",
    "HYBRID": "Гибрид",
    "ON_SITE": "На месте работодателя",
    "FIELD_WORK": "Разъездная работа",
    "full": "Полная занятость",
    "part": "Частичная занятость",
    "project": "Проектная работа",
    "volunteer": "Волонтёрство",
    "probation": "Стажировка",
    "FULL": "Полная занятость",
    "PART": "Частичная занятость",
    "PROJECT": "Проектная работа",
    # Дни и часы приходят своими идентификаторами.
    "FIVE_ON_TWO_OFF": "5/2",
    "TWO_ON_TWO_OFF": "2/2",
    "SIX_ON_ONE_OFF": "6/1",
    "THREE_ON_THREE_OFF": "3/3",
    "FOUR_ON_FOUR_OFF": "4/4",
    "FOUR_ON_THREE_OFF": "4/3",
    "ONE_ON_THREE_OFF": "1/3",
    "HOURS_4": "4 часа",
    "HOURS_6": "6 часов",
    "HOURS_8": "8 часов",
    "HOURS_12": "12 часов",
    "HOURS_24": "24 часа",
    "START_AFTER_SIXTEEN": "старт после 16:00",
    "FROM_FOUR_TO_SIX_HOURS_IN_A_DAY": "4–6 часов в день",
}


def name_of(value: Any) -> str | None:
    """Поле может быть строкой, словарём с name/title/$ или списком таких.

    Список — не экзотика, а текущая вёрстка hh.ru: после редизайна график и
    формат работы приходят массивами (`workFormat: [{"name": "Удалённо"}]`).
    Пока список возвращал None, поля `schedule` и `employment` оставались
    пустыми у почти всех вакансий, и пайплайн спрашивал у модели то, что
    источник уже прислал.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return HH_NAMES.get(value, value)
    if isinstance(value, dict):
        for key in ("name", "title", "text", "$", "trl", "@type", "type", "id"):
            if isinstance(value.get(key), str):
                return HH_NAMES.get(value[key], value[key])
        # Обёртка вокруг одного значения: `{"workScheduleByDaysElement":
        # ["FIVE_ON_TWO_OFF"]}`. Имя ключа своё у каждого поля, поэтому
        # разворачиваем по форме, а не по списку имён.
        if len(value) == 1:
            return name_of(next(iter(value.values())))
        return None
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            part = name_of(item)
            if part and part not in parts:
                parts.append(part)
        return ", ".join(parts) or None
    return None


# Один вопрос «как работать» hh.ru разложил по нескольким ключам: формат
# (удалённо/гибрид/офис), дни недели и часы. Модель вакансии держит одну
# строку, поэтому склеиваем — скоринг ищет в ней признак удалёнки, а карточка
# показывает как есть.
SCHEDULE_KEYS = (
    "workFormat",
    "workFormats",
    "workSchedule",
    "schedule",
    "workScheduleByDays",
    "workingHours",
)


def schedule_of(node: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for key in SCHEDULE_KEYS:
        part = name_of(first_of(node, key))
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts) or None


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
        schedule=schedule_of(node),
        experience=name_of(first_of(node, "workExperience", "experience")),
        employment=name_of(first_of(node, "employmentForm", "employment", "employmentType")),
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
