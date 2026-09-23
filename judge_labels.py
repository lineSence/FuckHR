"""Разметка учителя для решателя: полный текст, который видела модель, и её вердикт.

Зачем. Laya без дообучения на этапах `review_fake` и `ai_text` отвечает по
позиции варианта (docs/laya.md), а дообучать было не на чем: в `review_items`
лежит выдержка в 240 символов, и отрицательный ответ модели не сохранялся
вовсе. Здесь хранится ровно тот текст, что ушёл в промпт, и ответ «да/нет»
(решение владельца 23.09.2026, B-22 вопрос 39).

Что считается меткой. «Да» — только с дословной цитатой, как в самом этапе.
«Нет» — явный ответ `experience`/`human`. «Да» без цитаты не пишется: такой
ответ этап выбрасывает, и учить на нём нельзя. Источник `owner` — поправка
владельца, она весит больше учителя (laya_dataset.py).

Новой категории данных нет: тот же текст отзыва уже лежит в
`company_reviews.body` [CORE-013]. Контакты маскируются при выгрузке.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Mapping

from fake_reviews import text_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS judge_labels (
    stage      TEXT NOT NULL,
    text_hash  TEXT NOT NULL,
    text       TEXT NOT NULL,
    verdict    INTEGER NOT NULL,
    source     TEXT NOT NULL DEFAULT 'llm',
    company    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (stage, text_hash, source)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def record(
    conn: sqlite3.Connection,
    stage: str,
    labels: Mapping[str, bool],
    company: str = "",
    source: str = "llm",
) -> int:
    """Пишет метки «текст → да/нет». Повторный ответ по тому же тексту заменяет прежний."""
    if not labels:
        return 0
    ensure_schema(conn)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows = [
        (stage, text_hash(text), text, int(bool(verdict)), source, company, now)
        for text, verdict in labels.items()
        if (text or "").strip()
    ]
    conn.executemany(
        """
        INSERT INTO judge_labels (stage, text_hash, text, verdict, source, company, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stage, text_hash, source) DO UPDATE SET
            text = excluded.text, verdict = excluded.verdict,
            company = excluded.company, created_at = excluded.created_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def rows(conn: sqlite3.Connection, stage: str) -> list[sqlite3.Row]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            "SELECT * FROM judge_labels WHERE stage = ? ORDER BY company, text_hash, source",
            (stage,),
        )
    )


__all__ = ("ensure_schema", "record", "rows")
