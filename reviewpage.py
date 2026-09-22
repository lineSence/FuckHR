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

Зачем дамп (REVIEW_DUMP_PATH). Правила в dossier_rules.PATTERN_RULES писались по
примерам из тестов, и это уже один раз вышло боком: «зарплату задерживают» не
матчилось ни одной иглой. С выставленным REVIEW_DUMP_PATH каждая впервые
прочитанная страница дописывается в текстовый файл, и правила можно
сверять с живым языком. Повторные страницы из кэша в дамп не идут. В файле
оказываются чужие тексты, поэтому путь по умолчанию не задан и держать его
стоит в data/, которая вне git.

Настройки:

    REVIEW_FETCH_ENABLED=1     # 0 — вернуться к анализу одних сниппетов
    REVIEW_FETCH_PAGES=8       # сколько страниц открывать за прогон
    REVIEW_FETCH_TIMEOUT=20
    REVIEW_FETCH_CHARS=8000    # сколько символов текста брать с одной страницы
    REVIEW_FETCH_PAUSE=1.0     # пауза между загрузками, секунды
    REVIEW_SITE_PAGES=2        # сколько страниц листать у знакомых площадок
    REVIEW_FETCH_CACHE_DAYS=30
    REVIEW_DUMP_PATH=          # пусто — не писать; например data/reviews.txt
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Sequence

import injection
import reviewsites

log = logging.getLogger(__name__)

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS page_cache (
    url        TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    items      TEXT NOT NULL DEFAULT '[]',
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


MAX_FETCH_WORKERS = 8  # площадки отзывов не любят частых заходов


def fetch_workers() -> int:
    """Сколько страниц отзывов читается одновременно."""
    try:
        raw = int(float(os.getenv("REVIEW_FETCH_WORKERS", "4")))
    except (TypeError, ValueError):
        raw = 4
    return max(1, min(MAX_FETCH_WORKERS, raw))


def ensure_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)
    # Кэш из баз, созданных до разбора страницы на отдельные отзывы.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(page_cache)")}
    if "items" not in columns:
        log.info("добавляю колонку items в page_cache")
        conn.execute("ALTER TABLE page_cache ADD COLUMN items TEXT NOT NULL DEFAULT '[]'")
    conn.commit()


def encode_items(items: Sequence[object]) -> str:
    from dataclasses import asdict

    return json.dumps([asdict(item) for item in items], ensure_ascii=False)  # type: ignore[arg-type]


def decode_items(raw: str, url: str, text: str) -> tuple[object, ...]:
    """Отзывы из кэша. Пусто — страница считается одним отзывом [CORE-017]."""
    import reviewitems

    try:
        data = json.loads(raw or "[]")
    except ValueError:
        data = []
    items = tuple(
        reviewitems.ReviewItem(**row) for row in data if isinstance(row, dict)
    )
    if items or not (text or "").strip():
        return items
    return (
        reviewitems.ReviewItem(
            url=url, body=text, rating=reviewitems.extract_rating(text)
        ),
    )


def split_items(raw_html: str, url: str, text: str) -> tuple[object, ...]:
    """Страница → отдельные отзывы, с откатом на «одна страница — один отзыв».

    Сначала пробуется парсер площадки (`reviewsites`): у знакомых сайтов в
    разметке лежат должность, город и оценка, которых в тексте нет. Парсер
    ничего не нашёл — разбираем общим путём, как незнакомую страницу
    [CORE-017].
    """
    import reviewitems
    import reviewsites

    items = reviewsites.parse(raw_html, url)
    if items:
        return items
    items = reviewitems.split_page(raw_html, url=url)
    if items:
        return items
    return decode_items("[]", url, text)


def strip_tags(page: str) -> str:
    """HTML → плоский текст с сохранением границ абзацев.

    Скрытые вёрсткой блоки снимаются первыми: после снятия тегов текст
    «белым по белому» неотличим от обычного, а прячут в нём инструкции для
    ИИ-ассистента (ADR-020).
    """
    visible, _hidden = injection.drop_hidden(page or "")
    text = DROP_BLOCK_RE.sub(" ", visible)
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
        site_pages: int = 1,
        transport: Callable[[str], str] | None = None,
        dump_path: str | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.conn = conn
        self.timeout = timeout
        self.max_pages = max(0, int(max_pages))
        self.max_chars = max(500, int(max_chars))
        self.cache_days = max(0, int(cache_days))
        self.pause = max(0.0, float(pause))
        # Сколько страниц читать у площадок с листанием. Одна — как было.
        self.site_pages = max(1, int(site_pages))
        self.transport = transport
        self.dump_path = (dump_path or "").strip() or None
        self.usage = FetchUsage()
        # Отдельные отзывы прочитанных страниц: url → кортеж ReviewItem.
        self.items: dict[str, tuple[object, ...]] = {}
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
            site_pages=int(number("REVIEW_SITE_PAGES", "2")),
            dump_path=os.getenv("REVIEW_DUMP_PATH"),
        )

    def _cache_get(self, url: str) -> str | None:
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT text, fetched_at, items FROM page_cache WHERE url = ?", (url,)
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
        text = str(row[0])
        self.items[url] = decode_items(str(row[2] or "[]"), url, text)
        return text

    def _cache_put(self, url: str, text: str) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO page_cache (url, text, items, fetched_at)"
            " VALUES (?, ?, ?, ?)",
            (
                url,
                text,
                encode_items(self.items.get(url, ())),
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            ),
        )
        self.conn.commit()

    def _dump(self, url: str, text: str) -> None:
        """Дописывает прочитанные отзывы в файл для ревизии правил.

        Диагностика не имеет права ронять сбор [CORE-017].
        """
        if not self.dump_path or not text:
            return
        try:
            path = Path(self.dump_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n----- {url}\n{text}\n")
        except OSError as exc:
            log.warning("дамп отзывов не пишется (%s): %s", self.dump_path, exc)

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
        self.items[url] = split_items(raw, url, text)
        self._follow(url)
        self._dump(url, text)
        self._cache_put(url, text)
        if self.pause and self.transport is None:
            time.sleep(self.pause)
        return text

    def _follow(self, url: str) -> None:
        """Догружает следующие страницы знакомой площадки к тем же отзывам.

        У «Верного» на Dream Job двадцать страниц по 50 отзывов, у «Правды
        сотрудников» — десять по 19. Со страницы поиска приходит только первая,
        и без листания досье крупной сети строилось по одному проценту отзывов.

        Отзывы всех страниц складываются под адресом первой: для досье это
        одна площадка, один источник и одна запись в кэше.
        """
        extra = reviewsites.next_pages(url, self.site_pages - 1)
        if not extra:
            return
        known = {str(getattr(item, "text", "")).lower() for item in self.items.get(url, ())}
        for page in extra:
            if self.usage.fetched >= self.max_pages:
                log.info("потолок страниц не даёт листать дальше: %s", page)
                return
            if self.pause and self.transport is None:
                time.sleep(self.pause)
            try:
                raw = (self.transport or self._http_get)(page)
            except Exception as exc:  # noqa: BLE001 — [CORE-017]
                self.usage.failures += 1
                log.warning("страница %s не открылась: %s", page, exc)
                return
            self.usage.fetched += 1
            items = [
                item
                for item in reviewsites.parse(raw, page)
                if str(getattr(item, "text", "")).lower() not in known
            ]
            if not items:
                log.info("на странице %s новых отзывов нет, останавливаюсь", page)
                return
            known.update(str(getattr(item, "text", "")).lower() for item in items)
            merged = list(self.items.get(url, ())) + items
            self.items[url] = tuple(
                replace(item, index=number) for number, item in enumerate(merged)
            )
            log.info("%s: отзывов всего %s", url, len(self.items[url]))

    def fetch_many(self, urls: Sequence[str]) -> dict[str, str]:
        """Несколько страниц отзывов разом. Сеть — параллельно.

        Кэш, счётчики и потолок страниц остаются в вызывающем потоке: соединение
        sqlite между потоками не делится, а REVIEW_FETCH_PAGES должен считаться
        один раз. Досье на компанию — это до восьми страниц с разных площадок,
        последовательно они складывались в десяток секунд ожидания.
        """
        plan: list[str] = []
        out: dict[str, str] = {}
        for url in urls:
            url = (url or "").strip()
            if not url or url in out or url in plan:
                continue
            if not self.enabled:
                self.usage.skipped += 1
                continue
            cached = self._cache_get(url)
            if cached is not None:
                self.usage.cached += 1
                out[url] = cached
            elif self.usage.fetched + len(plan) < self.max_pages:
                plan.append(url)
            else:
                self.usage.skipped += 1
                log.warning("потолок страниц отзывов исчерпан (%s)", self.max_pages)

        if not plan:
            return out
        workers = min(fetch_workers(), len(plan))
        log.info("отзывы: страниц %s, потоков %s", len(plan), workers)
        caller = self.transport or self._http_get
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reviews") as pool:
            futures = {pool.submit(caller, url): url for url in plan}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    raw = future.result()
                except Exception as exc:  # noqa: BLE001 — [CORE-017]
                    self.usage.failures += 1
                    log.warning("страница отзывов не открылась (%s): %s", url, exc)
                    continue
                self.usage.fetched += 1
                text = extract_reviews(raw, self.max_chars)
                if not text:
                    log.info("на странице не нашлось текста отзывов: %s", url)
                self.items[url] = split_items(raw, url, text)
                self._follow(url)
                self._dump(url, text)
                self._cache_put(url, text)
                out[url] = text
        return out


__all__ = (
    "PageFetcher",
    "decode_items",
    "encode_items",
    "split_items",
    "FetchUsage",
    "ensure_cache",
    "extract_reviews",
    "looks_like_review",
    "strip_tags",
    "MAX_PAGE_CHARS",
)
