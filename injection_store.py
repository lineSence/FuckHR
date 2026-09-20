"""Находки промпт-инъекций: хранение, улики, строки карточки.

Отделено от логики по [CORE-024]. Здесь нет ни разбора текста, ни моделей.

Почему это вообще хранится, а не просто вырезается в логах. Спрятанная в
вакансии команда для ИИ-ассистента — не техническая помеха, а поступок
работодателя: попытка обмануть инструмент кандидата. Такое место в карточке и
в оценке компании, а не в отладочном выводе [HRD-003].

Вес улики — 3 «подозрение» (решение владельца 19.09.2026): инъекцию мог
вставить не сам работодатель, а площадка-агрегатор или автор отзыва.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import company_score_rules as SR
import injection
import injection_rules as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS injection_hits (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    company TEXT DEFAULT '',
    code TEXT NOT NULL,
    level TEXT NOT NULL,
    quote TEXT DEFAULT '',
    seen_at TEXT,
    PRIMARY KEY (kind, key, code)
);
CREATE INDEX IF NOT EXISTS injection_company ON injection_hits (company);
"""

# Вес улики в оценке работодателя: подозрение, а не приговор.
EVIDENCE_WEIGHT = 3
EVIDENCE_CODE = "prompt_injection"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record(
    conn: sqlite3.Connection,
    kind: str,
    key: str,
    company: str,
    report: injection.Report,
) -> None:
    """Сохраняет находки. Чистый текст ничего не пишет и ничего не стирает."""
    if not report.dirty or not key:
        return
    ensure_schema(conn)
    for item in report.findings:
        conn.execute(
            "INSERT INTO injection_hits (kind, key, company, code, level, quote, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(kind, key, code) DO UPDATE SET quote = excluded.quote,"
            " level = excluded.level, company = excluded.company,"
            " seen_at = excluded.seen_at",
            (kind, key, company or "", item.code, item.level, item.quote, _now()),
        )
    conn.commit()


def check_text(
    conn: sqlite3.Connection, kind: str, key: str, company: str, text: str
) -> injection.Report:
    """Проверить и запомнить. Возвращает отчёт для вызывающего."""
    report = injection.scan(text)
    if report.dirty:
        record(conn, kind, key, company, report)
    return report


def hits(conn: sqlite3.Connection, kind: str, key: str) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM injection_hits WHERE kind = ? AND key = ? ORDER BY code",
            (kind, key),
        ).fetchall()
    except sqlite3.Error:  # старая база [CORE-017]
        return []


def lines(conn: sqlite3.Connection, key: str) -> list[str]:
    """Строки под карточкой вакансии."""
    out = []
    for row in hits(conn, "vacancy", key):
        if str(row["level"]) != R.RED:
            continue
        title = R.CODES.get(str(row["code"]), (str(row["code"]), 0))[0]
        out.append("🧨 {}: «{}»".format(title, str(row["quote"])[:120]))
    return out[:2]


def evidence(conn: sqlite3.Connection, company: str) -> list[tuple]:
    """Улики для company_score: код, ось, полярность, вес, доверие, текст, дата.

    Возвращаются примитивы, чтобы хранилище не импортировало оценку.
    """
    company = (company or "").strip()
    if not company:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM injection_hits WHERE company = ? AND level = ?"
            " ORDER BY seen_at DESC LIMIT 5",
            (company, R.RED),
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        title = R.CODES.get(str(row["code"]), (str(row["code"]), 0))[0]
        out.append(
            (
                EVIDENCE_CODE,
                SR.TRUTH,
                "red",
                EVIDENCE_WEIGHT,
                1.0,  # наше наблюдение, а не чужие слова
                "{} в тексте: «{}»".format(title, str(row["quote"])[:120]),
                str(row["seen_at"] or ""),
            )
        )
    return out


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """Сколько объектов с инъекциями и сколько из них красных."""
    try:
        total = conn.execute(
            "SELECT COUNT(DISTINCT kind || key) FROM injection_hits"
        ).fetchone()[0]
        red = conn.execute(
            "SELECT COUNT(DISTINCT kind || key) FROM injection_hits WHERE level = ?",
            (R.RED,),
        ).fetchone()[0]
    except sqlite3.Error:
        return 0, 0
    return int(total or 0), int(red or 0)


__all__ = (
    "EVIDENCE_CODE",
    "EVIDENCE_WEIGHT",
    "check_text",
    "counts",
    "ensure_schema",
    "evidence",
    "hits",
    "lines",
    "record",
)
