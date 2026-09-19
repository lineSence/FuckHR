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

log = logging.getLogger(__name__)

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    hash        TEXT PRIMARY KEY,
    stage       TEXT NOT NULL,
    profile     TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

# Профили шлюза вместо model="auto" (wiki/architecture/model-routing.md).
FAST = "auto:fast"
SMART = "auto:smart"
LONG = "auto:long"
LOCAL = "local-only"
EMBEDDINGS = "embeddings"

# Этап пайплайна -> профиль. Новый этап обязан объявить свой профиль здесь:
# молчаливого дефолта нет специально, иначе данные о людях когда-нибудь
# утекут в облако через «забыли добавить этап».
#
# resume_section и resume_tailor работают с резюме владельца — это тоже
# персональные данные, но его собственные, а не третьих лиц. Своё резюме
# владелец и так отдаёт работодателям, поэтому в PERSONAL_STAGES эти этапы не
# внесены: запрет облака здесь защищал бы его от самого себя. FAST — потому что
# задачи узкие (перефразировать ответ, выбрать номера блоков), а вызовов много:
# версия собирается на каждую прошедшую скоринг вакансию [CORE-016].
STAGE_PROFILES: dict[str, str] = {
    "extract": FAST,
    "hr_filter": SMART,
    "company": LONG,
    "score": SMART,
    "contacts": LOCAL,
    "dossier": LOCAL,
    "draft": LOCAL,
    "resume_section": FAST,
    "resume_tailor": FAST,
    "embeddings": EMBEDDINGS,
}

# Этапы, где в промпте есть данные о конкретных людях.
PERSONAL_STAGES = frozenset({"contacts", "dossier", "draft"})

# Имена маршрутов — то, что видно в логах и в интерфейсе.
ROUTE_LOCAL = "local"
ROUTE_PROXY = "proxy"

# Переменная окружения с именем модели на прокси для каждого профиля.
PROXY_MODEL_ENV = {
    FAST: "LLM_PROXY_MODEL_FAST",
    SMART: "LLM_PROXY_MODEL_SMART",
    LONG: "LLM_PROXY_MODEL_LONG",
    LOCAL: "LLM_PROXY_MODEL_LOCAL",
    EMBEDDINGS: "LLM_PROXY_MODEL_EMBEDDINGS",
}

# Коды, при которых повтор имеет смысл: это состояние сервиса, не запроса.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
MAX_ERROR_CHARS = 600


class ProfileError(RuntimeError):
    """Неизвестный этап или попытка увести ПД из local-only."""


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


@dataclass(frozen=True)
class Route:
    """Куда физически уходит запрос и под каким именем модели."""

    name: str
    base_url: str
    api_key: str | None
    model: str

    @property
    def is_proxy(self) -> bool:
        return self.name == ROUTE_PROXY


def profile_for(stage: str) -> str:
    try:
        profile = STAGE_PROFILES[stage]
    except KeyError as exc:
        raise ProfileError(
            f"этап {stage!r} не объявил профиль в STAGE_PROFILES"
        ) from exc
    if stage in PERSONAL_STAGES and profile != LOCAL:
        raise ProfileError(
            f"этап {stage!r} работает с персональными данными и требует {LOCAL}"
        )
    return profile


def ensure_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)
    conn.commit()


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


def _digest(
    profile: str,
    messages: Sequence[dict[str, str]],
    temperature: float,
    route: str = ROUTE_LOCAL,
    model: str = "",
) -> str:
    """Маршрут и модель входят в ключ кэша.

    Иначе ответ слабой локальной модели навсегда подменит собой ответ с
    прокси на тот же промпт.
    """
    blob = json.dumps(
        {
            "profile": profile,
            "messages": list(messages),
            "t": temperature,
            "route": route,
            "model": model,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
        personal_via_proxy: bool = False,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.proxy_base_url = (proxy_base_url or "").rstrip("/")
        self.proxy_api_key = proxy_api_key
        self.proxy_models = dict(proxy_models or {})
        self.personal_via_proxy = bool(personal_via_proxy)
        self.timeout = timeout
        self.max_calls = max_calls
        self.backoff = tuple(backoff)
        self.usage = Usage()
        self._transport = transport
        self.conn = conn
        # Профили, по которым прокси уже отказал: второй раз такой вызов делать
        # незачем — конфиг прокси внутри одного прогона не меняется.
        self._rejected: dict[tuple[str, str], str] = {}
        if conn is not None:
            ensure_cache(conn)

    @classmethod
    def from_env(cls, conn: sqlite3.Connection | None = None) -> "Gateway":
        models = {}
        for profile, name in PROXY_MODEL_ENV.items():
            value = os.getenv(name)
            if value:
                models[profile] = value.strip()
        return cls(
            base_url=os.getenv("LLM_BASE_URL") or None,
            api_key=os.getenv("LLM_API_KEY") or None,
            conn=conn,
            timeout=float(os.getenv("LLM_TIMEOUT", "60")),
            max_calls=int(os.getenv("LLM_MAX_CALLS", "300")),
            proxy_base_url=os.getenv("LLM_PROXY_BASE_URL") or None,
            proxy_api_key=os.getenv("LLM_PROXY_API_KEY") or None,
            proxy_models=models,
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

    def route_for(self, stage: str) -> Route | None:
        """Где будет считаться этап. None — считать негде.

        Правила по порядку:

        1. этапы с ПД идут на локальный адрес, если владелец явно не разрешил
           обратное через LLM_PERSONAL_VIA_PROXY;
        2. остальные предпочитают прокси: модели там сильнее;
        3. если нужный адрес не задан — берётся второй, кроме случая ПД без
           разрешения: там фолбэка на прокси нет вообще;
        4. если прокси уже отказал по этой модели в этом же прогоне — идём
           на локальный адрес, если он вообще есть.
        """
        profile = profile_for(stage)
        personal = stage in PERSONAL_STAGES

        local = (
            Route(ROUTE_LOCAL, self.base_url, self.api_key, profile)
            if self.base_url
            else None
        )
        proxy_model = self._proxy_model(profile)
        proxy = (
            Route(ROUTE_PROXY, self.proxy_base_url, self.proxy_api_key, proxy_model)
            if self.proxy_base_url
            else None
        )
        if proxy is not None and (ROUTE_PROXY, proxy_model) in self._rejected:
            # Отказ по сути запроса не пройдёт и со второй вакансией.
            proxy = None

        if personal and not self.personal_via_proxy:
            if proxy is not None and local is None:
                log.warning(
                    "этап %s работает с ПД и пропущен: локальной модели нет, "
                    "а на прокси его пускать не разрешено (LLM_PERSONAL_VIA_PROXY)",
                    stage,
                )
            return local

        if personal and proxy is not None:
            # Громко и каждый раз: такое решение должно быть видно в логе.
            log.warning(
                "этап %s с персональными данными уходит на внешний прокси (%s)",
                stage,
                proxy.model,
            )
            return proxy

        return proxy or local

    def describe_routes(self) -> list[tuple[str, str, str, str]]:
        """(этап, профиль, маршрут, модель) — для интерфейса и логов."""
        out = []
        for stage in STAGE_PROFILES:
            route = self.route_for(stage)
            out.append(
                (
                    stage,
                    profile_for(stage),
                    route.name if route else "нет маршрута",
                    route.model if route else "—",
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
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT response FROM llm_cache WHERE hash = ?", (digest,)
        ).fetchone()
        return None if row is None else row[0]

    def _cache_put(self, digest: str, stage: str, profile: str, response: str) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            """
            INSERT OR REPLACE INTO llm_cache (hash, stage, profile, response, created_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (digest, stage, profile, response),
        )
        self.conn.commit()

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
        route = self.route_for(stage)
        if route is None:
            self.usage.skipped += 1
            log.debug("этап %s пропущен: %s", stage, self.disabled_reason)
            return None

        digest = _digest(profile, messages, temperature, route.name, route.model)
        cached = self._cache_get(digest)
        if cached is not None:
            self.usage.cached += 1
            return cached

        if self.usage.calls >= self.max_calls:
            # Лимит «умного» профиля — 100–300 вызовов в сутки [LLM-004].
            self.usage.skipped += 1
            log.warning("бюджет вызовов исчерпан (%s), этап %s пропущен", self.max_calls, stage)
            return None

        for attempt, pause in enumerate(self.backoff, start=1):
            try:
                if self._transport is not None:
                    text = self._transport(profile, messages, temperature)
                else:
                    text = self._http_call(route, messages, temperature)
            except Exception as exc:  # noqa: BLE001 — модель не должна ронять прогон
                final = isinstance(exc, ApiError) and not exc.retryable
                log.warning(
                    "%s/%s ответил ошибкой (%s/%s, этап %s): %s",
                    route.name,
                    route.model,
                    attempt,
                    len(self.backoff),
                    stage,
                    exc,
                )
                if final:
                    # Отказ по сути запроса: повтор даст то же самое, только медленнее.
                    self.usage.failures += 1
                    self._rejected[(route.name, route.model)] = str(exc)
                    log.error(
                        "%s отклонил запрос с model=%r — повторы не помогут. "
                        "Проверь, есть ли такое имя в его config.yaml (GET /models), "
                        "и задай его в %s",
                        route.name,
                        route.model,
                        PROXY_MODEL_ENV.get(profile, "настройках моделей"),
                    )
                    return None
                if attempt == len(self.backoff):
                    self.usage.failures += 1
                    return None
                time.sleep(pause)
                continue
            self.usage.calls += 1
            self._cache_put(digest, stage, profile, text)
            return text
        return None


__all__ = (
    "ApiError",
    "EMBEDDINGS",
    "FAST",
    "Gateway",
    "LOCAL",
    "LONG",
    "PERSONAL_STAGES",
    "PROXY_MODEL_ENV",
    "ProfileError",
    "RETRY_STATUSES",
    "ROUTE_LOCAL",
    "ROUTE_PROXY",
    "Route",
    "SMART",
    "STAGE_PROFILES",
    "Usage",
    "ensure_cache",
    "error_message",
    "profile_for",
)
