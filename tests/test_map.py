"""Карта: что проверяется и почему именно это.

Сети в тестах нет: ни hh.ru, ни тайлов, ни Leaflet [CORE-017]. База — в памяти.

Главные риски фичи не в разметке, а в данных: вакансия без адреса, нулевая
точка (0,0) и поиск со знаком % — именно они тихо портят карту.
"""

from __future__ import annotations

import sqlite3

import geo
import geo_query
import jobs
import ui_core
import ui_map

SCHEMA = """
CREATE TABLE vacancies (
    key TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    area TEXT,
    source TEXT,
    score REAL,
    url TEXT,
    published_at TEXT,
    last_seen_at TEXT
)
"""

ROWS = (
    ("hh:1", "Python-разработчик", "Компания А", "Москва", 80.0),
    ("hh:2", "Аналитик 100% удалённо", "Компания Б", "Казань", 40.0),
    ("hh:3", "Без адреса", "Компания В", "Урюпинск", 10.0),
)


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    for key, title, company, area, score in ROWS:
        conn.execute(
            "INSERT INTO vacancies (key, title, company, area, source, score, url,"
            " published_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                title,
                company,
                area,
                "hh.ru",
                score,
                "https://hh.ru/vacancy/" + key.split(":")[1],
                "2026-09-01T10:00:00",
                "2026-09-02T10:00:00",
            ),
        )
    conn.commit()
    geo.ensure_schema(conn)
    geo.save(conn, "hh:1", geo.Point("Москва, Ленинский пр-т, 1", 55.71, 37.59, "Октябрьская"))
    geo.save(conn, "hh:2", geo.Point("Казань, Баумана, 5", 55.79, 49.12, None))
    return conn


def test_ensure_schema_idempotent() -> None:
    """Колонки добавляются на живой базе и только один раз."""
    conn = make_db()
    geo.ensure_schema(conn)
    names = {row[1] for row in conn.execute("PRAGMA table_info(vacancies)")}
    assert {"address", "lat", "lng", "metro"} <= names


def test_valid_rejects_null_island() -> None:
    """(0, 0) — это пустое поле, а не Атлантика."""
    assert geo.valid(55.7, 37.6)
    assert not geo.valid(0.0, 0.0)
    assert not geo.valid(None, 37.6)
    assert not geo.valid(91.0, 37.6)


def test_num_survives_comma_and_junk() -> None:
    assert geo._num("55,75") == 55.75
    assert geo._num("") is None
    assert geo._num("москва") is None
    assert geo._num(True) is None


def test_address_of_object() -> None:
    node = {
        "address": {
            "city": "Москва",
            "street": "Ленинский пр-т",
            "building": "1",
            "lat": "55,71",
            "lng": 37.59,
            "metroStations": [{"stationName": "Октябрьская"}],
        }
    }
    point = geo.address_of(node)
    assert point is not None
    assert point.mappable
    assert "Москва" in point.address
    assert point.metro == "Октябрьская"


def test_address_of_string_and_missing() -> None:
    """Адрес бывает строкой; его может не быть вовсе — это не ошибка."""
    point = geo.address_of({"address": "Казань, Баумана, 5"})
    assert point is not None and not point.mappable
    assert geo.address_of({"name": "Удалённо"}) is None
    assert geo.address_of({"address": {"lat": 0, "lng": 0}}) is None


def test_points_and_coverage() -> None:
    # Отбор переехал в geo_query и берёт условия из filters.py, как список
    # вакансий: имена параметров теперь те же, что в адресной строке.
    conn = make_db()
    assert geo.coverage(conn) == (2, 3)
    assert len(geo_query.points(conn, {})) == 2
    assert [p["key"] for p in geo_query.points(conn, {"min_score": "60"})] == ["hh:1"]
    assert [p["key"] for p in geo_query.points(conn, {"area": "Казань"})] == ["hh:2"]
    assert [p["key"] for p in geo_query.points(conn, {"q": "Python"})] == ["hh:1"]


def test_points_treat_percent_as_text() -> None:
    """Процент в запросе — буква, а не джокер LIKE."""
    conn = make_db()
    assert [p["key"] for p in geo_query.points(conn, {"q": "100%"})] == ["hh:2"]
    # Один знак процента — это поиск знака процента, а не «покажи всё»:
    # находится только вакансия, у которой он есть в названии.
    assert [p["key"] for p in geo_query.points(conn, {"q": "%"})] == ["hh:2"]


def test_one_and_areas() -> None:
    conn = make_db()
    item = geo.one(conn, "hh:1")
    assert item is not None and item["lat"] is not None
    assert geo.one(conn, "hh:3")["lat"] is None
    assert geo.one(conn, "hh:404") is None
    assert dict(geo.areas(conn)) == {"Москва": 1, "Казань": 1}


def test_pending_skips_mapped_and_alien_sites() -> None:
    """Без точки — hh:3 и вакансия с Работы.ру, но вторую в дозаполнение брать
    нельзя: её id на hh.ru ведёт на другую, живую вакансию, и на карте появился
    бы чужой адрес, выданный за наш."""
    conn = make_db()
    conn.execute(
        "INSERT INTO vacancies (key, title, company, area, source, score, url,"
        " published_at, last_seen_at) VALUES ('rb:1', 'С Работы.ру', 'Компания Г',"
        " 'Тверь', 'rabota', 90.0, 'https://www.rabota.ru/vacancy/54421864/',"
        " '2026-09-01T10:00:00', '2026-09-02T10:00:00')"
    )
    conn.commit()
    assert [row["key"] for row in geo.pending(conn)] == ["hh:3"]


def test_vacancy_id() -> None:
    assert geo.vacancy_id("https://hh.ru/vacancy/12345?from=x") == "12345"
    assert geo.vacancy_id("https://hh.ru/employer/1") is None
    assert geo.vacancy_id(None) is None


def test_points_json_has_focus() -> None:
    conn = make_db()
    import json

    data = json.loads(ui_map.points_json(conn, {"key": "hh:2"}))
    assert data["focus"] == "hh:2"
    assert {p["key"] for p in data["points"]} == {"hh:1", "hh:2"}


def test_render_map_offline() -> None:
    """Страница собирается без сети и честно считает вакансии без адреса."""
    conn = make_db()
    html = ui_map.render_map(conn, {})
    assert 'id=map' in html
    assert "без адреса" in html
    # Список адресов есть всегда: это запасной вид без Leaflet.
    assert "maplist" in html and "Казань, Баумана, 5" in html


def test_tiles_placeholders_are_literal() -> None:
    """Адрес тайлов уходит в Leaflet как есть: {s}/{z}/{x}/{y} разбирает он."""
    assert "{s}" in ui_map.tile_url() and "{z}" in ui_map.tile_url()
    assert "{{" not in ui_map.DEFAULT_TILES


def test_link_and_hint() -> None:
    conn = make_db()
    assert "/map?key=hh%3A1" in ui_map.link(conn, "hh:1") or "/map?key=hh:1" in ui_map.link(
        conn, "hh:1"
    )
    # У вакансии без точки ссылки на карту нет и быть не должно.
    assert "на карте" not in ui_map.link(conn, "hh:3")
    assert "2 из 3" in ui_map.hint(conn)


def test_map_is_reachable_from_menu() -> None:
    """Фича, до которой нельзя дойти мышью, для владельца не существует."""
    assert any(path == "/map" for path, _ in ui_core.NAV_ITEMS)
    assert "geo-backfill" in jobs.TASKS


def test_backfill_button_lives_on_the_map() -> None:
    """Кнопка сбора адресов там же, где видна пустота на карте."""
    conn = make_db()
    html = ui_map.render_map(conn, {})
    assert 'action="/map/geo"' in html
    # Осталась ровно одна вакансия без точки — число написано до нажатия.
    assert "Осталось 1" in html


def test_backfill_task_takes_everything() -> None:
    """Кнопка берёт все недостающие адреса, а не порцию из GEO_BACKFILL_LIMIT."""
    _, argv, _ = jobs.TASKS["geo-backfill"]
    assert argv == ("geo_backfill.py", "--all")


def test_run_page_keeps_only_regular_tasks() -> None:
    """На странице запуска — только то, что гоняется регулярно."""
    keys = {key for key, _, _ in jobs.task_list()}
    assert "collect" in keys and "tests" in keys
    for hidden in (
        "collect-dry",
        "outreach",
        "outreach-dry",
        "target-scan",
        "geo-backfill",
    ):
        assert hidden not in keys
        # Из командной строки и из своих разделов они всё равно запускаются.
        assert hidden in jobs.TASKS


def test_светофор_и_поиск_отбирают_метки_на_месте() -> None:
    """Галочки уровней и поиск работают в браузере: перезагрузка карты стоит
    секунды ожидания и сбрасывает масштаб."""
    conn = make_db()
    html = ui_map.render_map(conn, {})

    # Легенда стала выключателями, а не только подписью цветов.
    assert "id=maplevels" in html
    assert html.count("input type=checkbox class=lvl") == 5
    # Список адресов помечен уровнем, чтобы прятаться вместе с меткой.
    assert "id=maprows" in html
    assert "data-level=" in html
    # Отбор живёт в скрипте карты, а не в ссылке с перезагрузкой.
    assert "addEventListener('change', refresh)" in html
    assert "addEventListener('input', refresh)" in html
