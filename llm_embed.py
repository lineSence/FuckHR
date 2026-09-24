"""Вызов /v1/embeddings через маршрут шлюза.

Почему отдельный файл, а не метод `Gateway`. `llm.py` упёрся в 25 КБ
[CORE-024], и это тот же приём, что с `llm_batch.py` и `llm_profiles.py`.
Правило `[CORE-010]` не нарушено: адрес, ключ и имя модели берутся у шлюза
(`route_for("embeddings")`), своего списка провайдеров модуль не знает и
собственной конфигурации не имеет.

Этап `embeddings` в `PERSONAL_STAGES` не внесён: в векторизацию уходят описания
вакансий и тексты отзывов — публичные тексты, не данные о людях [CORE-012].

Модель нельзя менять на живой базе: векторы разных моделей несравнимы
[LLM-011]. Поэтому имя модели возвращается вместе с векторами и пишется рядом
с каждой строкой.
"""

from __future__ import annotations

import logging
from typing import Sequence

import diag

STAGE = "embeddings"

# Больше 32 текстов за раз локальный эмбеддер на 8 ГБ VRAM переваривает хуже,
# чем два запроса подряд.
BATCH = 16

log = logging.getLogger("fuckhr")


def model_name(gateway: object | None) -> str:
    """Имя модели, которой считаются векторы. Пусто — считать негде.

    Имя берётся из маршрута: для прокси это LLM_STAGE_MODEL_EMBEDDINGS или имя
    профиля, для локального адреса — LLM_LOCAL_STAGE_MODEL_EMBEDDINGS или
    LLM_LOCAL_MODEL_EMBEDDINGS.

    Когда ничего не задано, `Route.model` равен имени профиля (`embeddings`),
    а это не имя модели: Ollama на `model=embeddings` отвечает «model not found».
    Такой ответ считается «имя не задано»; для совместимости проверяется ещё
    LLM_STAGE_MODEL_EMBEDDINGS — до этой версии локальное имя жило там.
    """
    route = getattr(gateway, "route_for", lambda _stage: None)(STAGE)
    if route is None:
        return ""
    name = str(getattr(route, "model", "") or "")
    if name and name != STAGE:
        return name
    stage_models = getattr(gateway, "stage_models", {}) or {}
    return str(stage_models.get(STAGE, "") or "")


def embed(gateway: object | None, texts: Sequence[str]) -> list[list[float]] | None:
    """Векторы для текстов или None, если считать негде или не вышло.

    None — штатный ответ, а не ошибка: без эмбеддера пайплайн теряет два
    сигнала и доходит до конца [CORE-017].
    """
    if gateway is None or not texts:
        return None
    route = gateway.route_for(STAGE)  # type: ignore[attr-defined]
    model = model_name(gateway)
    if route is None or not model:
        log.info(
            "эмбеддинги пропущены: не задан маршрут этапа %s или имя модели "
            "(LLM_LOCAL_STAGE_MODEL_EMBEDDINGS)",
            STAGE,
        )
        return None

    import httpx

    headers = {"Content-Type": "application/json"}
    if route.api_key:
        headers["Authorization"] = "Bearer {}".format(route.api_key)

    out: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        batch = [text or " " for text in texts[start : start + BATCH]]
        try:
            response = httpx.post(
                "{}/embeddings".format(route.base_url),
                json={"model": model, "input": batch},
                headers=headers,
                timeout=getattr(gateway, "timeout", 60.0),
            )
            if response.status_code >= 400:
                log.warning(
                    "эмбеддер %s/%s ответил %s: %s",
                    route.name,
                    model,
                    response.status_code,
                    response.text[:200],
                )
                diag.model_failed(
                    STAGE,
                    "embeddings",
                    "HTTP {}".format(response.status_code),
                    маршрут=route.name,
                    модель=model,
                    текстов=len(batch),
                )
                if response.status_code < 500:
                    # Отказ по сути запроса (обычно «нет такой модели») не
                    # исправится ни на втором батче, ни на второй вакансии.
                    getattr(gateway, "reject", lambda *_a: None)(
                        route.name, model, "HTTP {}".format(response.status_code)
                    )
                return None
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — модель не роняет прогон [CORE-017]
            diag.model_failed(
                STAGE, "embeddings", exc, маршрут=route.name, модель=model, текстов=len(batch)
            )
            log.warning("эмбеддер недоступен: %s", exc)
            return None

        data = payload.get("data") or []
        if len(data) != len(batch):
            log.warning(
                "эмбеддер вернул %s векторов на %s текстов — пропускаем",
                len(data),
                len(batch),
            )
            return None
        for item in data:
            vector = item.get("embedding") or []
            if not vector:
                log.warning("эмбеддер вернул пустой вектор")
                return None
            out.append([float(value) for value in vector])
        usage = getattr(gateway, "usage", None)
        if usage is not None:
            usage.calls += 1
        # Батч эмбеддингов — такой же вызов модели и такая же строка бюджета,
        # как ответ на этапе. Без события «вызовов модели» в итоге прогона не
        # сходилось с числом событий в файле диагностики.
        diag.event(
            "модель",
            этап=STAGE,
            профиль="embeddings",
            исход="ответ",
            маршрут=route.name,
            модель=model,
            текстов=len(batch),
        )
    return out


__all__ = ("BATCH", "STAGE", "embed", "model_name")
