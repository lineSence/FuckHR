"""Отчёт по воротам на карточки считает цену, а не обещает её (B-15, п. 4)."""

from __future__ import annotations

from types import SimpleNamespace

import conditions
import db
import gate_report
import profiles
import score
from hh import Vacancy


def loaded(min_score: float = 50.0) -> object:
    return profiles.Loaded(
        id="основной",
        profile=score.Profile(
            queries=[{"text": "оператор 1с"}],
            skills=["1с", "поддержка"],
            stop_words=[],
            areas=[113],
            min_score=min_score,
        ),
    )


def vacancy(number: int, title: str) -> Vacancy:
    return Vacancy(
        source="hh.ru",
        external_id=str(number),
        url="https://hh.ru/vacancy/{}".format(number),
        title=title,
        company="АКМЕ",
        description="1С, поддержка пользователей",
        salary_from=150000,
        currency="RUR",
    )


def test_отчёт_показывает_потери(tmp_path) -> None:
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    conditions.ensure_schema(conn)
    близкая = vacancy(1, "Оператор 1С поддержка")
    далёкая = vacancy(2, "Повар горячего цеха")
    for item in (близкая, далёкая):
        db.upsert_vacancy(conn, item, 0.0, [])
    conditions.store(
        conn,
        далёкая.key,
        [SimpleNamespace(field="salary", value="150000", quote="оклад 150 000")],
    )

    rows = gate_report.report(conn, [loaded(min_score=90.0)], deltas=[0.0, 5.0])
    by_delta = {row.delta: row for row in rows}
    # Дельта 0 — ворота выключены: не пропускается ничего.
    assert by_delta[0.0].skipped == 0
    # При высоком пороге и дельте 5 далёкая вакансия не качалась бы, и вместе
    # с ней потерялись бы её условия.
    assert by_delta[5.0].skipped >= 1
    assert by_delta[5.0].lost_conditions >= 1
    assert by_delta[5.0].total == 2
    assert "дельта" in gate_report.render(rows)
