"""Точка вакансии на карте: адрес берётся из той же страницы hh.ru.

Геокодера здесь нет сознательно. hh.ru сам рисует карту в карточке, значит
широта и долгота уже лежат в JSON состояния, который разбирает hh_html.py.
Внешний геокодер на тысяче вакансий — это тысяча чужих запросов, лимиты и ключи
ради данных, которые уже приехали вместе со страницей [CORE-016].

Колонки address/lat/lng/metro добавляются к живой таблице vacancies: точка —
это свойство вакансии, а не отдельная сущность, и своя таблица только добавила бы
джойн к каждому запросу [CORE-012].

Чего здесь сознательно не делается:

- не выдумывается точка по названию города: сто вакансий в центре Москвы — враньё,
  которое выглядит как данные;
- нулевая точка (0, 0) считается отсутствием данных, а не Атлантикой.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

log = logging.getLogger(__name__)

# Колонки добавляются на живой базе: у владельца она одна и живёт месяцами.
COLUMNS = (
    ("address", "TEXT"),
    ("lat", "REAL"),
    ("lng", "REAL"),
    ("metro", "TEXT"),
)

VACANCY_ID_RE = re.compile(r"/vacancy/(\d+)")
MAX_POINTS = 2000


def ensure_schema(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(vacancies)")}
    for column, kind in COLUMNS:
        if column not in existing:
            log.info("добавляю колонку %s в vacancies", column)
            conn.execute("ALTER TABLE vacancies ADD COLUMN {} {}".format(column, kind))
    conn.commit()


@dataclass(frozen=True)
class Point:
    """Адрес вакансии. Координаты могут отсутствовать при живом адресе."""

    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    metro: str | None = None

    @property
    def mappable(self) -> bool:
        return self.lat is not None and self.lng is not None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", ".")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def valid(lat: float | None, lng: float | None) -> bool:
    """Точка годится для карты."""
    if lat is None or lng is None:
        return False
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lng <= 180.0:
        return False
    # Ровный ноль приходит тогда, когда поле есть, а адреса нет.
    return not (abs(lat) < 1e-6 and abs(lng) < 1e-6)


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "title", "text", "$"):
            if isinstance(value.get(key), str):
                return value[key].strip()
    return ""


def _first(node: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _metro(raw: Any) -> str | None:
    """Станции списком через запятую. Пересадки не считаем дублями."""
    if isinstance(raw, dict):
        raw = raw.get("stations") or raw.get("items") or [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    names: list[str] = []
    for item in raw:
        name = _text(item) or _text(
            (item or {}).get("stationName") if isinstance(item, dict) else None
        )
        if name and name not in names:
            names.append(name)
    return ", ".join(names[:3]) or None


ADDRESS_KEYS = ("address", "vacancyAddress", "addresses")


def address_of(node: dict[str, Any]) -> Point | None:
    """Адрес из узла состояния страницы. None — адреса нет вообще.

    Поле бывает объектом, списком или просто строкой: привязываться к одному
    виду разметки нельзя по той же причине, что и в hh_html (ADR-015).
    """
    if not isinstance(node, dict):
        return None
    raw: dict[str, Any] | None = None
    for key in ADDRESS_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            raw = value
            break
        if isinstance(value, (list, tuple)):
            found = [item for item in value if isinstance(item, dict)]
            if found:
                raw = found[0]
                break
        if isinstance(value, str) and value.strip():
            raw = {"rawAddress": value.strip()}
            break
    if raw is None:
        return None

    lat = _num(_first(raw, "lat", "latitude"))
    lng = _num(_first(raw, "lng", "lon", "longitude"))
    if not valid(lat, lng):
        lat = lng = None

    line = _text(_first(raw, "rawAddress", "displayName", "fullAddress"))
    if not line:
        parts = [
            _text(_first(raw, "city", "cityName")),
            _text(_first(raw, "street", "streetName")),
            _text(_first(raw, "building", "house")),
        ]
        line = ", ".join(part for part in parts if part)
    description = _text(raw.get("description"))
    if description and description not in line:
        line = (line + " (" + description + ")").strip()

    point = Point(
        address=line or None,
        lat=lat,
        lng=lng,
        metro=_metro(_first(raw, "metroStations", "metro", "stations")),
    )
    return point if (point.address or point.mappable or point.metro) else None


def save(conn: sqlite3.Connection, key: str, point: Point | None) -> bool:
    """Записывает адрес. True — если появилась точка для карты."""
    if point is None:
        return False
    conn.execute(
        """
        UPDATE vacancies
        SET address = COALESCE(?, address),
            lat = COALESCE(?, lat),
            lng = COALESCE(?, lng),
            metro = COALESCE(?, metro)
        WHERE key = ?
        """,
        (point.address, point.lat, point.lng, point.metro, key),
    )
    conn.commit()
    return point.mappable


def _like(value: str) -> str:
    """Строка поиска как данные: % и _ от владельца — буквы, а не джокеры."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def points(
    conn: sqlite3.Connection,
    min_score: float = 0.0,
    area: str = "",
    query: str = "",
    limit: int = MAX_POINTS,
) -> list[dict[str, Any]]:
    """Вакансии с координатами. Отбор тот же, что в списке: город, скор, слово."""
    where = ["lat IS NOT NULL", "lng IS NOT NULL", "COALESCE(score, 0) >= ?"]
    args: list[Any] = [float(min_score or 0.0)]
    if area:
        where.append("COALESCE(area, '') = ?")
        args.append(area)
    if query:
        where.append("(title LIKE ? ESCAPE '\\' OR COALESCE(company, '') LIKE ? ESCAPE '\\')")
        args += [_like(query), _like(query)]
    rows = conn.execute(
        """
        SELECT key, title, company, area, score, lat, lng, address, metro, url,
               published_at
        FROM vacancies
        WHERE {}
        ORDER BY COALESCE(score, 0) DESC, published_at DESC
        LIMIT ?
        """.format(" AND ".join(where)),
        [*args, max(1, int(limit))],
    ).fetchall()
    return [
        {
            "key": row["key"],
            "title": row["title"],
            "company": row["company"] or "",
            "area": row["area"] or "",
            "score": float(row["score"] or 0.0),
            "lat": float(row["lat"]),
            "lng": float(row["lng"]),
            "address": row["address"] or "",
            "metro": row["metro"] or "",
            "url": row["url"] or "",
            "published": (row["published_at"] or "")[:10],
        }
        for row in rows
    ]


def one(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """Точка одной вакансии — для ссылки «на карте»."""
    row = conn.execute(
        "SELECT key, title, address, metro, lat, lng FROM vacancies WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    lat, lng = _num(row["lat"]), _num(row["lng"])
    return {
        "key": row["key"],
        "title": row["title"],
        "address": row["address"] or "",
        "metro": row["metro"] or "",
        "lat": lat if valid(lat, lng) else None,
        "lng": lng if valid(lat, lng) else None,
    }


def coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(с точкой, всего вакансий). Разница — то, что карта не покажет."""
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN lat IS NOT NULL AND lng IS NOT NULL THEN 1 ELSE 0 END) AS mapped,
            COUNT(*) AS total
        FROM vacancies
        """
    ).fetchone()
    return int(row["mapped"] or 0), int(row["total"] or 0)


def areas(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Города, у которых есть точки, со счётчиками."""
    rows = conn.execute(
        """
        SELECT COALESCE(area, '') AS area, COUNT(*) AS total
        FROM vacancies
        WHERE lat IS NOT NULL AND lng IS NOT NULL AND COALESCE(area, '') <> ''
        GROUP BY COALESCE(area, '')
        ORDER BY total DESC, area COLLATE NOCASE
        """
    ).fetchall()
    return [(row["area"], int(row["total"])) for row in rows]


def pending(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Вакансии без точки: сначала самые интересные по скору."""
    return conn.execute(
        """
        SELECT key, url, title FROM vacancies
        WHERE lat IS NULL OR lng IS NULL
        ORDER BY COALESCE(score, 0) DESC, last_seen_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()


def vacancy_id(url: str | None) -> str | None:
    match = VACANCY_ID_RE.search(url or "")
    return match.group(1) if match else None


def from_page(page: str, vacancy: str | None = None) -> Point | None:
    """Адрес со страницы вакансии. Сломанная страница — это None, а не исключение."""
    import hh_html  # локально: hh_html тянет httpx, а интерфейс читает только базу

    try:
        state = hh_html.extract_state(page)
    except hh_html.ExtractionError:
        return None
    nodes = hh_html.find_vacancy_nodes(state)
    chosen: dict[str, Any] | None = None
    for node in nodes:
        if vacancy and str(node.get("vacancyId") or node.get("id")) == str(vacancy):
            chosen = node
            break
    if chosen is None and nodes:
        chosen = nodes[0]
    return address_of(chosen or {})


def backfill(
    conn: sqlite3.Connection, client: Any, limit: int = 100
) -> tuple[int, int]:
    """Добирает адреса для уже собранных вакансий. Возвращает (точек, попыток).

    Каждая страница — пауза в пару секунд, поэтому есть потолок и счётчик в логе:
    интерфейс рисует полоску из строк вида [3/30] (jobs.py).
    """
    import hh_html

    rows = pending(conn, limit)
    filled = 0
    tried = 0
    for index, row in enumerate(rows, start=1):
        ident = vacancy_id(row["url"])
        if not ident:
            continue
        tried += 1
        log.info("[%s/%s] адрес: %s", index, len(rows), row["title"])
        try:
            page = client.fetch(hh_html.VACANCY_PREFIX + ident)
        except hh_html.BlockedError:
            # Капча не лечится следующей страницей: честнее остановиться.
            log.warning("hh.ru больше не пускает, останавливаюсь на %s из %s", index, len(rows))
            break
        except Exception as exc:  # noqa: BLE001 — одна страница не стоит всего прогона
            log.warning("страница %s не открылась: %s", ident, exc)
            continue
        if save(conn, row["key"], from_page(page, ident)):
            filled += 1
    log.info("адреса: точек добавлено %s из %s попыток", filled, tried)
    return filled, tried


__all__ = (
    "COLUMNS",
    "MAX_POINTS",
    "Point",
    "address_of",
    "areas",
    "backfill",
    "coverage",
    "ensure_schema",
    "from_page",
    "one",
    "pending",
    "points",
    "save",
    "vacancy_id",
    "valid",
)
