"""Тесты очистки базы.

Проверяется ровно одно главное свойство: удаляется только выбранное. Ошибка
здесь стоит месяцы истории публикаций, которая повторным сбором не
восстанавливается.
"""

from __future__ import annotations

import sqlite3

import pytest

import db
import dossier
import maintenance


def test_цели_описаны_и_совпадают():
    codes = [code for code, _label, _warning, _danger in maintenance.describe()]
    assert codes == list(maintenance.TARGET_CODES)
    assert set(maintenance.EVERYTHING) == set(maintenance.TARGET_CODES)
    # Контакты, история и наблюдения по зарплатам сбором не восстанавливаются.
    assert set(maintenance.DANGEROUS) == {"contacts", "history", "market"}
    assert maintenance.target_label("history") == "История публикаций"


def test_счётчики_показывают_все_цели(conn, make_vacancy):
    counts = maintenance.counts(conn)
    assert set(counts) == set(maintenance.TARGET_CODES)
    assert counts["vacancies"] == 0

    vacancy = make_vacancy()
    db.upsert_vacancy(conn, vacancy, 80.0, ["вилка"])
    db.add_snapshot(conn, vacancy)
    counts = maintenance.counts(conn)
    assert counts["vacancies"] >= 1
    assert counts["history"] >= 1


def test_удаляется_только_выбранное(conn, make_vacancy):
    vacancy = make_vacancy()
    db.upsert_vacancy(conn, vacancy, 80.0, ["вилка"])
    db.add_snapshot(conn, vacancy)
    dossier.store(conn, dossier.analyze("ООО Ромашка", []))

    removed = maintenance.wipe(conn, ("vacancies",))
    assert removed["vacancies"] >= 1

    counts = maintenance.counts(conn)
    assert counts["vacancies"] == 0
    # История и досье не тронуты: их не выбирали.
    assert counts["history"] >= 1
    assert counts["dossier"] >= 1


def test_история_удаляется_только_отдельным_выбором(conn, make_vacancy):
    db.add_snapshot(conn, make_vacancy())
    assert maintenance.counts(conn)["history"] >= 1
    maintenance.wipe(conn, ("history",))
    assert maintenance.counts(conn)["history"] == 0


def test_досье_удаляется_вместе_с_отзывами(conn):
    досье = dossier.analyze(
        "ООО Ромашка",
        [
            dossier.Review(
                url="https://dreamjob.ru/c/1",
                snippet="Переработки",
                site="dreamjob.ru",
                polarity="negative",
            )
        ],
    )
    dossier.store(conn, досье)
    assert maintenance.counts(conn)["dossier"] == 2  # досье + отзыв

    maintenance.wipe(conn, ["dossier"])
    assert maintenance.counts(conn)["dossier"] == 0
    assert dossier.load(conn, "ООО Ромашка") is None


def test_неизвестная_цель_это_ошибка(conn):
    with pytest.raises(ValueError):
        maintenance.wipe(conn, ("всё_сразу",))


def test_сжатие_базы_работает_после_очистки(conn, make_vacancy):
    db.upsert_vacancy(conn, make_vacancy(), 80.0, ["вилка"])
    maintenance.wipe(conn, ("vacancies",))
    maintenance.vacuum(conn)
    assert maintenance.counts(conn)["vacancies"] == 0


def test_очистка_вакансий_забирает_производные_таблицы():
    """Таблица, которая считается из вакансий, не должна их переживать."""
    import detector
    import source_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    detector.ensure_schema(conn)
    source_store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO vacancies (key, external_id, title, company, url, source,"
        " first_seen_at, last_seen_at) VALUES"
        " ('k1', '1', 't', 'c', 'u', 'hh.ru', '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO vacancy_signals (key, created_at, flags, payload)"
        " VALUES ('k1', '2026-01-01', '[]', '{}')"
    )
    conn.execute(
        "INSERT INTO vacancy_profiles (key, profile_id, score, matched_at)"
        " VALUES ('k1', 'p1', 10.0, '2026-01-01')"
    )
    source_store.remember_many(conn, [("k1", "hh.ru", "1", "")])

    maintenance.wipe(conn, ["vacancies"])

    for table in ("vacancies", "vacancy_signals", "vacancy_profiles", "vacancy_sources"):
        assert conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0] == 0
