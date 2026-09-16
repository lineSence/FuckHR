"""Внешний поиск тестируется без сети: transport подменяется заглушкой."""

from __future__ import annotations

import sqlite3

import pytest

import websearch


def _hits(n: int = 2) -> list[websearch.Hit]:
    return [
        websearch.Hit(title=f"Страница {i}", url=f"https://acme.ru/{i}", snippet="команда")
        for i in range(n)
    ]


def test_без_ключа_провайдер_выключен() -> None:
    provider = websearch.SearchProvider(api_key="")
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


def test_неизвестный_провайдер_ошибка() -> None:
    with pytest.raises(ValueError):
        websearch.SearchProvider(provider="ololo", api_key="k")
