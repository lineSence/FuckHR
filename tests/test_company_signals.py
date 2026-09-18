"""Факты о работодателе считаются из слепков, а не из чьих-то слов.

Главное, что здесь проверяется: при короткой истории модуль говорит
«недостаточно данных», а не выдаёт осторожный намёк на плохое [HRD-004].
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import company_signals
import db


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    return conn


def _ts(days_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.isoformat(timespec="seconds")


def _vacancy(conn, key, company, salary_from=100000, salary_to=150000):
    conn.execute(
        """
        INSERT INTO vacancies (
            key, source, external_id, url, title, company,
            salary_from, salary_to, first_seen_at, last_seen_at
        ) VALUES (?, 'hh', ?, 'https://hh.ru/v', 'Инженер', ?, ?, ?, ?, ?)
        """,
        (key, key, company, salary_from, salary_to, _ts(200), _ts(0)),
    )
    conn.commit()


def _snapshot(conn, key, days_ago, published_at=None, salary_to=150000, active=True):
    conn.execute(
        """
        INSERT INTO vacancy_snapshots (
            key, seen_at, external_id, published_at, company_id,
            salary_from, salary_to, is_active
        ) VALUES (?, ?, ?, ?, NULL, 100000, ?, ?)
        """,
        (key, _ts(days_ago), key, published_at, salary_to, int(active)),
    )
    conn.commit()


def test_короткая_история_не_даёт_выводов():
    conn = _conn()
    _vacancy(conn, "hh:1", "ООО «Ромашка»")
    _snapshot(conn, "hh:1", days_ago=3)
    _snapshot(conn, "hh:1", days_ago=0)

    signals = company_signals.collect(conn, "Ромашка")
    assert signals.vacancies == 1
    assert not signals.enough
    assert "недостаточно" in " ".join(company_signals.facts(signals)) or "мало" in " ".join(
        company_signals.facts(signals)
    )


def test_разные_написания_считаются_одной_компанией():
    conn = _conn()
    _vacancy(conn, "hh:1", "ООО «Ромашка»")
    _vacancy(conn, "hh:2", "Ромашка, ООО")
    _vacancy(conn, "hh:3", "Лютик")

    rows = company_signals.vacancy_rows(conn, "Ромашка")
    assert {r["key"] for r in rows} == {"hh:1", "hh:2"}


def test_перепубликации_и_вечные_вакансии_попадают_в_факты():
    conn = _conn()
    _vacancy(conn, "hh:1", "ООО «Ромашка»")
    _vacancy(conn, "hh:2", "Ромашка")
    # Первая вакансия: три разные даты публикации и сто дней в выдаче.
    _snapshot(conn, "hh:1", days_ago=150, published_at="2026-04-01T10:00:00")
    _snapshot(conn, "hh:1", days_ago=90, published_at="2026-06-01T10:00:00")
    _snapshot(conn, "hh:1", days_ago=0, published_at="2026-08-01T10:00:00")
    # Вторая: история есть, но двигалась только вилка.
    _snapshot(conn, "hh:2", days_ago=60, published_at="2026-07-01T10:00:00", salary_to=150000)
    _snapshot(conn, "hh:2", days_ago=0, published_at="2026-07-01T10:00:00", salary_to=120000)

    signals = company_signals.collect(conn, "Ромашка")
    assert signals.enough
    assert signals.tracked == 2
    assert signals.republished == 1
    assert signals.long_running == 1
    assert signals.salary_moved == 1

    text = " ".join(company_signals.facts(signals))
    assert "публиковались заново" in text
    assert "висят в выдаче" in text
    assert "вилка менялась" in text


def test_без_вилки_считается_отдельно():
    conn = _conn()
    _vacancy(conn, "hh:1", "Ромашка", salary_from=None, salary_to=None)
    _vacancy(conn, "hh:2", "Ромашка")
    _snapshot(conn, "hh:1", days_ago=40)
    _snapshot(conn, "hh:1", days_ago=0)
    _snapshot(conn, "hh:2", days_ago=40)
    _snapshot(conn, "hh:2", days_ago=0)

    signals = company_signals.collect(conn, "Ромашка")
    assert signals.no_salary == 1
    assert "без вилки вообще: 1 из 2" in " ".join(company_signals.facts(signals))


def test_незнакомая_компания_не_падает():
    conn = _conn()
    assert company_signals.lines(conn, "Неизвестное ООО")
    assert company_signals.collect(conn, "").vacancies == 0
