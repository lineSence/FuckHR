"""Локальный интерфейс: что именно здесь проверяется.

Рисование табличек тестами не ловится и ловиться не должно. Важны две вещи,
которые ломаются тихо и дорого:

1. Сохранение facts через форму перезаписывает profile.yaml. Если оно затрёт
   queries или min_score, пользователь узнает об этом через день, когда утренний
   прогон вернёт пустоту.
2. Экранирование. В описаниях вакансий и сниппетах поиска постоянно встречается
   сырой HTML; без экранирования чужая разметка выполнится в браузере владельца.

Сеть не трогаем: провайдер поиска либо выключен, либо подменяется transport.

Про ключи вакансий. Vacancy.key считается от названия и компании, а не от id или URL:
две вакансии с одинаковыми названием и компанией — одна и та же строка в базе, и
второй upsert перезапишет скор первой. Именно на этом раньше ломался тест фильтра.

Про тексты предупреждений. Проверяем факт предупреждения и класс блока, а не
формулировку целиком: иначе любая правка текста в интерфейсе красит тесты
красным, не найдя ни одной ошибки.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import conditions
import contacts
import db
import detector
import settings
import ui_views
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
    db.upsert_vacancy(
        conn,
        make_vacancy(external_id="1", title="Python разработчик", company="ООО Ромашка"),
        80.0,
        ["высокий"],
    )
    db.upsert_vacancy(
        conn,
        make_vacancy(external_id="2", title="Backend Python", company="ООО Ландыш"),
        30.0,
        ["низкий"],
    )

    rows = webui.vacancy_rows(conn, min_score=60.0, limit=10)

    assert [row["score"] for row in rows] == [80.0]


def test_одинаковые_название_и_компания_считаются_одной_вакансией(conn, make_vacancy) -> None:
    """Закрепляет поведение дедупа, на котором споткнулся тест выше.

    Разные external_id и разные URL новой строки не дают: ключ считается от названия
    и компании, и это сознательное решение: перепубликацию мы как раз ловим.
    """
    db.upsert_vacancy(conn, make_vacancy(external_id="1", url="https://hh.ru/vacancy/1"), 80.0, [])
    db.upsert_vacancy(conn, make_vacancy(external_id="2", url="https://hh.ru/vacancy/2"), 30.0, [])

    rows = webui.vacancy_rows(conn, min_score=0.0, limit=10)

    assert len(rows) == 1
    assert rows[0]["score"] == 30.0  # последний upsert перезаписал скор


def test_чужой_html_из_базы_не_попадает_в_страницу(conn, make_vacancy) -> None:
    # Страница вакансий показывает покрытие контактов и условий, а фикстура conn
    # создаёт только базовую схему. В живом интерфейсе это делает open_db().
    contacts.ensure_schema(conn)
    conditions.ensure_schema(conn)
    detector.ensure_schema(conn)
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
    """Без фактов письма собираются без конкретики, и это надо говорить вслух."""
    profile = tmp_path / "profile.yaml"
    profile.write_text("facts: []\n", encoding="utf-8")

    html = webui.render_profile(str(profile))

    assert "Факты о себе" in html
    assert "class=warn" in html


def test_страница_профиля_предупреждает_о_пустых_запросах(tmp_path: Path) -> None:
    """Без запросов сбор молча возвращает нуль вакансий — самая обидная тишина."""
    profile = tmp_path / "profile.yaml"
    profile.write_text("queries: []\nfacts:\n  - факт\n", encoding="utf-8")

    html = webui.render_profile(str(profile))

    assert "Запросы не заданы" in html


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


def test_сортировка_списка_вакансий(conn, make_vacancy) -> None:
    conditions.ensure_schema(conn)
    contacts.ensure_schema(conn)
    db.upsert_vacancy(
        conn,
        make_vacancy(external_id="1", title="Аналитик", company="ООО Ромашка"),
        90.0,
        [],
    )
    db.upsert_vacancy(
        conn,
        make_vacancy(external_id="2", title="Backend Python", company="ООО Ландыш"),
        10.0,
        [],
    )

    by_score = webui.render_vacancies(conn, 0.0, 10, sort="score")
    assert by_score.index("Аналитик") < by_score.index("Backend Python")
    # По алфавиту латиница идёт раньше кириллицы — порядок меняется на обратный.
    by_title = webui.render_vacancies(conn, 0.0, 10, sort="title")
    assert by_title.index("Backend Python") < by_title.index("Аналитик")
    # Неизвестное имя сортировки не роняет страницу и не уходит в SQL.
    assert "Аналитик" in webui.render_vacancies(conn, 0.0, 10, sort="1=1")


def test_страница_настроек_складывается_в_подкаты() -> None:
    """Подкаты и поиск: проверяем каркас, а не вёрстку.

    Важно ровно одно — поля остаются внутри формы. Если группа однажды окажется
    после </form>, свёрнутые настройки перестанут сохраняться молча.
    """
    html = ui_views.render_settings()

    assert html.count("<details class=setgroup") == len(settings.GROUPS)
    assert html.count(" open>") == 1  # раскрыт только первый подкат
    assert 'id="setq"' in html or "id=setq" in html
    assert html.index("<details class=setgroup") > html.index("<form method=post")
    assert html.rindex("</details>") < html.index("</form>")
    # data-find даёт поиску по чему искать: ключ, название, подсказка.
    assert 'data-find="сколько вакансий собирать за прогон run_limit' in html


def test_мёртвые_ключи_hh_api_убраны_из_настроек() -> None:
    """HH_TOKEN и HH_USER_AGENT не читает ни один модуль: HHClient не в пайплайне."""
    assert "HH_TOKEN" not in settings.FIELD_BY_KEY
    assert "HH_USER_AGENT" not in settings.FIELD_BY_KEY
