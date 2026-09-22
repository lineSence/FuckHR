"""Работа.ру: вакансии из JSON-LD на странице поиска.

Площадка сама отдаёт структурированные данные: в каждой странице поиска лежит
`<script type="application/ld+json">` со списком `JobPosting` — заголовок,
ссылка, дата, полное описание и вилка. Это подарок: реверсить разметку не нужно,
а schema.org меняется куда реже вёрстки.

Страница также несёт состояние `window.__NUXT__`, но лезть туда незачем, пока
хватает JSON-LD: меньше кода — меньше починок `[CORE-025]`.

Zarplata.ru здесь нет: она работает на движке hh.ru, и её забирает
`src_hhlike.py`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

import src_common as C
from hh import Vacancy, normalize_published_at, strip_html

log = logging.getLogger("fuckhr")

CODE = "rabota"
LABEL = "Работа.ру"
SEARCH = "https://www.rabota.ru/vacancy"
MAX_PAGES = 3

LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
ID_RE = re.compile(r"/vacancy/(\d+)")


def _postings(html: str) -> list[dict[str, Any]]:
    """Все JobPosting со страницы. Битый блок пропускается, а не роняет разбор."""
    out: list[dict[str, Any]] = []
    for raw in LD_RE.findall(html or ""):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                out.append(item)
    return out


def _salary(node: dict[str, Any]) -> tuple[int | None, int | None, str]:
    for key in ("baseSalary", "estimatedSalary"):
        block = node.get(key)
        if not isinstance(block, dict):
            continue
        value = block.get("value")
        value = value if isinstance(value, dict) else {}
        low = C.money(value.get("minValue") or value.get("value"))
        high = C.money(value.get("maxValue"))
        if low or high:
            return low, high, C.currency(block.get("currency") or value.get("currency"))
    return None, None, "RUR"


def to_vacancy(node: dict[str, Any]) -> Vacancy | None:
    url = str(node.get("url") or "").strip()
    title = str(node.get("title") or "").strip()
    match = ID_RE.search(url)
    if not title or match is None:
        return None
    org = node.get("hiringOrganization")
    org = org if isinstance(org, dict) else {}
    location = node.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else {}
    address = (location or {}).get("address") if isinstance(location, dict) else {}
    salary_from, salary_to, currency = _salary(node)
    return Vacancy(
        source=CODE,
        external_id=match.group(1),
        url=url,
        title=title,
        company=str(org.get("name") or "").strip() or None,
        area=str((address or {}).get("addressLocality") or "").strip() or None,
        salary_from=salary_from,
        salary_to=salary_to,
        currency=currency,
        employment=str(node.get("employmentType") or "").strip() or None,
        description=strip_html(str(node.get("description") or "")),
        published_at=normalize_published_at(node.get("datePosted")),
    )


def search(
    text: str,
    area: Any = None,
    period: int = 7,
    limit: int = 0,
    fetcher: Any = None,
    max_pages: int = MAX_PAGES,
) -> Iterator[Vacancy]:
    own = fetcher is None
    fetcher = fetcher or C.client()
    city = C.area_for(CODE, area)
    found = 0
    try:
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {"query": text, "page": page}
            if city:
                params["city"] = city
            html = fetcher.text(SEARCH, params=params)
            nodes = _postings(html)
            if not nodes:
                return
            for node in nodes:
                vacancy = to_vacancy(node)
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
