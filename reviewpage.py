"""Чтение самих страниц с отзывами, а не только выдачи поиска.

Зачем это понадобилось. Поисковый провайдер отдаёт `title` и `snippet`, а у
отзовиков сниппет — это подпись сайта («Отзывы сотрудников о работе в компании
X — читайте на …»). Досье собиралось из таких обрывков: закономерности не
находились, модель честно отвечала «данных недостаточно, это рекламные
заголовки». Лечится единственным способом — открыть найденную страницу и взять
текст отзывов.

Границы, чтобы это не превратилось в краулер:

- ходим только по страницам, которые уже прошли фильтр площадок в dossier.py;
- потолок страниц за прогон (REVIEW_FETCH_PAGES), пауза между запросами;
- всё скачанное кэшируется в SQLite на месяц: досье пересобирается редко,
  а долбить отзовики одинаковыми запросами — прямой путь в бан;
- любая сетевая ошибка — это пустой текст и запись в лог, а не падение
  этапа [CORE-017];
- из страницы вырезается обвязка сайта (меню, футер, формы, реклама), потому
  что иначе в анализ попадут слова из шапки, а не из отзывов.

Имён и контактов авторов мы не извлекаем: досье на контору, а не на людей
[CORE-012].

Настройки:

    REVIEW_FETCH_ENABLED=1     # 0 — вернуться к анализу одних сниппетов
    REVIEW_FETCH_PAGES=8       # сколько страниц открывать за прогон
    REVIEW_FETCH_TIMEOUT=20
    REVIEW_FETCH_CHARS=8000    # сколько символов текста брать с одной страницы
    REVIEW_FETCH_PAUSE=1.0     # пауза между загрузками, секунды
    REVIEW_FETCH_CACHE_DAYS=30
"""

from __future__ import annotations

import html as html_mod
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

log = logging.getLogger(__name__)

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS page_cache (
    url        TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

MAX_PAGE_CHARS = 8000
CACHE_DAYS = 30
MIN_LINE_CHARS = 40

# Блоки, внутри которых текста отзывов не бывает никогда.
DROP_BLOCK_RE = re.compile(
    r"<(script|style|noscript|svg|template|head|nav|footer|form|select|aside)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Границы абзацев: без них весь текст страницы слипается в одну строку и
# отделить отзыв от меню становится нечем.
BREAK_RE = re.compile(
    r"</?(p|div|li|tr|td|h[1-6]|section|article|blockquote|br)\b[^>]*>",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACES_RE = re.compile(r"[ \t\u00a0]+")

# Строки обвязки сайта. Проверяется вхождение в нижнем регистре.
BOILERPLATE = (
    "cookie", "куки", "политика конфиденциальн", "все права защищены",
    "пользовательское соглашение", "подпишитесь", "подписаться на рассылку",
    "войти через", "зарегистрироваться", "оставьте отзыв", "оставить отзыв",
    "читайте отзывы сотрудников", "реклама", "мы используем",
    "нажимая кнопку", "вакансии компании", "добавить компанию",
)

# Признаки живого отзыва. Строка без них — почти наверняка описание сервиса.
REVIEW_HINTS = (
    "плюс", "минус", "работал", "работаю", "работала", "уволил", "уволен",
    "зарплат", "оклад", "преми", "руководств", "начальник", "директор",
    "коллектив", "команд", "офис", "переработ", "график", "отпуск",
    "собеседован", "испытательн", "проект", "задач", "рекомендую",
    "сотрудник", "текучк", "обещал", "платят", "выплат",
)


@dataclass
class FetchUsage:
    fetched: int = 0
    cached: int = 0
    failures: int = 0
    skipped: int = 0


def ensure_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)
    conn.commit()


def strip_tags(page: str) -> str:
    """HTML → плоский текст с сохранением границ абзацев."""
    text = DROP_BLOCK_RE.sub(" ", page or "")
    text = BREAK_RE.sub("\n", text)
    text = TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = SPACES_RE.sub(" ", text)
    return text


def looks_like_review(line: str) -> bool:
    """Похожа ли строка на кусок отзыва, а не на меню и не на рекламу."""
    low = line.lower()
    if len(line) < MIN_LINE_CHARS:
        return False
    if any(mark in low for mark in BOILERPLATE):
        return False
    if not any(hint in low for hint in REVIEW_HINTS):
        return False
    # Меню и хлебные крошки — это перечисления через разделители без точек.
    letters = sum(ch.isalpha() for ch in line)
    return letters >= len(line) * 0.5


def extract_reviews(page: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    """Достаёт из страницы только те абзацы, которые похожи на отзывы.

    Никакого понимания вёрстки конкретного отзовика: они все разные и все
    переделываются. Фильтр по смыслу строки переживает редизайн, селекторы —
    нет.
    """
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw in strip_tags(page).split("\n"):
        line = raw.strip()
        if not looks_like_review(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        total += len(line) + 1
        if total >= max_chars:
            break
    return "\n".join(out)[:max_chars]


class PageFetcher:
    """Загрузчик страниц отзывов. Выключенный просто возвращает пустой текст."""

    def __init__(
        self,
        enabled: bool = True,
        conn: sqlite3.Connection | None = None,
        timeout: float = 20.0,
        max_pages: int = 8,
        max_chars: int = MAX_PAGE_CHARS,
        cache_days: int = CACHE_DAYS,
        pause: float = 1.0,
        transport: Callable[[str], str] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.conn = conn
        self.timeout = timeout
        self.max_pages = max(0, int(max_pages))
        self.max_chars = max(500, int(max_chars))
        self.cache_days = max(0, int(cache_days))
        self.pause = max(0.0, float(pause))
        self.transport = transport
        self.usage = FetchUsage()
        if conn is not None:
            ensure_cache(conn)

    @classmethod
    def from_env(cls, conn: sqlite3.Connection | None = None) -> "PageFetcher":
        def number(name: str, default: str) -> float:
            raw = (os.getenv(name) or "").strip() or default
            try:
                return float(raw)
            except ValueError:
                log.warning("%s=%r не число, беру %s", name, raw, default)
                return float(default)

        flag = (os.getenv("REVIEW_FETCH_ENABLED") or "1").strip().lower()
        return cls(
            enabled=flag not in ("0", "false", "no", "off", ""),
            conn=conn,
            timeout=number("REVIEW_FETCH_TIMEOUT", "20"),
            max_pages=int(number("REVIEW_FETCH_PAGES", "8")),
            max_chars=int(number("REVIEW_FETCH_CHARS", str(MAX_PAGE_CHARS))),
            cache_days=int(number("REVIEW_FETCH_CACHE_DAYS", str(CACHE_DAYS))),
            pause=number("REVIEW_FETCH_PAUSE", "1.0"),
        )

    def _cache_get(self, url: str) -> str | None:
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT text, fetched_at FROM page_cache WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None
        try:
            fetched = datetime.fromisoformat(str(row[1]))
        except ValueError:
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > timedelta(days=self.cache_days):
            return None
        return str(row[0])

    def _cache_put(self, url: str, text: str) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO page_cache (url, text, fetched_at) VALUES (?, ?, ?)",
            (url, text, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        )
        self.conn.commit()

    def _http_get(self, url: str) -> str:
        import httpx

        response = httpx.get(
            url,
            headers=BROWSER_HEADERS,
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    def fetch(self, url: str) -> str:
        """Текст отзывов со страницы. Никогда не бросает исключение."""
        url = (url or "").strip()
        if not url:
            return ""
        if not self.enabled:
            self.usage.skipped += 1
            return ""

        cached = self._cache_get(url)
        if cached is not None:
            self.usage.cached += 1
            log.debug("страница из кэша: %s", url)
            return cached

        if self.usage.fetched >= self.max_pages:
            self.usage.skipped += 1
            log.warning("потолок страниц отзывов исчерпан (%s)", self.max_pages)
            return ""

        log.info("читаю отзывы: %s", url)
        try:
            raw = (self.transport or self._http_get)(url)
        except Exception as exc:  # noqa: BLE001 — градуальная деградация [CORE-017]
            self.usage.failures += 1
            log.warning("страница отзывов не открылась (%s): %s", url, exc)
            return ""

        self.usage.fetched += 1
        text = extract_reviews(raw, self.max_chars)
        if not text:
            log.info("на странице не нашлось текста отзывов: %s", url)
        self._cache_put(url, text)
        if self.pause and self.transport is None:
            time.sleep(self.pause)
        return text


__all__ = (
    "PageFetcher",
    "FetchUsage",
    "ensure_cache",
    "extract_reviews",
    "looks_like_review",
    "strip_tags",
    "MAX_PAGE_CHARS",
)
