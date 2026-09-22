"""Разбор адреса со страницы вакансии без сети.

Зачем этот файл отдельно от test_map.py: там проверяется витрина и база, здесь —
самое хрупкое место: чужая разметка. Именно она сломалась на боевом прогоне:
адрес лежал не внутри узла вакансии, а рядом в состоянии.
"""

from __future__ import annotations

import json

import geo

VACANCY = {
    "vacancyId": 12345678,
    "name": "Аналитик",
    "company": {"name": "Рога и Копыта"},
    "area": {"name": "Москва"},
}


def page(state: dict) -> str:
    """Страница в том же виде, в каком её читает hh_html.extract_state."""
    body = json.dumps(state, ensure_ascii=False)
    return (
        "<html><body><template id=\"HH-Lux-InitialState\">"
        + body
        + "</template></body></html>"
    )


def test_address_inside_vacancy_node() -> None:
    node = dict(VACANCY)
    node["address"] = {
        "city": "Москва",
        "street": "Строителей",
        "building": "12",
        "lat": 55.67,
        "lng": 37.53,
        "metroStations": [{"stationName": "Университет"}],
    }
    point = geo.from_page(page({"vacancyView": node}), "12345678")
    assert point is not None and point.mappable
    assert "Строителей" in (point.address or "")
    assert point.metro == "Университет"


def test_address_in_separate_branch() -> None:
    """Главный случай: в узле вакансии адреса нет вообще."""
    state = {
        "vacancyView": dict(VACANCY),
        "vacancyAddresses": {
            "items": [
                {
                    "rawAddress": "Москва, Ленинский пр-т, 1",
                    "lat": "55.71",
                    "lng": "37.58",
                }
            ]
        },
    }
    point = geo.from_page(page(state), "12345678")
    assert point is not None and point.mappable
    assert point.lat == 55.71 and point.lng == 37.58


def test_point_without_coordinates_still_saved() -> None:
    """Адрес без координат лучше, чем пустота: его видно в карточке."""
    node = dict(VACANCY)
    node["address"] = {"city": "Казань", "street": "Баумана", "building": "3"}
    point = geo.from_page(page({"vacancyView": node}), "12345678")
    assert point is not None and not point.mappable
    assert point.address == "Казань, Баумана, 3"


def test_zero_coordinates_are_not_a_point() -> None:
    state = {
        "vacancyView": dict(VACANCY),
        "map": {"rawAddress": "где-то", "lat": 0, "lng": 0},
    }
    point = geo.from_page(page(state), "12345678")
    assert point is None or not point.mappable


def test_no_address_returns_none() -> None:
    assert geo.from_page(page({"vacancyView": dict(VACANCY)}), "12345678") is None


def test_broken_page_is_not_an_exception() -> None:
    assert geo.from_page("<html>капча</html>", "1") is None


def test_vacancy_detail_carries_address(monkeypatch):
    """Адрес приезжает вместе с описанием: отдельного похода за точкой нет."""
    import geo
    import hh_html

    state = {
        "vacancyView": {
            "vacancyId": "42",
            "description": "<p>Работа</p>",
            "address": {
                "city": "Москва",
                "street": "Ленина",
                "building": "1",
                "lat": 55.7,
                "lng": 37.6,
                "metroStations": [{"stationName": "Тверская"}],
            },
        }
    }

    client = hh_html.HHHtmlClient.__new__(hh_html.HHHtmlClient)
    monkeypatch.setattr(
        hh_html.HHHtmlClient,
        "fetch",
        lambda self, url: '<template id="HH-Lux-InitialState">{}</template>'.format(
            json.dumps(state)
        ),
    )
    detail = client.vacancy("42")
    point = geo.point_of(detail)
    assert point is not None and point.mappable
    assert point.address == "Москва, Ленина, 1"
    assert point.metro == "Тверская"


def test_point_of_tolerates_broken_detail():
    """Страница без состояния — деталь без адреса, а не исключение [CORE-017]."""
    import geo

    assert geo.point_of({"description": "", "key_skills": []}) is None
    assert geo.point_of(None) is None


def test_save_needs_the_vacancy_in_base():
    """Адрес без вакансии записать некуда: счётчик не должен врать."""
    import sqlite3

    import geo

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE vacancies (key TEXT PRIMARY KEY)")
    point = geo.Point(address="Москва, Ленина, 1", lat=55.7, lng=37.6)
    geo.ensure_schema(conn)
    assert geo.save(conn, "hh:missing", point) is False
    conn.execute("INSERT INTO vacancies (key) VALUES ('hh:1')")
    assert geo.save(conn, "hh:1", point) is True
