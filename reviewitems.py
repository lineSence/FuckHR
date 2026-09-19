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
from dossier_rules import RATING_RE, STARS_RE

ITEM_MARK = "\x00ITEM\x00"
DATE_MARK = "\x00DATE:{}\x00"

# Блок с отзывом. Ищем по классу и id, потому что это единственное, что
# переживает редизайн: сетка меняется, а слово review в классе остаётся.
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

PROS_RE = re.compile(r"(?:^|[\s.;])(плюсы|достоинства|понравилось)\s*[:.\-—]?", re.IGNORECASE)
CONS_RE = re.compile(r"(?:^|[\s.;])(минусы|недостатки|не понравилось)\s*[:.\-—]?", re.IGNORECASE)
REPLY_RE = re.compile(r"ответ\s+(компании|работодателя|представителя)", re.IGNORECASE)

MIN_ITEM_CHARS = 40  # короче — это подпись или кнопка, а не отзыв
MAX_ITEMS_PER_PAGE = 60


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


def mark_blocks(html: str) -> str:
    """Расставляет в HTML метки границ отзывов и найденных дат."""
    text = TIME_RE.sub(
        lambda m: DATE_MARK.format("{}-{}-{}".format(*m.groups())), html or ""
    )
    return BLOCK_RE.sub(lambda m: ITEM_MARK + m.group(0), text)


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

    Граница по переводу строки нужна для сигнала «минусы пустые»: без неё в
    минусы затекает следующий абзац и «Минусы: нет» перестаёт быть пустым.
    """
    start = start_re.search(text)
    if not start:
        return ""
    tail = text[start.end():]
    stop = stop_re.search(tail)
    if stop:
        tail = tail[: stop.start()]
    return tail.split("\n", 1)[0].strip(" :;.-—")


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
    match = RATING_RE.search(text) or STARS_RE.search(text)
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

    Разделителей не нашлось — страница считается одним отзывом: так работает
    старое поведение, и досье не теряет текст [CORE-017].
    """
    flat = reviewpage.strip_tags(mark_blocks(html or ""))
    chunks = flat.split(ITEM_MARK)
    if len(chunks) > 1:
        chunks = chunks[1:]  # до первой метки лежит шапка сайта
    chunks = [c for c in chunks if c.strip()]
    items: list[ReviewItem] = []
    seen: set[str] = set()
    for chunk in chunks[:MAX_ITEMS_PER_PAGE]:
        item = _item(chunk, url=url, site=site, index=len(items), today=today)
        if item is None:
            continue
        key = item.text.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return tuple(items)


def _item(
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
