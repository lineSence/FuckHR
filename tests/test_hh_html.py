"""Разбор страницы и диагностика сбоев — без сети, на синтетической верстке."""

from __future__ import annotations

import json

import pytest

import hh_html

NODE = {
    "vacancyId": 111,
    "name": "Python разработчик",
    "area": {"name": "Москва"},
    "compensation": {"from": 300000, "currencyCode": "RUR", "gross": False},
    "company": {"id": 42, "name": "Ромашка"},
    "workSchedule": {"name": "Удаленная работа"},
    "links": {"desktop": "/vacancy/111"},
    "publicationTime": {"@timestamp": 1757000000000},
    "snippet": {"requirement": "<b>Python</b>, FastAPI"},
}


def page_with_state(nodes: list[dict]) -> str:
    state = {"vacancySearchResult": {"vacancies": nodes}}
    return (
        "<html><body><template id=\"HH-Lux-InitialState\">"
        + json.dumps(state, ensure_ascii=False)
        + "</template></body></html>"
    )


def test_extract_state_and_find_nodes():
    state = hh_html.extract_state(page_with_state([NODE]))
    nodes = hh_html.find_vacancy_nodes(state)
    assert len(nodes) == 1
    assert nodes[0]["vacancyId"] == 111


def test_node_to_vacancy():
    vacancy = hh_html.node_to_vacancy(NODE)
    assert vacancy.external_id == "111"
    assert vacancy.url == "https://hh.ru/vacancy/111"
    assert vacancy.company == "Ромашка"
    assert vacancy.salary_from == 300000
    assert vacancy.monthly_salary_net() == 300000
    assert "FastAPI" in vacancy.description
    # Главное: дата публикации разобралась, а не осталась пустой.
    assert vacancy.published_at is not None
    assert vacancy.published_at.startswith("2025-09-04")


def test_extract_state_raises_on_redesign():
    with pytest.raises(hh_html.ExtractionError):
        hh_html.extract_state("<html><body>ничего похожего</body></html>")


def test_parse_cards_fallback():
    page = (
        '<a href="https://hh.ru/vacancy/222?from=search">Backend Python</a>'
        '<a href="https://hh.ru/vacancy/222">Backend Python</a>'
        '<a href="https://hh.ru/vacancy/333">Data Engineer</a>'
    )
    vacancies = hh_html.parse_cards_fallback(page)
    assert [v.external_id for v in vacancies] == ["222", "333"]
    assert vacancies[0].url == "https://hh.ru/vacancy/222"


def test_dump_failure_scrubs_secrets_and_prunes(tmp_path):
    page = '<html>hhtoken=abcdef123456; my-cookie-value</html>'
    first = hh_html.dump_failure(
        page, "no state", tmp_path, secrets=["my-cookie-value"], keep=2
    )
    saved = first.read_text(encoding="utf-8")
    assert "my-cookie-value" not in saved
    assert "abcdef123456" not in saved
    assert "hhtoken=***" in saved

    for _ in range(3):
        hh_html.dump_failure(page, "no state", tmp_path, keep=2)
    assert len(list(tmp_path.glob("*.html"))) == 2
