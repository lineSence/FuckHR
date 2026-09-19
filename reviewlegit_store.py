"""Хранение того, что нужно проверке легитимности: шаблоны площадок и здоровье сбора.

Две таблицы, обе — наблюдения, а не настройки.

`site_lines` — какая строка на какой площадке у какой компании встретилась.
Строка, попавшаяся у BOILERPLATE_COMPANIES разных компаний, и есть шаблон
сайта. Так стоп-лист собирается сам и не устаревает после редизайна, в отличие
от ведомого руками словаря [CORE-015].

`site_health` — итог последнего захода на площадку: сколько страниц прочитано,
сколько отзывов вышло, сколько отброшено. По решению владельца это строка в
интерфейсе, а не тревога в Telegram: редизайн отзовика не ломает прогон, но
знать о нём нужно раньше, чем по опустевшему досье.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import reviewlegit_rules as R

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_lines (
    site       TEXT NOT NULL,
    line_hash  TEXT NOT NULL,
    company    TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (site, line_hash, company)
);

CREATE TABLE IF NOT EXISTS site_health (
    site       TEXT PRIMARY KEY,
    pages      INTEGER NOT NULL DEFAULT 0,
    items      INTEGER NOT NULL DEFAULT 0,
    dropped    INTEGER NOT NULL DEFAULT 0,
    no_date    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_site_lines_hash ON site_lines(site, line_hash);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def remember(conn: sqlite3.Connection, site: str, company: str, hashes: list[str]) -> int:
    """Запоминает, что эти строки встретились у этой компании на этой площадке."""
    if not site or not company or not hashes:
        return 0
    ensure_schema(conn)
    now = _now()
    conn.executemany(
        "INSERT OR IGNORE INTO site_lines (site, line_hash, company, seen_at)"
        " VALUES (?, ?, ?, ?)",
        [(site, digest, company, now) for digest in set(hashes)],
    )
    conn.commit()
    return len(set(hashes))


def boilerplate(conn: sqlite3.Connection, site: str) -> frozenset[str]:
    """Хэши строк, встреченных у разных компаний одной площадки."""
    if not site:
        return frozenset()
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT line_hash FROM site_lines WHERE site = ?"
        " GROUP BY line_hash HAVING COUNT(DISTINCT company) >= ?",
        (site, R.BOILERPLATE_COMPANIES),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def note(
    conn: sqlite3.Connection,
    site: str,
    *,
    pages: int = 0,
    items: int = 0,
    dropped: int = 0,
    no_date: int = 0,
) -> None:
    """Складывает итоги захода на площадку. Числа накапливаются между прогонами."""
    if not site:
        return
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO site_health (site, pages, items, dropped, no_date, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(site) DO UPDATE SET
            pages = pages + excluded.pages,
            items = items + excluded.items,
            dropped = dropped + excluded.dropped,
            no_date = no_date + excluded.no_date,
            updated_at = excluded.updated_at
        """,
        (site, int(pages), int(items), int(dropped), int(no_date), _now()),
    )
    conn.commit()


def health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM site_health ORDER BY items DESC, site"
    ).fetchall()


def health_line(rows: list[sqlite3.Row]) -> str:
    """Одна строка для интерфейса: где сколько отзывов и сколько выброшено."""
    if not rows:
        return ""
    parts = []
    for row in rows:
        share = int(row["dropped"]) / max(1, int(row["items"]) + int(row["dropped"]))
        parts.append(
            "{site}: {items} отзывов, отброшено {dropped} ({share:.0%})".format(
                site=row["site"], items=row["items"], dropped=row["dropped"], share=share
            )
        )
    return "; ".join(parts)


__all__ = ("boilerplate", "ensure_schema", "health", "health_line", "note", "remember")
