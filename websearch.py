"""Внешний поиск для contact discovery (ADR-011).

Один интерфейс, два провайдера (Tavily и Brave), провайдер меняется одной
переменной окружения. Важные границы:

- детерминированные источники опрашиваются до поиска, поиск — только для того,
  чего не нашлось;
- в запрос уходят только название компании и должность — никакого досье
  и никаких данных о владельце;
- ответы кэшируются в SQLite: бесплатный тайр конечен;
- недоступность поиска — меньше кандидатов, а не падение этапа [CORE-017].
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

log = logging.getLogger(__name__)

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_cache (
    hash TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

TAVILY = "tavily"
BRAVE = "brave"
PROVIDERS = (TAVILY, BRAVE)

ENDPOINTS = {
    TAVILY: "https://api.tavily.com/search",
    BRAVE: "https://api.search.brave.com/res/v1/web/search",
}


@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    snippet: str


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    failures: int = 0
    skipped: int = 0


def ensure_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)
    conn.commit()


def _digest(provider: str, query: str, limit: int) -> str:
    payload = json.dumps([provider, query, limit], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def contact_queries(company: str, roles: Sequence[str] = ()) -> list[str]:
    """Запросы под поиск нанимающего менеджера. Только компания и роль."""
    company = (company or "").strip()
    if not company:
        return []
    roles = tuple(roles) or ("руководитель разработки", "тимлид backend")
    queries = [f"{company} команда разработки сайт"]
    queries += [f"{company} {role}" for role in roles]
    queries.append(f"{company} инженерный блог habr")
    return queries


class SearchProvider:
    """Клиент внешнего поиска. Без ключа просто не работает и не мешает."""

    def __init__(
        self,
        provider: str = TAVILY,
        api_key: str | None = None,
        conn: sqlite3.Connection | None = None,
        timeout: float = 20.0,
        max_calls: int = 60,
        transport: Callable[[str, str, int], list[Hit]] | None = None,
    ) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"неизвестный провайдер поиска: {provider}")
        self.provider = provider
        self.api_key = (api_key or "").strip()
        self.conn = conn
        self.timeout = timeout
        self.max_calls = max_calls
        self.transport = transport
        self.usage = Usage()
        if conn is not None:
            ensure_cache(conn)

    @classmethod
    def from_env(cls, conn: sqlite3.Connection | None = None) -> "SearchProvider":
        return cls(
            provider=(os.getenv("SEARCH_PROVIDER") or TAVILY).strip().lower(),
            api_key=os.getenv("SEARCH_API_KEY"),
            conn=conn,
            timeout=float(os.getenv("SEARCH_TIMEOUT", "20")),
            max_calls=int(os.getenv("SEARCH_MAX_CALLS", "60")),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) or self.transport is not None

    def _cache_get(self, digest: str) -> list[Hit] | None:
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT response FROM search_cache WHERE hash = ?", (digest,)
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except ValueError:
            return None
        return [Hit(**item) for item in payload]

    def _cache_put(self, digest: str, query: str, hits: Sequence[Hit]) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            """
            INSERT OR REPLACE INTO search_cache (hash, provider, query, response, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                digest,
                self.provider,
                query,
                json.dumps([h.__dict__ for h in hits], ensure_ascii=False),
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            ),
        )
        self.conn.commit()

    def _http_call(self, query: str, limit: int) -> list[Hit]:
        import httpx

        url = ENDPOINTS[self.provider]
        if self.provider == TAVILY:
            response = httpx.post(
                url,
                json={"api_key": self.api_key, "query": query, "max_results": limit},
                timeout=self.timeout,
            )
            response.raise_for_status()
            items = response.json().get("results") or []
            return [
                Hit(
                    title=str(i.get("title") or ""),
                    url=str(i.get("url") or ""),
                    snippet=str(i.get("content") or ""),
                )
                for i in items
            ]

        response = httpx.get(
            url,
            params={"q": query, "count": limit},
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        items = (response.json().get("web") or {}).get("results") or []
        return [
            Hit(
                title=str(i.get("title") or ""),
                url=str(i.get("url") or ""),
                snippet=str(i.get("description") or ""),
            )
            for i in items
        ]

    def search(self, query: str, limit: int = 5) -> list[Hit]:
        """Никогда не бросает исключение: пустой список — тоже результат."""
        query = (query or "").strip()
        if not query:
            return []
        if not self.enabled:
            self.usage.skipped += 1
            log.info("внешний поиск выключен: нет SEARCH_API_KEY")
            return []

        digest = _digest(self.provider, query, limit)
        cached = self._cache_get(digest)
        if cached is not None:
            self.usage.cached += 1
            return cached

        if self.usage.calls >= self.max_calls:
            self.usage.skipped += 1
            log.warning("потолок запросов к поиску исчерпан (%s)", self.max_calls)
            return []

        caller = self.transport or self._http_call_adapter
        try:
            hits = list(caller(self.provider, query, limit))
        except Exception as exc:  # noqa: BLE001 — градуальная деградация [CORE-017]
            self.usage.failures += 1
            log.warning("поиск %s не ответил: %s", self.provider, exc)
            return []

        self.usage.calls += 1
        self._cache_put(digest, query, hits)
        return hits

    def _http_call_adapter(self, provider: str, query: str, limit: int) -> list[Hit]:
        return self._http_call(query, limit)

    def search_many(self, queries: Sequence[str], limit: int = 5) -> list[Hit]:
        """Объединяет результаты по нескольким запросам, убирая дубли по URL."""
        seen: set[str] = set()
        out: list[Hit] = []
        for query in queries:
            for hit in self.search(query, limit=limit):
                if hit.url and hit.url not in seen:
                    seen.add(hit.url)
                    out.append(hit)
        return out
