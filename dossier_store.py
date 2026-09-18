"""Хранение досье в SQLite: схема, запись, чтение, сводки для страниц.

Вынесено из dossier.py по [CORE-024]: здесь нет ни сети, ни модели, ни разбора
текстов — только таблицы и запросы.

Досье принимается по утиному типу, а не импортом dossier.Dossier: иначе
получится цикл импортов.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from dossier_rules import RISK_RU, STALE_AFTER_DAYS

if TYPE_CHECKING:
    from dossier import Dossier

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_dossier (
    company       TEXT PRIMARY KEY,
    domain        TEXT,
    site_url      TEXT,
    review_count  INTEGER NOT NULL DEFAULT 0,
    avg_rating    REAL,
    risk          TEXT NOT NULL DEFAULT 'unknown',
    patterns      TEXT NOT NULL DEFAULT '[]',
    red_flags     TEXT NOT NULL DEFAULT '[]',
    green_flags   TEXT NOT NULL DEFAULT '[]',
    summary       TEXT,
    summary_by    TEXT,
    sources       TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company    TEXT NOT NULL,
    site       TEXT,
    url        TEXT NOT NULL,
    title      TEXT,
    snippet    TEXT,
    body       TEXT,
    rating     REAL,
    polarity   TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    UNIQUE (company, url)
);

CREATE INDEX IF NOT EXISTS idx_reviews_company ON company_reviews(company);
CREATE INDEX IF NOT EXISTS idx_dossier_risk ON company_dossier(risk);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Миграция для баз, созданных до чтения страниц: колонки body там нет,
    # а ронять прогон из-за этого нельзя.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(company_reviews)")}
    if "body" not in columns:
        log.info("добавляю колонку body в company_reviews")
        conn.execute("ALTER TABLE company_reviews ADD COLUMN body TEXT")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def store(conn: sqlite3.Connection, dossier: "Dossier") -> None:
    """Перезаписывает досье и добавляет новые отзывы."""
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO company_dossier (
            company, domain, site_url, review_count, avg_rating, risk,
            patterns, red_flags, green_flags, summary, summary_by, sources, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company) DO UPDATE SET
            domain = excluded.domain,
            site_url = excluded.site_url,
            review_count = excluded.review_count,
            avg_rating = excluded.avg_rating,
            risk = excluded.risk,
            patterns = excluded.patterns,
            red_flags = excluded.red_flags,
            green_flags = excluded.green_flags,
            summary = excluded.summary,
            summary_by = excluded.summary_by,
            sources = excluded.sources,
            updated_at = excluded.updated_at
        """,
        (
            dossier.company,
            dossier.domain,
            dossier.site_url,
            dossier.review_count,
            dossier.avg_rating,
            dossier.risk,
            json.dumps(
                [
                    {
                        "code": p.code,
                        "label": p.label,
                        "polarity": p.polarity,
                        "hits": p.hits,
                        "quotes": list(p.quotes),
                    }
                    for p in dossier.patterns
                ],
                ensure_ascii=False,
            ),
            json.dumps([p.label for p in dossier.red_flags], ensure_ascii=False),
            json.dumps([p.label for p in dossier.green_flags], ensure_ascii=False),
            dossier.summary,
            dossier.summary_by,
            json.dumps(list(dossier.sources), ensure_ascii=False),
            _now(),
        ),
    )
    for review in dossier.reviews:
        # Текст страницы мог появиться позже сниппета: обновляем существующую
        # строку, иначе прочитанный отзыв так и останется заголовком.
        conn.execute(
            """
            INSERT INTO company_reviews
                (company, site, url, title, snippet, body, rating, polarity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company, url) DO UPDATE SET
                title = excluded.title,
                snippet = excluded.snippet,
                body = COALESCE(NULLIF(excluded.body, ''), company_reviews.body),
                rating = excluded.rating,
                polarity = excluded.polarity
            """,
            (
                dossier.company,
                review.site,
                review.url,
                review.title,
                review.snippet,
                review.body,
                review.rating,
                review.polarity,
                _now(),
            ),
        )
    conn.commit()


def load(conn: sqlite3.Connection, company: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM company_dossier WHERE company = ?", (company,)
    ).fetchone()


def load_reviews(conn: sqlite3.Connection, company: str) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM company_reviews WHERE company = ? ORDER BY polarity, id",
        (company,),
    ).fetchall()


def is_fresh(row: sqlite3.Row | None, days: int = STALE_AFTER_DAYS) -> bool:
    """Свежее досье не собирается заново: это главная экономия запросов к поиску."""
    if row is None:
        return False
    try:
        updated = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated < timedelta(days=days)


def row_to_lines(row: sqlite3.Row) -> list[str]:
    """Строки карточки из сохранённого досье — без повторного поиска."""
    try:
        red = json.loads(row["red_flags"] or "[]")
        green = json.loads(row["green_flags"] or "[]")
    except ValueError:
        red, green = [], []
    head = "Работодатель: {} · отзывов {}".format(
        RISK_RU.get(row["risk"], row["risk"]), row["review_count"]
    )
    if row["avg_rating"] is not None:
        head += " · оценка {:.1f}".format(float(row["avg_rating"]))
    lines = [head]
    lines += ["— {}".format(label) for label in red[:3]]
    lines += ["+ {}".format(label) for label in green[:2]]
    return lines


def coverage(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """(досье всего, с красными флагами, без отзывов) — для страницы компаний."""
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN risk = 'red' THEN 1 ELSE 0 END) AS red,
            SUM(CASE WHEN review_count = 0 THEN 1 ELSE 0 END) AS empty
        FROM company_dossier
        """
    ).fetchone()
    return int(row["total"] or 0), int(row["red"] or 0), int(row["empty"] or 0)


def list_dossiers(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        """
        SELECT * FROM company_dossier
        ORDER BY CASE risk
                    WHEN 'red' THEN 0
                    WHEN 'yellow' THEN 1
                    WHEN 'green' THEN 2
                    ELSE 3
                 END,
                 review_count DESC,
                 company
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
