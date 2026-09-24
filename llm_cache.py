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
    created_at  TEXT NOT NULL,
    prompt      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llm_cache_prompt ON llm_cache(prompt);
"""

def ensure_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)
    # Колонка появилась позже схемы: у кого база старая, добавляем на месте.
    names = {row[1] for row in conn.execute("PRAGMA table_info(llm_cache)")}
    if "prompt" not in names:
        conn.execute("ALTER TABLE llm_cache ADD COLUMN prompt TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_cache_prompt ON llm_cache(prompt)")
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


def prompt_digest(
    profile: str, messages: Sequence[dict[str, str]], temperature: float
) -> str:
    """Тот же ключ без маршрута и модели: «этот же вопрос, кем бы ни отвечали».

    Нужен не для поиска ответа, а для объяснения промаха: спросили то же самое,
    но другой моделью, — или спросили новое.
    """
    return digest(profile, messages, temperature, "", "")


def miss_reason(conn: sqlite3.Connection | None, prompt: str) -> str:
    """Почему не попали в кэш: `model` — тот же вопрос был задан другой
    моделью, `new` — вопроса раньше не было, `unknown` — базы нет.

    Смена модели или маршрута обнуляет кэш целиком (модель входит в ключ, и это
    правильно: ответ слабой локальной модели не должен подменять ответ прокси).
    Но в сводке прогона «из кэша 0» выглядит как поломка кэша, хотя это
    последствие смены модели, — разница видна только здесь.
    """
    if conn is None or not prompt:
        return "unknown"
    row = conn.execute(
        "SELECT 1 FROM llm_cache WHERE prompt = ? LIMIT 1", (prompt,)
    ).fetchone()
    return "model" if row is not None else "new"


def get(conn: sqlite3.Connection | None, key: str) -> str | None:
    if conn is None:
        return None
    row = conn.execute("SELECT response FROM llm_cache WHERE hash = ?", (key,)).fetchone()
    return None if row is None else row[0]


def put(
    conn: sqlite3.Connection | None,
    key: str,
    stage: str,
    profile: str,
    response: str,
    prompt: str = "",
) -> None:
    if conn is None:
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO llm_cache (hash, stage, profile, response, created_at, prompt)
        VALUES (?, ?, ?, ?, datetime('now'), ?)
        """,
        (key, stage, profile, response, prompt),
    )
    conn.commit()


__all__ = (
    "CACHE_SCHEMA",
    "digest",
    "ensure_cache",
    "get",
    "miss_reason",
    "prompt_digest",
    "put",
)
