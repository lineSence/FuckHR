"""Локальный адрес: у каждого этапа своё имя модели.

До этих проверок на локальный адрес всегда уходило название профиля
(`auto:fast`), а Ollama на такое имя отвечает «model not found». Здесь закреплён
порядок выбора имени: этап сильнее профиля, пусто везде — старое поведение.
"""

from __future__ import annotations

import llm
import llm_embed

LOCAL_URL = "http://127.0.0.1:8080/v1"
PROXY_URL = "http://127.0.0.1:4000/v1"


def шлюз(**kwargs) -> llm.Gateway:
    return llm.Gateway(base_url=LOCAL_URL, **kwargs)


def test_без_настроек_уходит_имя_профиля() -> None:
    """Старое поведение должно сохраниться: шлюз с одной моделью имя игнорирует."""
    assert шлюз().local_model_for("extract") == (llm.FAST, "по умолчанию")


def test_имя_профиля_берётся_из_настроек() -> None:
    """Одно имя на класс задач — минимальная настройка для нескольких моделей."""
    gateway = шлюз(local_models={llm.FAST: "qwen3:4b"})
    assert gateway.local_model_for("extract") == ("qwen3:4b", "профиль")


def test_имя_этапа_сильнее_имени_профиля() -> None:
    """Извлечению полей хватает 4b, черновику письма нужна 8b."""
    gateway = шлюз(
        local_models={llm.FAST: "qwen3:4b"},
        local_stage_models={"extract": "qwen3:8b"},
    )
    assert gateway.local_model_for("extract") == ("qwen3:8b", "этап")


def test_локальный_кандидат_каскада_знает_своё_имя() -> None:
    """Имя должно дойти до самого запроса, а не остаться в справке для интерфейса."""
    gateway = шлюз(
        proxy_base_url=PROXY_URL,
        proxy_models={llm.FAST: "gpt-4o-mini"},
        local_models={llm.FAST: "qwen3:4b"},
    )
    chain = gateway.cascade_for("extract")
    pairs = [(route.name, route.model) for route in chain]
    assert (llm.ROUTE_LOCAL, "qwen3:4b") in pairs
    assert (llm.ROUTE_PROXY, "gpt-4o-mini") in pairs


def test_персональный_этап_остаётся_на_локальной_модели() -> None:
    """Черновик письма видит ФИО [CORE-012]: маршрут локальный, имя — своё."""
    gateway = шлюз(
        proxy_base_url=PROXY_URL,
        local_stage_models={"draft": "qwen3:8b"},
    )
    route = gateway.route_for("draft")
    assert route is not None
    assert (route.name, route.model) == (llm.ROUTE_LOCAL, "qwen3:8b")


def test_таблица_маршрутов_показывает_откуда_имя() -> None:
    """На странице «Модель» видно, почему в запрос уйдёт именно это имя."""
    gateway = шлюз(
        local_models={llm.FAST: "qwen3:4b"},
        local_stage_models={"draft": "qwen3:8b"},
    )
    rows = {row[0]: row for row in gateway.describe_routes()}
    assert rows["extract"][3:] == ("qwen3:4b", "профиль")
    assert rows["draft"][3:] == ("qwen3:8b", "этап")
    assert rows["score"][4] == "по умолчанию"


def test_имена_читаются_из_окружения(monkeypatch) -> None:
    """Имена живут в .env рядом с адресом локального сервера."""
    monkeypatch.setenv("LLM_BASE_URL", LOCAL_URL)
    monkeypatch.setenv("LLM_LOCAL_MODEL_FAST", "qwen3:4b")
    monkeypatch.setenv("LLM_LOCAL_MODEL_SMART", "  ")
    monkeypatch.setenv("LLM_LOCAL_STAGE_MODEL_DRAFT", "qwen3:8b")
    gateway = llm.Gateway.from_env()
    assert gateway.local_models == {llm.FAST: "qwen3:4b"}
    assert gateway.local_stage_models == {"draft": "qwen3:8b"}


def test_эмбеддинги_берут_локальное_имя() -> None:
    """Модель векторов пишется рядом с каждой строкой [LLM-011]."""
    gateway = шлюз(local_stage_models={"embeddings": "bge-m3"})
    assert llm_embed.model_name(gateway) == "bge-m3"


def test_эмбеддинги_помнят_старую_переменную() -> None:
    """До этой версии локальное имя жило в LLM_STAGE_MODEL_EMBEDDINGS."""
    gateway = шлюз(stage_models={"embeddings": "bge-m3"})
    assert llm_embed.model_name(gateway) == "bge-m3"


def test_без_имени_эмбеддинги_выключены() -> None:
    """Название профиля — не имя модели: лучше пропустить сигнал [CORE-017]."""
    assert llm_embed.model_name(шлюз()) == ""
