"""Цели: хранилище, разбор ввода и шаги (ADR-025).

Сеть в тестах не трогается: клиент hh.ru подменяется заглушкой, отдающей
заранее записанный кусок страницы.
"""

from __future__ import annotations

import sqlite3

import pytest

import db
import hh_employer
import targets
import ui_targets
from hh import Vacancy


EMPLOYER_PAGE = """
<a href="/employer/1455?hhtmFrom=main">Яндекс</a>
<a href="/employer/3529?from=search">Сбербанк</a>
<a href="/employer/1455">Яндекс</a>
<a href="/vacancy/999">не работодатель</a>
"""


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


@pytest.fixture()
def conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    targets.ensure_schema(conn)
    return conn


def vacancy(external_id: str, company: str = "Яндекс") -> Vacancy:
    return Vacancy(
        source="hh",
        external_id=external_id,
        url="https://hh.ru/vacancy/" + external_id,
        title="Python разработчик " + external_id,
        company=company,
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


def test_candidates_survive_broken_page() -> None:
    # Редизайн hh.ru — это пустой список, а не падение страницы.
    assert hh_employer.candidates(FakeClient("<html>ничего</html>"), "Яндекс") == []


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
