"""Шлюз проверяется с подставным транспортом: ни одного HTTP-запроса."""

from __future__ import annotations

import sqlite3
from typing import Sequence

import pytest

import llm

MESSAGES = [{"role": "user", "content": "привет"}]


def test_выключенный_шлюз_возвращает_none() -> None:
    gateway = llm.Gateway()
    assert gateway.enabled is False
    assert gateway.complete("extract", MESSAGES) is None
    assert gateway.usage.skipped == 1


def test_персональные_этапы_прибиты_к_local_only() -> None:
    assert llm.profile_for("contacts") == llm.LOCAL
    assert llm.profile_for("draft") == llm.LOCAL
    assert llm.profile_for("hr_filter") == llm.SMART


def test_неизвестный_этап_это_ошибка() -> None:
    with pytest.raises(llm.ProfileError):
        llm.profile_for("telepathy")


def test_кэш_не_даёт_повторного_вызова(conn: sqlite3.Connection) -> None:
    calls: list[str] = []

    def transport(profile: str, messages: Sequence[dict[str, str]], temperature: float) -> str:
        calls.append(profile)
        return "ответ"

    gateway = llm.Gateway(
        base_url="http://localhost:3001/v1", conn=conn, transport=transport
    )
    first = gateway.complete("extract", MESSAGES)
    second = gateway.complete("extract", MESSAGES)
    assert (first, second) == ("ответ", "ответ")
    assert len(calls) == 1
    assert gateway.usage.calls == 1
    assert gateway.usage.cached == 1
    # Первый вызов — новый вопрос, а не «сменилась модель».
    assert (gateway.usage.miss_new, gateway.usage.miss_model) == (1, 0)


def test_смена_модели_видна_в_причине_промаха(conn: sqlite3.Connection) -> None:
    """Маршрут и модель входят в ключ кэша, и «из кэша 0» после смены модели —
    не поломка кэша. Сводка прогона должна показывать разницу."""

    def transport(profile: str, messages: Sequence[dict[str, str]], temperature: float) -> str:
        return "ответ"

    first = llm.Gateway(
        base_url="http://localhost:3001/v1", conn=conn, transport=transport
    )
    first.complete("extract", MESSAGES)
    second = llm.Gateway(
        base_url="http://localhost:3001/v1",
        conn=conn,
        transport=transport,
        local_stage_models={"extract": "другая-модель"},
    )
    second.complete("extract", MESSAGES)
    assert second.usage.cached == 0
    assert (second.usage.miss_model, second.usage.miss_new) == (1, 0)


def test_потолок_вызовов_останавливает_шлюз() -> None:
    def transport(profile: str, messages: Sequence[dict[str, str]], temperature: float) -> str:
        raise AssertionError("при исчерпанном бюджете вызовов быть не должно")

    gateway = llm.Gateway(
        base_url="http://localhost:3001/v1", max_calls=0, transport=transport
    )
    assert gateway.complete("extract", MESSAGES) is None
    assert gateway.usage.skipped == 1


def test_ошибка_транспорта_деградирует_до_none() -> None:
    attempts: list[int] = []

    def transport(profile: str, messages: Sequence[dict[str, str]], temperature: float) -> str:
        attempts.append(1)
        raise RuntimeError("429 от провайдера")

    gateway = llm.Gateway(
        base_url="http://localhost:3001/v1", transport=transport, backoff=(0.0, 0.0)
    )
    # Пайплайн обязан дожить прогон до конца без модели [CORE-017].
    assert gateway.complete("extract", MESSAGES) is None
    assert len(attempts) == 2
    assert gateway.usage.failures == 1


def test_старая_база_кэша_доживает_до_колонки_prompt() -> None:
    """Колонка появилась позже схемы: у кого база с прошлых прогонов, миграция
    обязана пройти молча. Индекс по новой колонке в самом скрипте схемы валил
    её с «no such column: prompt» — и вместе с ней любую страницу интерфейса."""
    import llm_cache

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE llm_cache (hash TEXT PRIMARY KEY, stage TEXT NOT NULL,"
        " profile TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL);"
    )
    conn.execute("INSERT INTO llm_cache VALUES ('h', 'extract', 'fast', 'ответ', 'now')")
    llm_cache.ensure_cache(conn)
    assert llm_cache.get(conn, "h") == "ответ"
    assert llm_cache.miss_reason(conn, "чужой-вопрос") == "new"
