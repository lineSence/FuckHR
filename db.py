"""Слой хранения MVP: SQLite без ORM и без sqlite-vec (ADR-008, ADR-014).

Главное здесь — таблица vacancy_snapshots. Она начинает заполняться с первого
запуска, хотя польза от неё появится только через месяцы: без истории публикаций
детектор HR-брехни (ADR-009) не работает.

История состоит из двух половин. Первую пишет прогон: «вакансия видна, вот её
поля». Вторую — deactivate_missing: «вакансию перестали показывать». Без второй
половины сценарий «закрыли и открыли снова» неотличим от «висит месяц», а это и
есть главный сигнал детектора.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

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


def cached_details(conn: sqlite3.Connection, key: str) -> tuple[str, list[str], str | None] | None:
    """Описание и навыки, которые уже лежат в базе, если они там есть.

    Нужно, чтобы не качать карточку вакансии второй раз: страница детали стоит
    паузы в пару секунд, и на повторном прогоне это основная часть времени.
    Возвращает (описание, навыки, дата публикации) или None.
    """
    row = conn.execute(
        "SELECT description, skills, published_at FROM vacancies WHERE key = ?", (key,)
    ).fetchone()
    if row is None or not (row["description"] or "").strip():
        return None
    try:
        skills = json.loads(row["skills"] or "[]")
    except (TypeError, ValueError):
        skills = []
    return row["description"], list(skills), row["published_at"]


def touch_seen(conn: sqlite3.Connection, keys: Iterable[str]) -> None:
    """Отмечает, что вакансия попалась в выдаче, даже если скоринг её отклонил.

    Без этого отклонённые вакансии никогда не считались бы «пропавшими»:
    строки в vacancies у них нет, и deactivate_missing их не увидит.
    """
    now = utcnow()
    conn.executemany(
        "UPDATE vacancies SET last_seen_at = ? WHERE key = ?",
        [(now, key) for key in keys],
    )
    conn.commit()


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


def last_snapshot_active(conn: sqlite3.Connection, key: str) -> bool | None:
    """Состояние последнего слепка: True, False или None, если слепков нет."""
    row = conn.execute(
        """
        SELECT is_active FROM vacancy_snapshots
        WHERE key = ?
        ORDER BY seen_at DESC, id DESC
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    return None if row is None else bool(row["is_active"])


def known_keys(conn: sqlite3.Connection, days: int = 30) -> list[str]:
    """Ключи вакансий, которые попадались за последние `days` дней."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    cur = conn.execute("SELECT key FROM vacancies WHERE last_seen_at >= ?", (since,))
    return [row["key"] for row in cur.fetchall()]


def deactivate_missing(
    conn: sqlite3.Connection, seen_keys: Iterable[str], days: int = 30
) -> list[str]:
    """Пишет слепок is_active = 0 для вакансий, которых не было в этом прогоне.

    Вызывать ТОЛЬКО после успешного прогона. Если hh.ru показал капчу или отдал
    пустую выдачу, вызов приведёт к тому, что вся база разом «закроется» — и
    история, ради которой всё это делается, превратится в мусор.

    Повторный вызов ничего не дублирует: у вакансии, чей последний слепок уже
    неактивен, новый слепок не появляется.
    """
    seen = set(seen_keys)
    now = utcnow()
    closed: list[str] = []
    for key in known_keys(conn, days):
        if key in seen or last_snapshot_active(conn, key) is False:
            continue
        row = conn.execute(
            """
            SELECT external_id, published_at, company_id, salary_from, salary_to
            FROM vacancies WHERE key = ?
            """,
            (key,),
        ).fetchone()
        if row is None:
            continue
        conn.execute(
            """
            INSERT INTO vacancy_snapshots (
                key, seen_at, external_id, published_at, company_id,
                salary_from, salary_to, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                key,
                now,
                row["external_id"],
                row["published_at"],
                row["company_id"],
                row["salary_from"],
                row["salary_to"],
            ),
        )
        closed.append(key)
    conn.commit()
    if closed:
        log.info("пропали из выдачи: %s", len(closed))
    return closed


def _reopen_cycles(conn: sqlite3.Connection, key: str, since: str) -> int:
    """Сколько раз вакансия возвращалась в выдачу после исчезновения."""
    rows = conn.execute(
        """
        SELECT is_active FROM vacancy_snapshots
        WHERE key = ? AND seen_at >= ?
        ORDER BY seen_at, id
        """,
        (key, since),
    ).fetchall()
    if not rows:
        return 0
    cycles = 1
    previous: bool | None = None
    for row in rows:
        current = bool(row["is_active"])
        if previous is False and current is True:
            cycles += 1
        previous = current
    return cycles


def republish_count(conn: sqlite3.Connection, key: str, months: int = 8) -> int:
    """Сколько раз вакансия публиковалась заново за период.

    Считается тремя независимыми способами, берётся максимум:
    1. разные даты публикации — точнее всего, но published_at из HTML бывает пустым;
    2. разные external_id при одном ключе — hh.ru выдаёт новый id при перепубликации;
    3. циклы «пропала — появилась снова» по слепкам.

    Раньше работал только первый способ, и при пустом published_at сигнал молча
    возвращал 0. Молчаливый ноль хуже грубой оценки.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat(
        timespec="seconds"
    )
    row = conn.execute(
        """
        SELECT
            COUNT(DISTINCT substr(published_at, 1, 10)) AS by_date,
            COUNT(DISTINCT external_id) AS by_id
        FROM vacancy_snapshots
        WHERE key = ? AND seen_at >= ?
        """,
        (key, since),
    ).fetchone()
    by_date = int(row["by_date"] or 0)
    by_id = int(row["by_id"] or 0)
    return max(by_date, by_id, _reopen_cycles(conn, key, since))


def published_at_coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(слепков с датой публикации, всего слепков) — здоровье сигнала одним взглядом."""
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN published_at IS NOT NULL AND published_at <> '' THEN 1 ELSE 0 END) AS filled,
            COUNT(*) AS total
        FROM vacancy_snapshots
        """
    ).fetchone()
    return int(row["filled"] or 0), int(row["total"] or 0)


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


def set_feedback(conn: sqlite3.Connection, key: str, value: str) -> int:
    """Записывает оценку и возвращает число изменённых строк.

    Нуль означает, что ключ из кнопки не найден в базе — обычно это разные DB_PATH
    у run.py и bot.py, а не потеря данных.
    """
    cur = conn.execute("UPDATE vacancies SET feedback = ? WHERE key = ?", (value, key))
    conn.commit()
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM vacancies) AS vacancies,
            (SELECT COUNT(*) FROM vacancy_snapshots) AS snapshots,
            (SELECT COUNT(*) FROM vacancy_snapshots WHERE is_active = 0) AS closed,
            (SELECT COUNT(*) FROM vacancies WHERE notified_at IS NOT NULL) AS notified
        """
    ).fetchone()
    return {k: int(row[k]) for k in row.keys()}
