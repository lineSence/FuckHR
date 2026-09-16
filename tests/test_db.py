"""История публикаций — самая дорогая часть базы: ошибка здесь видна только через месяцы."""

from __future__ import annotations

import db


def test_upsert_reports_new_only_once(conn, make_vacancy):
    vacancy = make_vacancy()
    assert db.upsert_vacancy(conn, vacancy, 90.0, ["стек"]) is True
    assert db.upsert_vacancy(conn, vacancy, 91.0, ["стек"]) is False


def test_missing_vacancy_gets_inactive_snapshot(conn, make_vacancy):
    vacancy = make_vacancy()
    db.add_snapshot(conn, vacancy)
    db.upsert_vacancy(conn, vacancy, 90.0, [])

    # Вакансия в выдаче — ничего не закрываем.
    assert db.deactivate_missing(conn, [vacancy.key]) == []
    assert db.last_snapshot_active(conn, vacancy.key) is True

    # Исчезла — фиксируем факт одним слепком.
    assert db.deactivate_missing(conn, []) == [vacancy.key]
    assert db.last_snapshot_active(conn, vacancy.key) is False

    # Повторные прогоны не должны плодить одинаковые слепки каждые 12 часов.
    assert db.deactivate_missing(conn, []) == []
    total = conn.execute("SELECT COUNT(*) AS n FROM vacancy_snapshots").fetchone()["n"]
    assert total == 2


def test_republish_count_uses_dates(conn, make_vacancy):
    db.add_snapshot(conn, make_vacancy(published_at="2026-07-01"))
    db.add_snapshot(conn, make_vacancy(published_at="2026-09-01"))
    key = make_vacancy().key
    assert db.republish_count(conn, key) == 2


def test_republish_count_falls_back_to_external_id(conn, make_vacancy):
    """Пустой published_at больше не обнуляет сигнал: hh.ru меняет id при перепубликации."""
    db.add_snapshot(conn, make_vacancy(external_id="100", published_at=None))
    db.add_snapshot(conn, make_vacancy(external_id="200", published_at=None))
    assert db.republish_count(conn, make_vacancy().key) == 2


def test_republish_count_counts_reopen_cycles(conn, make_vacancy):
    vacancy = make_vacancy(external_id="100", published_at=None)
    db.add_snapshot(conn, vacancy)
    db.add_snapshot(conn, vacancy, is_active=False)
    db.add_snapshot(conn, vacancy)
    assert db.republish_count(conn, vacancy.key) == 2


def test_published_at_coverage(conn, make_vacancy):
    db.add_snapshot(conn, make_vacancy(published_at="2026-09-01"))
    db.add_snapshot(conn, make_vacancy(external_id="2", published_at=None))
    assert db.published_at_coverage(conn) == (1, 2)


def test_touch_seen_keeps_rejected_vacancies_known(conn, make_vacancy):
    vacancy = make_vacancy()
    db.upsert_vacancy(conn, vacancy, 90.0, [])
    db.touch_seen(conn, [vacancy.key])
    assert vacancy.key in db.known_keys(conn)
