"""Цели: хранилище, разбор ввода и шаги (ADR-025).

Сеть в тестах не трогается: клиент hh.ru подменяется заглушкой, отдающей
заранее записанный кусок страницы.
"""

from __future__ import annotations

import sqlite3

import pytest

import db
import hh_employer
import hh_html
import targets
import ui_targets
from hh import Vacancy


EMPLOYER_PAGE = """
<a href="/employer/1455?hhtmFrom=main">Яндекс</a>
<a href="/employer/3529?from=search">Сбербанк</a>
<a href="/employer/1455">Яндекс</a>
<a href="/vacancy/999">не работодатель</a>
"""

# Страница после редизайна: ссылок в разметке нет, всё в JSON состояния.
STATE_PAGE = (
    '<template id="HH-Lux-InitialState">'
    '{"employerSearch": {"employers": ['
    '{"id": 1455, "name": "\\u042f\\u043d\\u0434\\u0435\\u043a\\u0441", '
    '"vacanciesCount": 100, "areaName": "\\u041c\\u043e\\u0441\\u043a\\u0432\\u0430"}, '
    '{"id": 3529, "name": "\\u0421\\u0431\\u0435\\u0440", "vacanciesCount": 7}'
    "]}}"
    "</template>"
)


class FakeClient:
    def __init__(self, body: str = EMPLOYER_PAGE, items: list[Vacancy] | None = None):
        self.body = body
        self.items = items or []
        self.calls: list[tuple[str, dict]] = []

    def fetch(self, url: str, params: dict | None = None) -> str:
        self.calls.append((url, params or {}))
        return self.body

    def search(self, text, period=0, max_pages=0, extra=None):
        self.calls.append(("search", dict(extra or {})))
        yield from self.items

    def close(self) -> None:
        pass


class BlockedClient(FakeClient):
    """hh.ru показывает капчу вместо выдачи."""

    def fetch(self, url: str, params: dict | None = None) -> str:
        raise hh_html.BlockedError("капча на странице поиска")

    def search(self, text, period=0, max_pages=0, extra=None):
        raise hh_html.BlockedError("капча на странице поиска")
        yield  # pragma: no cover


@pytest.fixture()
def conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    targets.ensure_schema(conn)
    return conn


def vacancy(
    external_id: str, company: str = "Яндекс", company_id: str | None = None
) -> Vacancy:
    return Vacancy(
        source="hh",
        external_id=external_id,
        url="https://hh.ru/vacancy/" + external_id,
        title="Python разработчик " + external_id,
        company=company,
        company_id=company_id,
        description="текст",
    )


def test_parse_input_understands_three_ways() -> None:
    assert hh_employer.parse_input("https://hh.ru/employer/1455") == hh_employer.Ask(
        "link", "1455"
    )
    assert hh_employer.parse_input("hh.ru/vacancy/777?f=1").kind == "vacancy"
    assert hh_employer.parse_input("7736207543") == hh_employer.Ask("inn", "7736207543")
    assert hh_employer.parse_input(" Яндекс ") == hh_employer.Ask("name", "Яндекс")


def test_candidates_deduplicated() -> None:
    found = hh_employer.candidates(FakeClient(), "Яндекс")
    assert [(e.id, e.name) for e in found] == [("1455", "Яндекс"), ("3529", "Сбербанк")]


def test_candidates_read_page_state() -> None:
    # Ссылок в разметке нет — работодатели берутся из состояния страницы.
    found = hh_employer.candidates(FakeClient(STATE_PAGE), "Яндекс")
    assert [(e.id, e.name) for e in found] == [("1455", "Яндекс"), ("3529", "Сбер")]


def test_candidates_fall_back_to_vacancy_search() -> None:
    # Страница поиска компаний непонятная, но выдача вакансий работает.
    client = FakeClient(
        "<html>ничего</html>",
        items=[vacancy("1", company_id="1455"), vacancy("2", company_id="1455")],
    )
    found = hh_employer.candidates(client, "Яндекс")
    assert [(e.id, e.name) for e in found] == [("1455", "Яндекс")]
    assert ("search", {"search_field": "company_name"}) in client.calls


def test_candidates_survive_broken_page() -> None:
    # Редизайн hh.ru — это пустой список, а не падение страницы.
    assert hh_employer.candidates(FakeClient("<html>ничего</html>"), "Яндекс") == []


def test_captcha_is_not_reported_as_empty_result(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # «Не нашлось» на капче заставляет владельца править название вместо cookie.
    monkeypatch.setattr(ui_targets, "_client", lambda: BlockedClient())
    note = ui_targets.add_from_input(conn, "Яндекс")
    assert "hh.ru не пускает" in note
    assert targets.all_targets(conn) == []


def test_inn_target_explains_next_step(conn: sqlite3.Connection) -> None:
    note = ui_targets.add_from_input(conn, "7736207543")
    assert "ИНН" in note and "Ресёрч" in note.replace("ресёрч", "Ресёрч")
    assert len(targets.all_targets(conn)) == 1


def test_add_updates_without_duplicating(conn: sqlite3.Connection) -> None:
    first = targets.add(conn, "Яндекс", inn="7736207543", source="inn")
    second = targets.add(conn, "Яндекс", employer_id="1455", source="link")
    assert first == second
    target = targets.get(conn, first)
    assert target is not None
    assert target.inn == "7736207543" and target.employer_id == "1455"


def test_link_marks_only_new(conn: sqlite3.Connection) -> None:
    tid = targets.add(conn, "Яндекс", employer_id="1455")
    assert targets.link(conn, tid, ["a", "b"]) == 2
    assert targets.link(conn, tid, ["a", "b", "c"]) == 1
    assert targets.counts(conn, tid) == (3, 1)
    targets.seen(conn, tid)
    assert targets.counts(conn, tid) == (3, 0)


def test_watch_due_only_when_enabled(conn: sqlite3.Connection) -> None:
    tid = targets.add(conn, "Яндекс", employer_id="1455")
    assert targets.due(conn) == []
    targets.set_watch(conn, tid, True)
    assert [t.id for t in targets.due(conn)] == [tid]
    targets.mark_scan(conn, tid, watch=True)
    # Только что смотрели — второй обход в тот же день не нужен.
    assert targets.due(conn) == []


def test_scan_saves_all_vacancies_without_threshold(conn: sqlite3.Connection) -> None:
    import targets_hh

    tid = targets.add(conn, "Яндекс", employer_id="1455")
    target = targets.get(conn, tid)
    assert target is not None
    client = FakeClient(items=[vacancy("1"), vacancy("2")])
    total, fresh = targets_hh.scan(conn, client, target)
    assert (total, fresh) == (2, 2)
    rows = targets.vacancies(conn, tid)
    assert len(rows) == 2
    # Скор без профилей нулевой, но вакансия всё равно сохранена и показана.
    assert all((row["score"] or 0) == 0 for row in rows)
    assert client.calls[0][1] == {"employer_id": "1455"}


def test_star_form_knows_existing_target(conn: sqlite3.Connection) -> None:
    assert "★ В цели" in ui_targets.star_form(conn, "Яндекс")
    targets.add(conn, "Яндекс", source="run")
    assert "уже в целях" in ui_targets.star_form(conn, "Яндекс")


def test_step_needs_resolved_employer(conn: sqlite3.Connection) -> None:
    tid = targets.add(conn, "ООО Ромашка", inn="7736207543", source="inn")
    problem = ui_targets.start_step(conn, tid, "vacancies")
    assert "hh.ru" in problem


def test_render_targets_lists_cards(conn: sqlite3.Connection) -> None:
    targets.add(conn, "Яндекс", employer_id="1455")
    html = ui_targets.render_targets(conn)
    assert "Яндекс" in html and "Добавить цель" in html and "Следить" in html


# --- отбор вакансий внутри цели (B-16) -------------------------------------


def seeded(conn: sqlite3.Connection) -> int:
    """Цель с тремя вакансиями из разных городов и с разным скором."""
    import targets_hh

    tid = targets.add(conn, "Яндекс", employer_id="1455")
    target = targets.get(conn, tid)
    assert target is not None
    targets_hh.scan(
        conn, FakeClient(items=[vacancy("1"), vacancy("2"), vacancy("3")]), target
    )
    plan = [
        ("Москва", 80.0, "2026-09-01"),
        ("Казань", 30.0, "2026-08-01"),
        ("Урюпинск", 0.0, "2026-07-01"),
    ]
    for row, (area, score, day) in zip(targets.vacancies(conn, tid), plan):
        conn.execute(
            "UPDATE vacancies SET area = ?, score = ?, published_at = ? WHERE key = ?",
            (area, score, day, row["key"]),
        )
    conn.commit()
    return tid


def test_vacancies_filtered_by_city_and_score(conn: sqlite3.Connection) -> None:
    # Тысяча вакансий в куче — это то же, что ни одной: нужен отбор.
    tid = seeded(conn)
    assert len(targets.vacancies(conn, tid)) == 3
    only_kazan = targets.vacancies(conn, tid, area="Казань")
    assert [row["area"] for row in only_kazan] == ["Казань"]
    good = targets.vacancies(conn, tid, min_score=50.0)
    assert [row["area"] for row in good] == ["Москва"]


def test_vacancies_sorted_by_known_keys_only(conn: sqlite3.Connection) -> None:
    tid = seeded(conn)
    targets.seen(conn, tid)
    by_score = targets.vacancies(conn, tid, sort="score")
    assert [row["score"] for row in by_score] == [80.0, 30.0, 0.0]
    # Чужое имя порядка не попадает в SQL, а молча заменяется умолчанием.
    strange = targets.vacancies(conn, tid, sort="1; DROP TABLE vacancies")
    assert len(strange) == 3
    by_area = targets.vacancies(conn, tid, sort="area")
    assert [row["area"] for row in by_area] == ["Казань", "Москва", "Урюпинск"]


def test_vacancies_search_is_a_parameter_not_sql(conn: sqlite3.Connection) -> None:
    tid = seeded(conn)
    assert len(targets.vacancies(conn, tid, query="разработчик 2")) == 1
    # Строка из формы остаётся данными, а не кодом и не джокером.
    assert targets.vacancies(conn, tid, query="' OR 1=1 --") == []
    assert targets.vacancies(conn, tid, query="%") == []
    assert len(targets.vacancies(conn, tid)) == 3


def test_areas_counts_cities_of_this_target(conn: sqlite3.Connection) -> None:
    tid = seeded(conn)
    assert targets.areas(conn, tid) == [("Казань", 1), ("Москва", 1), ("Урюпинск", 1)]


def test_target_page_keeps_steps_and_adds_filters(conn: sqlite3.Connection) -> None:
    tid = seeded(conn)
    html = ui_targets.render_target(conn, tid)
    # Три кнопки шагов остаются как были [CORE-016].
    for _, label, _hint in ui_targets.STEPS:
        assert label in html
    assert 'name="area"' in html and 'name="sort"' in html and 'name="q"' in html
    assert "Показано 3 из 3" in html


def test_chosen_filter_survives_until_reset(conn: sqlite3.Connection) -> None:
    tid = seeded(conn)
    note = ui_targets.handle(
        conn,
        "/targets/step",
        {"id": [str(tid)], "step": ["view"], "area": ["Казань"], "min": ["20"]},
    )
    # Отбор ничего не запускает, поэтому и сообщать о запуске нечего.
    assert note == ""
    html = ui_targets.render_target(conn, tid)
    assert "Показано 1 из 3" in html and "Сбросить отбор" in html
    ui_targets.handle(conn, "/targets/step", {"id": [str(tid)], "step": ["view"]})
    assert "Показано 3 из 3" in ui_targets.render_target(conn, tid)


def test_empty_filter_result_explains_itself(conn: sqlite3.Connection) -> None:
    tid = seeded(conn)
    ui_targets.handle(
        conn, "/targets/step", {"id": [str(tid)], "step": ["view"], "min": ["80"], "area": ["Урюпинск"]}
    )
    html = ui_targets.render_target(conn, tid)
    assert "не попала ни одна вакансия" in html
