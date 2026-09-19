"""Разбор страницы отзовика на отдельные отзывы: дата, оценка, плюсы, минусы.

Зачем отдельно от reviewpage.py. Тот отвечает на вопрос «как достать текст со
страницы и не попасть в бан», здесь — «где кончается один отзыв и начинается
следующий». Без этой границы не считается ни один сигнал накрутки: всплеск,
одинаковая длина и рассинхрон оценки с текстом живут на уровне отзыва, а не
страницы.

Вёрстку конкретных площадок не разбираем: они разные и все переделываются.
Разделитель ищется по смыслу — блок, в классе которого есть «review», «otzyv»,
«comment», «feedback». Если разделителей нет, страница остаётся одним отзывом:
хуже, но не пусто [CORE-017], это и есть fallback-стратегия разбора [CORE-014].

Даты. Точность честно помечается, потому что «месяц назад» и «12.03.2026» — это
разные данные, а окно всплеска в 7 дней считается только по тем, где известен
день:

    exact  — дата из <time datetime> или «12.03.2026», «12 марта 2026»;
    approx — посчитана из «3 дня назад», «вчера», погрешность около суток;
    month  — известен только месяц («март 2026»);
    none   — даты нет.

Авторов не извлекаем: ни имени, ни ника, ни ссылки на профиль [CORE-012].
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

import reviewpage
from dossier_rules import LONE_RATING_RE, RATING_RE, STARS_RE

ITEM_MARK = "\x00ITEM\x00"
DATE_MARK = "\x00DATE:{}\x00"

# Блок с отзывом. Ищем по классу и id: сетка меняется, а слово review в
# разметке остаётся. Но искать подстроку недостаточно — на живых страницах
# нашлось и review__text, и review__header (один отзыв резался на три куска),
# и review-other-city-item (восемьсот ссылок на соседние города вытесняли
# настоящие отзывы за потолок). Поэтому сначала пробуем строгие признаки
# контейнера, и только если не вышло ничего — прежний широкий поиск [CORE-017].

# id вида review4373235 или div_review_651760 — якорь на отзыв.
ANCHOR_RE = re.compile(
    r"<(?:div|li|article|section)\b[^>]*id=[\"'][a-z_]*review[_-]?\d+[\"'][^>]*>",
    re.IGNORECASE,
)
# Класс-токен: review, company-reviews-list-item, otzyv-card. Служебные
# review__text и review-other-city-item под это не подходят.
CLASS_BLOCK_RE = re.compile(
    r"<(?:div|li|article|section)\b[^>]*class=[\"'][^\"']*(?<![\w-])"
    r"(?:[a-z]+-)*(?:review|reviews|otzyv|otziv|comment|feedback|opinion)"
    r"(?:[-_](?:item|card|block|list[-_]item|wrap|wrapper))?(?![\w-])[^\"']*[\"'][^>]*>",
    re.IGNORECASE,
)
BLOCK_RE = re.compile(
    r"<(?:div|li|article|section)\b[^>]*(?:class|id)=[\"'][^\"']*"
    r"(?:review|otzyv|otziv|comment|feedback|opinion)[^\"']*[\"'][^>]*>",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"<time\b[^>]*datetime=[\"'](\d{4})-(\d{2})-(\d{2})", re.IGNORECASE
)
DATE_TOKEN_RE = re.compile(r"\x00DATE:(\d{4}-\d{2}-\d{2})\x00")

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}
DMY_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")
DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})\s+([а-яё]{3,8})\.?\s+(\d{4})", re.IGNORECASE
)
MONTH_YEAR_RE = re.compile(r"\b([а-яё]{3,8})\.?\s+(\d{4})\b", re.IGNORECASE)
AGO_RE = re.compile(
    r"\b(\d{1,3})\s+(день|дня|дней|недел\w*|месяц\w*|год\w*|лет)\s+назад", re.IGNORECASE
)
AGO_WORDS = {"сегодня": 0, "вчера": 1, "позавчера": 2}
AGO_DAYS = {"день": 1, "дня": 1, "дней": 1, "недел": 7, "месяц": 30, "год": 365, "лет": 365}

# Заголовки секций. Варианты взяты с живых страниц: dreamjob пишет «Что
# нравится?», «правда сотрудников» — «Плюсы в работе» и «Отрицательные стороны».
PROS_RE = re.compile(
    r"(?:^|[\s.;])(плюсы в работе|плюсы|достоинства|положительные стороны|"
    r"понравилось|что нравится)\s*[:.?\-—]?",
    re.IGNORECASE,
)
CONS_RE = re.compile(
    r"(?:^|[\s.;])(минусы в работе|минусы|недостатки|отрицательные стороны|"
    r"не понравилось|что не нравится|что можно улучшить)\s*[:.?\-—]?",
    re.IGNORECASE,
)
REPLY_RE = re.compile(r"ответ\s+(компании|работодателя|представителя)", re.IGNORECASE)

MIN_ITEM_CHARS = 40  # короче — это подпись или кнопка, а не отзыв
MAX_ITEMS_PER_PAGE = 60
# Порядок попыток разбора: сначала якорь на id, потом класс-контейнер, потом
# прежний широкий поиск. Побеждает первая, давшая хоть один отзыв.
STRATEGIES = ("anchor", "class", "loose")


@dataclass(frozen=True)
class ReviewItem:
    """Один отзыв со страницы. Текст автора, без данных об авторе."""

    url: str
    site: str = ""
    index: int = 0
    body: str = ""
    pros: str = ""
    cons: str = ""
    rating: float | None = None
    dated_at: str | None = None          # ISO-дата
    date_precision: str = "none"         # exact | approx | month | none
    has_reply: bool = False

    @property
    def text(self) -> str:
        return " ".join(p for p in (self.pros, self.cons, self.body) if p).strip()

    @property
    def dated_by_day(self) -> bool:
        """Известен ли день. Всплески считаются только по таким отзывам."""
        return self.date_precision in ("exact", "approx")


def mark_blocks(html: str, pattern: "re.Pattern[str] | None" = None) -> str:
    """Расставляет в HTML метки границ отзывов и найденных дат."""
    text = TIME_RE.sub(
        lambda m: DATE_MARK.format("{}-{}-{}".format(*m.groups())), html or ""
    )
    return (pattern or BLOCK_RE).sub(lambda m: ITEM_MARK + m.group(0), text)


def parse_date(text: str, today: date | None = None) -> tuple[str | None, str]:
    """Дата отзыва и её точность. Первая найденная форма выигрывает."""
    today = today or date.today()
    token = DATE_TOKEN_RE.search(text)
    if token:
        return token.group(1), "exact"

    dmy = DMY_RE.search(text)
    if dmy:
        day, month, year = (int(x) for x in dmy.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return "{:04d}-{:02d}-{:02d}".format(year, month, day), "exact"

    low = text.lower()
    named = DAY_MONTH_RE.search(low)
    if named:
        month = _month_number(named.group(2))
        if month:
            return "{}-{:02d}-{:02d}".format(named.group(3), month, int(named.group(1))), "exact"

    ago = AGO_RE.search(low)
    if ago:
        amount = int(ago.group(1))
        step = next((v for k, v in AGO_DAYS.items() if ago.group(2).startswith(k)), 0)
        if step:
            shifted = today - timedelta(days=amount * step)
            return shifted.isoformat(), "approx" if step <= 7 else "month"

    for word, days in AGO_WORDS.items():
        if word in low:
            return (today - timedelta(days=days)).isoformat(), "approx"

    only_month = MONTH_YEAR_RE.search(low)
    if only_month:
        month = _month_number(only_month.group(1))
        if month:
            return "{}-{:02d}-01".format(only_month.group(2), month), "month"
    return None, "none"


def _month_number(word: str) -> int | None:
    for stem, number in MONTHS.items():
        if word.startswith(stem):
            return number
    return None


def _section(text: str, start_re: re.Pattern[str], stop_re: re.Pattern[str]) -> str:
    """Кусок текста от «Плюсы» до «Минусы», конца абзаца или конца текста.

    Граница по строке нужна для сигнала «минусы пустые»: без неё в минусы
    затекает следующий абзац и «Минусы: нет» перестаёт быть пустым. Но берётся
    не первая строка, а первая непустая: на живых страницах заголовок секции
    («Что нравится?») стоит отдельным блоком, и после него идут пустые строки.
    """
    start = start_re.search(text)
    if not start:
        return ""
    tail = text[start.end():]
    stop = stop_re.search(tail)
    if stop:
        tail = tail[: stop.start()]
    for line in tail.split("\n"):
        cleaned = line.strip(" :;.-—\t")
        if cleaned:
            return cleaned
    return ""


def _clean(chunk: str) -> str:
    """Оставляет строки, похожие на отзыв, и убирает метки."""
    lines = [
        line.strip()
        for line in DATE_TOKEN_RE.sub(" ", chunk).split("\n")
        if reviewpage.looks_like_review(line.strip())
    ]
    return "\n".join(dict.fromkeys(lines)).strip()


def extract_rating(text: str) -> float | None:
    """Оценка из текста. Стобалльная шкала приводится к пятибалльной.

    Живёт здесь, а не в dossier.py: оценку теперь достают и со страницы целиком,
    и из отдельного отзыва. В dossier имя реэкспортируется [CORE-025].
    """
    text = text or ""
    match = RATING_RE.search(text) or STARS_RE.search(text) or LONE_RATING_RE.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if "/10" in text or "из 10" in text:
        value = value / 2
    if not 0 < value <= 5:
        return None
    return round(value, 2)


def split_page(
    html: str, url: str = "", site: str = "", today: date | None = None
) -> tuple[ReviewItem, ...]:
    """Страница → отдельные отзывы.

    Пробуются три способа найти границу отзыва, от точного к грубому
    (STRATEGIES). Побеждает первый, давший хоть один отзыв: на живых страницах
    широкий поиск резал один отзыв на заголовок, текст и кнопки. Не сработал
    ни один — страница считается одним отзывом, и досье не теряет текст
    [CORE-017].
    """
    html = html or ""
    for pattern in (ANCHOR_RE, CLASS_BLOCK_RE, BLOCK_RE):
        items = _split_with(html, pattern, url=url, site=site, today=today)
        if items:
            return items
    # Границ не нашлось ни одним способом — страница считается одним отзывом.
    whole = _split_item(
        reviewpage.strip_tags(mark_blocks(html, ANCHOR_RE)),
        url=url,
        site=site,
        index=0,
        today=today,
    )
    return (whole,) if whole is not None else ()


def _split_with(
    html: str,
    pattern: "re.Pattern[str]",
    *,
    url: str,
    site: str,
    today: date | None,
) -> tuple[ReviewItem, ...]:
    flat = reviewpage.strip_tags(mark_blocks(html, pattern))
    if ITEM_MARK not in flat:
        return ()  # этот способ границ не нашёл, пробуем следующий
    chunks = flat.split(ITEM_MARK)[1:]  # до первой метки лежит шапка сайта
    items: list[ReviewItem] = []
    seen: set[str] = set()
    for chunk in chunks:
        if not chunk.strip():
            continue
        item = _split_item(chunk, url=url, site=site, index=len(items), today=today)
        if item is None:
            continue
        key = item.text.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        # Потолок считается по принятым отзывам, а не по кускам разметки:
        # иначе восемь сотен ссылок на соседние города съедают его целиком.
        if len(items) >= MAX_ITEMS_PER_PAGE:
            break
    return tuple(items)


def _split_item(
    chunk: str, *, url: str, site: str, index: int, today: date | None
) -> ReviewItem | None:
    body = _clean(chunk)
    pros = _section(chunk, PROS_RE, CONS_RE)
    cons = _section(chunk, CONS_RE, PROS_RE)
    if len(body) + len(pros) + len(cons) < MIN_ITEM_CHARS:
        return None
    dated_at, precision = parse_date(chunk, today)
    plain = DATE_TOKEN_RE.sub(" ", chunk)
    return ReviewItem(
        url=url,
        site=site,
        index=index,
        body=body,
        pros=_short(pros),
        cons=_short(cons),
        rating=extract_rating(plain),
        dated_at=dated_at,
        date_precision=precision,
        has_reply=bool(REPLY_RE.search(plain)),
    )


def _short(text: str, limit: int = 600) -> str:
    return " ".join(text.split())[:limit]


__all__ = (
    "ReviewItem",
    "extract_rating",
    "mark_blocks",
    "parse_date",
    "split_page",
)
