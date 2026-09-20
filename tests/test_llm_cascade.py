"""Каскад фолбэка проверяется на подставном транспорте: ни одного HTTP-запроса."""

from __future__ import annotations

import sqlite3
from typing import Sequence

import bench
import llm

MESSAGES = [{"role": "user", "content": "привет"}]


def _gateway(**kwargs: object) -> llm.Gateway:
    params: dict[str, object] = {
        "base_url": "http://local/v1",
        "proxy_base_url": "http://proxy/v1",
        "backoff": (0.0, 0.0, 0.0),
    }
    params.update(kwargs)
    return llm.Gateway(**params)  # type: ignore[arg-type]


def _chain(gateway: llm.Gateway, stage: str) -> list[tuple[str, str]]:
    return [(r.name, r.model) for r in gateway.cascade_for(stage)]


def test_каскад_кончается_локальной_моделью() -> None:
    """Прогон обязан доходить до конца при мёртвом облаке [CORE-017], [LLM-010]."""
    gateway = _gateway(stage_cascades={"extract": ["первая", "вторая"]})
    assert _chain(gateway, "extract") == [
        ("proxy", "первая"),
        ("proxy", "вторая"),
        ("local", llm.FAST),
    ]


def test_каскад_не_длиннее_трёх_кандидатов() -> None:
    """Четвёртый поход в сеть на одну вакансию дороже пропуска этапа."""
    assert llm.parse_models("a, b, b, c, d") == ["a", "b", "c"]
    assert llm.parse_models(" ") == []


def test_персональные_этапы_каскадом_наружу_не_уходят() -> None:
    """ПД остаются на локальном адресе, пока владелец не разрешил иное [CORE-012]."""
    gateway = _gateway(stage_cascades={"draft": ["облачная-1", "облачная-2"]})
    assert _chain(gateway, "draft") == [("local", llm.LOCAL)]
    allowed = _gateway(
        stage_cascades={"draft": ["облачная-1", "облачная-2"]}, personal_via_proxy=True
    )
    assert _chain(allowed, "draft") == [
        ("proxy", "облачная-1"),
        ("proxy", "облачная-2"),
        ("local", llm.LOCAL),
    ]


def test_у_эмбеддингов_каскада_нет() -> None:
    """Модель векторов пиннится: «фолбэк» здесь портит базу молча [LLM-011]."""
    gateway = _gateway(stage_cascades={"embeddings": ["чужая-1", "чужая-2"]})
    assert _chain(gateway, "embeddings") == [("local", llm.EMBEDDINGS)]


def test_квота_выбивает_кандидата_до_конца_прогона() -> None:
    """429 — не повод ждать: следующий кандидат отвечает сразу [CORE-016]."""
    seen: list[str] = []
    gateway = _gateway(stage_cascades={"extract": ["занятая", "свободная"]})

    def http_call(route: llm.Route, messages: Sequence[dict[str, str]], t: float) -> str:
        seen.append(route.model)
        if route.model == "занятая":
            raise llm.ApiError(429, "rate limit")
        return "ответ"

    gateway._http_call = http_call  # type: ignore[method-assign]
    assert gateway.complete("extract", MESSAGES) == "ответ"
    # Одна попытка занятой, без повторов, и сразу следующая.
    assert seen == ["занятая", "свободная"]
    assert gateway.usage.degraded == 1
    # В этом же прогоне занятую больше не спрашивают.
    assert _chain(gateway, "extract") == [("proxy", "свободная"), ("local", llm.FAST)]


def test_последний_кандидат_получает_полный_backoff() -> None:
    """Повторять одно и то же осмысленно, только когда следующего нет."""
    seen: list[str] = []
    gateway = _gateway(proxy_base_url="", stage_cascades={"extract": ["неважно"]})

    def http_call(route: llm.Route, messages: Sequence[dict[str, str]], t: float) -> str:
        seen.append(route.model)
        raise llm.ApiError(503, "сервис лежит")

    gateway._http_call = http_call  # type: ignore[method-assign]
    assert gateway.complete("extract", MESSAGES) is None
    assert len(seen) == 3


def test_кэш_проверяется_по_всему_каскаду(conn: sqlite3.Connection) -> None:
    """Ответ сильнейшего кандидата не должен пропадать при фолбэке [LLM-006]."""
    gateway = _gateway(conn=conn, stage_cascades={"extract": ["первая", "вторая"]})

    def http_call(route: llm.Route, messages: Sequence[dict[str, str]], t: float) -> str:
        if route.model == "первая":
            raise llm.ApiError(429, "rate limit")
        return "ответ второй"

    gateway._http_call = http_call  # type: ignore[method-assign]
    assert gateway.complete("extract", MESSAGES) == "ответ второй"

    # Новый прогон: первая снова в строю, но её ответа в кэше нет, а ответ
    # второй лежит и должен найтись без единого вызова.
    fresh = _gateway(conn=conn, stage_cascades={"extract": ["первая", "вторая"]})
    fresh._http_call = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("при попадании в кэш вызова быть не должно")
    )
    assert fresh.complete("extract", MESSAGES) == "ответ второй"
    assert fresh.usage.cached == 1


def test_бюджет_считает_попытки_а_не_успехи() -> None:
    """Три кандидата на упавшем провайдере — это три реальных вызова [LLM-004]."""
    gateway = _gateway(
        stage_cascades={"extract": ["первая", "вторая"]}, max_calls=2
    )

    def http_call(route: llm.Route, messages: Sequence[dict[str, str]], t: float) -> str:
        raise llm.ApiError(500, "упал")

    gateway._http_call = http_call  # type: ignore[method-assign]
    assert gateway.complete("extract", MESSAGES) is None
    assert gateway.usage.calls == 2


def test_порядок_каскада_берётся_из_бенча() -> None:
    """Балл по убыванию, при разнице меньше 0.05 — быстрый вперёд."""
    rows = [
        bench.Row("точная-медленная", "extract", "к1", 1.0, "", 8.0),
        bench.Row("точная-быстрая", "extract", "к1", 0.98, "", 0.7),
        bench.Row("врушка", "extract", "к1", 0.3, "выдумала вилку", 0.2),
    ]
    chain = [model for model, _, _ in bench.cascades(rows)["extract"]]
    assert chain == ["точная-быстрая", "точная-медленная", "врушка"]
    assert bench.recommend(rows)["extract"][0] == "точная-быстрая"
