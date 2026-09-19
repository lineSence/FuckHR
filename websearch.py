"""Внешний поиск для contact discovery (ADR-011).

Один интерфейс, три провайдера (свой SearXNG, Tavily, Brave), провайдер
меняется одной переменной окружения. Важные границы:

- детерминированные источники опрашиваются до поиска, поиск — только для того,
  чего не нашлось;
- в запрос уходят только название компании и должность — никакого досье
  и никаких данных о владельце;
- ответы кэшируются в SQLite: бесплатный тайр конечен, а свой инстанс не любит
  повторных одинаковых запросов к апстримам;
- недоступность поиска — меньше кандидатов, а не падение этапа [CORE-017].

Про выбор провайдера. Tavily и Brave требуют привязки карты даже на бесплатном
плане, поэтому основной вариант теперь — свой SearXNG в контейнере на VPS. Ключа нет,
биллинга нет, запросы не уходят третьей стороне — та же логика, по которой LLM
у нас локальная (ADR-005). Взамен появляется своя эксплуатация: инстанс надо
держать живым и не давать ему улететь в бан у апстримных движков.

Настройка своего инстанса:

    SEARCH_PROVIDER=searxng
    SEARCH_BASE_URL=http://127.0.0.1:8888
    SEARCH_BASIC_AUTH=user:pass   # если закрыт реверс-прокси, иначе оставь пустым

В settings.yml инстанса обязательно включить JSON, иначе отдаёт только HTML:

    search:
      formats:
        - html
        - json

Тонкая настройка выдачи (всё необязательное, пустое значение = как решит инстанс):

    SEARCH_ENGINES=google,brave,yandex   # какие движки спрашивать
    SEARCH_LANGUAGE=ru-RU
    SEARCH_CATEGORIES=general
    SEARCH_TIME_RANGE=                   # day | week | month | year
    SEARCH_SAFESEARCH=0
    SEARCH_PAGES=1                       # сколько страниц выдачи добирать

Эти параметры входят в ключ кэша: сменил движки — прошлые ответы не
подставляются, иначе настройка выглядела бы сломанной.
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

import contacts_rules

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

SEARXNG = "searxng"
TAVILY = "tavily"
BRAVE = "brave"
PROVIDERS = (SEARXNG, TAVILY, BRAVE)

# У SearXNG адрес свой у каждого, поэтому берётся из SEARCH_BASE_URL.
ENDPOINTS = {
    TAVILY: "https://api.tavily.com/search",
    BRAVE: "https://api.search.brave.com/res/v1/web/search",
}

# Ключ нужен не всем: свой инстанс аутентификации не требует.
KEYLESS_PROVIDERS = (SEARXNG,)

TIME_RANGES = ("", "day", "week", "month", "year")

# Описание настроек для интерфейса: ключ, подпись, тип, значение по умолчанию,
# пояснение. Здесь, а не в settings.py, потому что это знание про SearXNG,
# и меняться оно будет вместе с этим клиентом.
SEARXNG_FIELDS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "SEARCH_BASE_URL",
        "Адрес инстанса",
        "text",
        "",
        "Через SSH-туннель это http://127.0.0.1:8888. Без адреса поиск выключен.",
    ),
    (
        "SEARCH_BASIC_AUTH",
        "Логин:пароль",
        "text",
        "",
        "Только если инстанс закрыт реверс-прокси. За туннелем не нужно.",
    ),
    (
        "SEARCH_ENGINES",
        "Движки",
        "text",
        "",
        "Через запятую: google,brave,yandex,wikipedia. Пусто — как настроен инстанс. "
        "duckduckgo часто отвечает капчей, от него больше вреда, чем пользы.",
    ),
    (
        "SEARCH_LANGUAGE",
        "Язык выдачи",
        "text",
        "ru-RU",
        "ru-RU для российских компаний; all — не ограничивать.",
    ),
    (
        "SEARCH_CATEGORIES",
        "Категории",
        "text",
        "general",
        "general хватает. Категории вроде it сужают выдачу до профильных движков.",
    ),
    (
        "SEARCH_TIME_RANGE",
        "Период",
        "text",
        "",
        "Пусто | day | week | month | year. Для страниц «о команде» ограничение вредно: "
        "они старые.",
    ),
    (
        "SEARCH_SAFESEARCH",
        "Safesearch",
        "int",
        "0",
        "0 — не фильтровать, 1 — умеренно, 2 — строго.",
    ),
    (
        "SEARCH_PAGES",
        "Страниц выдачи",
        "int",
        "1",
        "Сколько страниц добирать, если на первой мало ссылок. Каждая страница — "
        "отдельный запрос к апстримам, 2–3 безопасный максимум.",
    ),
    (
        "SEARCH_TIMEOUT",
        "Таймаут, с",
        "int",
        "20",
        "Инстанс опрашивает несколько движков подряд, меньше 10 с ставить бессмысленно.",
    ),
    (
        "SEARCH_MAX_CALLS",
        "Потолок запросов за прогон",
        "int",
        "60",
        "Защита и от бана у апстримов, и от бесконечного цикла в коде.",
    ),
)


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


def _digest(provider: str, query: str, limit: int, variant: str = "") -> str:
    payload = json.dumps(
        [provider, query, limit, variant], ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def contact_queries(company: str, roles: Sequence[str] = ()) -> list[str]:
    """Запросы под поиск нанимающего менеджера. Только компания и роль.

    Роли приходят от вызывающего: их выводит contacts_rules.lead_roles из
    названия вакансии. Без этого на «Оператора 1С» уходил «тимлид backend».
    """
    company = (company or "").strip()
    if not company:
        return []
    roles = tuple(roles) or contacts_rules.DEFAULT_LEAD_ROLES
    queries = [f"{company} официальный сайт команда контакты"]
    queries += [f"{company} {role}" for role in roles]
    return queries


class SearchProvider:
    """Клиент внешнего поиска. Ненастроенный просто не работает и не мешает."""

    def __init__(
        self,
        provider: str = SEARXNG,
        api_key: str | None = None,
        conn: sqlite3.Connection | None = None,
        timeout: float = 20.0,
        max_calls: int = 60,
        transport: Callable[[str, str, int], list[Hit]] | None = None,
        base_url: str | None = None,
        basic_auth: str | None = None,
        language: str = "ru-RU",
        engines: str = "",
        categories: str = "general",
        time_range: str = "",
        safesearch: int = 0,
        pages: int = 1,
    ) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"неизвестный провайдер поиска: {provider}")
        self.provider = provider
        self.api_key = (api_key or "").strip()
        self.conn = conn
        self.timeout = timeout
        self.max_calls = max_calls
        self.transport = transport
        self.base_url = (base_url or "").strip().rstrip("/")
        self.basic_auth = (basic_auth or "").strip()
        self.language = (language or "").strip()
        self.engines = ",".join(
            part.strip() for part in (engines or "").split(",") if part.strip()
        )
        self.categories = (categories or "").strip()
        self.time_range = (time_range or "").strip().lower()
        if self.time_range not in TIME_RANGES:
            log.warning(
                "неизвестный период поиска %r, игнорирую", self.time_range
            )
            self.time_range = ""
        self.safesearch = int(safesearch)
        self.pages = max(1, int(pages))
        self.usage = Usage()
        if conn is not None:
            ensure_cache(conn)

    @classmethod
    def from_env(cls, conn: sqlite3.Connection | None = None) -> "SearchProvider":
        def number(name: str, default: str) -> float:
            raw = (os.getenv(name) or "").strip() or default
            try:
                return float(raw)
            except ValueError:
                log.warning("%s=%r не число, беру %s", name, raw, default)
                return float(default)

        return cls(
            provider=(os.getenv("SEARCH_PROVIDER") or SEARXNG).strip().lower(),
            api_key=os.getenv("SEARCH_API_KEY"),
            conn=conn,
            timeout=number("SEARCH_TIMEOUT", "20"),
            max_calls=int(number("SEARCH_MAX_CALLS", "60")),
            base_url=os.getenv("SEARCH_BASE_URL"),
            basic_auth=os.getenv("SEARCH_BASIC_AUTH"),
            language=os.getenv("SEARCH_LANGUAGE") or "ru-RU",
            engines=os.getenv("SEARCH_ENGINES") or "",
            categories=os.getenv("SEARCH_CATEGORIES") or "general",
            time_range=os.getenv("SEARCH_TIME_RANGE") or "",
            safesearch=int(number("SEARCH_SAFESEARCH", "0")),
            pages=int(number("SEARCH_PAGES", "1")),
        )

    @property
    def enabled(self) -> bool:
        """Готов ли провайдер к работе.

        У своего инстанса признак готовности — адрес, а не ключ.
        """
        if self.transport is not None:
            return True
        if self.provider in KEYLESS_PROVIDERS:
            return bool(self.base_url)
        return bool(self.api_key)

    @property
    def disabled_reason(self) -> str:
        if self.provider in KEYLESS_PROVIDERS:
            return "не задан SEARCH_BASE_URL"
        return "не задан SEARCH_API_KEY"

    def _variant(self) -> str:
        """Подпись настроек для ключа кэша: иначе смена движков ничего не меняет."""
        if self.provider != SEARXNG:
            return ""
        return "|".join(
            [
                self.base_url,
                self.engines,
                self.language,
                self.categories,
                self.time_range,
                str(self.safesearch),
                str(self.pages),
            ]
        )

    def describe(self) -> list[tuple[str, str]]:
        """Что именно уйдёт в инстанс — для страницы проверки поиска."""
        return [
            ("провайдер", self.provider),
            ("адрес", self.base_url or "не задан"),
            ("движки", self.engines or "как настроен инстанс"),
            ("язык", self.language or "любой"),
            ("категории", self.categories or "general"),
            ("период", self.time_range or "без ограничения"),
            ("safesearch", str(self.safesearch)),
            ("страниц выдачи", str(self.pages)),
            ("таймаут", "{:.0f} с".format(self.timeout)),
            ("потолок запросов", str(self.max_calls)),
            ("доступ", "логин:пароль задан" if self.basic_auth else "без авторизации"),
        ]

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

    def _searxng_params(self, query: str, pageno: int) -> dict[str, object]:
        """Пустые параметры не отправляются: у инстанса свои значения по умолчанию."""
        params: dict[str, object] = {
            "q": query,
            "format": "json",
            "safesearch": self.safesearch,
        }
        if pageno > 1:
            params["pageno"] = pageno
        if self.language:
            params["language"] = self.language
        if self.categories:
            params["categories"] = self.categories
        if self.engines:
            params["engines"] = self.engines
        if self.time_range:
            params["time_range"] = self.time_range
        return params

    def _searxng_call(self, query: str, limit: int) -> list[Hit]:
        """Свой инстанс: GET /search?format=json.

        Если инстанс ответил HTML, значит в settings.yml не включён формат json —
        говорим об этом прямо, иначе диагностика превращается в гадание.
        """
        import httpx

        auth = None
        if self.basic_auth and ":" in self.basic_auth:
            user, _, password = self.basic_auth.partition(":")
            auth = (user, password)

        seen: set[str] = set()
        hits: list[Hit] = []
        for pageno in range(1, self.pages + 1):
            response = httpx.get(
                self.base_url + "/search",
                params=self._searxng_params(query, pageno),
                headers={"Accept": "application/json"},
                auth=auth,
                timeout=self.timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    "инстанс SearXNG ответил не JSON: добавь формат json в search.formats в settings.yml"
                ) from exc

            items = payload.get("results") or []
            for item in items:
                url = str(item.get("url") or "")
                if url and url in seen:
                    continue
                seen.add(url)
                hits.append(
                    Hit(
                        title=str(item.get("title") or ""),
                        url=url,
                        snippet=str(item.get("content") or ""),
                    )
                )
            if len(hits) >= limit or not items:
                break
        return hits[:limit]

    def _http_call(self, query: str, limit: int) -> list[Hit]:
        import httpx

        if self.provider == SEARXNG:
            return self._searxng_call(query, limit)

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
            log.info("внешний поиск выключен: %s", self.disabled_reason)
            return []

        digest = _digest(self.provider, query, limit, self._variant())
        cached = self._cache_get(digest)
        if cached is not None:
            self.usage.cached += 1
            log.info("поиск из кэша: %s (%s ссылок)", query, len(cached))
            return cached

        if self.usage.calls >= self.max_calls:
            self.usage.skipped += 1
            log.warning("потолок запросов к поиску исчерпан (%s)", self.max_calls)
            return []

        log.info("поиск: %s", query)
        caller = self.transport or self._http_call_adapter
        try:
            hits = list(caller(self.provider, query, limit))
        except Exception as exc:  # noqa: BLE001 — градуальная деградация [CORE-017]
            self.usage.failures += 1
            log.warning("поиск %s не ответил: %s", self.provider, exc)
            return []

        self.usage.calls += 1
        log.info("найдено ссылок: %s", len(hits))
        self._cache_put(digest, query, hits)
        return hits

    def _http_call_adapter(self, provider: str, query: str, limit: int) -> list[Hit]:
        return self._http_call(query, limit)

    def search_many(self, queries: Sequence[str], limit: int = 5) -> list[Hit]:
        """Объединяет результаты по нескольким запросам, убирая дубли по URL."""
        seen: set[str] = set()
        out: list[Hit] = []
        total = len(queries)
        for number, query in enumerate(queries, 1):
            log.info("запрос %s/%s", number, total)
            for hit in self.search(query, limit=limit):
                if hit.url and hit.url not in seen:
                    seen.add(hit.url)
                    out.append(hit)
        return out
