"""Хранение зарплатных наблюдений и посчитанных срезов. Только таблицы и запросы.

Три таблицы:

- `market_observations` — сырые точки из выдачи: срез, границы вилки, компания,
  дата. Сырые, потому что пороги и ставка НДФЛ поменяются, а пересобрать выдачу
  за 180 дней неоткуда.
- `market_stats` — посчитанные срезы всех уровней каскада: медиана, p25, p75,
  число наблюдений, дата пересчёта.
- `company_market` — метка работодателя по деньгам с раскрытием признаков.

Одна вакансия — одна строка наблюдений (`key` уникален): вечно висящая вакансия
не должна голосовать девяносто раз.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from typing import Sequence

import market
import market_rules as R

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_observations (
    key          TEXT PRIMARY KEY,
    company      TEXT,
    role         TEXT NOT NULL DEFAULT '',
    grade        TEXT NOT NULL DEFAULT '',
    geo          TEXT NOT NULL DEFAULT '',
    format       TEXT NOT NULL DEFAULT '',
    salary_low   INTEGER,
    salary_high  INTEGER,
    point        INTEGER,
    range_kind   TEXT NOT NULL DEFAULT 'closed',
    published_at TEXT NOT NULL,
    seen_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_stats (
    bucket     TEXT PRIMARY KEY,
    level      INTEGER NOT NULL DEFAULT 0,
    n          INTEGER NOT NULL DEFAULT 0,
    median     REAL NOT NULL DEFAULT 0,
    p25        REAL NOT NULL DEFAULT 0,
    p75        REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_market (
    company    TEXT PRIMARY KEY,
    level      TEXT NOT NULL DEFAULT 'none',
    signs      TEXT NOT NULL DEFAULT '[]',
    vacancies  INTEGER NOT NULL DEFAULT 0,
    deviation  REAL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_obs_published ON market_observations(published_at);
CREATE INDEX IF NOT EXISTS idx_obs_company ON market_observations(company);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record(conn: sqlite3.Connection, observations: Sequence[market.Observation]) -> int:
    """Пишет наблюдения выдачи. Повторная встреча обновляет строку, а не множит."""
    ensure_schema(conn)
    now = _now()
    written = 0
    for item in observations:
        if not item.key:
            continue
        conn.execute(
            """
            INSERT INTO market_observations (
                key, company, role, grade, geo, format, salary_low, salary_high,
                point, range_kind, published_at, seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                company = excluded.company,
                role = excluded.role,
                grade = excluded.grade,
                geo = excluded.geo,
                format = excluded.format,
                salary_low = excluded.salary_low,
                salary_high = excluded.salary_high,
                point = excluded.point,
                range_kind = excluded.range_kind,
                published_at = excluded.published_at,
                seen_at = excluded.seen_at
            """,
            (
                item.key, item.company, item.role, item.grade, item.geo, item.format,
                item.low, item.high, item.point, item.range_kind, item.published_at, now,
            ),
        )
        written += 1
    conn.commit()
    return written


def recompute(conn: sqlite3.Connection, today: date | None = None) -> int:
    """Пересчитывает все срезы за окно. Возвращает число срезов.

    Считаются сразу все уровни каскада: дешевле один проход по наблюдениям, чем
    поход в базу за каждым срезом, которого может не оказаться.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT company, role, grade, geo, format, point, range_kind, published_at"
        " FROM market_observations WHERE point IS NOT NULL AND role <> ''"
    ).fetchall()

    buckets: dict[tuple[str, int], list[tuple[str, float]]] = {}
    for row in rows:
        if row["range_kind"] == market.TO_ONLY:
            continue  # «до X» — приманка, а не зарплата
        if not market.within_window(str(row["published_at"]), today):
            continue
        for level in range(len(R.CASCADE)):
            key = market.bucket_key(
                str(row["role"]), str(row["grade"]), str(row["geo"]), str(row["format"]), level
            )
            buckets.setdefault((key, level), []).append(
                (str(row["company"] or ""), float(row["point"]))
            )

    now = _now()
    conn.execute("DELETE FROM market_stats")
    for (bucket, level), pairs in buckets.items():
        values = market.cap_by_company(pairs)
        stats = market.summarize(bucket, level, values)
        conn.execute(
            "INSERT OR REPLACE INTO market_stats (bucket, level, n, median, p25, p75, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bucket, level, stats.count, stats.median, stats.p25, stats.p75, now),
        )
    conn.commit()
    log.info("рынок: наблюдений %s, срезов %s", len(rows), len(buckets))
    return len(buckets)


def stats_for(
    conn: sqlite3.Connection, role: str, grade: str, geo: str, fmt: str
) -> market.Stats | None:
    """Срез по каскаду: точный, иначе всё более грубый. Пусто — рынка нет."""
    ensure_schema(conn)
    for level in range(len(R.CASCADE)):
        bucket = market.bucket_key(role, grade, geo, fmt, level)
        row = conn.execute(
            "SELECT * FROM market_stats WHERE bucket = ?", (bucket,)
        ).fetchone()
        if row is None:
            continue
        stats = market.Stats(
            bucket=str(row["bucket"]),
            level=int(row["level"]),
            count=int(row["n"]),
            median=float(row["median"]),
            p25=float(row["p25"]),
            p75=float(row["p75"]),
        )
        if stats.enough:
            return stats
    return None


def marker_for(conn: sqlite3.Connection, vacancy: object) -> market.Marker:
    """Метка одной вакансии: наблюдение + срез из базы."""
    observation = market.observe(vacancy)
    if observation is None:
        return market.Marker(label=R.UNKNOWN)
    stats = stats_for(conn, observation.role, observation.grade, observation.geo, observation.format)
    return market.marker(observation.point, stats)


def company_rows(conn: sqlite3.Connection, company: str) -> list[sqlite3.Row]:
    """Наблюдения по вакансиям работодателя за окно."""
    ensure_schema(conn)
    return [
        row
        for row in conn.execute(
            "SELECT * FROM market_observations WHERE company = ?", (company,)
        ).fetchall()
        if market.within_window(str(row["published_at"]))
    ]


def companies(conn: sqlite3.Connection) -> list[str]:
    """Работодатели, у которых есть хотя бы одно наблюдение."""
    ensure_schema(conn)
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT company FROM market_observations WHERE company <> ''"
        )
    ]


def save_company(conn: sqlite3.Connection, company: str, mark: object) -> None:
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO company_market (company, level, signs, vacancies, deviation, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(company) DO UPDATE SET
            level = excluded.level,
            signs = excluded.signs,
            vacancies = excluded.vacancies,
            deviation = excluded.deviation,
            updated_at = excluded.updated_at
        """,
        (
            company,
            str(getattr(mark, "level", R.MARK_NONE)),
            json.dumps(
                [{"code": s.code, "text": s.text} for s in getattr(mark, "signs", ())],
                ensure_ascii=False,
            ),
            int(getattr(mark, "vacancies", 0)),
            getattr(mark, "deviation", None),
            _now(),
        ),
    )
    conn.commit()


def load_company(conn: sqlite3.Connection, company: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM company_market WHERE company = ?", (company,)
    ).fetchone()


def drop_stale(conn: sqlite3.Connection, today: date | None = None) -> int:
    """Убирает наблюдения старше окна: за пределами окна они уже не рынок."""
    ensure_schema(conn)
    rows = conn.execute("SELECT key, published_at FROM market_observations").fetchall()
    stale = [
        (str(row["key"]),)
        for row in rows
        if not market.within_window(str(row["published_at"]), today)
    ]
    conn.executemany("DELETE FROM market_observations WHERE key = ?", stale)
    conn.commit()
    return len(stale)


__all__ = (
    "SCHEMA",
    "companies",
    "company_rows",
    "drop_stale",
    "ensure_schema",
    "load_company",
    "marker_for",
    "recompute",
    "record",
    "save_company",
    "stats_for",
)
