"""Хранение отдельных отзывов и их хэшей. Только таблицы и запросы.

Две таблицы:

- `review_items` — один разобранный отзыв: дата, оценка, `fake_score`, список
  сработавших сигналов и метка. Полный текст не дублируется (он уже лежит в
  `company_reviews.body`), хранится короткая выдержка для интерфейса.
- `review_hashes` — хэш нормализованного текста и компания, у которой он
  встретился. Это общая таблица на всю базу, и именно она ловит фабрики
  отзывов, работающие на несколько компаний сразу.

Персональные данные авторов не хранятся [CORE-013].
"""

from __future__ import annotations

import json
import logging
import sqlite3

from datetime import datetime, timezone
from typing import Sequence

import dossier_text

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT NOT NULL,
    url         TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    site        TEXT,
    text_hash   TEXT NOT NULL,
    excerpt     TEXT,
    rating      REAL,
    dated_at    TEXT,
    date_precision TEXT NOT NULL DEFAULT 'none',
    has_reply   INTEGER NOT NULL DEFAULT 0,
    fake_score  REAL NOT NULL DEFAULT 0,
    signals     TEXT NOT NULL DEFAULT '[]',
    patterns    TEXT NOT NULL DEFAULT '[]',
    label       TEXT NOT NULL DEFAULT 'clean',
    created_at  TEXT NOT NULL,
    UNIQUE (company, url, idx)
);

CREATE TABLE IF NOT EXISTS review_hashes (
    text_hash  TEXT NOT NULL,
    company    TEXT NOT NULL,
    url        TEXT,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (text_hash, company)
);

CREATE INDEX IF NOT EXISTS idx_items_company ON review_items(company);
CREATE INDEX IF NOT EXISTS idx_items_label ON review_items(label);
CREATE INDEX IF NOT EXISTS idx_hashes_hash ON review_hashes(text_hash);
"""

EXCERPT_CHARS = 240


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # База у владельца одна и живёт месяцами: недостающая колонка не имеет
    # права ронять прогон.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(review_items)")}
    if "patterns" not in columns:
        log.info("добавляю колонку patterns в review_items")
        conn.execute(
            "ALTER TABLE review_items ADD COLUMN patterns TEXT NOT NULL DEFAULT '[]'"
        )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def known_hashes(conn: sqlite3.Connection, company: str) -> dict[str, str]:
    """Хэши отзывов о *других* компаниях: хэш → первая встреченная компания."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT text_hash, company FROM review_hashes WHERE company <> ?", (company,)
    )
    return {str(row[0]): str(row[1]) for row in rows}


def store(
    conn: sqlite3.Connection,
    company: str,
    items: Sequence[object],
    verdicts: Sequence[object],
) -> None:
    """Перезаписывает разобранные отзывы компании и пополняет таблицу хэшей."""
    ensure_schema(conn)
    if not company:
        return
    by_index = {getattr(v, "index", 0): v for v in verdicts}
    now = _now()
    for item in items:
        verdict = by_index.get(getattr(item, "index", 0))
        digest = str(getattr(verdict, "text_hash", "") or "")
        if not digest:
            continue
        conn.execute(
            """
            INSERT INTO review_items (
                company, url, idx, site, text_hash, excerpt, rating, dated_at,
                date_precision, has_reply, fake_score, signals, patterns, label,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company, url, idx) DO UPDATE SET
                text_hash = excluded.text_hash,
                excerpt = excluded.excerpt,
                rating = excluded.rating,
                dated_at = excluded.dated_at,
                date_precision = excluded.date_precision,
                has_reply = excluded.has_reply,
                fake_score = excluded.fake_score,
                signals = excluded.signals,
                patterns = excluded.patterns,
                label = excluded.label
            """,
            (
                company,
                getattr(item, "url", ""),
                int(getattr(item, "index", 0)),
                getattr(item, "site", ""),
                digest,
                str(getattr(item, "text", ""))[:EXCERPT_CHARS],
                getattr(item, "rating", None),
                getattr(item, "dated_at", None),
                getattr(item, "date_precision", "none"),
                int(bool(getattr(item, "has_reply", False))),
                float(getattr(verdict, "score", 0.0) or 0.0),
                json.dumps(list(getattr(verdict, "signals", ())), ensure_ascii=False),
                json.dumps(
                    list(dossier_text.codes_in(str(getattr(item, "text", "")))),
                    ensure_ascii=False,
                ),
                str(getattr(verdict, "label", "clean")),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO review_hashes (text_hash, company, url, first_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(text_hash, company) DO NOTHING
            """,
            (digest, company, getattr(item, "url", ""), now),
        )
    conn.commit()


def load_items(
    conn: sqlite3.Connection, company: str, label: str = ""
) -> list[sqlite3.Row]:
    """Разобранные отзывы компании, подозрительные — первыми."""
    ensure_schema(conn)
    if label:
        return conn.execute(
            "SELECT * FROM review_items WHERE company = ? AND label = ?"
            " ORDER BY fake_score DESC, idx",
            (company, label),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM review_items WHERE company = ? ORDER BY fake_score DESC, idx",
        (company,),
    ).fetchall()


def drop_orphan_hashes(conn: sqlite3.Connection) -> int:
    """Убирает хэши, для которых не осталось ни одного отзыва.

    Хэши живут ровно столько, сколько живут сами отзывы: иначе таблица растёт
    вечно и после чистки базы начинает обвинять компании в совпадении с
    текстами, которых уже нет.
    """
    ensure_schema(conn)
    cursor = conn.execute(
        "DELETE FROM review_hashes WHERE text_hash NOT IN"
        " (SELECT text_hash FROM review_items)"
    )
    conn.commit()
    return int(cursor.rowcount or 0)


__all__ = (
    "SCHEMA",
    "drop_orphan_hashes",
    "ensure_schema",
    "known_hashes",
    "load_items",
    "store",
)
