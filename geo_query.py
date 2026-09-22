"""Отбор точек для карты: те же фильтры, что в списке, плюс метро и радиус.

Почему это не функция в `geo.py`. Там ответ на вопрос «откуда берётся точка»:
разбор страницы hh.ru, колонки в `vacancies`, добор адресов. Здесь — ответ на
вопрос «какие точки показать». Поводы для правок разные, файл делится по
смыслу [CORE-024].

Фильтры не переписываются, а берутся из `filters.py`: карта и список должны
одинаково понимать «свежие» и «скор от 60». Отсюда же алиас `v.` в SQL — все
куски условий написаны для `FROM vacancies v`.

Радиус считается только от точки на карте. Геокодера в проекте нет сознательно
(ADR-026 в `wiki/architecture/map.md`), поэтому спросить «в 5 км от Тверской, 1»
нельзя, а «в 5 км отсюда» — можно: центр задаёт клик.

Отбор по кругу идёт в два шага. SQL режет прямоугольник вокруг центра — это
дёшево и работает по тем же колонкам lat/lng. Точный круг досчитывается в
Python: тригонометрии в SQLite без расширений нет, а углы прямоугольника дают
до сорока процентов лишнего.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping

import filters
import geo
from filters import Filter

EARTH_KM = 6371.0
# Градус широты почти постоянен; для прямоугольника этой точности хватает.
KM_PER_DEGREE = 111.2
# Шаги, а не свободное число: «17 км» — это не вопрос, который кто-то задаёт.
RADIUS_STEPS = (1, 3, 5, 10, 20)

_METRO_SQL = filters.ilike("COALESCE(v.metro, '')")

MAP_FILTERS: tuple[Filter, ...] = filters.VACANCY_FILTERS + (
    Filter(
        "metro",
        "Метро",
        "text",
        lambda value: (_METRO_SQL, (filters.like(value),)),
        hint="станция так, как её назвал hh.ru; пересадки записаны через запятую",
    ),
    Filter(
        "has_metro",
        "Станция указана",
        "choice",
        lambda value: (
            "COALESCE(v.metro, '') <> ''" if value == "yes" else "COALESCE(v.metro, '') = ''",
            (),
        ),
        options=((filters.ANY, "неважно"), ("yes", "да"), ("no", "нет")),
    ),
)


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def center_of(params: Mapping[str, Any]) -> tuple[float, float] | None:
    """Центр круга из адресной строки. Мусор в параметрах — просто нет центра."""
    lat, lng = _num(params.get("clat")), _num(params.get("clng"))
    if lat is None or lng is None or not geo.valid(lat, lng):
        return None
    return lat, lng


def radius_of(params: Mapping[str, Any]) -> int:
    """Радиус в км. Чужое значение — ноль, то есть круга нет [CORE-017]."""
    value = _num(params.get("radius"))
    if value is None:
        return 0
    step = int(round(value))
    return step if step in RADIUS_STEPS else 0


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Расстояние по прямой. Пешком и на метро выйдет больше — так и написано."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lng2 - lng1)
    part = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(part)))


@dataclass(frozen=True)
class Selection:
    """Что показать на карте и чем это объяснить человеку."""

    points: list[dict[str, Any]]
    active: list[tuple[str, str]] = field(default_factory=list)
    center: tuple[float, float] | None = None
    radius: int = 0
    dropped: int = 0  # рядом, но за кругом: иначе круг выглядит как пустая база


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
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
        "level": row["level"] or "",
        "distance": None,
    }


def select(
    conn: sqlite3.Connection,
    params: Mapping[str, Any],
    limit: int = geo.MAX_POINTS,
) -> Selection:
    """Точки под условия из адресной строки."""
    geo.ensure_schema(conn)
    filters.ensure_tables(conn)
    where, args, active = filters.build_where(MAP_FILTERS, params)
    clauses = ["v.lat IS NOT NULL", "v.lng IS NOT NULL", where]

    center = center_of(params)
    radius = radius_of(params) if center else 0
    if center and radius:
        lat, lng = center
        step_lat = radius / KM_PER_DEGREE
        # Градус долготы к полюсу короче: без косинуса прямоугольник сужается
        # в полоску и режет нужные точки. У самых полюсов косинус зажат снизу.
        step_lng = radius / (KM_PER_DEGREE * max(0.2, math.cos(math.radians(lat))))
        clauses.append("v.lat BETWEEN ? AND ? AND v.lng BETWEEN ? AND ?")
        args += [lat - step_lat, lat + step_lat, lng - step_lng, lng + step_lng]
        active = list(active) + [("radius", "Радиус: {} км от точки".format(radius))]

    rows = conn.execute(
        """
        SELECT v.key, v.title, v.company, v.area, v.score, v.lat, v.lng,
               v.address, v.metro, v.url, v.published_at, s.level AS level
        FROM vacancies v
        LEFT JOIN company_score s ON s.company = v.company
        WHERE {where}
        ORDER BY {order}
        LIMIT ?
        """.format(
            where=" AND ".join(clauses),
            order=filters.order_by(
                filters.VACANCY_SORTS, str(params.get("sort", "") or ""), "score"
            ),
        ),
        [*args, max(1, int(limit))],
    ).fetchall()

    points = [_row(row) for row in rows]
    dropped = 0
    if center and radius:
        inside = []
        for item in points:
            item["distance"] = distance_km(center[0], center[1], item["lat"], item["lng"])
            if item["distance"] <= radius:
                inside.append(item)
        dropped = len(points) - len(inside)
        # Внутри круга интересно ближайшее, а не самое высокобалльное.
        inside.sort(key=lambda item: item["distance"])
        points = inside
    return Selection(points, list(active), center, radius, dropped)


def points(
    conn: sqlite3.Connection,
    params: Mapping[str, Any],
    limit: int = geo.MAX_POINTS,
) -> list[dict[str, Any]]:
    """Только точки — для JSON карты."""
    return select(conn, params, limit).points


def stations(conn: sqlite3.Connection, limit: int = 40) -> list[tuple[str, int]]:
    """Станции у точек со счётчиками: список для выбора, а не ручной ввод.

    Пересадки лежат в одной ячейке через запятую, поэтому счёт идёт в Python:
    разбирать строку в SQL пришлось бы рекурсивным CTE ради того же ответа.
    """
    counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT metro FROM vacancies WHERE lat IS NOT NULL AND lng IS NOT NULL"
        " AND COALESCE(metro, '') <> ''"
    ).fetchall()
    for row in rows:
        for name in str(row[0] or "").split(","):
            name = name.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].lower()))
    return ordered[:limit]


__all__ = (
    "EARTH_KM",
    "KM_PER_DEGREE",
    "MAP_FILTERS",
    "RADIUS_STEPS",
    "Selection",
    "center_of",
    "distance_km",
    "points",
    "radius_of",
    "select",
    "stations",
)
