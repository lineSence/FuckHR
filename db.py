"""Слой хранения MVP: SQLite без ORM и без sqlite-vec (ADR-008, ADR-014).

Главное здесь — таблица vacancy_snapshots. Она начинает заполняться с первого
запуска, хотя польза от неё появится только через месяцы: без истории публикаций
детектор HR-брехни (ADR-009) не работает.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    key             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT,
    company_id      TEXT,
    area            TEXT,
    salary_from     INTEGER,
    salary_to       INTEGER,
    currency        TEXT,
    gross           INTEGER,
    schedule        TEXT,
    experience      TEXT,
    employment      TEXT,
    skills          TEXT,
    description     TEXT,
    published_at    TEXT,
    score           REAL,
    score_reasons   TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    notified_at     TEXT,
    feedback        TEXT
);

CREATE TABLE IF NOT EXISTS vacancy_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL,
    seen_at       TEXT NOT NULL,
    external_id   TEXT,
    published_at  TEXT,
    company_id    TEXT,
    salary_from   INTEGER,
    salary_to     INTEGER,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_snapshots_key_seen
    ON vacancy_snapshots (key, seen_at);
CREATE INDEX IF NOT EXISTS idx_vacancies_notified
    ON vacancies (notified_at, score);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """WAL + busy_timeout: на Windows блокировки файла жёстче, чем на Linux."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_vacancy(
    conn: sqlite3.Connection,
    vacancy: Any,
    score: float,
    reasons: Sequence[str],
) -> bool:
    """Возвращает True, если вакансия видится впервые.

    Повторная встреча обновляет данные и last_seen_at, но не трогает notified_at и
    feedback — иначе одна и та же вакансия пришла бы в Telegram каждое утро.
    """
    now = utcnow()
    cur = conn.execute("SELECT key FROM vacancies WHERE key = ?", (vacancy.key,))
    is_new = cur.fetchone() is None
    payload = {
        "key": vacancy.key,
        "source": vacancy.source,
        "external_id": vacancy.external_id,
        "url": vacancy.url,
        "title": vacancy.title,
        "company": vacancy.company,
        "company_id": vacancy.company_id,
        "area": vacancy.area,
        "salary_from": vacancy.salary_from,
        "salary_to": vacancy.salary_to,
        "currency": vacancy.currency,
        "gross": None if vacancy.gross is None else int(vacancy.gross),
        "schedule": vacancy.schedule,
        "experience": vacancy.experience,
        "employment": vacancy.employment,
        "skills": json.dumps(vacancy.skills, ensure_ascii=False),
        "description": vacancy.description,
        "published_at": vacancy.published_at,
        "score": score,
        "score_reasons": json.dumps(list(reasons), ensure_ascii=False),
        "now": now,
    }
    if is_new:
        conn.execute(
            """
            INSERT INTO vacancies (
                key, source, external_id, url, title, company, company_id, area,
                salary_from, salary_to, currency, gross, schedule, experience,
                employment, skills, description, published_at, score, score_reasons,
                first_seen_at, last_seen_at
            ) VALUES (
                :key, :source, :external_id, :url, :title, :company, :company_id, :area,
                :salary_from, :salary_to, :currency, :gross, :schedule, :experience,
                :employment, :skills, :description, :published_at, :score, :score_reasons,
                :now, :now
            )
            """,
            payload,
        )
    else:
        conn.execute(
            """
            UPDATE vacancies SET
                url = :url, title = :title, company = :company, company_id = :company_id,
                area = :area, salary_from = :salary_from, salary_to = :salary_to,
                currency = :currency, gross = :gross, schedule = :schedule,
                experience = :experience, employment = :employment, skills = :skills,
                description = :description, published_at = :published_at,
                score = :score, score_reasons = :score_reasons, last_seen_at = :now
            WHERE key = :key
            """,
            payload,
        )
    conn.commit()
    return is_new


def add_snapshot(conn: sqlite3.Connection, vacancy: Any, is_active: bool = True) -> None:
    conn.execute(
        """
        INSERT INTO vacancy_snapshots (
            key, seen_at, external_id, published_at, company_id,
            salary_from, salary_to, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            vacancy.key,
            utcnow(),
            vacancy.external_id,
            vacancy.published_at,
            vacancy.company_id,
            vacancy.salary_from,
            vacancy.salary_to,
            int(is_active),
        ),
    )
    conn.commit()


def republish_count(conn: sqlite3.Connection, key: str, months: int = 8) -> int:
    """Сколько разных дат публикации у этой вакансии за период.

    Заготовка для детектора брехни. На свежей базе всегда вернёт 1 — это нормально.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat()
    cur = conn.execute(
        """
        SELECT COUNT(DISTINCT substr(published_at, 1, 10)) AS n
        FROM vacancy_snapshots
        WHERE key = ? AND seen_at >= ? AND published_at IS NOT NULL
        """,
        (key, since),
    )
    row = cur.fetchone()
    return int(row["n"] or 0)


def pending_cards(
    conn: sqlite3.Connection, min_score: float, limit: int = 10
) -> list[sqlite3.Row]:
    cur = conn.execute(
        """
        SELECT * FROM vacancies
        WHERE notified_at IS NULL AND score >= ?
        ORDER BY score DESC, first_seen_at DESC
        LIMIT ?
        """,
        (min_score, limit),
    )
    return cur.fetchall()


def mark_notified(conn: sqlite3.Connection, keys: Iterable[str]) -> None:
    now = utcnow()
    conn.executemany(
        "UPDATE vacancies SET notified_at = ? WHERE key = ?",
        [(now, key) for key in keys],
    )
    conn.commit()


def set_feedback(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("UPDATE vacancies SET feedback = ? WHERE key = ?", (value, key))
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM vacancies) AS vacancies,
            (SELECT COUNT(*) FROM vacancy_snapshots) AS snapshots,
            (SELECT COUNT(*) FROM vacancies WHERE notified_at IS NOT NULL) AS notified
        """
    ).fetchone()
    return {k: int(row[k]) for k in row.keys()}
