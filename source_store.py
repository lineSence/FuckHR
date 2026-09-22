"""Где найдена вакансия: одна запись на пару (вакансия, площадка).

Зачем отдельная таблица. Ключ дедупа `Vacancy.key` не содержит площадку:
одна и та же вакансия с hh.ru и SuperJob — это одна строка в `vacancies`, и
это правильно (дубли не должны проходить этапы модели дважды `[CORE-016]`).
Но тогда `source` и `url` в `vacancies` затирали бы друг друга по очереди, а
история публикаций смешала бы площадки — и детектор увидел бы перепубликации,
которых не было.

Поэтому «где видели» живёт здесь, а в `vacancies` остаётся одна главная ссылка
по приоритету площадок (`sources.PRIORITY`).

Побочная польза — метрика, без которой новый источник подключать бессмысленно:
сколько вакансий нашлось **только** на этой площадке. Источник, который месяц
не приносит уникальных, стоит выключить, а не терпеть его капчи.

О людях здесь ничего нет: только ключ вакансии, код площадки и её ссылка.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancy_sources (
    key         TEXT NOT NULL,
    source      TEXT NOT NULL,
    external_id TEXT,
    url         TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (key, source)
);

CREATE INDEX IF NOT EXISTS idx_vacancy_sources_source ON vacancy_sources(source);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def remember(
    conn: sqlite3.Connection, key: str, source: str, external_id: str = "", url: str = ""
) -> None:
    """Отмечает, что вакансия видна на этой площадке. Повтор обновляет last_seen."""
    if not key or not source:
        return
    now = _now()
    conn.execute(
        """
        INSERT INTO vacancy_sources (key, source, external_id, url, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key, source) DO UPDATE SET
            external_id = excluded.external_id,
            url = COALESCE(excluded.url, url),
            last_seen = excluded.last_seen
        """,
        (key, source, external_id or "", url or "", now, now),
    )


def remember_many(conn: sqlite3.Connection, rows: object) -> int:
    """Пачкой: rows — последовательность (key, source, external_id, url)."""
    ensure_schema(conn)
    count = 0
    for row in rows or ():  # type: ignore[union-attr]
        key, source, external_id, url = row
        remember(conn, key, source, external_id, url)
        count += 1
    conn.commit()
    return count


def sources_of(conn: sqlite3.Connection, key: str) -> list[str]:
    ensure_schema(conn)
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT source FROM vacancy_sources WHERE key = ? ORDER BY source", (key,)
        )
    ]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько вакансий видно на каждой площадке."""
    ensure_schema(conn)
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT source, COUNT(*) FROM vacancy_sources GROUP BY source"
        )
    }


def unique_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько вакансий нашлось только на этой площадке и больше нигде.

    Это и есть ответ на вопрос «зачем нам этот источник». Считается по тем
    вакансиям, которые вообще размечены площадками: старые записи без разметки
    в знаменатель не попадают.
    """
    ensure_schema(conn)
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """
            SELECT source, COUNT(*) FROM (
                SELECT key, MIN(source) AS source, COUNT(*) AS n
                FROM vacancy_sources GROUP BY key
            ) WHERE n = 1 GROUP BY source
            """
        )
    }


def drop_orphans(conn: sqlite3.Connection) -> int:
    """Убирает разметку вакансий, которых уже нет в базе."""
    ensure_schema(conn)
    cursor = conn.execute(
        "DELETE FROM vacancy_sources WHERE key NOT IN (SELECT key FROM vacancies)"
    )
    conn.commit()
    return int(cursor.rowcount or 0)


__all__ = (
    "SCHEMA",
    "counts",
    "drop_orphans",
    "ensure_schema",
    "remember",
    "remember_many",
    "sources_of",
    "unique_counts",
)
