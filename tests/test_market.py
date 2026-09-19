"""Тесты рыночной зарплаты и меток отклонения.

Проверяется то, что стоит дорого при ошибке: какие точки вообще попадают в
статистику, не подменяет ли один работодатель собой рынок и не появляется ли
метка на пустом месте [CORE-019].
"""

from __future__ import annotations

from datetime import date

import market
import market_company
import market_rules as R
import market_store
import score


def _observations(conn, count: int, point: int, company: str = "К", role: str = "python"):
    obs = [
        market.Observation(
            key="{}-{}".format(company, index),
            company=company,
            role=role,
            grade="middle",
            geo=R.MOSCOW,
            format=R.REMOTE,
            point=point,
            published_at="2026-09-01",
        )
        for index in range(count)
    ]
    market_store.record(conn, obs)
    return obs


def test_наблюдение_снимается_с_вакансии(make_vacancy):
    vacancy = make_vacancy(salary_from=200_000, salary_to=300_000, gross=True)
    observation = market.observe(vacancy)
    assert observation is not None
    assert observation.role == "python"
    assert observation.geo == R.MOSCOW
    assert observation.format == R.REMOTE
    # Вилка закрытая: точка — середина, уже за вычетом НДФЛ.
    assert observation.point == int((200_000 + 300_000) * R.NET_RATE / 2)
    assert observation.usable


def test_вилка_до_не_идёт_в_статистику(make_vacancy):
    only_to = market.observe(make_vacancy(salary_from=None, salary_to=400_000))
    only_from = market.observe(make_vacancy(salary_from=400_000, salary_to=None))
    assert only_to is not None and only_to.range_kind == market.TO_ONLY
    assert not only_to.usable
    assert only_from is not None and only_from.usable


def test_валюта_кроме_рубля_отбрасывается(make_vacancy):
    assert market.observe(make_vacancy(currency="USD")) is None


def test_перцентили_и_метки():
    values = [100, 200, 300, 400, 500, 600, 700, 800]
    stats = market.summarize("python|||", 0, values)
    assert stats.count == 8 and stats.enough
    assert stats.median == 450.0
    assert stats.p25 < stats.median < stats.p75

    assert market.marker(100, stats).label == R.BELOW
    assert market.marker(450, stats).label == R.IN_MARKET
    assert market.marker(900, stats).label == R.ABOVE
    assert market.marker(None, stats).label == R.NO_SALARY
    # Мало наблюдений — рынка нет, метки тоже.
    thin = market.summarize("python|||", 0, [100, 200])
    assert market.marker(100, thin).label == R.UNKNOWN


def test_одна_компания_не_становится_рынком():
    rows = [("Один", 100.0)] * 20 + [("Другая", 300.0)]
    values = market.cap_by_company(rows)
    # Потолок 20%: от одной конторы остаётся четыре точки, не двадцать.
    assert values.count(100.0) == int(21 * R.COMPANY_CAP)
    assert 300.0 in values


def test_каскад_огрубляет_срез(conn):
    # Точного среза (роль+грейд+гео+формат) не хватит, более грубого — да.
    _observations(conn, 10, 200_000, company="К")
    for index in range(10):
        market_store.record(
            conn,
            [
                market.Observation(
                    key="spb-{}".format(index),
                    company="Компания {}".format(index),
                    role="python",
                    grade="middle",
                    geo=R.SPB,
                    format=R.ONSITE,
                    point=180_000,
                    published_at="2026-09-01",
                )
            ],
        )
    market_store.recompute(conn, today=date(2026, 9, 20))
    stats = market_store.stats_for(conn, "python", "middle", R.SPB, R.ONSITE)
    assert stats is not None and stats.enough
    assert stats.level >= 0


def test_наблюдения_вне_окна_уходят(conn):
    market_store.record(
        conn,
        [
            market.Observation(
                key="old", company="К", role="python", point=100_000,
                published_at="2025-01-01",
            )
        ],
    )
    assert market_store.drop_stale(conn, today=date(2026, 9, 20)) == 1
    assert market_store.company_rows(conn, "К") == []


def test_метка_работодателя_требует_двух_признаков(conn):
    # Рынок: десять компаний по 300 тысяч.
    for index in range(10):
        market_store.record(
            conn,
            [
                market.Observation(
                    key="rynok-{}".format(index),
                    company="Компания {}".format(index),
                    role="python", grade="middle", geo=R.MOSCOW, format=R.REMOTE,
                    point=300_000, published_at="2026-09-01",
                )
            ],
        )
    # Скупой работодатель: три вакансии сильно ниже и семь вовсе без вилки.
    for index in range(7):
        market_store.record(
            conn,
            [
                market.Observation(
                    key="skupoy-{}".format(index), company="Скупые",
                    role="python", grade="middle", geo=R.MOSCOW, format=R.REMOTE,
                    point=150_000 if index < 3 else None, published_at="2026-09-01",
                ),
                market.Observation(
                    key="skupoy-none-{}".format(index), company="Скупые",
                    role="python", grade="middle", geo=R.MOSCOW, format=R.REMOTE,
                    point=None, published_at="2026-09-01",
                ),
            ],
        )
    market_store.recompute(conn, today=date(2026, 9, 20))

    mark = market_company.evaluate(conn, "Скупые")
    assert {sign.code for sign in mark.signs} == {"below", "hidden"}
    assert mark.flagged and mark.level == R.MARK_SET
    assert mark.weight == R.SIGN_WEIGHT["below"]

    market_company.refresh(conn, market_store.companies(conn))
    row = market_store.load_company(conn, "Скупые")
    assert row is not None and market_company.row_lines(row)

    # У честного работодателя из общего рынка признаков нет.
    assert market_company.evaluate(conn, "Компания 1").level == R.MARK_NONE


def test_метка_даёт_вес_в_скоринге(make_vacancy):
    profile = score.Profile.load("profile.yaml")
    vacancy = make_vacancy()
    stats = market.summarize("python|||", 0, [200_000] * 8)
    below = score.evaluate(vacancy, profile, market_marker=market.marker(100_000, stats))
    above = score.evaluate(vacancy, profile, market_marker=market.marker(300_000, stats))
    assert above.score > below.score
    assert any("Рынок" in reason for reason in below.reasons)


def test_сквозной_прогон_метки_вакансии(conn, make_vacancy):
    _observations(conn, 12, 250_000, company="Разные")
    # Разводим наблюдения по компаниям, иначе сработает потолок доли.
    conn.execute("UPDATE market_observations SET company = 'К' || rowid")
    conn.commit()
    market_store.recompute(conn, today=date(2026, 9, 20))

    marker = market_store.marker_for(conn, make_vacancy(salary_from=100_000, salary_to=110_000))
    assert marker.label == R.BELOW
    assert "Рынок" in marker.line() and "медианы" in marker.line()
