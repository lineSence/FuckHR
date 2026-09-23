"""Модель Vacancy и нормализация (ADR-010).

Исторически здесь жил клиент hh.ru Open API. С апреля 2026 публичный GET /vacancies
отдаёт 403 всем неавторизованным, поэтому сбор переехал в hh_html.py (ADR-015).
Клиент HHClient оставлен: он сразу заработает, если появится токен приложения
(передаётся аргументом token, переменной в .env под него нет).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

import httpx
from pydantic import BaseModel, Field

import injection

log = logging.getLogger(__name__)

API_ROOT = "https://api.hh.ru"
TAG_RE = re.compile(r"<[^>]+>")

WS_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)

# Слова, которые не должны мешать дедупу: «Python Developer» и «Python-разработчик (Senior)»
# должны схлопнуться в один ключ.
TITLE_NOISE = {
    "вакансия",
    "удаленно",
    "удаленка",
    "remote",
    "senior",
    "middle",
    "junior",
    "lead",
    "сеньор",
    "миддл",
    "старший",
    "ведущий",
}
COMPANY_NOISE = {"ооо", "ао", "зао", "пао", "ип", "llc", "ltd", "inc", "group", "группа"}


class Vacancy(BaseModel):
    """Единая модель вакансии. Адаптеры Habr Career и карьерных страниц придут сюда же."""

    source: str = "hh.ru"
    external_id: str
    url: str
    title: str
    company: str | None = None
    company_id: str | None = None
    area: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str | None = None
    gross: bool | None = None
    schedule: str | None = None
    experience: str | None = None
    employment: str | None = None
    skills: list[str] = Field(default_factory=list)
    description: str = ""
    published_at: str | None = None

    @property
    def key(self) -> str:
        """Ключ дедупа (company_norm, title_norm).

        simhash(description) добавится на шаге 2 — без него две разные вакансии
        с одинаковым названием в одной компании считаются одной. Для MVP это
        приемлемо: повторная публикация как раз и есть то, что мы ловим.
        """
        raw = f"{normalize(self.company or '', COMPANY_NOISE)}|{normalize(self.title, TITLE_NOISE)}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return f"{self.source}:{digest}"

    def monthly_salary_net(self) -> int | None:
        """Грубая оценка net: gross уменьшается на 13% НДФЛ."""
        value = self.salary_from or self.salary_to
        if value is None:
            return None
        if self.currency and self.currency.upper() not in {"RUR", "RUB"}:
            return None
        return int(value * 0.87) if self.gross else int(value)


def normalize(text: str, noise: set[str] = frozenset()) -> str:
    text = text.lower().replace("\u0451", "\u0435").replace("-", " ")
    text = NON_WORD_RE.sub(" ", text)
    words = [w for w in WS_RE.sub(" ", text).strip().split() if w and w not in noise]
    return " ".join(sorted(words))


def strip_html(value: str | None) -> str:
    """HTML → текст. Скрытые вёрсткой блоки выбрасываются до снятия тегов.

    После strip_tags спрятанный текст неотличим от обычного, а прячут в нём
    ровно одно — инструкции для ИИ-ассистента (ADR-020).
    """
    if not value:
        return ""
    visible, _hidden = injection.drop_hidden(value)
    return WS_RE.sub(" ", TAG_RE.sub(" ", visible)).strip()


# --- Дата публикации ----------------------------------------------------------
#
# У API это было аккуратное поле published_at в ISO-8601. В HTML на его месте может
# оказаться что угодно: миллисекунды эпохи, вложенный объект, «вчера» или «12 сентября».
# Пустое значение опаснее кривого: republish_count молча вернёт ноль, и главный сигнал
# детектора брехни (ADR-009, ADR-010) окажется мёртвым, а заметно это станет через месяцы.

RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}

ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?")
DMY_RE = re.compile(r"(\d{1,2})\s+([а-яе]+)(?:\s+(\d{4}))?")
RELATIVE_RE = re.compile(r"(\d+)\s*(минут|дней|дня|день|сут|час|недел|месяц)")
NESTED_DATE_KEYS = (
    "iso",
    "value",
    "$",
    "date",
    "time",
    "text",
    "timestamp",
    "@timestamp",
    "publicationTime",
    "creationTime",
)


def _from_epoch(value: float) -> str:
    seconds = value / 1000.0 if abs(value) > 1e11 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")


def normalize_published_at(
    value: Any, now: datetime | None = None, _depth: int = 0
) -> str | None:
    """Приводит дату публикации к ISO-строке. Возвращает None, если распознать нечем.

    Сознательно возвращаем дату без времени там, где времени не было: republish_count
    всё равно считает по дню, а выдуманное время создаёт ложную точность.
    """
    if value is None or isinstance(value, bool) or _depth > 3:
        return None
    now = now or datetime.now(timezone.utc)

    if isinstance(value, (int, float)):
        return _from_epoch(float(value))

    if isinstance(value, dict):
        for key in NESTED_DATE_KEYS:
            nested = value.get(key)
            if nested in (None, "", [], {}):
                continue
            found = normalize_published_at(nested, now, _depth + 1)
            if found:
                return found
        return None

    if isinstance(value, (list, tuple)):
        for item in value:
            found = normalize_published_at(item, now, _depth + 1)
            if found:
                return found
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.lstrip("-").isdigit() and len(text.lstrip("-")) >= 9:
        return _from_epoch(float(text))

    iso = ISO_DATE_RE.search(text)
    if iso:
        day = f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
        if iso.group(4) is None:
            return day
        return f"{day}T{iso.group(4)}:{iso.group(5)}:{iso.group(6) or '00'}"

    lowered = text.lower().replace("\u0451", "\u0435")
    if "позавчера" in lowered:
        return (now - timedelta(days=2)).date().isoformat()
    if "вчера" in lowered:
        return (now - timedelta(days=1)).date().isoformat()
    if "сегодня" in lowered or "только что" in lowered or "сейчас" in lowered:
        return now.date().isoformat()

    relative = RELATIVE_RE.search(lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit == "минут":
            delta = timedelta(minutes=amount)
        elif unit == "час":
            delta = timedelta(hours=amount)
        elif unit == "недел":
            delta = timedelta(days=7 * amount)
        elif unit == "месяц":
            delta = timedelta(days=30 * amount)
        else:
            delta = timedelta(days=amount)
        return (now - delta).date().isoformat()

    dmy = DMY_RE.search(lowered)
    if dmy:
        month = RU_MONTHS.get(dmy.group(2)) or RU_MONTHS.get(dmy.group(2)[:3])
        if month:
            year = int(dmy.group(3)) if dmy.group(3) else now.year
            try:
                candidate = datetime(year, month, int(dmy.group(1)), tzinfo=timezone.utc)
            except ValueError:
                return None
            # «12 сентября» в январе означает прошлый год, а не будущее.
            if dmy.group(3) is None and candidate > now + timedelta(days=1):
                candidate = candidate.replace(year=year - 1)
            return candidate.date().isoformat()

    log.debug("не разобрал дату публикации: %r", text[:80])
    return None


class HHClient:
    """Клиент Open API. В пайплайне не используется: поиск закрыт без токена (ADR-015)."""

    def __init__(
        self,
        user_agent: str,
        pause: float = 0.34,
        timeout: float = 20.0,
        token: str | None = None,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "user_agent должен содержать контакт, например 'FuckHR/0.1 (me@example.com)'"
            )
        self.pause = pause
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        self._client = httpx.Client(base_url=API_ROOT, headers=headers, timeout=timeout)

    def __enter__(self) -> "HHClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None, attempts: int = 4) -> dict:
        delay = 2.0
        for attempt in range(1, attempts + 1):
            response = self._client.get(path, params=params)
            if response.status_code == 403:
                # С апреля 2026 это штатный ответ на любой анонимный поиск, а не сбой.
                raise PermissionError(
                    "hh.ru Open API закрыт для неавторизованных запросов (403). "
                    "Используй hh_html.HHHtmlClient или передай токен приложения. "
                    f"Ответ: {response.text[:200]}"
                )
            if response.status_code == 429 or response.status_code >= 500:
                log.warning("hh.ru %s -> %s, попытка %s", path, response.status_code, attempt)
                if attempt == attempts:
                    response.raise_for_status()
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            time.sleep(self.pause)
            return response.json()
        raise RuntimeError("unreachable")

    def search(
        self,
        text: str,
        area: int | Sequence[int] | None = None,
        period: int = 7,
        per_page: int = 100,
        max_pages: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> Iterator[dict]:
        """Постраничный поиск. Отдаёт сырые items — без описания и навыков.

        max_pages=0 — до конца выдачи: API сам сообщает число страниц.
        """
        self.exhausted = False
        page = 0
        while not max_pages or page < max_pages:
            params: dict[str, Any] = {
                "text": text,
                "period": period,
                "per_page": per_page,
                "page": page,
                "order_by": "publication_time",
            }
            if area is not None:
                params["area"] = area if isinstance(area, int) else list(area)
            if extra:
                params.update(extra)
            payload = self._get("/vacancies", params)
            items = payload.get("items", [])
            yield from items
            if page + 1 >= payload.get("pages", 0) or not items:
                self.exhausted = True
                break
            page += 1

    def vacancy(self, vacancy_id: str) -> dict:
        return self._get("/vacancies/" + str(vacancy_id))


def from_search_item(item: dict) -> Vacancy:
    """Черновая модель из выдачи поиска API: достаточно для предфильтра."""
    salary = item.get("salary") or {}
    employer = item.get("employer") or {}
    snippet = item.get("snippet") or {}
    description = " ".join(
        strip_html(snippet.get(part)) for part in ("requirement", "responsibility")
    ).strip()
    return Vacancy(
        external_id=str(item["id"]),
        url=item.get("alternate_url") or "https://hh.ru/vacancy/" + str(item["id"]),
        title=item.get("name") or "",
        company=employer.get("name"),
        company_id=str(employer["id"]) if employer.get("id") else None,
        area=(item.get("area") or {}).get("name"),
        salary_from=salary.get("from"),
        salary_to=salary.get("to"),
        currency=salary.get("currency"),
        gross=salary.get("gross"),
        schedule=(item.get("schedule") or {}).get("name"),
        experience=(item.get("experience") or {}).get("id"),
        employment=(item.get("employment") or {}).get("name"),
        description=description,
        published_at=normalize_published_at(item.get("published_at")),
    )


def enrich(vacancy: Vacancy, detail: dict) -> Vacancy:
    """Доклеивает полное описание и key_skills из карточки вакансии."""
    data = vacancy.model_dump()
    data["description"] = strip_html(detail.get("description")) or vacancy.description
    skills = [skill["name"] for skill in detail.get("key_skills", []) if skill.get("name")]
    data["skills"] = skills or vacancy.skills
    if (detail.get("schedule") or {}).get("name"):
        data["schedule"] = detail["schedule"]["name"]
    # Опыт и тип занятости карточка знает точнее выдачи, но перетирать
    # известное нечем: берём только то, чего в выдаче не было.
    for field in ("experience", "employment"):
        value = (detail.get(field) or {}).get("name")
        if value and not data.get(field):
            data[field] = value
    if (detail.get("employer") or {}).get("name"):
        data["company"] = detail["employer"]["name"]
    # Дата со страницы вакансии надёжнее, чем из выдачи: берём её, если своей нет.
    if not data.get("published_at"):
        data["published_at"] = normalize_published_at(detail.get("published_at"))
    return Vacancy(**data)
