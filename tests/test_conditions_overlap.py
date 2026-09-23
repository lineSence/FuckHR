"""Замер дублирования: условие от модели против структурного поля hh.

Сети и модели нет — в базу кладутся вакансия и условия руками. Проверяется то,
ради чего замер и делался: покрытие считается по полю источника, а согласие —
по смыслу, а не по буквальному совпадению строк.
"""

from __future__ import annotations

import json
import sqlite3

import conditions
import conditions_overlap as overlap
import db


def _conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conditions.ensure_schema(conn)
    return conn


def _vacancy(conn: sqlite3.Connection, key: str, **fields) -> None:
    columns = {
        "key": key,
        "source": "hh",
        "external_id": key,
        "url": "https://hh.ru/vacancy/1",
        "title": "Python-разработчик",
        "first_seen_at": "2026-01-01",
        "last_seen_at": "2026-01-01",
    }
    columns.update(fields)
    conn.execute(
        "INSERT INTO vacancies ({}) VALUES ({})".format(
            ", ".join(columns), ", ".join("?" * len(columns))
        ),
        tuple(columns.values()),
    )
    conn.commit()


def _condition(conn: sqlite3.Connection, key: str, field: str, value: str) -> None:
    conn.execute(
        "INSERT INTO vacancy_conditions (key, field, value, quote)"
        " VALUES (?, ?, ?, 'цитата')",
        (key, field, value),
    )
    conn.commit()


def test_условие_совпало_с_полем_источника() -> None:
    conn = _conn()
    _vacancy(
        conn,
        "hh:1",
        schedule="Удаленная работа",
        salary_from=200000,
        experience="between3And6",
        skills=json.dumps(["Python", "PostgreSQL"], ensure_ascii=False),
    )
    _condition(conn, "hh:1", "format", "удалёнка")
    _condition(conn, "hh:1", "salary", "от 200 000 рублей")
    _condition(conn, "hh:1", "grade", "middle+, от 3 лет")
    _condition(conn, "hh:1", "stack", "python, postgresql, docker")

    stats, with_conditions, total = overlap.report(conn)

    assert (with_conditions, total) == (1, 1)
    for name in ("format", "salary", "grade", "stack"):
        assert stats[name].covered == 1, name
        assert stats[name].agreed == 1, name


def test_модель_спорит_с_полем_источника() -> None:
    conn = _conn()
    _vacancy(conn, "hh:2", schedule="Полный день", salary_from=100000)
    _condition(conn, "hh:2", "format", "полная удалёнка")
    _condition(conn, "hh:2", "salary", "до 300 000")

    stats, _, _ = overlap.report(conn)

    assert stats["format"].disagreed == 1
    assert stats["salary"].disagreed == 1
    assert stats["format"].examples[0][0] == "hh:2"


def test_поля_без_структурного_ответа_остаются_за_моделью() -> None:
    conn = _conn()
    _vacancy(conn, "hh:3", schedule="Полный день")
    _condition(conn, "hh:3", "process", "четыре этапа собеседования")
    _condition(conn, "hh:3", "office", "Белорусская, опенспейс")

    stats, _, _ = overlap.report(conn)

    assert stats["process"].covered == 0
    assert stats["office"].covered == 0
    assert "источник" in overlap.render(stats)
