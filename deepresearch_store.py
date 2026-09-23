"""Хранилище глубокого ресёрча: отчёты, находки, кэш страниц.

Отделено от логики по [CORE-024]. Здесь нет ни сети, ни разбора текста —
только SQLite.

Находка хранится вместе с источником и датой: без ссылки это слух, а не факт
[HRD-003]. Ключ находки — компания, тема и адрес страницы: повторный ресёрч
обновляет цитату, а не плодит дубли.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import deepresearch_rules as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS deep_research (
    company TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    queries INTEGER DEFAULT 0,
    pages INTEGER DEFAULT 0,
    blocked TEXT DEFAULT '',
    inn TEXT DEFAULT '',
    registered_at TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS deep_findings (
    company TEXT NOT NULL,
    code TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT DEFAULT '',
    quote TEXT DEFAULT '',
    trust REAL DEFAULT 0.5,
    observed_at TEXT,
    PRIMARY KEY (company, code, url)
);
CREATE TABLE IF NOT EXISTS deep_pages (
    url TEXT PRIMARY KEY,
    text TEXT,
    fetched_at TEXT
);
"""


@dataclass(frozen=True)
class Finding:
    """Одна находка: тема, источник, цитата."""

    code: str
    url: str
    domain: str
    quote: str
    trust: float
    observed_at: str

    @property
    def title(self) -> str:
        topic = R.TOPIC_BY_CODE.get(self.code)
        return topic.title if topic else self.code


@dataclass(frozen=True)
class Report:
    """Итог ресёрча по компании."""

    company: str
    status: str = "нет"
    started_at: str = ""
    finished_at: str = ""
    queries: int = 0
    pages: int = 0
    blocked: tuple[str, ...] = ()
    inn: str = ""
    registered_at: str = ""
    note: str = ""
    findings: tuple[Finding, ...] = ()

    @property
    def fresh_until(self) -> str:
        return self.finished_at


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# —— кэш страниц ——


def page_get(conn: sqlite3.Connection, url: str, days: int) -> str | None:
    row = conn.execute(
        "SELECT text, fetched_at FROM deep_pages WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return None
    try:
        fetched = datetime.fromisoformat(str(row[1]))
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched > timedelta(days=max(0, days)):
        return None
    return str(row[0] or "")


def page_put(conn: sqlite3.Connection, url: str, text: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO deep_pages (url, text, fetched_at) VALUES (?, ?, ?)",
        (url, text, _now()),
    )
    conn.commit()


# —— отчёты ——


def start(conn: sqlite3.Connection, company: str) -> None:
    conn.execute(
        "INSERT INTO deep_research (company, started_at, status)"
        " VALUES (?, ?, 'идёт')"
        " ON CONFLICT(company) DO UPDATE SET started_at = excluded.started_at,"
        " status = 'идёт', finished_at = NULL",
        (company, _now()),
    )
    conn.commit()


def save(conn: sqlite3.Connection, report: Report) -> None:
    conn.execute(
        "INSERT INTO deep_research (company, started_at, finished_at, status, queries,"
        " pages, blocked, inn, registered_at, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(company) DO UPDATE SET finished_at = excluded.finished_at,"
        " status = excluded.status, queries = excluded.queries, pages = excluded.pages,"
        " blocked = excluded.blocked, inn = excluded.inn,"
        " registered_at = excluded.registered_at, note = excluded.note",
        (
            report.company,
            report.started_at or _now(),
            report.finished_at or _now(),
            report.status,
            report.queries,
            report.pages,
            ", ".join(report.blocked),
            report.inn,
            report.registered_at,
            report.note,
        ),
    )
    for item in report.findings:
        conn.execute(
            "INSERT INTO deep_findings (company, code, url, domain, quote, trust,"
            " observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(company, code, url) DO UPDATE SET quote = excluded.quote,"
            " trust = excluded.trust, observed_at = excluded.observed_at",
            (
                report.company,
                item.code,
                item.url,
                item.domain,
                item.quote,
                item.trust,
                item.observed_at,
            ),
        )
    conn.commit()


def load(conn: sqlite3.Connection, company: str) -> Report | None:
    company = (company or "").strip()
    if not company:
        return None
    row = conn.execute(
        "SELECT * FROM deep_research WHERE company = ?", (company,)
    ).fetchone()
    if row is None:
        return None
    findings = tuple(
        Finding(
            code=str(item["code"]),
            url=str(item["url"]),
            domain=str(item["domain"] or ""),
            quote=str(item["quote"] or ""),
            trust=float(item["trust"] or 0.0),
            observed_at=str(item["observed_at"] or ""),
        )
        for item in conn.execute(
            "SELECT * FROM deep_findings WHERE company = ? ORDER BY code, url",
            (company,),
        ).fetchall()
    )
    blocked = str(row["blocked"] or "")
    return Report(
        company=company,
        status=str(row["status"] or ""),
        started_at=str(row["started_at"] or ""),
        finished_at=str(row["finished_at"] or ""),
        queries=int(row["queries"] or 0),
        pages=int(row["pages"] or 0),
        blocked=tuple(part.strip() for part in blocked.split(",") if part.strip()),
        inn=str(row["inn"] or ""),
        registered_at=str(row["registered_at"] or ""),
        note=str(row["note"] or ""),
        findings=findings,
    )


def is_fresh(conn: sqlite3.Connection, company: str, ttl_days: int) -> bool:
    """Отчёт моложе TTL. Пересобирать такой незачем: источники так быстро
    не меняются, а запросы стоят денег [CORE-016]."""
    report = load(conn, company)
    if report is None or not report.finished_at:
        return False
    try:
        done = datetime.fromisoformat(report.finished_at)
    except ValueError:
        return False
    if done.tzinfo is None:
        done = done.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - done <= timedelta(days=max(0, ttl_days))


def evidence(conn: sqlite3.Connection, company: str) -> list[tuple]:
    """Находки в виде сырых кортежей для company_score.

    Возвращаются примитивы, а не Evidence: иначе хранилище пришлось бы
    импортировать в company_score и обратно — кольцо на ровном месте.
    Кортеж: код, ось, полярность, вес, доверие, текст, дата.
    """
    report = load(conn, company)
    if report is None:
        return []
    out: list[tuple] = []
    for item in report.findings:
        topic = R.TOPIC_BY_CODE.get(item.code)
        if topic is None or topic.lookup:
            continue
        text = "{}: {} ({})".format(topic.title, item.quote or "упоминание", item.domain)
        out.append(
            (
                topic.code.replace("_inn", ""),
                topic.axis,
                topic.polarity,
                topic.weight,
                item.trust,
                text,
                item.observed_at,
            )
        )
    return out


def companies(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT company FROM deep_research ORDER BY finished_at DESC"
        ).fetchall()
    ]


def drop(conn: sqlite3.Connection, company: str) -> None:
    conn.execute("DELETE FROM deep_findings WHERE company = ?", (company,))
    conn.execute("DELETE FROM deep_research WHERE company = ?", (company,))
    conn.commit()


__all__ = (
    "Finding",
    "Report",
    "companies",
    "drop",
    "ensure_schema",
    "evidence",
    "is_fresh",
    "load",
    "page_get",
    "page_put",
    "save",
    "start",
)
