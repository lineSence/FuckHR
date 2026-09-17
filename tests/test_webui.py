"""Локальный интерфейс: что именно здесь проверяется.

Рисование табличек тестами не ловится и ловиться не должно. Важны две вещи,
которые ломаются тихо и дорого:

1. Сохранение facts через форму перезаписывает profile.yaml. Если оно затрёт
   queries или min_score, пользователь узнает об этом через день, когда утренний
   прогон вернёт пустоту.
2. Экранирование. В описаниях вакансий и сниппетах поиска постоянно встречается
   сырой HTML; без экранирования чужая разметка выполнится в браузере владельца.

Сеть не трогаем: провайдер поиска либо выключен, либо подменяется transport.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import contacts
import db
import webui
import websearch


def test_сохранение_фактов_не_трогает_остальной_профиль(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "min_score: 45\nskills:\n  - python\nfacts: []\n", encoding="utf-8"
    )

    facts = webui.save_facts(profile, "ускорил импорт в 12 раз\nсобрал пайплайн на 159 вакансий\n")

    assert facts == ("ускорил импорт в 12 раз", "собрал пайплайн на 159 вакансий")
    data = yaml.safe_load(profile.read_text(encoding="utf-8"))
    assert data["facts"] == list(facts)
    assert data["min_score"] == 45
    assert data["skills"] == ["python"]


def test_пустые_строки_формы_не_становятся_фактами(tmp_path: Path) -> None:
    """Тот же класс ошибки, что и пустой пункт YAML: мусор уезжает в письмо."""
    profile = tmp_path / "profile.yaml"

    facts = webui.save_facts(profile, "\n  \nединственный факт\n\n")

    assert facts == ("единственный факт",)
    assert yaml.safe_load(profile.read_text(encoding="utf-8"))["facts"] == ["единственный факт"]


def test_список_вакансий_фильтруется_по_скору(conn, make_vacancy) -> None:
    db.upsert_vacancy(conn, make_vacancy(external_id="1", url="https://hh.ru/vacancy/1"), 80.0, ["высокий"])
    db.upsert_vacancy(conn, make_vacancy(external_id="2", url="https://hh.ru/vacancy/2"), 30.0, ["низкий"])

    rows = webui.vacancy_rows(conn, min_score=60.0, limit=10)

    assert [row["score"] for row in rows] == [80.0]


def test_чужой_html_из_базы_не_попадает_в_страницу(conn, make_vacancy) -> None:
    contacts.ensure_schema(conn)
    db.upsert_vacancy(
        conn,
        make_vacancy(company="<script>alert(1)</script>"),
        90.0,
        ["тест"],
    )

    html = webui.render_vacancies(conn, min_score=0.0, limit=10)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_без_адреса_инстанса_страница_поиска_объясняет_причину(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "searxng")
    monkeypatch.delenv("SEARCH_BASE_URL", raising=False)

    html = webui.render_search(conn, query="любой запрос", company="")

    assert "SEARCH_BASE_URL" in html


def test_сводка_профиля_читается_без_падения_на_отсутствующем_файле(tmp_path: Path) -> None:
    rows = webui.profile_summary(tmp_path / "нет-такого.yaml")

    assert rows and "не найден" in rows[0][1]


def test_страница_профиля_предупреждает_о_пустых_фактах(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("facts: []\n", encoding="utf-8")

    html = webui.render_profile(str(profile))

    assert "facts пуст" in html


def test_выдача_поиска_показывается_с_доменом(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """Провайдер подменён: ни одного сетевого вызова."""
    hits = [
        websearch.Hit(
            title="Команда разработки",
            url="https://example.com/team",
            snippet="<b>Руководитель</b> разработки",
        )
    ]
    fake = websearch.SearchProvider(
        provider=websearch.SEARXNG,
        conn=conn,
        transport=lambda provider, query, limit: hits,
    )
    monkeypatch.setattr(websearch.SearchProvider, "from_env", classmethod(lambda cls, c=None: fake))

    html = webui.render_search(conn, query="example руководитель разработки", company="")

    assert "example.com" in html
    assert "&lt;b&gt;" in html  # сниппет экранирован, а не вставлен разметкой
