"""Хранение общей оценки работодателя: таблица, запись, чтение для карточек.

Оценка живёт своей таблицей, а не колонкой в company_dossier. Причина
записана ещё в market_company.py: досье пересобирается раз в месяц, а рынок и
слепки — каждый прогон. В одной строке получился бы светофор, который врёт
ровно между пересчётами.

Ключ — каноническое название работодателя: сводится тем же resolve_company,
что и досье, иначе «ООО «Ромашка»» и «Ромашка» разъедутся по двум строкам.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import company_score
import company_score_rules as R
import dossier_store
from db import utcnow

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_score (
    company     TEXT PRIMARY KEY,
    level       TEXT NOT NULL DEFAULT 'unknown',
    axes        TEXT NOT NULL DEFAULT '{}',
    evidence    TEXT NOT NULL DEFAULT '[]',
    covered     INTEGER NOT NULL DEFAULT 0,
    veto        TEXT NOT NULL DEFAULT '',
    computed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_score_level ON company_score(level);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def save(conn: sqlite3.Connection, score: company_score.CompanyScore) -> None:
    ensure_schema(conn)
    payload = score.to_dict()
    conn.execute(
        """
        INSERT INTO company_score (
            company, level, axes, evidence, covered, veto, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company) DO UPDATE SET
            level = excluded.level,
            axes = excluded.axes,
            evidence = excluded.evidence,
            covered = excluded.covered,
            veto = excluded.veto,
            computed_at = excluded.computed_at
        """,
        (
            dossier_store.resolve_company(conn, score.company),
            score.level,
            json.dumps(payload["axes"], ensure_ascii=False),
            json.dumps(payload["evidence"], ensure_ascii=False),
            len(score.covered),
            score.veto,
            utcnow(),
        ),
    )
    conn.commit()


def load(conn: sqlite3.Connection, company: str) -> sqlite3.Row | None:
    if not (company or "").strip():
        return None
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM company_score WHERE company = ?",
        (dossier_store.resolve_company(conn, company),),
    ).fetchone()


def level_of(conn: sqlite3.Connection, company: str) -> str:
    row = load(conn, company)
    return str(row["level"]) if row is not None else R.LEVEL_UNKNOWN


def levels(conn: sqlite3.Connection) -> dict[str, str]:
    """Уровень по каждому работодателю разом: список компаний иначе делал бы
    отдельный запрос со сведением названия на каждую строку."""
    ensure_schema(conn)
    return {
        str(row["company"]): str(row["level"])
        for row in conn.execute("SELECT company, level FROM company_score")
    }


def row_lines(row: sqlite3.Row | None, limit: int = 3) -> list[str]:
    """Строки карточки из сохранённой оценки — без пересчёта."""
    if row is None:
        return []
    level = str(row["level"] or R.LEVEL_UNKNOWN)
    if level == R.LEVEL_UNKNOWN:
        return []
    try:
        evidence = json.loads(row["evidence"] or "[]")
    except ValueError:
        evidence = []
    head = "Оценка работодателя: {} · осей с данными {} из {}".format(
        R.LEVEL_RU.get(level, level), int(row["covered"] or 0), len(R.AXES)
    )
    veto = str(row["veto"] or "")
    if veto:
        head += " · {}".format(R.VETO_RU.get(veto, veto))
    red = [e for e in evidence if e.get("polarity") == "red" and e.get("text")]
    red.sort(key=lambda e: float(e.get("weight", 0)) * float(e.get("trust", 0)), reverse=True)
    # По одной строке на код: одна и та же жалоба с трёх площадок — одна новость.
    seen: set[str] = set()
    out: list[str] = []
    for item in red:
        code = str(item.get("code") or "")
        if code in seen:
            continue
        seen.add(code)
        out.append("— {}".format(item["text"]))
        if len(out) >= limit:
            break
    return [head] + out


def refresh(conn: sqlite3.Connection, companies: list[str]) -> int:
    """Пересчитывает оценки списка работодателей и сохраняет их."""
    ensure_schema(conn)
    done = 0
    for company in dict.fromkeys(c for c in companies if c):
        save(conn, company_score.evaluate(conn, company))
        done += 1
    log.info("оценки работодателей пересчитаны: компаний %s", done)
    return done


def coverage(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """(оценок всего, красных, без данных) — для страницы компаний и лога."""
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN level = 'red' THEN 1 ELSE 0 END) AS red,
            SUM(CASE WHEN level = 'unknown' THEN 1 ELSE 0 END) AS unknown
        FROM company_score
        """
    ).fetchone()
    return int(row["total"] or 0), int(row["red"] or 0), int(row["unknown"] or 0)


__all__ = (
    "coverage",
    "ensure_schema",
    "level_of",
    "levels",
    "load",
    "refresh",
    "row_lines",
    "save",
)
