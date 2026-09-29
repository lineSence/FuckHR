"""Веса линейного гейта в базе: по одной строке на этап и эмбеддер.

Зачем в базе, а не файлом. Веса — производная от разметки и от модели
векторов: та же таблица `embeddings`, тот же ключ модели [LLM-011]. Лежать им
рядом с данными, из которых они посчитаны, а не в папке, которую забудут
скопировать.

Почему не `embeddings.pack`. Тот нормирует вектор: для косинуса это правильно,
для весов регрессии — потеря масштаба вместе со смещением.
"""

from __future__ import annotations

import array
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

TYPECODE = "f"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_models (
    stage      TEXT NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    weights    BLOB NOT NULL,
    bias       REAL NOT NULL,
    rows       INTEGER NOT NULL DEFAULT 0,
    positives  INTEGER NOT NULL DEFAULT 0,
    auroc      REAL,
    accuracy   REAL,
    low        REAL,
    high       REAL,
    decide     REAL,
    trained_at TEXT NOT NULL,
    PRIMARY KEY (stage, model)
);
"""


@dataclass(frozen=True)
class GateModel:
    """Обученный гейт одного этапа."""

    stage: str
    model: str
    weights: array.array
    bias: float
    rows: int
    positives: int
    auroc: float | None
    accuracy: float | None
    low: float | None
    high: float | None
    decide: float | None
    trained_at: str


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Колонка появилась позже схемы; у старой базы CREATE TABLE IF NOT EXISTS
    # её не добавит, поэтому добавляем на месте (урок llm_cache, PR #61).
    names = {row[1] for row in conn.execute("PRAGMA table_info(gate_models)")}
    if "decide" not in names:
        conn.execute("ALTER TABLE gate_models ADD COLUMN decide REAL")


def pack(values: Sequence[float]) -> bytes:
    return array.array(TYPECODE, (float(value) for value in values)).tobytes()


def unpack(blob: bytes) -> array.array:
    values = array.array(TYPECODE)
    values.frombytes(blob)
    return values


def save(
    conn: sqlite3.Connection,
    stage: str,
    model: str,
    weights: Sequence[float],
    bias: float,
    rows: int = 0,
    positives: int = 0,
    auroc: float | None = None,
    accuracy: float | None = None,
    low: float | None = None,
    high: float | None = None,
    decide: float | None = None,
) -> None:
    """Пишет веса этапа. Повторное обучение заменяет прежние."""
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO gate_models
            (stage, model, dim, weights, bias, rows, positives,
             auroc, accuracy, low, high, decide, trained_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stage, model) DO UPDATE SET
            dim = excluded.dim, weights = excluded.weights, bias = excluded.bias,
            rows = excluded.rows, positives = excluded.positives,
            auroc = excluded.auroc, accuracy = excluded.accuracy,
            low = excluded.low, high = excluded.high, decide = excluded.decide,
            trained_at = excluded.trained_at
        """,
        (
            stage, model, len(weights), pack(weights), float(bias), int(rows),
            int(positives), auroc, accuracy, low, high, decide,
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        ),
    )
    conn.commit()


def _to_model(row: sqlite3.Row) -> GateModel:
    return GateModel(
        stage=row["stage"],
        model=row["model"],
        weights=unpack(row["weights"]),
        bias=float(row["bias"]),
        rows=int(row["rows"] or 0),
        positives=int(row["positives"] or 0),
        auroc=row["auroc"],
        accuracy=row["accuracy"],
        low=row["low"],
        high=row["high"],
        decide=row["decide"] if "decide" in row.keys() else None,
        trained_at=row["trained_at"],
    )


def load(conn: sqlite3.Connection, stage: str, model: str) -> GateModel | None:
    """Веса этапа для этой модели векторов. Нет строки — None, гейт молчит."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM gate_models WHERE stage = ? AND model = ?", (stage, model)
    ).fetchone()
    return None if row is None else _to_model(row)


def all_models(conn: sqlite3.Connection) -> list[GateModel]:
    """Все обученные гейты: странице надо показать и устаревшие."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return [
        _to_model(row)
        for row in conn.execute("SELECT * FROM gate_models ORDER BY stage, model")
    ]


__all__ = ("GateModel", "all_models", "ensure_schema", "load", "pack", "save", "unpack")
