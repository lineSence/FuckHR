"""Досье на компанию: отзывы сотрудников, закономерности, красные флаги.

Зачем это отдельно от contacts.py. Контакт отвечает на вопрос «кому писать»,
досье — на вопрос «стоит ли писать вообще». Второе важнее: письмо нанимающему
менеджеру в контору с задержками зарплаты — потраченный вечер, а не шанс.

Что здесь считается детерминированно ([CORE-015]):

- поиск отзывов по известным площадкам (site:…), а не по всему интернету;
- оценка из текста регуляркой («3,2 из 5»);
- закономерности — словарь признаков с полярностью и весом.

Модель добавляет только сводку словами и никогда не влияет на флаги и цифры: если шлюз
выключен или ответил ошибкой, досье собирается без неё [CORE-017], [LLM-009].

Граница по данным. В поиск уходит только название компании. В отзывах часто
встречаются ФИО руководителей и авторов — мы их не извлекаем и не складываем
в базу: досье на контору, а не на людей [CORE-012].

Отрицания. Маркеры и признаки ищутся подстрокой, поэтому «рекомендую» лежит
внутри «не рекомендую», а «платят вовремя» — внутри «не платят вовремя». Перед
зачётом позитивного совпадения проверяется префикс отрицания, иначе злой отзыв
становится mixed и перестаёт красить работодателя.

Тексты отзывов. Выдача поиска даёт заголовок и сниппет, а у отзовиков сниппет —
рекламная подпись сайта. По ней ни закономерностей, ни тональности не видно,
поэтому страницы найденных отзывов открываются целиком (reviewpage.py), и весь
анализ идёт по их тексту. Если загрузка выключена или страница не открылась,
остаётся прежнее поведение по сниппету — хуже, но не пусто [CORE-017].
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

import contacts
import reviewpage

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_dossier (
    company       TEXT PRIMARY KEY,
    domain        TEXT,
    site_url      TEXT,
    review_count  INTEGER NOT NULL DEFAULT 0,
    avg_rating    REAL,
    risk          TEXT NOT NULL DEFAULT 'unknown',
    patterns      TEXT NOT NULL DEFAULT '[]',
    red_flags     TEXT NOT NULL DEFAULT '[]',
    green_flags   TEXT NOT NULL DEFAULT '[]',
    summary       TEXT,
    summary_by    TEXT,
    sources       TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company    TEXT NOT NULL,
    site       TEXT,
    url        TEXT NOT NULL,
    title      TEXT,
    snippet    TEXT,
    body       TEXT,
    rating     REAL,
    polarity   TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    UNIQUE (company, url)
);

CREATE INDEX IF NOT EXISTS idx_reviews_company ON company_reviews(company);
CREATE INDEX IF NOT EXISTS idx_dossier_risk ON company_dossier(risk);
"""

# Площадки с отзывами сотрудников. Список закрытый сознательно: открытый поиск
# по «отзывы о компании X» даёт рекламные агрегаторы и накрученные отзывы.
# trust — насколько площадка похожа на живые отзывы, а не на карточку компании.
REVIEW_SITES: tuple[tuple[str, str, float], ...] = (
    ("dreamjob.ru", "Dream Job", 1.0),
    ("pravda-sotrudnikov.ru", "Правда сотрудников", 1.0),
    ("orabote.top", "О работе", 0.9),
    ("otzyvy-sotrudnikov.ru", "Отзывы сотрудников", 0.8),
    ("antijob.net", "Antijob", 0.7),
    ("career.habr.com", "Хабр Карьера", 0.9),
    ("habr.com", "Хабр", 0.6),
    ("glassdoor.com", "Glassdoor", 0.8),
)

SITE_NAMES = {host: name for host, name, _ in REVIEW_SITES}
SITE_TRUST = {host: trust for host, _, trust in REVIEW_SITES}

# Закономерности: код, формулировка для человека, полярность, вес, признаки.
# Вес нужен, чтобы отделить «не платят» от «старый офис»: первое закрывает вопрос,
# второе — вкусовщина.
PATTERN_RULES: tuple[tuple[str, str, str, int, tuple[str, ...]], ...] = (
    (
        "salary_delay",
        "Задержки и проблемы с выплатами",
        "red",
        5,
        (
            "задерживают зарплат", "задержка зарплат", "задержки зарплат",
            "не выплатил", "не платят", "кинули на деньги", "должали зарплату",
        ),
    ),
    (
        "grey_salary",
        "Серая зарплата или оформление",
        "red",
        4,
        ("в конверт", "серая зарплат", "серый оклад", "без оформления", "по гпх вместо"),
    ),
    (
        "overtime",
        "Переработки как норма",
        "red",
        3,
        (
            "переработк", "овертайм", "работа по выходным", "задерживаться до ночи",
            "неоплачиваемые переработ", "горит дедлайн постоянно",
        ),
    ),
    (
        "churn",
        "Высокая текучка",
        "red",
        3,
        ("текучк", "никто не задерживается", "все уволились", "команда полностью сменилась"),
    ),
    (
        "micromanagement",
        "Микроменеджмент и слежка",
        "red",
        3,
        (
            "микроменеджмент", "тотальный контроль", "тайм-трекер", "скриншоты экрана",
            "следят за каждым", "отчёт каждый час",
        ),
    ),
    (
        "toxic",
        "Токсичное руководство",
        "red",
        4,
        (
            "кричит на сотрудник", "хамство", "унижени", "токсичн",
            "самодур", "публичные разборы",
        ),
    ),
    (
        "fake_vacancy",
        "Вакансия не совпадает с реальностью",
        "red",
        4,
        (
            "обещали одно", "обманули на собеседовани", "вилка оказалась",
            "по факту другие обязанности", "на испытательном урезали",
        ),
    ),
    (
        "chaos",
        "Нет процессов, хаос в задачах",
        "red",
        2,
        ("нет процессов", "полный хаос", "требования меняются каждый", "легаси без тестов"),
    ),
    (
        "hr_pressure",
        "Давление на найме и многоэтапные отборы",
        "red",
        2,
        ("пять этапов", "тестовое на неделю", "стресс-интервью", "бесплатное тестовое"),
    ),
    (
        "pays_on_time",
        "Платят вовремя, белая зарплата",
        "green",
        3,
        ("платят вовремя", "белая зарплат", "зарплата без задержек", "индексация зарплат"),
    ),
    (
        "sane_management",
        "Адекватное руководство",
        "green",
        2,
        ("адекватный руководител", "адекватное руководств", "нет микроменеджмент", "слышат команду"),
    ),
    (
        "tech_culture",
        "Интересные задачи и техкультура",
        "green",
        2,
        ("интересные задач", "сильная команд", "есть ревью код", "есть тесты и ci"),
    ),
    (
        "remote_ok",
        "Реальная удалёнка и гибкий график",
        "green",
        1,
        ("гибкий график", "полностью удалённо", "полностью удаленно", "никто не сидит в офисе"),
    ),
)

RATING_RE = re.compile(r"(\d[.,]\d|\d)\s*(?:из|/)\s*(?:5|10)\b")
STARS_RE = re.compile(r"рейтинг[^\d]{0,12}(\d[.,]\d|\d)", re.IGNORECASE)

NEGATIVE_MARKERS = (
    "не рекомендую", "не советую", "бегите", "ужас", "кошмар", "обходите стороной",
    "разочарова", "обман", "минусы",
)
POSITIVE_MARKERS = (
    "рекомендую", "лучшая компания", "доволен работой", "плюсы", "всё нравится",
)

# Отрицания перед позитивным совпадением. Без этой проверки «не платят вовремя»
# считается похвалой: маркеры ищутся подстрокой, а не по словам.
NEGATION_PREFIXES = (
    "не ", "ни ", "никогда не ", "перестали ", "так и не ", "вообще не ",
)
NEGATION_WINDOW = 20  # сколько символов слева смотрим на отрицание

RISK_UNKNOWN = "unknown"
RISK_GREEN = "green"
RISK_YELLOW = "yellow"
RISK_RED = "red"

RISK_RU = {
    RISK_UNKNOWN: "нет данных",
    RISK_GREEN: "претензий не видно",
    RISK_YELLOW: "есть к чему придраться",
    RISK_RED: "красные флаги",
}

STALE_AFTER_DAYS = 30  # досье старше месяца собирается заново
MAX_QUOTE_CHARS = 240
MAX_LLM_REVIEWS = 12
MAX_LLM_CHARS = 1500  # сколько текста одного отзыва уходит в модель


@dataclass(frozen=True)
class Review:
    """Один найденный отзыв или карточка компании на площадке отзывов.

    `snippet` — обрывок из выдачи поиска, `body` — текст самой страницы.
    Разделены сознательно: по наличию body видно, читали отзыв или гадали по
    рекламному заголовку.
    """

    url: str
    title: str = ""
    snippet: str = ""
    body: str = ""
    site: str = ""
    rating: float | None = None
    polarity: str = "unknown"  # negative | positive | mixed | unknown

    @property
    def site_name(self) -> str:
        return SITE_NAMES.get(self.site, self.site or "источник не определён")

    @property
    def text(self) -> str:
        return " ".join(part for part in (self.title, self.snippet, self.body) if part)

    @property
    def has_body(self) -> bool:
        return bool(self.body.strip())


@dataclass(frozen=True)
class Pattern:
    """Закономерность: не один злой отзыв, а повторяющаяся жалоба."""

    code: str
    label: str
    polarity: str
    weight: int
    hits: int
    quotes: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        """Закономерность — от двух упоминаний или одного тяжёлого признака."""
        return self.hits >= 2 or self.weight >= 4


@dataclass
class Dossier:
    """Итог по компании. Собирается один раз и переиспользуется всеми вакансиями."""

    company: str
    domain: str | None = None
    site_url: str | None = None
    reviews: tuple[Review, ...] = ()
    patterns: tuple[Pattern, ...] = ()
    avg_rating: float | None = None
    risk: str = RISK_UNKNOWN
    summary: str | None = None
    summary_by: str = ""
    sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def red_flags(self) -> tuple[Pattern, ...]:
        return tuple(p for p in self.patterns if p.polarity == "red" and p.confirmed)

    @property
    def green_flags(self) -> tuple[Pattern, ...]:
        return tuple(p for p in self.patterns if p.polarity == "green" and p.confirmed)

    @property
    def review_count(self) -> int:
        return len(self.reviews)

    @property
    def read_count(self) -> int:
        """Сколько отзывов прочитано со страницы, а не по сниппету выдачи."""
        return sum(1 for r in self.reviews if r.has_body)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Миграция для баз, созданных до чтения страниц: колонки body там нет,
    # а ронять прогон из-за этого нельзя.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(company_reviews)")}
    if "body" not in columns:
        log.info("добавляю колонку body в company_reviews")
        conn.execute("ALTER TABLE company_reviews ADD COLUMN body TEXT")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def review_queries(company: str) -> list[str]:
    """Запросы по площадкам отзывов плюс один широкий.

    Широкий запрос нужен для компаний, которых нет ни на одной площадке:
    иногда единственный отзыв лежит в статье или треде.
    """
    company = (company or "").strip()
    if not company:
        return []
    queries = [
        '"{}" отзывы сотрудников site:{}'.format(company, host)
        for host, _name, _trust in REVIEW_SITES[:5]
    ]
    queries.append('"{}" отзывы работодатель задержка зарплаты'.format(company))
    queries.append('"{}" как работать отзыв разработчика'.format(company))
    return queries


def dossier_queries(company: str) -> list[str]:
    """Всё, что нужно по компании за один проход: отзывы и контактные страницы."""
    return review_queries(company) + contacts_queries(company)


def contacts_queries(company: str) -> list[str]:
    """Обёртка над websearch.contact_queries — чтобы не тянуть импорт в вызывающий код."""
    import websearch

    return websearch.contact_queries(company)


def site_of(url: str) -> str:
    """Площадка отзывов по URL. Неизвестные домены возвращаются как есть."""
    host = (url or "").lower()
    for known in SITE_NAMES:
        if known in host:
            return known
    from urllib.parse import urlsplit

    netloc = urlsplit(url if "//" in (url or "") else "//{}".format(url or "")).netloc
    return netloc.lower().removeprefix("www.")


def is_review_source(url: str) -> bool:
    return site_of(url) in SITE_NAMES


def extract_rating(text: str) -> float | None:
    """Оценка из текста выдачи. Стобалльная шкала приводится к пятибалльной."""
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


def is_negated(low: str, at: int) -> bool:
    """Стоит ли отрицание прямо перед совпадением.

    Смотрим узкое окно слева: «не платят вовремя» — отрицание, а «платят
    вовремя, не придраться» — нет.
    """
    before = low[max(0, at - NEGATION_WINDOW):at]
    return any(before.endswith(prefix) for prefix in NEGATION_PREFIXES)


def count_markers(markers: Sequence[str], low: str, *, skip_negated: bool) -> int:
    """Сколько маркеров нашлось. Под отрицанием позитивные не считаются."""
    total = 0
    for marker in markers:
        start = 0
        while True:
            at = low.find(marker, start)
            if at < 0:
                break
            start = at + len(marker)
            if skip_negated and is_negated(low, at):
                continue
            total += 1
    return total


def matched_needle(
    low: str, needles: Sequence[str], *, skip_negated: bool
) -> str | None:
    """Первый сработавший признак или None. Отрицания пропускаются."""
    for needle in needles:
        start = 0
        while True:
            at = low.find(needle, start)
            if at < 0:
                break
            start = at + len(needle)
            if skip_negated and is_negated(low, at):
                continue
            return needle
    return None


def polarity_of(text: str) -> str:
    """Грубая тональность без модели: маркеры плюс признаки закономерностей."""
    low = (text or "").lower()
    negative = count_markers(NEGATIVE_MARKERS, low, skip_negated=False)
    positive = count_markers(POSITIVE_MARKERS, low, skip_negated=True)
    for _code, _label, pol, weight, needles in PATTERN_RULES:
        # Зелёные признаки под отрицанием не считаются: «не платят вовремя» —
        # это жалоба, а не похвала.
        found = matched_needle(low, needles, skip_negated=pol == "green")
        if not found:
            continue
        if pol == "red":
            negative += 1 if weight < 4 else 2
        else:
            positive += 1
    if negative and positive:
        return "mixed"
    if negative:
        return "negative"
    if positive:
        return "positive"
    return "unknown"


def _quote(text: str, needle: str) -> str:
    """Кусок текста вокруг признака: без цитаты вывод нельзя проверить."""
    low = text.lower()
    at = low.find(needle)
    if at < 0:
        return text[:MAX_QUOTE_CHARS].strip()
    start = max(0, at - 80)
    end = min(len(text), at + len(needle) + 120)
    piece = text[start:end].strip()
    if start:
        piece = "…" + piece
    if end < len(text):
        piece = piece + "…"
    return piece[:MAX_QUOTE_CHARS]


def find_patterns(reviews: Sequence[Review]) -> tuple[Pattern, ...]:
    """Ищет повторяющиеся сюжеты по всем отзывам сразу.

    Считаются отзывы, а не встреченные слова: один эмоциональный текст с пятью
    упоминаниями переработок — это один голос, а не закономерность.

    Зелёные признаки под отрицанием не засчитываются: иначе отзыв «не платят
    вовремя» давал бы green-флаг и подкрашивал risk_level в зелёный.
    """
    out: list[Pattern] = []
    for code, label, polarity, weight, needles in PATTERN_RULES:
        hits = 0
        quotes: list[str] = []
        for review in reviews:
            text = review.text
            low = text.lower()
            matched = matched_needle(low, needles, skip_negated=polarity == "green")
            if not matched:
                continue
            hits += 1
            if len(quotes) < 3:
                quotes.append(_quote(text, matched))
        if hits:
            out.append(
                Pattern(
                    code=code,
                    label=label,
                    polarity=polarity,
                    weight=weight,
                    hits=hits,
                    quotes=tuple(quotes),
                )
            )
    out.sort(key=lambda p: (p.polarity != "red", -p.weight * p.hits))
    return tuple(out)


def average_rating(reviews: Sequence[Review]) -> float | None:
    values = [r.rating for r in reviews if r.rating is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def risk_level(
    reviews: Sequence[Review], patterns: Sequence[Pattern], avg_rating: float | None
) -> str:
    """Цвет светофора. Нет данных — значит нет данных, а не «всё хорошо» [CORE-019]."""
    if not reviews:
        return RISK_UNKNOWN
    red = [p for p in patterns if p.polarity == "red" and p.confirmed]
    green = [p for p in patterns if p.polarity == "green" and p.confirmed]
    heavy = [p for p in red if p.weight >= 4]
    negative = sum(1 for r in reviews if r.polarity in ("negative", "mixed"))
    share = negative / len(reviews)

    if heavy or share >= 0.6 or (avg_rating is not None and avg_rating <= 2.5):
        return RISK_RED
    if red or share >= 0.3 or (avg_rating is not None and avg_rating < 3.8):
        return RISK_YELLOW
    if green or (avg_rating is not None and avg_rating >= 4.0):
        return RISK_GREEN
    return RISK_YELLOW


def analyze(company: str, reviews: Sequence[Review], site_url: str | None = None) -> Dossier:
    """Детерминированная часть досье: без сети и без модели."""
    reviews = tuple(reviews)
    patterns = find_patterns(reviews)
    avg = average_rating(reviews)
    return Dossier(
        company=company,
        domain=contacts.domain_of(site_url),
        site_url=site_url,
        reviews=reviews,
        patterns=patterns,
        avg_rating=avg,
        risk=risk_level(reviews, patterns, avg),
        sources=tuple(dict.fromkeys(r.url for r in reviews if r.url)),
    )


def reviews_from_hits(
    hits: Sequence[object], fetcher: object | None = None
) -> tuple[Review, ...]:
    """Превращает выдачу поиска в отзывы, отбрасывая посторонние сайты.

    Если передан загрузчик страниц, каждая ссылка открывается и в анализ идёт
    текст отзывов, а не рекламный сниппет выдачи. Без загрузчика поведение
    прежнее — по заголовку и сниппету.
    """
    out: list[Review] = []
    seen: set[str] = set()
    for hit in hits:
        url = str(getattr(hit, "url", "") or "")
        if not url or url in seen:
            continue
        if not is_review_source(url):
            continue
        seen.add(url)
        title = str(getattr(hit, "title", "") or "")
        snippet = str(getattr(hit, "snippet", "") or "")
        body = ""
        if fetcher is not None and getattr(fetcher, "enabled", False):
            body = str(fetcher.fetch(url) or "")  # type: ignore[attr-defined]
        text = " ".join(part for part in (title, snippet, body) if part)
        out.append(
            Review(
                url=url,
                title=title,
                snippet=snippet,
                body=body,
                site=site_of(url),
                rating=extract_rating(text),
                polarity=polarity_of(text),
            )
        )
    return tuple(out)


def summarize(gateway: object | None, dossier: Dossier) -> tuple[str | None, str]:
    """Сводка словами. Возвращает (текст, кем собрана).

    Модель видит только название компании и тексты публичных отзывов. Она не
    меняет ни флаги, ни риск: иначе один галлюцинированный абзац перекрашивал бы
    компанию из красной в зелёную.

    В выдержки идут сначала прочитанные страницы и только потом сниппеты: если
    отдать модели рекламные заголовки отзовиков, она справедливо ответит, что
    данных недостаточно.
    """
    deterministic = format_summary(dossier)
    if gateway is None or not getattr(gateway, "enabled", False) or not dossier.reviews:
        return deterministic, "правила"

    ordered = sorted(dossier.reviews, key=lambda r: (not r.has_body, -len(r.text)))
    excerpts = []
    for review in ordered[:MAX_LLM_REVIEWS]:
        text = (review.body or review.text).strip()
        excerpts.append("[{}] {}".format(review.site_name, text[:MAX_LLM_CHARS]))

    if dossier.read_count:
        preface = "Ниже тексты отзывов сотрудников о работодателе «{company}»."
    else:
        preface = (
            "Ниже только заголовки и сниппеты выдачи по работодателю «{company}»: "
            "сами страницы отзывов открыть не удалось."
        )
    prompt = (
        preface + "\n"
        "Назови 3–5 повторяющихся закономерностей одним списком, без введения.\n"
        "Правила: опирайся только на текст; если данных мало — скажи это прямо; "
        "не выдумывай цифры; не упоминай имён людей.\n\n{body}"
    ).format(company=dossier.company, body="\n\n".join(excerpts))

    try:
        answer = gateway.complete(  # type: ignore[attr-defined]
            "company",
            [{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — досье важнее красивой сводки
        log.warning("сводка по отзывам не собрана: %s", exc)
        return deterministic, "правила"

    if not answer:
        return deterministic, "правила"
    return answer.strip(), "модель"


def format_summary(dossier: Dossier) -> str:
    """Сводка без модели: только то, что посчитано."""
    if not dossier.reviews:
        return "Отзывов не найдено — о работодателе неизвестно ничего."
    parts = [
        "Отзывов: {}".format(dossier.review_count),
    ]
    if dossier.read_count:
        parts.append(
            "прочитано страниц: {} из {}".format(
                dossier.read_count, dossier.review_count
            )
        )
    else:
        parts.append("тексты страниц не прочитаны, только выдача поиска")
    if dossier.avg_rating is not None:
        parts.append("средняя оценка {:.1f} из 5".format(dossier.avg_rating))
    red = dossier.red_flags
    green = dossier.green_flags
    if red:
        parts.append(
            "повторяется: "
            + "; ".join("{} ({})".format(p.label.lower(), p.hits) for p in red[:4])
        )
    if green:
        parts.append(
            "в плюс: "
            + "; ".join("{} ({})".format(p.label.lower(), p.hits) for p in green[:3])
        )
    if not red and not green:
        parts.append("повторяющихся сюжетов не видно")
    return ". ".join(parts) + "."


def format_lines(dossier: Dossier, limit: int = 3) -> list[str]:
    """Строки для карточки в Telegram и для страницы вакансии."""
    lines = [
        "Работодатель: {} · отзывов {}".format(
            RISK_RU.get(dossier.risk, dossier.risk), dossier.review_count
        )
    ]
    if dossier.avg_rating is not None:
        lines[0] += " · оценка {:.1f}".format(dossier.avg_rating)
    for pattern in dossier.red_flags[:limit]:
        lines.append("— {} (упоминаний: {})".format(pattern.label, pattern.hits))
    for pattern in dossier.green_flags[:2]:
        lines.append("+ {} (упоминаний: {})".format(pattern.label, pattern.hits))
    return lines


def store(conn: sqlite3.Connection, dossier: Dossier) -> None:
    """Перезаписывает досье и добавляет новые отзывы."""
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO company_dossier (
            company, domain, site_url, review_count, avg_rating, risk,
            patterns, red_flags, green_flags, summary, summary_by, sources, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company) DO UPDATE SET
            domain = excluded.domain,
            site_url = excluded.site_url,
            review_count = excluded.review_count,
            avg_rating = excluded.avg_rating,
            risk = excluded.risk,
            patterns = excluded.patterns,
            red_flags = excluded.red_flags,
            green_flags = excluded.green_flags,
            summary = excluded.summary,
            summary_by = excluded.summary_by,
            sources = excluded.sources,
            updated_at = excluded.updated_at
        """,
        (
            dossier.company,
            dossier.domain,
            dossier.site_url,
            dossier.review_count,
            dossier.avg_rating,
            dossier.risk,
            json.dumps(
                [
                    {
                        "code": p.code,
                        "label": p.label,
                        "polarity": p.polarity,
                        "hits": p.hits,
                        "quotes": list(p.quotes),
                    }
                    for p in dossier.patterns
                ],
                ensure_ascii=False,
            ),
            json.dumps([p.label for p in dossier.red_flags], ensure_ascii=False),
            json.dumps([p.label for p in dossier.green_flags], ensure_ascii=False),
            dossier.summary,
            dossier.summary_by,
            json.dumps(list(dossier.sources), ensure_ascii=False),
            _now(),
        ),
    )
    for review in dossier.reviews:
        # Текст страницы мог появиться позже сниппета: обновляем существующую
        # строку, иначе прочитанный отзыв так и останется заголовком.
        conn.execute(
            """
            INSERT INTO company_reviews
                (company, site, url, title, snippet, body, rating, polarity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company, url) DO UPDATE SET
                title = excluded.title,
                snippet = excluded.snippet,
                body = COALESCE(NULLIF(excluded.body, ''), company_reviews.body),
                rating = excluded.rating,
                polarity = excluded.polarity
            """,
            (
                dossier.company,
                review.site,
                review.url,
                review.title,
                review.snippet,
                review.body,
                review.rating,
                review.polarity,
                _now(),
            ),
        )
    conn.commit()


def load(conn: sqlite3.Connection, company: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM company_dossier WHERE company = ?", (company,)
    ).fetchone()


def load_reviews(conn: sqlite3.Connection, company: str) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM company_reviews WHERE company = ? ORDER BY polarity, id",
        (company,),
    ).fetchall()


def is_fresh(row: sqlite3.Row | None, days: int = STALE_AFTER_DAYS) -> bool:
    """Свежее досье не собирается заново: это главная экономия запросов к поиску."""
    if row is None:
        return False
    try:
        updated = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated < timedelta(days=days)


def row_to_lines(row: sqlite3.Row) -> list[str]:
    """Строки карточки из сохранённого досье — без повторного поиска."""
    try:
        red = json.loads(row["red_flags"] or "[]")
        green = json.loads(row["green_flags"] or "[]")
    except ValueError:
        red, green = [], []
    head = "Работодатель: {} · отзывов {}".format(
        RISK_RU.get(row["risk"], row["risk"]), row["review_count"]
    )
    if row["avg_rating"] is not None:
        head += " · оценка {:.1f}".format(float(row["avg_rating"]))
    lines = [head]
    lines += ["— {}".format(label) for label in red[:3]]
    lines += ["+ {}".format(label) for label in green[:2]]
    return lines


def coverage(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """(досье всего, с красными флагами, без отзывов) — для страницы компаний."""
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN risk = 'red' THEN 1 ELSE 0 END) AS red,
            SUM(CASE WHEN review_count = 0 THEN 1 ELSE 0 END) AS empty
        FROM company_dossier
        """
    ).fetchone()
    return int(row["total"] or 0), int(row["red"] or 0), int(row["empty"] or 0)


def list_dossiers(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        """
        SELECT * FROM company_dossier
        ORDER BY CASE risk
                    WHEN 'red' THEN 0
                    WHEN 'yellow' THEN 1
                    WHEN 'green' THEN 2
                    ELSE 3
                 END,
                 review_count DESC,
                 company
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def build(
    company: str,
    provider: object,
    gateway: object | None = None,
    site_url: str | None = None,
    limit: int = 5,
    fetcher: object | None = None,
) -> Dossier:
    """Полный сбор по одной компании: поиск → страницы отзывов → разбор → сводка.

    Сетевые ошибки не выбрасываются: провайдер возвращает пустой список, а
    недоступная страница — пустой текст. Досье получается беднее, но собирается.
    """
    company = (company or "").strip()
    if not company:
        return Dossier(company="")

    hits: list[object] = []
    if getattr(provider, "enabled", False):
        hits = list(provider.search_many(review_queries(company), limit=limit))  # type: ignore[attr-defined]
    else:
        log.info(
            "досье на %s без отзывов: %s",
            company,
            getattr(provider, "disabled_reason", "поиск недоступен"),
        )

    if fetcher is None:
        # Кэш страниц живёт в той же базе, что и кэш поиска.
        fetcher = reviewpage.PageFetcher.from_env(conn=getattr(provider, "conn", None))

    dossier = analyze(company, reviews_from_hits(hits, fetcher=fetcher), site_url=site_url)
    summary, by = summarize(gateway, dossier)
    dossier.summary = summary
    dossier.summary_by = by
    log.info(
        "досье %s: отзывов %s (прочитано страниц %s), риск %s, красных флагов %s",
        company,
        dossier.review_count,
        dossier.read_count,
        dossier.risk,
        len(dossier.red_flags),
    )
    return dossier
