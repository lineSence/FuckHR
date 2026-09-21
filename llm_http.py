"""HTTP к OpenAI-совместимому адресу: один чат-вызов и список моделей.

Отдельный файл, потому что `llm.py` упёрся в 25 КБ [CORE-024]. Граница
проведена по смыслу: здесь «как сходить по HTTP и разобрать ответ», в `llm.py`
остались «куда идти, что класть в кэш и когда сдаваться». Публичные имена
(`ApiError`, `RETRY_STATUSES`, `error_message`) по-прежнему доступны через
`llm`, вызовы и тесты переписывать не нужно.

Об ошибках адреса. 4xx и 5xx разные по природе: 500, 502, 503, таймаут и 429 —
состояние мира, оно меняется, повтор осмыслен; 400 и 404 — суждение о самом
запросе (нет такой модели, не тот формат, длинный контекст), и повторять их
значит втрое дольше ждать того же отказа. Причина живёт в теле ответа, а не в
статусе, поэтому тело читается и попадает в лог.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

log = logging.getLogger(__name__)

# Коды, при которых повтор имеет смысл: это состояние сервиса, не запроса.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
MAX_ERROR_CHARS = 600


class ApiError(RuntimeError):
    """Ответ адреса с кодом ошибки и разобранным телом."""

    def __init__(self, status: int, message: str) -> None:
        self.status = int(status)
        self.message = message or "тело ответа пустое"
        super().__init__("HTTP {}: {}".format(self.status, self.message))

    @property
    def retryable(self) -> bool:
        return self.status in RETRY_STATUSES


def error_message(payload: Any, fallback: str = "") -> str:
    """Вытаскивает человеческую причину из тела ошибки.

    OpenAI-совместимые сервисы отвечают {"error": {"message": ...}}, LiteLLM
    иногда кладёт текст в detail, а Ollama — просто в error строкой.
    """
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:MAX_ERROR_CHARS]
            if isinstance(value, dict):
                for inner in ("message", "detail", "code", "type"):
                    text = value.get(inner)
                    if isinstance(text, str) and text.strip():
                        return text.strip()[:MAX_ERROR_CHARS]
            if isinstance(value, list) and value:
                return json.dumps(value, ensure_ascii=False)[:MAX_ERROR_CHARS]
        return json.dumps(payload, ensure_ascii=False)[:MAX_ERROR_CHARS]
    return (fallback or "").strip()[:MAX_ERROR_CHARS]


def chat(
    route: Any, messages: Sequence[dict[str, str]], temperature: float, timeout: float
) -> str:
    """Один вызов /chat/completions. Ошибка адреса — ApiError."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if route.api_key:
        headers["Authorization"] = f"Bearer {route.api_key}"
    response = httpx.post(
        f"{route.base_url}/chat/completions",
        json={
            "model": route.model,
            "messages": list(messages),
            "temperature": temperature,
        },
        headers=headers,
        timeout=timeout,
    )
    if response.status_code >= 400:
        # Статус без тела бесполезен: всё по делу — имя модели, лимит контекста,
        # неверный параметр — лежит в теле ответа.
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        raise ApiError(response.status_code, error_message(payload, response.text))
    # Без этой строки невозможно узнать, какая модель сгенерировала ответ.
    log.info(
        "llm %s/%s -> %s",
        route.name,
        route.model,
        response.headers.get("X-Routed-Via")
        or response.headers.get("x-litellm-model-id")
        or "неизвестно",
    )
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        # Пустой choices бывает при сработавшем фильтре провайдера.
        raise ApiError(200, error_message(payload, "ответ без choices"))
    return (choices[0].get("message") or {}).get("content") or ""


def fetch_models(base_url: str, api_key: str | None, timeout: float) -> list[str]:
    """Имена моделей с адреса (GET /models) — проверка живости.

    Ошибка сети — пустой список, а не исключение: это диагностика, а не работа.
    """
    import httpx

    if not base_url:
        return []
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = httpx.get(f"{base_url}/models", headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — диагностика не должна ронять вызывающего
        log.warning("адрес %s не отдал список моделей: %s", base_url, exc)
        return []
    items = payload.get("data") or []
    return [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")]


__all__ = (
    "ApiError",
    "MAX_ERROR_CHARS",
    "RETRY_STATUSES",
    "chat",
    "error_message",
    "fetch_models",
)
