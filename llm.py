"""Единая точка входа к моделям: локальный шлюз и внешний прокси (ADR-005, ADR-017).

Код знает два OpenAI-совместимых адреса: локальный (FreeLLMAPI/Ollama) и внешний
прокси (LiteLLM через SSH-туннель). Списки моделей, цепочки фолбэков и ключи
провайдеров живут в конфиге шлюза и прокси, а не здесь.

Здесь разведены два разных вопроса, которые раньше были одним:

- *профиль* — какого класса модель нужна этапу (STAGE_PROFILES, [LLM-003]);
- *маршрут* — на какой из двух адресов запрос уходит физически.

Разделение нужно из-за персональных данных. Этапы contacts/dossier/draft видят ФИО и
контакты живых людей [CORE-012]. По умолчанию они остаются на локальном
адресе даже при настроенном прокси. Владелец может разрешить им уходить наружу:

    LLM_PERSONAL_VIA_PROXY=1

— но только явно и с записью в лог каждого такого вызова. Обратите внимание:
прокси на своём VPS сам по себе приватности не даёт — важно, куда он сам ходит.
Если в его config.yaml стоят облачные провайдеры, ФИО уйдут им.

Остальное как было: кэш по хэшу промпта всегда включён [LLM-006], любая ошибка
возвращает None, а не исключение [CORE-017], [LLM-009].

О каскаде. У этапа теперь не одна модель, а до трёх кандидатов в порядке
бенча плюс локальный адрес последним слотом (`llm_cascade`, ADR-022). Кэш
проверяется по всему каскаду сверху вниз: ответ сильнейшего кандидата не
должен пропадать оттого, что в этом прогоне спрашивали второго [LLM-006].
Повторы к одному кандидату остались только там, где следующего нет: пока
кандидат в списке не последний, на ошибку сервиса тратится одна попытка,
а не три.

Об ошибках адреса. Ответы 4xx и 5xx разные по природе, и обращаться с ними надо
по-разному. 500, 502, 503, таймаут и 429 — состояние мира, оно меняется, повтор
осмыслен. 400 и 404 — суждение о самом запросе: нет такой модели, не тот формат,
слишком длинный контекст. Повторять такое три раза — втрое дольше ждать того же
отказа, поэтому такие ответы признаются окончательными сразу.

И главное: причина отказа живёт в теле ответа, а не в статусе. Без неё запись
«400 Bad Request» сообщает ровно ничего, поэтому тело читается и попадает в лог.

Настройка прокси:

    LLM_PROXY_BASE_URL=http://127.0.0.1:4000/v1
    LLM_PROXY_API_KEY=sk-...
    LLM_PROXY_MODEL_FAST=gpt-4o-mini
    LLM_PROXY_MODEL_SMART=claude-3-7-sonnet
    LLM_PROXY_MODEL_LONG=gemini-1.5-pro
    LLM_PROXY_MODEL_LOCAL=qwen3:8b

Имена берутся из model_name в config.yaml прокси. Если имя для профиля не задано,
в запрос уйдёт само название профиля (auto:fast, local-only и так далее) — и если
такого алиаса у прокси нет, он ответит 400. Об этом предупреждает warn_unmapped().
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from llm_profiles import (  # noqa: F401 — публичные имена остаются в llm
    EMBEDDINGS,
    FAST,
    LOCAL,
    LOCAL_FIRST_STAGES,
    LONG,
    MAX_CANDIDATES,
    PERSONAL_STAGES,
    PROXY_MODEL_ENV,
    ROUTE_LOCAL,
    ROUTE_PROXY,
    SMART,
    STAGE_MODEL_ENV,
    STAGE_MODELS_ENV,
    STAGE_PROFILES,
    ProfileError,
    profile_for,
)
import llm_cache
from llm_cache import CACHE_SCHEMA, ensure_cache  # noqa: F401 — публичные имена остаются в llm
from llm_cascade import Dropped, Route, build_chain, parse_models

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


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    failures: int = 0
    skipped: int = 0
    degraded: int = 0


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


class Gateway:
    """Тонкий клиент к двум адресам. Без обоих выключен и всегда возвращает None."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        conn: sqlite3.Connection | None = None,
        timeout: float = 60.0,
        max_calls: int = 300,
        transport: Callable[[str, Sequence[dict[str, str]], float], str] | None = None,
        backoff: Sequence[float] = (0.5, 2.0, 5.0),
        proxy_base_url: str | None = None,
        proxy_api_key: str | None = None,
        proxy_models: dict[str, str] | None = None,
        stage_models: dict[str, str] | None = None,
        stage_cascades: dict[str, list[str]] | None = None,
        personal_via_proxy: bool = False,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.proxy_base_url = (proxy_base_url or "").rstrip("/")
        self.proxy_api_key = proxy_api_key
        self.proxy_models = dict(proxy_models or {})
        self.stage_models = dict(stage_models or {})
        self.stage_cascades = {k: list(v) for k, v in (stage_cascades or {}).items()}
        self.personal_via_proxy = bool(personal_via_proxy)
        self.timeout = timeout
        self.max_calls = max_calls
        self.backoff = tuple(backoff)
        self.usage = Usage()
        self._transport = transport
        self.conn = conn
        # Кандидаты, выбывшие до конца прогона: отказ по сути запроса (400/404)
        # и исчерпанная квота (429). Подробности — llm_cascade.Dropped.
        self._dropped = Dropped()
        if conn is not None:
            ensure_cache(conn)

    @classmethod
    def from_env(cls, conn: sqlite3.Connection | None = None) -> "Gateway":
        models = {}
        for profile, name in PROXY_MODEL_ENV.items():
            value = os.getenv(name)
            if value:
                models[profile] = value.strip()
        stages = {}
        for stage, name in STAGE_MODEL_ENV.items():
            value = os.getenv(name)
            if value:
                stages[stage] = value.strip()
        cascades = {}
        for stage, name in STAGE_MODELS_ENV.items():
            chain = parse_models(os.getenv(name))
            if chain:
                cascades[stage] = chain
        return cls(
            base_url=os.getenv("LLM_BASE_URL") or None,
            api_key=os.getenv("LLM_API_KEY") or None,
            conn=conn,
            timeout=float(os.getenv("LLM_TIMEOUT", "60")),
            max_calls=int(os.getenv("LLM_MAX_CALLS", "300")),
            proxy_base_url=os.getenv("LLM_PROXY_BASE_URL") or None,
            proxy_api_key=os.getenv("LLM_PROXY_API_KEY") or None,
            proxy_models=models,
            stage_models=stages,
            stage_cascades=cascades,
            personal_via_proxy=(os.getenv("LLM_PERSONAL_VIA_PROXY", "") or "").strip()
            in {"1", "true", "yes", "on"},
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url or self.proxy_base_url)

    @property
    def disabled_reason(self) -> str:
        return "не задан ни LLM_BASE_URL, ни LLM_PROXY_BASE_URL"

    def _proxy_model(self, profile: str) -> str:
        """Имя модели на прокси; по умолчанию — сам профиль.

        У LiteLLM имена задаёт его config.yaml, и там можно объявить алиасы
        auto:fast / auto:smart / auto:long — тогда настраивать здесь нечего.
        """
        return self.proxy_models.get(profile, profile)

    def model_for(self, stage: str) -> tuple[str, str]:
        """Имя модели на прокси для этапа и откуда оно взялось.

        Имя этапа сильнее имени профиля: профиль — это класс задачи, а победитель
        бенчмарка считается по этапу (`bench.recommend`).
        """
        chain = self.stage_cascades.get(stage)
        if chain:
            return chain[0], "каскад"
        name = self.stage_models.get(stage)
        if name:
            return name, "этап"
        return self._proxy_model(profile_for(stage)), "профиль"

    def models_for(self, stage: str) -> list[str]:
        """Имена моделей на прокси по порядку: каскад целиком или одно имя."""
        chain = self.stage_cascades.get(stage)
        if chain:
            return list(chain)
        return [self.model_for(stage)[0]]

    def unmapped_profiles(self) -> list[tuple[str, str, str]]:
        """Профили без явного имени модели: (профиль, что уйдёт, переменная).

        Главная причина 400 от прокси: мы просим модель «local-only», а такого
        имени в его config.yaml нет.
        """
        out = []
        for profile, env_name in PROXY_MODEL_ENV.items():
            if profile not in self.proxy_models:
                out.append((profile, profile, env_name))
        return out

    def warn_unmapped(self, profiles: Sequence[str] = ()) -> None:
        """Предупреждает о профилях, для которых имя модели не задано."""
        if not self.proxy_base_url:
            return
        wanted = set(profiles) if profiles else None
        for profile, sent, env_name in self.unmapped_profiles():
            if wanted is not None and profile not in wanted:
                continue
            log.warning(
                "для профиля %s имя модели не задано, на прокси уйдёт model=%r; "
                "если такого алиаса в config.yaml нет, будет 400 — задай %s",
                profile,
                sent,
                env_name,
            )

    def reject(self, route: str, model: str, reason: str) -> None:
        """Запомнить отказ по сути запроса: пара (маршрут, модель) больше не берётся.

        Нужно снаружи: `/embeddings` живёт в `llm_embed` [CORE-024], а 400 от
        прокси там означает то же, что и в чате, — конфиг прокси этой модели не
        знает, и в этом прогоне не узнает.
        """
        self._dropped.add(route, model, reason)

    def cascade_for(self, stage: str) -> list[Route]:
        """Кандидаты этапа по порядку. Пустой список — считать негде.

        Вся логика порядка — в `llm_cascade.build_chain`: здесь только сбор
        входных данных из окружения шлюза [CORE-024].
        """
        local = (
            Route(ROUTE_LOCAL, self.base_url, self.api_key, profile_for(stage))
            if self.base_url
            else None
        )
        return build_chain(
            stage,
            local,
            self.models_for(stage),
            self.proxy_base_url,
            self.proxy_api_key,
            self._dropped,
            self.personal_via_proxy,
        )

    def route_for(self, stage: str) -> Route | None:
        """Первый кандидат этапа. None — считать негде."""
        chain = self.cascade_for(stage)
        return chain[0] if chain else None

    def describe_routes(self) -> list[tuple[str, str, str, str, str]]:
        """(этап, профиль, маршрут, модель, откуда имя) — для интерфейса и логов."""
        out = []
        for stage in STAGE_PROFILES:
            route = self.route_for(stage)
            source = "профиль"
            if route is not None and route.is_proxy:
                source = self.model_for(stage)[1]
            out.append(
                (
                    stage,
                    profile_for(stage),
                    route.name if route else "нет маршрута",
                    route.model if route else "—",
                    source,
                )
            )
        return out

    def models(self, route: str = ROUTE_PROXY) -> list[str]:
        """Список моделей с адреса (GET /models) — проверка живости.

        Ошибка сети — пустой список, а не исключение: это диагностика, а не работа.
        """
        import httpx

        base = self.proxy_base_url if route == ROUTE_PROXY else self.base_url
        key = self.proxy_api_key if route == ROUTE_PROXY else self.api_key
        if not base:
            return []
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            response = httpx.get(
                f"{base}/models", headers=headers, timeout=min(self.timeout, 15.0)
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — диагностика не должна ронять вызывающего
            log.warning("адрес %s не отдал список моделей: %s", base, exc)
            return []
        items = payload.get("data") or []
        return [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")]

    def _cache_get(self, digest: str) -> str | None:
        return llm_cache.get(self.conn, digest)

    def _cache_put(self, digest: str, stage: str, profile: str, response: str) -> None:
        llm_cache.put(self.conn, digest, stage, profile, response)

    def _http_call(
        self, route: Route, messages: Sequence[dict[str, str]], temperature: float
    ) -> str:
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
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            # Статус без тела бесполезен: всё по делу — имя модели, лимит контекста,
            # неверный параметр — лежит в теле ответа.
            try:
                payload: Any = response.json()
            except ValueError:
                payload = None
            raise ApiError(
                response.status_code, error_message(payload, response.text)
            )
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

    def complete(
        self,
        stage: str,
        messages: Sequence[dict[str, str]],
        temperature: float = 0.0,
    ) -> str | None:
        profile = profile_for(stage)
        chain = self.cascade_for(stage)
        if not chain:
            self.usage.skipped += 1
            log.debug("этап %s пропущен: %s", stage, self.disabled_reason)
            return None

        digests = [
            llm_cache.digest(profile, messages, temperature, route.name, route.model)
            for route in chain
        ]
        # Кэш проверяется по всему каскаду: ответ сильнейшего кандидата не
        # должен пропадать оттого, что сегодня спрашивают второго [LLM-006].
        for digest in digests:
            cached = self._cache_get(digest)
            if cached is not None:
                self.usage.cached += 1
                return cached

        if self.usage.calls >= self.max_calls:
            # Лимит «умного» профиля — 100–300 вызовов в сутки [LLM-004].
            self.usage.skipped += 1
            log.warning("бюджет вызовов исчерпан (%s), этап %s пропущен", self.max_calls, stage)
            return None

        for position, route in enumerate(chain):
            last = position == len(chain) - 1
            text = self._attempt(route, stage, profile, messages, temperature, last)
            if text is None:
                continue
            if position:
                # Ответ пришёл не от лучшей модели: это деградация качества,
                # и она должна быть видна, а не выглядеть обычным прогоном.
                self.usage.degraded += 1
                log.warning(
                    "этап %s сделан кандидатом %s из %s (%s/%s)",
                    stage,
                    position + 1,
                    len(chain),
                    route.name,
                    route.model,
                )
            self._cache_put(digests[position], stage, profile, text)
            return text
        return None

    def _attempt(
        self,
        route: Route,
        stage: str,
        profile: str,
        messages: Sequence[dict[str, str]],
        temperature: float,
        last: bool,
    ) -> str | None:
        """Один кандидат. None — не вышло, пора к следующему.

        Повторять одну и ту же модель имеет смысл, только когда следующей нет:
        пока кандидат не последний, на ошибку сервиса тратится одна попытка,
        а не весь backoff [CORE-016].
        """
        tries = self.backoff if last else self.backoff[:1]
        for attempt, pause in enumerate(tries, start=1):
            if self.usage.calls >= self.max_calls:
                return None
            # Считаются попытки, а не успехи: каскад из трёх кандидатов на
            # упавшем провайдере — это три реальных похода в сеть, и бюджет
            # обязан их видеть [LLM-004].
            self.usage.calls += 1
            try:
                if self._transport is not None:
                    return self._transport(profile, messages, temperature)
                return self._http_call(route, messages, temperature)
            except Exception as exc:  # noqa: BLE001 — модель не должна ронять прогон
                out = isinstance(exc, ApiError) and (
                    not exc.retryable or exc.status == 429
                )
                log.warning(
                    "%s/%s ответил ошибкой (%s/%s, этап %s): %s",
                    route.name,
                    route.model,
                    attempt,
                    len(tries),
                    stage,
                    exc,
                )
                if out:
                    # 400/404 — отказ по сути запроса, 429 — кончилась квота.
                    # И то и другое внутри прогона не меняется [ADR-022].
                    self.usage.failures += 1
                    self._dropped.add(route.name, route.model, str(exc))
                    if getattr(exc, "status", 0) != 429:
                        log.error(
                            "%s отклонил запрос с model=%r — повторы не помогут. "
                            "Проверь, есть ли такое имя в его config.yaml "
                            "(GET /models), и задай его в %s",
                            route.name,
                            route.model,
                            PROXY_MODEL_ENV.get(profile, "настройках моделей"),
                        )
                    return None
                if attempt == len(tries):
                    self.usage.failures += 1
                    return None
                time.sleep(pause)
        return None


__all__ = (
    "ApiError",
    "Dropped",
    "EMBEDDINGS",
    "FAST",
    "Gateway",
    "LOCAL",
    "LOCAL_FIRST_STAGES",
    "LONG",
    "MAX_CANDIDATES",
    "PERSONAL_STAGES",
    "PROXY_MODEL_ENV",
    "STAGE_MODEL_ENV",
    "STAGE_MODELS_ENV",
    "ProfileError",
    "RETRY_STATUSES",
    "ROUTE_LOCAL",
    "ROUTE_PROXY",
    "Route",
    "SMART",
    "STAGE_PROFILES",
    "Usage",
    "build_chain",
    "ensure_cache",
    "error_message",
    "parse_models",
    "profile_for",
)
