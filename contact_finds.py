"""Найденные контакты по вакансии: результат этапа discovery при общем сборе.

Зачем отдельно от `contacts.py`. Таблица `contacts` — это лог аутрича по
[OUT-007]: там живёт решение владельца и его статусы. Здесь — наблюдение
сборщика: какие рабочие каналы вообще нашлись у этой вакансии. Смешивать их
нельзя: найденный канал ещё не контакт, и правило «не чаще раза в три месяца»
к нему не применяется.

Таблица переписывается при каждом сборе по ключу вакансии: это кэш находки, а
не история.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import company_key
import contacts

SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_finds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    company TEXT,
    person TEXT,
    role TEXT,
    role_rank INTEGER NOT NULL DEFAULT 99,
    channel_kind TEXT NOT NULL,
    channel_value TEXT NOT NULL,
    source_url TEXT,
    confidence TEXT NOT NULL DEFAULT 'low',
    guessed INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    dropped TEXT,
    found_at TEXT NOT NULL,
    UNIQUE (key, channel_value)
);
CREATE INDEX IF NOT EXISTS idx_finds_key ON contact_finds(key);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def save(conn: sqlite3.Connection, discovery: contacts.Discovery) -> int:
    """Перезаписывает находки по вакансии и возвращает их число."""
    ensure_schema(conn)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    dropped = "\n".join(discovery.dropped)
    conn.execute("DELETE FROM contact_finds WHERE key = ?", (discovery.key,))
    conn.executemany(
        """
        INSERT OR REPLACE INTO contact_finds
            (key, company, person, role, role_rank, channel_kind, channel_value,
             source_url, confidence, guessed, notes, dropped, found_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                discovery.key, discovery.company, c.person, c.role, c.role_rank,
                c.channel_kind, c.channel_value, c.source_url, c.confidence,
                int(c.guessed), c.notes, dropped, now,
            )
            for c in discovery.candidates
        ],
    )
    conn.commit()
    return len(discovery.candidates)


def load(conn: sqlite3.Connection, key: str, company: str | None = None) -> contacts.Discovery | None:
    """Находки по вакансии или None, если этап по ней не отрабатывал."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM contact_finds WHERE key = ? ORDER BY role_rank, id", (key,)
    ).fetchall()
    if not rows:
        return None
    found = tuple(
        contacts.Candidate(
            channel_kind=row["channel_kind"],
            channel_value=row["channel_value"],
            person=row["person"],
            role=row["role"],
            role_rank=int(row["role_rank"] or 99),
            source_url=row["source_url"],
            confidence=row["confidence"],
            guessed=bool(row["guessed"]),
            notes=row["notes"],
        )
        for row in rows
    )
    dropped = tuple(line for line in str(rows[0]["dropped"] or "").split("\n") if line)
    return contacts.Discovery(
        key=key,
        company=company or rows[0]["company"],
        candidates=found,
        dropped=dropped,
    )


def for_company(conn: sqlite3.Connection, company: str, limit: int = 20) -> list[sqlite3.Row]:
    """Находки по всем вакансиям работодателя — для карточки компании.

    Название сводится через company_key: «ООО «Ромашка»» и «Ромашка» — один
    работодатель, иначе половина каналов не попадёт в его карточку.
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM contact_finds ORDER BY role_rank, found_at DESC"
    ).fetchall()
    same = [r for r in rows if r["company"] and company_key.same(r["company"], company)]
    return same[:limit]


def coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(вакансий с прямым каналом, вакансий, по которым этап отработал)."""
    ensure_schema(conn)
    direct = conn.execute(
        "SELECT COUNT(DISTINCT key) FROM contact_finds WHERE role_rank <= 6"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(DISTINCT key) FROM contact_finds").fetchone()[0]
    return int(direct), int(total)
