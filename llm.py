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
возвращает None, а не исключение [CORE-017], [LLM-009]. Сам HTTP живёт в
`llm_http` [CORE-024]: здесь решается куда идти, там — как сходить.

О каскаде. У этапа теперь не одна модель, а до трёх кандидатов в порядке
бенча плюс локальный адрес последним слотом (`llm_cascade`, ADR-022). Кэш
проверяется по всему каскаду сверху вниз: ответ сильнейшего кандидата не
должен пропадать оттого, что в этом прогоне спрашивали второго [LLM-006].
Повторы к одному кандидату остались только там, где следующего нет: пока
кандидат в списке не последний, на ошибку сервиса тратится одна попытка,
а не три.

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

То же самое для локального адреса — раньше туда всегда уходило имя профиля:

    LLM_LOCAL_MODEL_FAST=qwen3:4b
    LLM_LOCAL_MODEL_SMART=qwen3:8b
    LLM_LOCAL_MODEL_LOCAL=qwen3:8b
    LLM_LOCAL_STAGE_MODEL_DRAFT=qwen3:8b

Имя этапа сильнее имени профиля, пусто везде — старое поведение (в запрос
уходит название профиля). Список живых имён показывает `models(ROUTE_LOCAL)`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from llm_profiles import (  # noqa: F401 — публичные имена остаются в llm
    EMBEDDINGS,
    FAST,
    LOCAL,
    LOCAL_FIRST_STAGES,
    LOCAL_MODEL_ENV,
    LOCAL_STAGE_MODEL_ENV,
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
import diag
import llm_cache
from llm_cache import CACHE_SCHEMA, ensure_cache  # noqa: F401 — публичные имена остаются в llm
from llm_budget import Budget, Usage  # noqa: F401 — Usage остаётся публичным именем llm
from llm_cascade import Dropped, Route, build_chain, parse_models
from llm_http import (  # noqa: F401 — публичные имена остаются в llm
    MAX_ERROR_CHARS,
    RETRY_STATUSES,
    ApiError,
    chat,
    error_message,
    fetch_models,
)

log = logging.getLogger(__name__)




def _env_map(names: dict[str, str]) -> dict[str, str]:
    """{ключ маршрутизации: имя модели} из окружения, пустые выброшены."""
    out: dict[str, str] = {}
    for key, env_name in names.items():
        value = (os.getenv(env_name) or "").strip()
        if value:
            out[key] = value
    return out


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
        local_models: dict[str, str] | None = None,
        local_stage_models: dict[str, str] | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.proxy_base_url = (proxy_base_url or "").rstrip("/")
        self.proxy_api_key = proxy_api_key
        self.proxy_models = dict(proxy_models or {})
        self.stage_models = dict(stage_models or {})
        self.local_models = dict(local_models or {})
        self.local_stage_models = dict(local_stage_models or {})
        self.stage_cascades = {k: list(v) for k, v in (stage_cascades or {}).items()}
        self.personal_via_proxy = bool(personal_via_proxy)
        self.timeout = timeout
        self.backoff = tuple(backoff)
        self._transport = transport
        self.conn = conn
        # Счётчик вызовов, потолок и кандидаты, выбывшие до конца прогона
        # (400/404 — отказ по сути запроса, 429 — кончилась квота), живут в
        # общем бюджете: шлюз создаётся на каждую вакансию, а прогон один
        # (ADR-022, docs/performance.md).
        self.budget = budget if budget is not None else Budget(max_calls)
        if conn is not None:
            ensure_cache(conn)

    @property
    def usage(self) -> Usage:
        return self.budget.usage

    @property
    def max_calls(self) -> int:
        return self.budget.max_calls

    @property
    def _dropped(self) -> Dropped:
        return self.budget.dropped

    @classmethod
    def from_env(
        cls, conn: sqlite3.Connection | None = None, budget: Budget | None = None
    ) -> "Gateway":
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
            proxy_models=_env_map(PROXY_MODEL_ENV),
            stage_models=_env_map(STAGE_MODEL_ENV),
            local_models=_env_map(LOCAL_MODEL_ENV),
            local_stage_models=_env_map(LOCAL_STAGE_MODEL_ENV),
            stage_cascades=cascades,
            personal_via_proxy=(os.getenv("LLM_PERSONAL_VIA_PROXY", "") or "").strip()
            in {"1", "true", "yes", "on"},
            budget=budget,
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

    def local_model_for(self, stage: str) -> tuple[str, str]:
        """Имя модели на локальном адресе для этапа и откуда оно взялось.

        Порядок тот же, что у прокси: этап сильнее профиля. Каскада здесь нет
        сознательно: локальный адрес — последний слот цепочки [LLM-010], и
        перебирать модели на 8 ГБ VRAM значит гонять веса туда-сюда без шансов
        на другой ответ (wiki/references/local-models.md).

        Пусто везде — в запрос уходит само название профиля, как было до
        появления этих переменных: шлюз с одной моделью имя игнорирует.
        """
        name = self.local_stage_models.get(stage)
        if name:
            return name, "этап"
        profile = profile_for(stage)
        name = self.local_models.get(profile)
        if name:
            return name, "профиль"
        return profile, "по умолчанию"

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
            Route(
                ROUTE_LOCAL, self.base_url, self.api_key, self.local_model_for(stage)[0]
            )
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
            source = "—"
            if route is not None:
                named = self.model_for if route.is_proxy else self.local_model_for
                source = named(stage)[1]
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

        Для локального адреса это тот же `ollama list`, только из интерфейса: имена
        оттуда и ставятся в LLM_LOCAL_MODEL_* и LLM_LOCAL_STAGE_MODEL_*.
        """
        if route == ROUTE_PROXY:
            base, key = self.proxy_base_url, self.proxy_api_key
        else:
            base, key = self.base_url, self.api_key
        return fetch_models(base, key, min(self.timeout, 15.0))

    def _cache_get(self, digest: str) -> str | None:
        return llm_cache.get(self.conn, digest)

    def _cache_put(
        self, digest: str, stage: str, profile: str, response: str, prompt: str = ""
    ) -> None:
        llm_cache.put(self.conn, digest, stage, profile, response, prompt)

    def _http_call(
        self, route: Route, messages: Sequence[dict[str, str]], temperature: float
    ) -> str:
        return chat(route, messages, temperature, self.timeout)

    def complete(
        self,
        stage: str,
        messages: Sequence[dict[str, str]],
        temperature: float = 0.0,
    ) -> str | None:
        profile = profile_for(stage)
        chain = self.cascade_for(stage)
        if not chain:
            self.budget.note("skipped")
            diag.model_skipped(stage, profile, self.disabled_reason)
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
                self.budget.note("cached")
                diag.event("модель", этап=stage, профиль=profile, исход="из кэша")
                return cached

        # Промах промаху рознь. «Из кэша 0» после прогона со ста попаданиями
        # обычно значит не поломку кэша, а смену модели: маршрут и модель
        # входят в ключ. Причина считается здесь и попадает в сводку прогона.
        prompt = llm_cache.prompt_digest(profile, messages, temperature)
        self.budget.note(
            "miss_model"
            if llm_cache.miss_reason(self.conn, prompt) == "model"
            else "miss_new"
        )

        if self.budget.spent:
            # Лимит «умного» профиля — 100–300 вызовов в сутки [LLM-004].
            self.budget.note("skipped")
            diag.model_skipped(stage, profile, "бюджет вызовов исчерпан")
            log.warning("бюджет вызовов исчерпан (%s), этап %s пропущен", self.max_calls, stage)
            return None

        for position, route in enumerate(chain):
            last = position == len(chain) - 1
            text = self._attempt(
                route, stage, profile, messages, temperature, last, position + 1
            )
            if text is None:
                continue
            if position:
                # Ответ пришёл не от лучшей модели: это деградация качества,
                # и она должна быть видна, а не выглядеть обычным прогоном.
                self.budget.note("degraded")
                log.warning(
                    "этап %s сделан кандидатом %s из %s (%s/%s)",
                    stage,
                    position + 1,
                    len(chain),
                    route.name,
                    route.model,
                )
            self._cache_put(digests[position], stage, profile, text, prompt)
            diag.event(
                "модель",
                этап=stage,
                профиль=profile,
                исход="ответ",
                маршрут=route.name,
                модель=route.model,
                кандидат=position + 1,
                символов=len(text or ""),
            )
            return text
        diag.event("модель", этап=stage, профиль=profile, исход="никто не ответил")
        return None

    def _attempt(
        self,
        route: Route,
        stage: str,
        profile: str,
        messages: Sequence[dict[str, str]],
        temperature: float,
        last: bool,
        number: int = 1,
    ) -> str | None:
        """Один кандидат. None — не вышло, пора к следующему.

        Повторять одну и ту же модель имеет смысл, только когда следующей нет:
        пока кандидат не последний, на ошибку сервиса тратится одна попытка,
        а не весь backoff [CORE-016].
        """
        tries = self.backoff if last else self.backoff[:1]
        for attempt, pause in enumerate(tries, start=1):
            # Считаются попытки, а не успехи: каскад из трёх кандидатов на
            # упавшем провайдере — это три реальных похода в сеть, и бюджет
            # обязан их видеть [LLM-004]. Взятие атомарно: потоки llm_batch
            # проверяют потолок одновременно.
            if not self.budget.take():
                diag.model_skipped(
                    stage,
                    profile,
                    "бюджет вызовов исчерпан",
                    маршрут=route.name,
                    модель=route.model,
                    кандидат=number,
                )
                return None
            try:
                if self._transport is not None:
                    return self._transport(profile, messages, temperature)
                return self._http_call(route, messages, temperature)
            except Exception as exc:  # noqa: BLE001 — модель не должна ронять прогон
                out = isinstance(exc, ApiError) and (
                    not exc.retryable or exc.status == 429
                )
                diag.model_failed(
                    stage,
                    profile,
                    exc,
                    маршрут=route.name,
                    модель=route.model,
                    кандидат=number,
                    попытка=attempt,
                    попыток=len(tries),
                    повторяемая=not out,
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
                    self.budget.note("failures")
                    self._dropped.add(route.name, route.model, str(exc))
                    if getattr(exc, "status", 0) != 429:
                        env_name = (
                            PROXY_MODEL_ENV.get(profile)
                            if route.is_proxy
                            else LOCAL_MODEL_ENV.get(profile)
                        )
                        log.error(
                            "%s отклонил запрос с model=%r — повторы не помогут. "
                            "Проверь, есть ли такое имя у него "
                            "(GET /models), и задай его в %s",
                            route.name,
                            route.model,
                            env_name or "настройках моделей",
                        )
                    return None
                if attempt == len(tries):
                    self.budget.note("failures")
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
    "LOCAL_MODEL_ENV",
    "LOCAL_STAGE_MODEL_ENV",
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
    "Budget",
    "Usage",
    "build_chain",
    "ensure_cache",
    "error_message",
    "fetch_models",
    "parse_models",
    "profile_for",
)
