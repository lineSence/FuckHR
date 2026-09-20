"""Фильтры и сортировки: проверяем не вёрстку, а честность выборки.

Самое дорогое здесь — три вещи:

1. Ни один символ из браузера не попадает в SQL. Проверяется попыткой
   уронить таблицу через поле поиска.
2. Сортировка идёт по всей базе, а не по срезу. Старая страница брала первые N
   по скору и сортировала уже их: «по зарплате» показывало самую денежную из
   верхушки. Такая ошибка не видна глазами и потому опаснее пустой страницы.
3. Незаданный фильтр ничего не фильтрует. Иначе забытое поле молча прячет
   половину базы.
"""

from __future__ import annotations

import sqlite3

import pytest

import db
import filters
import ui_filters
import ui_views
from hh import Vacancy


def _add(conn: sqlite3.Connection, title: str, company: str, score: float, **kwargs):
    vacancy = Vacancy(
        external_id=title,
        url="https://hh.ru/vacancy/" + title,
        title=title,
        company=company,
        description=kwargs.pop("description", "Описание вакансии для теста."),
        **kwargs,
    )
    db.upsert_vacancy(conn, vacancy, score, ["тест"])
    return vacancy


@pytest.fixture
def filled(conn: sqlite3.Connection) -> sqlite3.Connection:
    _add(conn, "Бедный сеньор", "А", 90.0, salary_from=50000, currency="RUR")
    _add(conn, "Богатый джун", "Б", 10.0, salary_from=400000, currency="RUR")
    _add(conn, "Без вилки", "В", 50.0)
    conn.commit()
    return conn


def test_сортировка_по_зарплате_смотрит_всю_базу(filled: sqlite3.Connection) -> None:
    rows, found, _ = ui_views.filtered_vacancies(filled, {"sort": "salary"}, limit=1)

    assert found == 3
    # Раньше здесь оказывался «Бедный сеньор»: срез брался по скору.
    assert rows[0]["title"] == "Богатый джун"


def test_фильтр_по_зарплате_считает_нижнюю_границу(filled: sqlite3.Connection) -> None:
    rows, found, active = ui_views.filtered_vacancies(filled, {"salary": "100000"}, 50)

    assert [row["title"] for row in rows] == ["Богатый джун"]
    assert found == 1
    assert active and "Зарплата от" in active[0][1]


def test_попытка_уронить_базу_через_поле_поиска(filled: sqlite3.Connection) -> None:
    rows, _found, _ = ui_views.filtered_vacancies(
        filled, {"q": "'; DROP TABLE vacancies; --"}, 50
    )

    assert rows == []
    assert filled.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 3


def test_неизвестный_фильтр_и_чужая_сортировка_игнорируются(
    filled: sqlite3.Connection,
) -> None:
    params = {"выдумка": "1", "sort": "; DELETE FROM vacancies", "market": "неважно"}
    rows, found, active = ui_views.filtered_vacancies(filled, params, 50)

    assert found == 3
    assert active == []
    assert [row["title"] for row in rows][0] == "Бедный сеньор"  # умолчание: по скору


def test_пустое_значение_не_становится_условием(filled: sqlite3.Connection) -> None:
    where, args, active = filters.build_where(
        filters.VACANCY_FILTERS, {"q": "   ", "min_score": "", "state": ""}
    )

    assert where == "1=1"
    assert args == []
    assert active == []


def test_быстрый_вид_подставляет_умолчания_а_явное_сильнее() -> None:
    params = ui_filters.apply_preset(filters.VACANCY_PRESETS, {"view": "open"})
    assert params["min_score"] == "50"
    assert params["state"] == "active"

    mine = ui_filters.apply_preset(
        filters.VACANCY_PRESETS, {"view": "open", "min_score": "80"}
    )
    assert mine["min_score"] == "80"


def test_чип_снимает_только_свой_фильтр() -> None:
    params = {"q": "питон", "min_score": "50"}
    _where, _args, active = filters.build_where(filters.VACANCY_FILTERS, params)
    html = ui_filters.chips(active, params, "/vacancies")

    assert "min_score=50" in html and "q=%D0%BF" in html  # ссылка снятия q
    assert "сбросить всё" in html


def test_фильтр_компаний_собирается_и_не_ломает_запрос(conn: sqlite3.Connection) -> None:
    import ui_companies

    rows, found, active = ui_companies.filtered_companies(
        conn, {"level": "red", "cq": "'"}, 50
    )

    assert rows == [] and found == 0
    assert len(active) == 2


def test_каждый_фильтр_превращается_в_рабочий_sql(filled: sqlite3.Connection) -> None:
    """Дешёвая страховка: любое поле формы обязано отрабатывать на живой базе."""
    probe = {
        "q": "тест",
        "company": "А",
        "area": "Москва",
        "min_score": "10",
        "salary": "1",
        "days": "30",
        "state": "active",
        "salary_shown": "yes",
        "notified": "no",
        "feedback": "none",
        "contact": "none",
        "market": "below",
        "ai": "human",
        "company_level": "red",
        "risk": "clean",
    }
    assert set(probe) == {item.key for item in filters.VACANCY_FILTERS}
    for key, value in probe.items():
        ui_views.filtered_vacancies(filled, {key: value}, 5)

    for item in filters.COMPANY_FILTERS:
        import ui_companies

        value = "1" if item.kind == "number" else (item.options[1][0] if item.options else "а")
        ui_companies.filtered_companies(filled, {item.key: value}, 5)
