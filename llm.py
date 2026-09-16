"""Единая точка входа к моделям: шлюз FreeLLMAPI (ADR-005, [CORE-010]).

Код знает один OpenAI-совместимый base_url. Списки моделей, цепочки
фолбэков и ключи живут в конфиге шлюза, а не здесь.

Три вещи, которые здесь важнее самого вызова:

1. Профиль выбирает код по этапу, а не настройка в дашборде [LLM-003].
   Этапы с данными о людях жёстко прибиты к local-only [CORE-012]: промах
   здесь отправляет ФИО и контакты случайному бесплатному провайдеру,
   который учится на промптах, и отменить это уже невозможно.
2. Кэш по хэшу промпта включён всегда [LLM-006]: в мониторинге 60–80%
   вакансий повторяются каждый прогон, и это экономит лимиты сильнее
   любой ротации ключей.
3. Любая ошибка возвращает None, а не исключение [CORE-017], [LLM-009].
   Пайплайн без модели работает хуже, но работает: всё ядро детектора
   детерминированное.
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
STAGE_PROFILES: dict[str, str] = {
    "extract": FAST,
    "hr_filter": SMART,
    "company": LONG,
    "score": SMART,
    "contacts": LOCAL,
    "dossier": LOCAL,
    "draft": LOCAL,
    "embeddings": EMBEDDINGS,
}

# Этапы, где в промпте есть данные о конкретных людях.
PERSONAL_STAGES = frozenset({"contacts", "dossier", "draft"})


class ProfileError(RuntimeError):
    """Неизвестный этап или попытка увести ПД из local-only."""


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    failures: int = 0
    skipped: int = 0


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


def _digest(profile: str, messages: Sequence[dict[str, str]], temperature: float) -> str:
    blob = json.dumps(
        {"profile": profile, "messages": list(messages), "t": temperature},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Gateway:
    """Тонкий клиент к шлюзу. Без base_url выключен и всегда возвращает None."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        conn: sqlite3.Connection | None = None,
        timeout: float = 60.0,
        max_calls: int = 300,
        transport: Callable[[str, Sequence[dict[str, str]], float], str] | None = None,
        backoff: Sequence[float] = (0.5, 2.0, 5.0),
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_calls = max_calls
        self.backoff = tuple(backoff)
        self.usage = Usage()
        self._transport = transport or self._http_call
        self.conn = conn
        if conn is not None:
            ensure_cache(conn)

    @classmethod
    def from_env(cls, conn: sqlite3.Connection | None = None) -> "Gateway":
        return cls(
            base_url=os.getenv("LLM_BASE_URL") or None,
            api_key=os.getenv("LLM_API_KEY") or None,
            conn=conn,
            timeout=float(os.getenv("LLM_TIMEOUT", "60")),
            max_calls=int(os.getenv("LLM_MAX_CALLS", "300")),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

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
        self, profile: str, messages: Sequence[dict[str, str]], temperature: float
    ) -> str:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": profile,
                "messages": list(messages),
                "temperature": temperature,
            },
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        # Без этой строки невозможно узнать, какая модель сгенерировала ответ.
        log.info(
            "llm %s -> %s", profile, response.headers.get("X-Routed-Via", "неизвестно")
        )
        payload: dict[str, Any] = response.json()
        return payload["choices"][0]["message"]["content"] or ""

    def complete(
        self,
        stage: str,
        messages: Sequence[dict[str, str]],
        temperature: float = 0.0,
    ) -> str | None:
        profile = profile_for(stage)
        if not self.enabled:
            self.usage.skipped += 1
            log.debug("шлюз выключен (нет LLM_BASE_URL), этап %s пропущен", stage)
            return None

        digest = _digest(profile, messages, temperature)
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
                text = self._transport(profile, messages, temperature)
            except Exception as exc:  # noqa: BLE001 — модель не должна ронять прогон
                log.warning(
                    "шлюз ответил ошибкой (%s/%s, этап %s): %s",
                    attempt,
                    len(self.backoff),
                    stage,
                    exc,
                )
                if attempt == len(self.backoff):
                    self.usage.failures += 1
                    return None
                time.sleep(pause)
                continue
            self.usage.calls += 1
            self._cache_put(digest, stage, profile, text)
            return text
        return None
