"""Модель Vacancy и нормализация (ADR-010).

Исторически здесь жил клиент hh.ru Open API. С апреля 2026 публичный GET /vacancies
отдаёт 403 всем неавторизованным, поэтому сбор переехал в hh_html.py (ADR-015).
Клиент HHClient оставлен: он сразу заработает, если появится токен приложения (HH_TOKEN).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Iterator, Sequence

import httpx
from pydantic import BaseModel, Field

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
    if not value:
        return ""
    return WS_RE.sub(" ", TAG_RE.sub(" ", value)).strip()


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
                "HH_USER_AGENT должен содержать контакт, например 'FuckHR/0.1 (me@example.com)'"
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
                    "Используй hh_html.HHHtmlClient или задай токен приложения HH_TOKEN. "
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
        max_pages: int = 5,
        extra: dict[str, Any] | None = None,
    ) -> Iterator[dict]:
        """Постраничный поиск. Отдаёт сырые items — без описания и навыков."""
        for page in range(max_pages):
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
                break

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
        published_at=item.get("published_at"),
    )


def enrich(vacancy: Vacancy, detail: dict) -> Vacancy:
    """Доклеивает полное описание и key_skills из карточки вакансии."""
    data = vacancy.model_dump()
    data["description"] = strip_html(detail.get("description")) or vacancy.description
    skills = [skill["name"] for skill in detail.get("key_skills", []) if skill.get("name")]
    data["skills"] = skills or vacancy.skills
    if (detail.get("schedule") or {}).get("name"):
        data["schedule"] = detail["schedule"]["name"]
    if (detail.get("employer") or {}).get("name"):
        data["company"] = detail["employer"]["name"]
    return Vacancy(**data)
