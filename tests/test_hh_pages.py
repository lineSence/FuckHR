"""Ускорения сбора из B-15: кэш страниц выдачи, остановка на известной
странице и ворота на карточки вакансий.

Всё без сети: страница подменяется заглушкой fetch, база — временный файл.
Главное свойство, которое стережётся здесь: ускорения не теряют вакансии,
а только убирают повторные запросы.
"""

from __future__ import annotations

import db
import hh_html
import hh_pages
from hh import Vacancy

STATE = (
    '<template id="HH-Lux-InitialState">'
    '{"vacancySearchResult": {"vacancies": [{"vacancyId": 1, "name": "Оператор 1С", '
    '"company": {"id": 7, "name": "Контора"}, "publicationTime": "2026-09-19T10:00:00+0300"}]}}'
    "</template>"
)


def _client(cache: hh_pages.PageCache | None) -> tuple[hh_html.HHHtmlClient, list[int]]:
    """Клиент с заглушкой вместо сети и счётчиком запросов."""
    client = hh_html.HHHtmlClient(pause=0.0, pause_min=0.0, failure_dir=None, cache=cache)
    calls: list[int] = []

    def fake_fetch(url: str, params: dict | None = None, attempts: int = 3) -> str:
        calls.append(int((params or {}).get("page", 0)))
        return STATE

    client.fetch = fake_fetch  # type: ignore[method-assign]
    return client, calls


def test_повторный_запрос_берётся_из_кэша_прогона() -> None:
    cache = hh_pages.PageCache()
    client, calls = _client(cache)
    try:
        first = list(client.search("оператор 1с"))
        after_first = len(calls)
        second = list(client.search("оператор 1с"))
    finally:
        client.close()
    # Одинаковый запрос у второго профиля не стоит ни одного HTTP-запроса,
    # но вакансии всё равно отдаются: иначе второй профиль их не увидит.
    assert len(calls) == after_first
    assert [v.external_id for v in second] == [v.external_id for v in first] == ["1"]
    assert cache.hits >= 1


def test_без_кэша_страницы_качаются_заново() -> None:
    client, calls = _client(None)
    try:
        list(client.search("оператор 1с"))
        after_first = len(calls)
        list(client.search("оператор 1с"))
    finally:
        client.close()
    assert len(calls) > after_first


def test_известная_страница_останавливает_обход(tmp_path) -> None:
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    vacancy = Vacancy(
        external_id="1",
        url="https://hh.ru/vacancy/1",
        title="Оператор 1С",
        company="Контора",
        published_at="2026-09-19T10:00:00+03:00",
    )
    db.upsert_vacancy(conn, vacancy, 0.0, [])
    # Страница целиком в базе с той же датой публикации — дальше только старше.
    assert hh_pages.page_is_known(conn, [vacancy]) is True
    # Перепубликация — факт для детектора (ADR-009), его терять нельзя.
    republished = vacancy.model_copy(update={"published_at": "2026-09-20T10:00:00+03:00"})
    assert hh_pages.page_is_known(conn, [republished]) is False
    # Новая вакансия на странице тоже держит обход открытым.
    fresh = vacancy.model_copy(update={"external_id": "2", "url": "https://hh.ru/vacancy/2"})
    assert hh_pages.page_is_known(conn, [vacancy, fresh]) is False
    conn.close()


def test_без_дельты_карточки_качаются_всем() -> None:
    vacancy = Vacancy(external_id="1", url="https://hh.ru/vacancy/1", title="Оператор 1С")
    # Нулевая дельта — поведение как до B-15: карточка качается всегда,
    # причём до обращения к профилям — поэтому bundle здесь не нужен.
    assert hh_pages.worth_details(vacancy, None, None, 88, 0.0) is True
