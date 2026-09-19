"""Внешний поиск тестируется без сети: transport подменяется заглушкой."""

from __future__ import annotations

import sqlite3

import pytest

import contacts_rules
import websearch


def _hits(n: int = 2) -> list[websearch.Hit]:
    return [
        websearch.Hit(title=f"Страница {i}", url=f"https://acme.ru/{i}", snippet="команда")
        for i in range(n)
    ]


def test_без_ключа_провайдер_выключен() -> None:
    provider = websearch.SearchProvider(provider=websearch.TAVILY, api_key="")
    assert provider.enabled is False
    assert provider.search("АКМЕ тимлид") == []
    assert provider.usage.skipped == 1


def test_кэш_не_трогает_сеть_второй_раз(conn: sqlite3.Connection) -> None:
    calls: list[str] = []

    def transport(provider: str, query: str, limit: int) -> list[websearch.Hit]:
        calls.append(query)
        return _hits()

    provider = websearch.SearchProvider(api_key="k", conn=conn, transport=transport)
    first = provider.search("АКМЕ тимлид")
    second = provider.search("АКМЕ тимлид")
    assert [h.url for h in first] == [h.url for h in second]
    assert len(calls) == 1
    assert provider.usage.cached == 1
    assert provider.usage.calls == 1


def test_падение_провайдера_не_ломает_пайплайн() -> None:
    def transport(provider: str, query: str, limit: int) -> list[websearch.Hit]:
        raise RuntimeError("502")

    provider = websearch.SearchProvider(api_key="k", transport=transport)
    assert provider.search("АКМЕ") == []
    assert provider.usage.failures == 1


def test_лимит_вызовов_соблюдается() -> None:
    def transport(provider: str, query: str, limit: int) -> list[websearch.Hit]:
        return _hits()

    provider = websearch.SearchProvider(api_key="k", max_calls=0, transport=transport)
    assert provider.search("АКМЕ") == []


def test_в_запрос_уходит_только_компания_и_роль() -> None:
    # В внешний поиск не уходят ни досье, ни персональные данные (ADR-011).
    queries = websearch.contact_queries("ООО Ромашка")
    assert queries
    for query in queries:
        assert "Ромашка" in query
        assert "@" not in query


def test_роль_в_запросе_идёт_от_вакансии() -> None:
    # Зашитый «тимлид backend» уходил и на вакансии «Оператор 1С» — лимит
    # SEARCH_MAX_CALLS тратился на посторонних людей.
    roles = contacts_rules.lead_roles("Оператор 1С")
    queries = websearch.contact_queries("АКМЕ", roles)
    assert any("1С" in query for query in queries)
    assert not any("backend" in query.lower() for query in queries)
    assert not any("habr" in query.lower() for query in queries)


def test_без_подсказки_роль_общая() -> None:
    queries = websearch.contact_queries("АКМЕ", contacts_rules.lead_roles("Курьер"))
    assert queries[1] == "АКМЕ руководитель отдела"


def test_неизвестный_провайдер_ошибка() -> None:
    with pytest.raises(ValueError):
        websearch.SearchProvider(provider="ololo", api_key="k")


# --- свой инстанс SearXNG ---


def test_searxng_готовность_определяет_адрес_а_не_ключ() -> None:
    без_адреса = websearch.SearchProvider(provider=websearch.SEARXNG, api_key="")
    assert без_адреса.enabled is False
    assert "SEARCH_BASE_URL" in без_адреса.disabled_reason

    с_адресом = websearch.SearchProvider(
        provider=websearch.SEARXNG, api_key="", base_url="https://searx.example.org/"
    )
    assert с_адресом.enabled is True
    # Слеш на конце не должен превращаться в //search.
    assert с_адресом.base_url == "https://searx.example.org"


def test_searxng_разбирает_ответ_и_режет_по_лимиту(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "query": "АКМЕ",
        "results": [
            {"url": f"https://acme.ru/{i}", "title": f"Страница {i}", "content": "команда"}
            for i in range(5)
        ],
    }
    запросы: list[dict] = []

    class Ответ:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload

    def fake_get(url: str, **kwargs: object) -> Ответ:
        запросы.append({"url": url, **kwargs})
        return Ответ()

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    provider = websearch.SearchProvider(
        provider=websearch.SEARXNG, base_url="https://searx.example.org"
    )
    hits = provider.search("АКМЕ тимлид", limit=2)

    assert [h.url for h in hits] == ["https://acme.ru/0", "https://acme.ru/1"]
    assert hits[0].title == "Страница 0"
    assert запросы[0]["url"] == "https://searx.example.org/search"
    assert запросы[0]["params"]["format"] == "json"


def test_searxng_html_вместо_json_не_роняет_этап(monkeypatch: pytest.MonkeyPatch) -> None:
    # Самая частая ошибка настройки: в settings.yml не включён формат json.
    class Ответ:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            raise ValueError("not json")

    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: Ответ())

    provider = websearch.SearchProvider(
        provider=websearch.SEARXNG, base_url="https://searx.example.org"
    )
    assert provider.search("АКМЕ") == []
    assert provider.usage.failures == 1
