"""Где видели вакансию: разметка площадок и метрика уникальности."""

from __future__ import annotations

import sqlite3

import source_store


def base() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE vacancies (key TEXT PRIMARY KEY)")
    source_store.ensure_schema(conn)
    return conn


def test_remember_is_idempotent():
    conn = base()
    source_store.remember_many(
        conn,
        [
            ("k1", "hh.ru", "1", "https://hh.ru/vacancy/1"),
            ("k1", "hh.ru", "1", "https://hh.ru/vacancy/1"),
        ],
    )
    assert source_store.sources_of(conn, "k1") == ["hh.ru"]


def test_unique_counts_show_what_only_one_site_has():
    """Смысл всей разметки: какая площадка приносит то, чего нет больше нигде."""
    conn = base()
    source_store.remember_many(
        conn,
        [
            ("k1", "hh.ru", "1", ""),
            ("k1", "superjob", "9", ""),
            ("k2", "trudvsem", "2", ""),
        ],
    )
    assert source_store.counts(conn) == {"hh.ru": 1, "superjob": 1, "trudvsem": 1}
    assert source_store.unique_counts(conn) == {"trudvsem": 1}


def test_drop_orphans_follows_vacancies():
    conn = base()
    conn.execute("INSERT INTO vacancies (key) VALUES ('k1')")
    source_store.remember_many(
        conn, [("k1", "hh.ru", "1", ""), ("gone", "rabota", "2", "")]
    )
    assert source_store.drop_orphans(conn) == 1
    assert source_store.sources_of(conn, "gone") == []
