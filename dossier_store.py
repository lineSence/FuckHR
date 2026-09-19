"""Хранение досье в SQLite: схема, запись, чтение, сводки для страниц.

Вынесено из dossier.py по [CORE-024]: здесь нет ни сети, ни модели, ни разбора
текстов — только таблицы и запросы.

Досье принимается по утиному типу, а не импортом dossier.Dossier: иначе
получится цикл импортов.

Ключ — название компании, а пишут его везде по-разному, поэтому перед
чтением и записью имя сводится к уже известному (company_key).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import company_key
import fake_rules
import fake_store
from dossier_rules import RISK_RU, STALE_AFTER_DAYS

if TYPE_CHECKING:
    from dossier import Dossier

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_dossier (
    company       TEXT PRIMARY KEY,
    domain        TEXT,
    site_url      TEXT,
    review_count  INTEGER NOT NULL DEFAULT 0,
    avg_rating    REAL,
    avg_rating_all REAL,
    fake_level    TEXT NOT NULL DEFAULT 'none',
    fake_signs    TEXT NOT NULL DEFAULT '[]',
    risk          TEXT NOT NULL DEFAULT 'unknown',
    patterns      TEXT NOT NULL DEFAULT '[]',
    red_flags     TEXT NOT NULL DEFAULT '[]',
    green_flags   TEXT NOT NULL DEFAULT '[]',
    summary       TEXT,
    summary_by    TEXT,
    sources       TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company    TEXT NOT NULL,
    site       TEXT,
    url        TEXT NOT NULL,
    title      TEXT,
    snippet    TEXT,
    body       TEXT,
    rating     REAL,
    polarity   TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    UNIQUE (company, url)
);

CREATE INDEX IF NOT EXISTS idx_reviews_company ON company_reviews(company);
CREATE INDEX IF NOT EXISTS idx_dossier_risk ON company_dossier(risk);
"""


DOSSIER_COLUMNS = (
    ("body", "company_reviews", "TEXT"),
    ("avg_rating_all", "company_dossier", "REAL"),
    ("fake_level", "company_dossier", "TEXT NOT NULL DEFAULT 'none'"),
    ("fake_signs", "company_dossier", "TEXT NOT NULL DEFAULT '[]'"),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    fake_store.ensure_schema(conn)
    # Миграции для баз, созданных раньше: недостающая колонка не имеет права
    # ронять прогон.
    for column, table, kind in DOSSIER_COLUMNS:
        columns = {row[1] for row in conn.execute("PRAGMA table_info({})".format(table))}
        if column not in columns:
            log.info("добавляю колонку %s в %s", column, table)
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, kind))
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def known_companies(conn: sqlite3.Connection) -> list[str]:
    ensure_schema(conn)
    return [str(row[0]) for row in conn.execute("SELECT company FROM company_dossier")]


def resolve_company(conn: sqlite3.Connection, company: str) -> str:
    """Каноническое название: «ООО «Ромашка»» и «Ромашка» — одна контора.

    Если такой работодатель ещё не встречался, возвращается исходное имя:
    первое написание и становится каноном.
    """
    company = (company or "").strip()
    if not company:
        return ""
    row = conn.execute(
        "SELECT company FROM company_dossier WHERE company = ?", (company,)
    ).fetchone()
    if row is not None:
        return company
    match = company_key.best_match(company, known_companies(conn))
    if match:
        log.info("%r считаю тем же работодателем, что и %r", company, match)
        return match
    return company


def store(conn: sqlite3.Connection, dossier: "Dossier") -> None:
    """Перезаписывает досье и добавляет новые отзывы."""
    ensure_schema(conn)
    company = resolve_company(conn, dossier.company)
    conn.execute(
        """
        INSERT INTO company_dossier (
            company, domain, site_url, review_count, avg_rating, avg_rating_all,
            fake_level, fake_signs, risk,
            patterns, red_flags, green_flags, summary, summary_by, sources, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company) DO UPDATE SET
            domain = excluded.domain,
            site_url = excluded.site_url,
            review_count = excluded.review_count,
            avg_rating = excluded.avg_rating,
            avg_rating_all = excluded.avg_rating_all,
            fake_level = excluded.fake_level,
            fake_signs = excluded.fake_signs,
            risk = excluded.risk,
            patterns = excluded.patterns,
            red_flags = excluded.red_flags,
            green_flags = excluded.green_flags,
            summary = excluded.summary,
            summary_by = excluded.summary_by,
            sources = excluded.sources,
            updated_at = excluded.updated_at
        """,
        (
            company,
            dossier.domain,
            dossier.site_url,
            dossier.review_count,
            dossier.avg_rating,
            getattr(dossier, "avg_rating_all", None),
            getattr(getattr(dossier, "mark", None), "level", fake_rules.MARK_NONE),
            json.dumps(
                [
                    {"code": sign.code, "text": sign.text}
                    for sign in getattr(getattr(dossier, "mark", None), "signs", ())
                ],
                ensure_ascii=False,
            ),
            dossier.risk,
            json.dumps(
                [
                    {
                        "code": p.code,
                        "label": p.label,
                        "polarity": p.polarity,
                        "hits": p.hits,
                        "quotes": list(p.quotes),
                    }
                    for p in dossier.patterns
                ],
                ensure_ascii=False,
            ),
            json.dumps([p.label for p in dossier.red_flags], ensure_ascii=False),
            json.dumps([p.label for p in dossier.green_flags], ensure_ascii=False),
            dossier.summary,
            dossier.summary_by,
            json.dumps(list(dossier.sources), ensure_ascii=False),
            _now(),
        ),
    )
    for review in dossier.reviews:
        # Текст страницы мог появиться позже сниппета: обновляем существующую
        # строку, иначе прочитанный отзыв так и останется заголовком.
        conn.execute(
            """
            INSERT INTO company_reviews
                (company, site, url, title, snippet, body, rating, polarity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company, url) DO UPDATE SET
                title = excluded.title,
                snippet = excluded.snippet,
                body = COALESCE(NULLIF(excluded.body, ''), company_reviews.body),
                rating = excluded.rating,
                polarity = excluded.polarity
            """,
            (
                company,
                review.site,
                review.url,
                review.title,
                review.snippet,
                review.body,
                review.rating,
                review.polarity,
                _now(),
            ),
        )
    fake_store.store(
        conn, company, getattr(dossier, "items", ()), getattr(dossier, "verdicts", ())
    )
    conn.commit()


def load(conn: sqlite3.Connection, company: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM company_dossier WHERE company = ?",
        (resolve_company(conn, company),),
    ).fetchone()


def load_reviews(conn: sqlite3.Connection, company: str) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM company_reviews WHERE company = ? ORDER BY polarity, id",
        (resolve_company(conn, company),),
    ).fetchall()


def is_fresh(row: sqlite3.Row | None, days: int = STALE_AFTER_DAYS) -> bool:
    """Свежее досье не собирается заново: это главная экономия запросов к поиску."""
    if row is None:
        return False
    try:
        updated = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated < timedelta(days=days)


def row_to_lines(row: sqlite3.Row) -> list[str]:
    """Строки карточки из сохранённого досье — без повторного поиска."""
    try:
        red = json.loads(row["red_flags"] or "[]")
        green = json.loads(row["green_flags"] or "[]")
    except ValueError:
        red, green = [], []
    head = "Работодатель: {} · отзывов {}".format(
        RISK_RU.get(row["risk"], row["risk"]), row["review_count"]
    )
    if row["avg_rating"] is not None:
        head += " · оценка {:.1f}".format(float(row["avg_rating"]))
    lines = [head]
    if _column(row, "fake_level") == fake_rules.MARK_FAKE:
        lines.append(
            "— {}: оценке площадки верить нельзя".format(fake_rules.MARK_FLAG_LABEL)
        )
    lines += ["— {}".format(label) for label in red[:3]]
    lines += ["+ {}".format(label) for label in green[:2]]
    return lines


def _column(row: sqlite3.Row, name: str) -> str:
    """Значение колонки, которой может не быть в старой базе."""
    try:
        return str(row[name] or "")
    except (IndexError, KeyError):
        return ""


def coverage(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """(досье всего, с красными флагами, без отзывов) — для страницы компаний."""
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN risk = 'red' THEN 1 ELSE 0 END) AS red,
            SUM(CASE WHEN review_count = 0 THEN 1 ELSE 0 END) AS empty
        FROM company_dossier
        """
    ).fetchone()
    return int(row["total"] or 0), int(row["red"] or 0), int(row["empty"] or 0)


def list_dossiers(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        """
        SELECT * FROM company_dossier
        ORDER BY CASE risk
                    WHEN 'red' THEN 0
                    WHEN 'yellow' THEN 1
                    WHEN 'green' THEN 2
                    ELSE 3
                 END,
                 review_count DESC,
                 company
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
