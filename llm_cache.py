"""Кэш ответов моделей: таблица, ключ и две операции.

Отдельный файл, потому что llm.py упёрся в 25 КБ [CORE-024], а кэш —
самостоятельная тема: схема, ключ, чтение, запись. Публичные имена остаются
доступными через `llm` (реэкспорт), вызовы и тесты переписывать не нужно.

Кэш включён всегда [LLM-006]: в мониторинге 60–80% запросов — это те же
вакансии, что вчера.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Sequence

from llm_profiles import ROUTE_LOCAL

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    hash        TEXT PRIMARY KEY,
    stage       TEXT NOT NULL,
    profile     TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

def ensure_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)
    conn.commit()


def digest(
    profile: str,
    messages: Sequence[dict[str, str]],
    temperature: float,
    route: str = ROUTE_LOCAL,
    model: str = "",
) -> str:
    """Маршрут и модель входят в ключ кэша.

    Иначе ответ слабой локальной модели навсегда подменит собой ответ с
    прокси на тот же промпт.
    """
    blob = json.dumps(
        {
            "profile": profile,
            "messages": list(messages),
            "t": temperature,
            "route": route,
            "model": model,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(conn: sqlite3.Connection | None, key: str) -> str | None:
    if conn is None:
        return None
    row = conn.execute("SELECT response FROM llm_cache WHERE hash = ?", (key,)).fetchone()
    return None if row is None else row[0]


def put(
    conn: sqlite3.Connection | None, key: str, stage: str, profile: str, response: str
) -> None:
    if conn is None:
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO llm_cache (hash, stage, profile, response, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        """,
        (key, stage, profile, response),
    )
    conn.commit()


__all__ = ("CACHE_SCHEMA", "digest", "ensure_cache", "get", "put")
