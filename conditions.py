"""Условия работы, извлечённые моделью из описания вакансии (этап extract).

Почему отдельная таблица, а не поле в vacancies: у одной вакансии условий
много (формат, график, вилка, стек), и каждое хранится со своей дословной
цитатой. Цитата — не украшение: без неё невозможно отличить условие, которое
действительно написано в вакансии, от того, что модель додумала [CORE-019].

Запись идёт полной заменой набора для вакансии: описание могло измениться,
и смешивать условия двух редакций в одном списке хуже, чем потерять старое.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Sequence

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancy_conditions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT NOT NULL,
    quote       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conditions_key ON vacancy_conditions(key);
"""

# Порядок полей в карточке: сначала то, из-за чего отказываются от вакансии.
FIELD_ORDER = ("format", "office", "schedule", "salary", "grade", "stack", "process", "other")

FIELD_LABELS = {
    "format": "формат",
    "office": "офис",
    "schedule": "график",
    "salary": "деньги",
    "grade": "уровень",
    "stack": "стек",
    "process": "процесс",
    "other": "прочее",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def store(conn: sqlite3.Connection, key: str, items: Sequence[object]) -> int:
    """Заменяет набор условий вакансии. Пустой список ничего не трогает.

    Пустота значит две разные вещи — «условий нет в тексте» и «модель не ответила»,
    и второе не повод вытирать уже известное.
    """
    rows = []
    for item in items or ():
        field = str(getattr(item, "field", "") or "other")
        value = str(getattr(item, "value", "") or "").strip()
        quote = str(getattr(item, "quote", "") or "").strip()
        if not value or not quote:
            continue
        rows.append((key, field, value, quote))
    if not rows:
        return 0

    conn.execute("DELETE FROM vacancy_conditions WHERE key = ?", (key,))
    conn.executemany(
        "INSERT INTO vacancy_conditions (key, field, value, quote) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM vacancy_conditions WHERE key = ? ORDER BY id", (key,)
    ).fetchall()


def lines(conn: sqlite3.Connection, key: str, limit: int = 6) -> list[str]:
    """Строки для карточки и интерфейса, в понятном владельцу порядке."""
    rows = load(conn, key)
    order = {field: i for i, field in enumerate(FIELD_ORDER)}
    rows.sort(key=lambda r: order.get(r["field"], len(FIELD_ORDER)))
    out = []
    for row in rows[:limit]:
        label = FIELD_LABELS.get(row["field"], row["field"])
        out.append(f"{label}: {row['value']}")
    return out


def coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(вакансий с условиями, всего вакансий) — видно, работает ли этап."""
    with_conditions = conn.execute(
        "SELECT COUNT(DISTINCT key) FROM vacancy_conditions"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]
    return int(with_conditions), int(total)


__all__ = (
    "FIELD_LABELS",
    "FIELD_ORDER",
    "SCHEMA",
    "coverage",
    "ensure_schema",
    "lines",
    "load",
    "store",
)
