"""Парсеры конкретных площадок отзывов.

Зачем это рядом с `reviewitems.split_page`, который и так режет страницу на
отзывы. Общий разбор снимает теги и ищет границы по смыслу строк — он
переживает любой редизайн и работает на незнакомой площадке. Но он видит
только текст, а живые страницы отдают в разметке то, чего в тексте нет вовсе
(проверка 24.09.2026, `docs/review-sites.md`):

- Dream Job пишет **должность** заголовком отзыва, рядом город, месяц и стаж, а
  ниже шесть под-оценок. Должность — самый сильный признак сферы
  (`docs/review-area.md`), и общий разбор выбрасывал её как слишком короткую
  строку: сферу получали 10 отзывов из 50 при должности у каждого;
- «Правда сотрудников» держит оценки в атрибутах `data-rating`, а словесные
  метки («Зарплата: серая») — отдельными полями. Общий разбор искал оценку
  строкой «4,0» и не находил ничего: средняя по компании считалась только по
  Dream Job;
- `jobtrue.ru` и `hrlike.ru` отдают отзывы машинной разметкой schema.org, и у
  второго шкала 0–10, которую нельзя принимать за пятибалльную.

Поэтому здесь селекторы, а не смысл строк. Цена известна: редизайн площадки
ломает парсер. Поэтому парсер никогда не обязателен — не нашёл отзывов, и
разбор идёт общим путём [CORE-017], а сломавшуюся площадку видно по счётчикам
в `reviewlegit_store.health`.

Автор отзыва не извлекается ни на одной площадке. «Бывший сотрудник» — это
статус, а не человек; имя, аватар и IP-флаг игнорируются [CORE-012], [CORE-013].
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import re
from datetime import date
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

MAX_ITEMS = 60          # столько же, сколько у общего разбора
BODY_CHARS = 1200
PAGE_PARAM = "page"

_TAG_RE = re.compile(r"<[^>]+>")
# Дата из schema.org: «2025-02-14T14:27:44+03:00» или «2026-04-15».
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_SPACE_RE = re.compile(r"\s+")


def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower().lstrip("www.")


def _text(raw: str) -> str:
    return _SPACE_RE.sub(" ", html_mod.unescape(_TAG_RE.sub(" ", raw or ""))).strip()


def _one(pattern: "re.Pattern[str]", block: str) -> str:
    match = pattern.search(block)
    return _text(match.group(1)) if match else ""


def _blocks(html: str, marker: str) -> list[str]:
    """Куски разметки от одного маркера до следующего.

    Сознательно без разбора дерева: отзывы лежат плоским списком, и конец
    предыдущего отзыва — это начало следующего.
    """
    parts = html.split(marker)
    return parts[1:] if len(parts) > 1 else []


def _average(values: Sequence[float]) -> float | None:
    clean = [v for v in values if 0 < v <= 5]
    return round(sum(clean) / len(clean), 2) if clean else None


# --- Dream Job ---------------------------------------------------------------

_DJ_ROLE_RE = re.compile(r'class="review__header-title"[^>]*>(.*?)</h2>', re.S)
_DJ_PLACE_RE = re.compile(r'class="review__location"[^>]*>(.*?)</span>', re.S)
_DJ_TAG_RE = re.compile(r'class="tags__item tags__item_grey"[^>]*>(.*?)</div>', re.S)
_DJ_RATING_RE = re.compile(r'class="[^"]*dj-rating dj-rating--\d+"[^>]*>\s*([0-5][.,]\d)')
_DJ_SUB_RE = re.compile(r'data-partly-switch="rating\|([0-9,]+)"')
_DJ_PAIR_RE = re.compile(
    r'class="review__title[^"]*"[^>]*>(.*?)</div>.*?class="review__text"[^>]*>(.*?)</div>',
    re.S,
)


def _dreamjob(html: str, url: str, today: date | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in _blocks(html, 'class="review review-fl"'):
        role = _one(_DJ_ROLE_RE, block)
        place = _one(_DJ_PLACE_RE, block)
        tags = [_text(m.group(1)) for m in _DJ_TAG_RE.finditer(block)]
        pros = cons = ""
        for match in _DJ_PAIR_RE.finditer(block):
            title = _text(match.group(1)).lower()
            body = _text(match.group(2))
            if not body:
                continue
            if "нрав" in title and not pros:
                pros = body
            elif "улучш" in title and not cons:
                cons = body
        rating = None
        found = _DJ_RATING_RE.search(block)
        if found:
            rating = float(found.group(1).replace(",", "."))
        else:
            subs = _DJ_SUB_RE.search(block)
            if subs:
                rating = _average([float(v) for v in subs.group(1).split(",") if v])
        if not (pros or cons):
            continue
        out.append(
            {
                "pros": pros,
                "cons": cons,
                # Стаж и город остаются в тексте: они про условия, а не про
                # человека, и правила закономерностей их читают.
                "body": " · ".join(part for part in (place, *tags[:1]) if part),
                "rating": rating,
                "role": role,
                "date_source": place,
            }
        )
        if len(out) >= MAX_ITEMS:
            break
    return out


# --- Правда сотрудников ------------------------------------------------------

_PS_CITY_RE = re.compile(r'-city"[^>]*>\s*Город:\s*(.*?)</div>', re.S)
_PS_DATE_RE = re.compile(r'-date"[^>]*>(.*?)</div>', re.S)
_PS_NAME_RE = re.compile(r'-name"[^>]*>(.*?)</div>', re.S)
_PS_TEXT_RE = re.compile(
    r'-text (pos|con)"[^>]*>(.*?)-text-message"[^>]*>(.*?)</div>', re.S
)
_PS_STARS_RE = re.compile(r'rating-autostars"\s+data-rating="([0-5])"')
_PS_MARK_RE = re.compile(
    r'-ratings2-item-label"[^>]*>(.*?)</span>\s*<span[^>]*-ratings2-item-value"[^>]*>(.*?)</span>',
    re.S,
)
_PS_STATUS_RE = re.compile(r"\(([^)]{3,40})\)")


def _pravda(html: str, url: str, today: date | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in _blocks(html, 'class="company-reviews-list-item '):
        pros = cons = ""
        for match in _PS_TEXT_RE.finditer(block):
            body = _text(match.group(3))
            if not body:
                continue
            if match.group(1) == "pos" and not pros:
                pros = body
            elif match.group(1) == "con" and not cons:
                cons = body
        if not (pros or cons):
            continue
        city = _one(_PS_CITY_RE, block)
        when = _one(_PS_DATE_RE, block)
        # Из подписи берём только статус в скобках: «Роман (Бывший сотрудник)»
        # — имя автора нам не нужно и в базу не идёт [CORE-013].
        status = _PS_STATUS_RE.search(_one(_PS_NAME_RE, block))
        marks = []
        for match in _PS_MARK_RE.finditer(block):
            label = _text(match.group(1)).rstrip(":").lower()
            value = _text(match.group(2)).lower()
            if label and value:
                # «серая зарплата», а не «зарплата: серая»: так словесную метку
                # видят правила закономерностей (dossier_rules).
                marks.append("{} {}".format(value, label))
        stars = [float(m.group(1)) for m in _PS_STARS_RE.finditer(block)]
        out.append(
            {
                "pros": pros,
                "cons": cons,
                "body": " · ".join(
                    part
                    for part in (city, status.group(1) if status else "", *marks)
                    if part
                ),
                "rating": _average(stars),
                "role": "",
                "date_source": when,
            }
        )
        if len(out) >= MAX_ITEMS:
            break
    return out


# --- schema.org (jobtrue.ru, hrlike.ru) --------------------------------------

_LD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


def _walk(node: Any, found: list[dict]) -> None:
    if isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Review" in kinds:
            found.append(node)
        for value in node.values():
            _walk(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, found)


def _scaled(raw: Any, best: Any) -> float | None:
    """Оценка в пятибалльной шкале. У hrlike.ru шкала 0–10."""
    try:
        value = float(str(raw).replace(",", "."))
        top = float(str(best or 5).replace(",", ".")) or 5.0
    except ValueError:
        return None
    if top <= 0:
        return None
    value = value * 5.0 / top
    return round(value, 2) if 0 < value <= 5 else None


def _ldjson(html: str, url: str, today: date | None) -> list[dict[str, Any]]:
    found: list[dict] = []
    for raw in _LD_RE.finditer(html):
        try:
            _walk(json.loads(raw.group(1)), found)
        except ValueError:
            continue
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in found:
        body = _text(str(node.get("reviewBody") or node.get("description") or ""))
        if not body or body.lower() in seen:
            continue
        seen.add(body.lower())
        rating = node.get("reviewRating") or {}
        out.append(
            {
                "pros": "",
                "cons": "",
                "body": body,
                "rating": _scaled(rating.get("ratingValue"), rating.get("bestRating")),
                # `name` у schema.org — заголовок отзыва; на этих площадках в
                # нём стоит должность, если она вообще есть.
                "role": _text(str(node.get("name") or "")),
                "date_source": str(node.get("datePublished") or ""),
            }
        )
        if len(out) >= MAX_ITEMS:
            break
    return out


Parser = Callable[[str, str, "date | None"], list[dict[str, Any]]]

# Площадка → парсер. Список закрытый: парсер по селекторам имеет смысл только
# там, где мы эти селекторы видели своими глазами.
PARSERS: dict[str, Parser] = {
    "dreamjob.ru": _dreamjob,
    "pravda-sotrudnikov.ru": _pravda,
    "jobtrue.ru": _ldjson,
    "hrlike.ru": _ldjson,
}

# У кого работает листание страницами `?page=N` и сколько их бывает. Dream Job
# отдаёт до двадцати страниц по 50 отзывов, «Правда» — по 19.
PAGED: tuple[str, ...] = ("dreamjob.ru", "pravda-sotrudnikov.ru")


def known(url: str) -> bool:
    return host_of(url) in PARSERS


def parse(html: str, url: str = "", today: date | None = None) -> tuple[Any, ...]:
    """Отзывы страницы по её разметке. Пусто — пусть разбирает общий путь."""
    import review_area
    import reviewitems

    parser = PARSERS.get(host_of(url))
    if parser is None or not html:
        return ()
    try:
        rows = parser(html, url, today)
    except Exception as exc:  # noqa: BLE001 — падение парсера не стоит досье
        log.warning("парсер %s не справился: %s", host_of(url), exc)
        return ()
    site = host_of(url)
    items: list[Any] = []
    for row in rows:
        chunk = " ".join(
            str(row.get(key) or "") for key in ("role", "pros", "cons", "body")
        )
        dated_at, precision = _dated(str(row.get("date_source") or ""), today)
        items.append(
            reviewitems.ReviewItem(
                url=url,
                site=site,
                index=len(items),
                body=str(row.get("body") or "")[:BODY_CHARS],
                pros=str(row.get("pros") or "")[:BODY_CHARS],
                cons=str(row.get("cons") or "")[:BODY_CHARS],
                rating=row.get("rating"),
                dated_at=dated_at,
                date_precision=precision,
                has_reply=False,
                role=str(row.get("role") or "") or review_area.role_in(chunk),
            )
        )
    if items:
        log.info("%s: отзывов по разметке %s", site, len(items))
    return tuple(items)


def _dated(raw: str, today: date | None) -> tuple[str | None, str]:
    """Дата отзыва. ISO из разметки — точная, всё прочее разбирает общий код."""
    import reviewitems

    iso = _ISO_RE.match(raw.strip())
    if iso:
        return "{}-{}-{}".format(*iso.groups()), "exact"
    return reviewitems.parse_date(raw, today)


def page_url(url: str, page: int) -> str:
    """Тот же адрес со страницей `?page=N`. Первая страница — без параметра."""
    parts = urlsplit(url)
    query = [
        piece
        for piece in parts.query.split("&")
        if piece and not piece.startswith(PAGE_PARAM + "=")
    ]
    if page > 1:
        query.append("{}={}".format(PAGE_PARAM, int(page)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(query), "")
    )


def next_pages(url: str, count: int) -> list[str]:
    """Адреса следующих страниц, если площадка листается и мы на первой."""
    if host_of(url) not in PAGED or count <= 0:
        return []
    parts = urlsplit(url)
    if PAGE_PARAM + "=" in parts.query:
        return []  # уже не первая страница: листает вызывающий
    return [page_url(url, page) for page in range(2, count + 2)]


__all__ = (
    "MAX_ITEMS",
    "PAGED",
    "PARSERS",
    "host_of",
    "known",
    "next_pages",
    "page_url",
    "parse",
)
