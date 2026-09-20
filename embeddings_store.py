"""Таблица векторов: что уже посчитано, чем и когда.

Одна таблица на все виды текстов (`kind`): вакансии и отзывы считаются одной
моделью, и смешать их нельзя только логически, а не физически. Имя модели
лежит в каждой строке: смена модели делает старые векторы несравнимыми, и
молча подмешать их в поиск было бы худшим из вариантов [LLM-011].

Пересчёт дешёвый (текст уже в базе, сеть локальная), поэтому чужие строки не
конвертируются, а удаляются: `drop_other_models` вызывается перед работой.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Iterable, Sequence

import embeddings
import llm_embed
from db import utcnow

log = logging.getLogger("fuckhr")

KIND_VACANCY = "vacancy"
KIND_REVIEW = "review"

SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (kind, key, model)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_kind ON embeddings(kind, model);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def save(
    conn: sqlite3.Connection, kind: str, model: str, rows: Iterable[tuple[str, Sequence[float]]]
) -> int:
    """Пишет векторы. Возвращает число записанных строк."""
    ensure_schema(conn)
    written = 0
    for key, values in rows:
        conn.execute(
            """
            INSERT INTO embeddings (kind, key, model, dim, vector, computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, key, model) DO UPDATE SET
                dim = excluded.dim,
                vector = excluded.vector,
                computed_at = excluded.computed_at
            """,
            (kind, key, model, len(values), embeddings.pack(values), utcnow()),
        )
        written += 1
    conn.commit()
    return written


def load(conn: sqlite3.Connection, kind: str, model: str) -> dict[str, "object"]:
    """Все векторы вида: {ключ: вектор}. Нет таблицы — пустой словарь."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT key, vector FROM embeddings WHERE kind = ? AND model = ?", (kind, model)
    ).fetchall()
    return {row[0]: embeddings.unpack(row[1]) for row in rows}


def missing(
    conn: sqlite3.Connection, kind: str, model: str, keys: Sequence[str]
) -> list[str]:
    """Ключи без вектора. Пересчитывать уже посчитанное незачем."""
    known = set(load(conn, kind, model))
    return [key for key in keys if key not in known]


def drop_other_models(conn: sqlite3.Connection, model: str) -> int:
    """Убирает векторы прежних моделей: сравнивать их с новыми нельзя [LLM-011]."""
    ensure_schema(conn)
    cursor = conn.execute("DELETE FROM embeddings WHERE model <> ?", (model,))
    conn.commit()
    if cursor.rowcount:
        log.info("удалено %s векторов прежних моделей", cursor.rowcount)
    return int(cursor.rowcount or 0)


def vectorize(
    conn: sqlite3.Connection,
    gateway: object | None,
    kind: str,
    texts: dict[str, str],
) -> str:
    """Досчитывает недостающие векторы. Возвращает имя модели или пустую строку."""
    model = llm_embed.model_name(gateway)
    if not model or not texts:
        return ""
    drop_other_models(conn, model)
    todo = missing(conn, kind, model, list(texts))
    if not todo:
        return model
    vectors = llm_embed.embed(gateway, [texts[key] for key in todo])
    if vectors is None:
        return ""
    save(conn, kind, model, zip(todo, vectors))
    log.info("посчитано векторов (%s): %s", kind, len(todo))
    return model


def counts(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(вид, модель, сколько) — для страницы очистки и отладки."""
    ensure_schema(conn)
    return [
        (row[0], row[1], int(row[2]))
        for row in conn.execute(
            "SELECT kind, model, COUNT(*) FROM embeddings GROUP BY kind, model"
        ).fetchall()
    ]


__all__ = (
    "KIND_REVIEW",
    "KIND_VACANCY",
    "SCHEMA",
    "counts",
    "drop_other_models",
    "ensure_schema",
    "load",
    "missing",
    "save",
    "vectorize",
)
