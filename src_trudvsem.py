"""Работа России (trudvsem.ru): официальное открытое API без ключа.

Почему эта площадка первой после hh.ru. Её API — единственный среди
рассмотренных, который работает без регистрации, ключа и капчи: сбор ничем не
рискует. Вакансий там полмиллиона, но для разработчика их немного и они
скромнее по деньгам — ценность в другом.

Главная ценность — компания. Вместе с вакансией приезжают ИНН, ОГРН и КПП:
проверка работодателя перестаёт быть догадкой по названию. Поэтому ИНН
сохраняется в `company_id`.

Описание собирается из `duty` (обязанности) и `requirement` (образование и
опыт): отдельного поля с полным текстом у площадки нет, и это честнее, чем
выдавать обязанности за всё описание.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import src_common as C
from hh import Vacancy, normalize_published_at

log = logging.getLogger("fuckhr")

CODE = "trudvsem"
LABEL = "Работа России"
API = "http://opendata.trudvsem.ru/api/v1/vacancies"
PER_PAGE = 50
MAX_PAGES = 4


def _description(node: dict[str, Any]) -> str:
    parts = [str(node.get("duty") or "").strip()]
    requirement = node.get("requirement")
    if isinstance(requirement, dict):
        education = str(requirement.get("education") or "").strip()
        experience = requirement.get("experience")
        if education:
            parts.append("Образование: {}".format(education))
        if experience not in (None, "", 0):
            parts.append("Опыт: {} г.".format(experience))
    elif requirement:
        parts.append(str(requirement).strip())
    return "\n".join(part for part in parts if part)


def to_vacancy(node: dict[str, Any]) -> Vacancy | None:
    """Узел API → общая модель. Без названия или id вакансии нет."""
    ident = str(node.get("id") or "").strip()
    title = str(node.get("job-name") or "").strip()
    if not ident or not title:
        return None
    company = node.get("company") if isinstance(node.get("company"), dict) else {}
    region = node.get("region") if isinstance(node.get("region"), dict) else {}
    return Vacancy(
        source=CODE,
        external_id=ident,
        url=str(node.get("vac_url") or ""),
        title=title,
        company=str(company.get("name") or "").strip() or None,
        # ИНН вместо внутреннего id: он одинаков на всех площадках и в реестрах.
        company_id=str(company.get("inn") or "").strip() or None,
        area=str(region.get("name") or "").strip() or None,
        salary_from=C.money(node.get("salary_min")),
        salary_to=C.money(node.get("salary_max")),
        currency=C.currency(node.get("currency")),
        gross=None,  # площадка не разделяет до и после налога
        schedule=str(node.get("schedule") or "").strip() or None,
        description=_description(node),
        published_at=normalize_published_at(node.get("creation-date")),
    )


def search(
    text: str,
    area: Any = None,
    period: int = 7,
    limit: int = 0,
    fetcher: Any = None,
    max_pages: int = MAX_PAGES,
) -> Iterator[Vacancy]:
    """Вакансии по запросу. Пустой ответ — это конец выдачи, а не ошибка."""
    own = fetcher is None
    fetcher = fetcher or C.client()
    region = C.area_for(CODE, area)
    url = "{}/region/{}".format(API, region) if region else API
    found = 0
    try:
        for page in range(max_pages):
            data = fetcher.json(
                url, params={"text": text, "limit": PER_PAGE, "offset": page}
            )
            nodes = (((data or {}).get("results") or {}).get("vacancies")) or []
            if not nodes:
                return
            for item in nodes:
                node = item.get("vacancy") if isinstance(item, dict) else None
                vacancy = to_vacancy(node) if isinstance(node, dict) else None
                if vacancy is None:
                    continue
                yield vacancy
                found += 1
                if limit and found >= limit:
                    return
    finally:
        if own:
            fetcher.close()


__all__ = ("CODE", "LABEL", "search", "to_vacancy")
