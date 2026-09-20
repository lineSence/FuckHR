"""Цели: компании, выбранные владельцем, а не найденные сбором (ADR-025).

Сбор отвечает на вопрос «где есть подходящая вакансия». Цель отвечает на
другой: «что вообще известно про эту контору». Компанию называет владелец —
руками, ссылкой на hh.ru или по ИНН, — и она не обязана встречаться в прогоне.
Это прямое следствие [CORE-018]: смысл работы — досье на компании и выход на
нанимающих менеджеров, а массовый сбор лишь один из способов их найти.

Решения владельца 20.09.2026 (B-14):

- вход всеми тремя способами: название, ссылка, ИНН;
- одноимённые конторы не угадываются — показываются кандидаты с hh.ru;
- сбор запускается руками, а не прогоном: он стоит времени [CORE-016]. Одной
  кнопкой, а не тремя — поправка того же дня, см. ADR-025;
- слежение за новыми вакансиями включается у каждой цели отдельно;
- целей около десятка;
- вакансии цели показываются **все**, порог профиля к ним не применяется:
  цель выбрана руками, и «не подходит по скору» здесь не повод прятать;
- письмо по-прежнему пишется от вакансии, а не от компании [OUT-004].

Здесь только хранилище и правила. Сеть — в `hh_employer.py`, шаги —
в `target_scan.py`, страница — в `ui_targets.py`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger(__name__)

# Оценка владельца: целей около десятка. Больше не запрещено, но каждая цель
# со слежением — это ещё один обход hh.ru в сутки, и сказать об этом надо.
EXPECTED_MAX = 10

# Слежение — раз в сутки. Чаще бессмысленно: вакансии не появляются ежечасно,
# а каждый лишний обход это риск капчи [CORE-014].
WATCH_HOURS = 24

# Порядок и фильтр списка. Из браузера приходит только имя, и оно ищется здесь:
# в SQL не попадает ни один символ владельца, чужое имя молча заменяется
# умолчанием [CORE-017]. Обратной сортировки нет намеренно [CORE-025].
SORTS = {
    "added": "t.added_at DESC, t.id DESC",
    "company": "t.company COLLATE NOCASE",
    "fresh": "fresh_count DESC, total DESC, t.company COLLATE NOCASE",
    "vacancies": "total DESC, t.company COLLATE NOCASE",
    "scan": "COALESCE(t.last_scan_at, '') DESC, t.company COLLATE NOCASE",
}

FILTERS = {
    "all": "1 = 1",
    "watch": "t.watch = 1",
    "fresh": "fresh_count > 0",
    "unresolved": "COALESCE(t.employer_id, '') = ''",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_targets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company       TEXT NOT NULL UNIQUE,
    employer_id   TEXT,
    inn           TEXT,
    site          TEXT,
    area          TEXT,
    source        TEXT NOT NULL DEFAULT 'manual',
    watch         INTEGER NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL,
    last_scan_at  TEXT,
    last_watch_at TEXT
);

CREATE TABLE IF NOT EXISTS target_vacancies (
    target_id  INTEGER NOT NULL,
    key        TEXT NOT NULL,
    found_at   TEXT NOT NULL,
    fresh      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (target_id, key)
);

CREATE INDEX IF NOT EXISTS idx_target_vacancies_target
    ON target_vacancies (target_id, found_at DESC);
"""


@dataclass(frozen=True)
class Target:
    id: int
    company: str
    employer_id: str
    inn: str
    site: str
    area: str
    source: str
    watch: bool
    added_at: str
    last_scan_at: str
    last_watch_at: str

    @property
    def resolved(self) -> bool:
        """Есть id работодателя на hh.ru — значит, вакансии можно собрать."""
        return bool(self.employer_id)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _row(row: sqlite3.Row) -> Target:
    return Target(
        id=int(row["id"]),
        company=row["company"],
        employer_id=row["employer_id"] or "",
        inn=row["inn"] or "",
        site=row["site"] or "",
        area=row["area"] or "",
        source=row["source"] or "manual",
        watch=bool(row["watch"]),
        added_at=row["added_at"] or "",
        last_scan_at=row["last_scan_at"] or "",
        last_watch_at=row["last_watch_at"] or "",
    )


def add(
    conn: sqlite3.Connection,
    company: str,
    employer_id: str = "",
    inn: str = "",
    site: str = "",
    area: str = "",
    source: str = "manual",
) -> int:
    """Новая цель или обновление известной. Возвращает id.

    Повторное добавление не плодит дубль и не стирает уже известное: у цели,
    добавленной по ИНН, позже появляется employer_id, и наоборот.
    """
    ensure_schema(conn)
    company = (company or "").strip()
    if not company:
        raise ValueError("у цели должно быть название")
    row = conn.execute(
        "SELECT * FROM company_targets WHERE company = ?", (company,)
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO company_targets
                (company, employer_id, inn, site, area, source, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (company, employer_id, inn, site, area, source, db.utcnow()),
        )
        conn.commit()
        log.info("цель добавлена: %s (%s)", company, source)
        return int(cur.lastrowid or 0)
    conn.execute(
        """
        UPDATE company_targets
           SET employer_id = COALESCE(NULLIF(?, ''), employer_id),
               inn         = COALESCE(NULLIF(?, ''), inn),
               site        = COALESCE(NULLIF(?, ''), site),
               area        = COALESCE(NULLIF(?, ''), area)
         WHERE id = ?
        """,
        (employer_id, inn, site, area, row["id"]),
    )
    conn.commit()
    return int(row["id"])


def all_targets(conn: sqlite3.Connection) -> list[Target]:
    ensure_schema(conn)
    cur = conn.execute("SELECT * FROM company_targets ORDER BY added_at DESC, id DESC")
    return [_row(row) for row in cur.fetchall()]


def listing(
    conn: sqlite3.Connection, sort: str = "added", only: str = "all"
) -> list[tuple[Target, int, int]]:
    """Цели со счётчиками: (цель, сколько вакансий, сколько новых).

    Порядок и фильтр берутся из словарей выше по имени. Считать вакансии
    отдельным запросом на каждую цель незачем: их десяток, а запрос и так уже
    идёт по обеим таблицам.
    """
    ensure_schema(conn)
    order = SORTS.get(sort, SORTS["added"])
    where = FILTERS.get(only, FILTERS["all"])
    cur = conn.execute(
        """
        SELECT t.*,
               COUNT(tv.key) AS total,
               COALESCE(SUM(tv.fresh), 0) AS fresh_count
          FROM company_targets t
          LEFT JOIN target_vacancies tv ON tv.target_id = t.id
         GROUP BY t.id
        HAVING {where}
         ORDER BY {order}
        """.format(where=where, order=order)
    )
    return [
        (_row(row), int(row["total"] or 0), int(row["fresh_count"] or 0))
        for row in cur.fetchall()
    ]


def get(conn: sqlite3.Connection, target_id: int) -> Target | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM company_targets WHERE id = ?", (int(target_id),)
    ).fetchone()
    return _row(row) if row is not None else None


def by_company(conn: sqlite3.Connection, company: str) -> Target | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM company_targets WHERE company = ?", ((company or "").strip(),)
    ).fetchone()
    return _row(row) if row is not None else None


def remove(conn: sqlite3.Connection, target_id: int) -> None:
    """Цель убирается из списка. Досье, отзывы и вакансии остаются в базе."""
    ensure_schema(conn)
    conn.execute("DELETE FROM target_vacancies WHERE target_id = ?", (int(target_id),))
    conn.execute("DELETE FROM company_targets WHERE id = ?", (int(target_id),))
    conn.commit()


def set_watch(conn: sqlite3.Connection, target_id: int, on: bool) -> None:
    ensure_schema(conn)
    conn.execute(
        "UPDATE company_targets SET watch = ? WHERE id = ?",
        (1 if on else 0, int(target_id)),
    )
    conn.commit()


def link(conn: sqlite3.Connection, target_id: int, keys: list[str]) -> int:
    """Связи «цель ↔ вакансия». Новой считается та, которой раньше не было.

    Флаг `fresh` живёт на связи, а не на вакансии: одна и та же вакансия может
    быть давно известна сбору и при этом впервые увидена у цели.
    """
    ensure_schema(conn)
    now = db.utcnow()
    known = {
        row[0]
        for row in conn.execute(
            "SELECT key FROM target_vacancies WHERE target_id = ?", (int(target_id),)
        ).fetchall()
    }
    fresh = [key for key in keys if key not in known]
    conn.executemany(
        """
        INSERT INTO target_vacancies (target_id, key, found_at, fresh)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(target_id, key) DO UPDATE SET fresh = 0
        """,
        [(int(target_id), key, now, 1 if key in set(fresh) else 0) for key in keys],
    )
    conn.commit()
    return len(fresh)


def vacancies(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    """Все вакансии цели: порог профиля здесь не применяется (решение владельца)."""
    ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT v.*, tv.fresh AS fresh, tv.found_at AS found_at
          FROM target_vacancies tv
          JOIN vacancies v ON v.key = tv.key
         WHERE tv.target_id = ?
         ORDER BY tv.fresh DESC, v.published_at DESC
        """,
        (int(target_id),),
    )
    return list(cur.fetchall())


def counts(conn: sqlite3.Connection, target_id: int) -> tuple[int, int]:
    """(сколько вакансий у цели, сколько из них новые с прошлого взгляда)."""
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT COUNT(*) AS total, COALESCE(SUM(fresh), 0) AS fresh
          FROM target_vacancies WHERE target_id = ?
        """,
        (int(target_id),),
    ).fetchone()
    return int(row["total"] or 0), int(row["fresh"] or 0)


def seen(conn: sqlite3.Connection, target_id: int) -> None:
    """Владелец открыл цель — новые перестают быть новыми."""
    ensure_schema(conn)
    conn.execute(
        "UPDATE target_vacancies SET fresh = 0 WHERE target_id = ?", (int(target_id),)
    )
    conn.commit()


def mark_scan(conn: sqlite3.Connection, target_id: int, watch: bool = False) -> None:
    ensure_schema(conn)
    now = db.utcnow()
    if watch:
        conn.execute(
            "UPDATE company_targets SET last_scan_at = ?, last_watch_at = ? WHERE id = ?",
            (now, now, int(target_id)),
        )
    else:
        conn.execute(
            "UPDATE company_targets SET last_scan_at = ? WHERE id = ?",
            (now, int(target_id)),
        )
    conn.commit()


def due(conn: sqlite3.Connection, hours: int = WATCH_HOURS) -> list[Target]:
    """Цели, за которыми следим и которых не смотрели дольше `hours`."""
    edge = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    out = [
        target
        for target in all_targets(conn)
        if target.watch and target.resolved and (target.last_watch_at or "") < edge
    ]
    if len(out) > EXPECTED_MAX:
        log.warning(
            "целей со слежением %s: каждая это отдельный обход hh.ru в прогоне",
            len(out),
        )
    return out


__all__ = (
    "EXPECTED_MAX",
    "FILTERS",
    "SCHEMA",
    "SORTS",
    "Target",
    "WATCH_HOURS",
    "add",
    "all_targets",
    "by_company",
    "counts",
    "due",
    "ensure_schema",
    "get",
    "link",
    "listing",
    "mark_scan",
    "remove",
    "seen",
    "set_watch",
    "vacancies",
)
