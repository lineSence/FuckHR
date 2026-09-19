"""Досье на компанию: отзывы сотрудников, закономерности, красные флаги.

Зачем это отдельно от contacts.py. Контакт отвечает на вопрос «кому писать»,
досье — на вопрос «стоит ли писать вообще». Второе важнее: письмо нанимающему
менеджеру в контору с задержками зарплаты — потраченный вечер, а не шанс.

Где что лежит после разбиения по [CORE-024]:

- `dossier_rules.py` — площадки, признаки, маркеры, пороги;
- `dossier_store.py` — таблицы и запросы SQLite;
- здесь — разбор текстов и сборка досье.

Имена из обоих модулей реэкспортируются ниже, поэтому `dossier.store`,
`dossier.PATTERN_RULES` и прочее работают по-прежнему.

Что здесь считается детерминированно ([CORE-015]): поиск отзывов по известным
площадкам (site:…), оценка из текста регуляркой («3,2 из 5»), закономерности —
словарь признаков с полярностью и весом.

Модель добавляет только сводку словами и никогда не влияет на флаги и цифры:
если шлюз выключен или ответил ошибкой, досье собирается без неё [CORE-017], [LLM-009].

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

import logging
from dataclasses import dataclass, field
from typing import Sequence

import contacts
import reviewpage
from dossier_rules import (  # noqa: F401 — реэкспорт для старых вызовов
    MAX_LLM_CHARS,
    MAX_LLM_REVIEWS,
    MAX_QUOTE_CHARS,
    NEGATION_PREFIXES,
    NEGATION_WINDOW,
    NEGATIVE_MARKERS,
    PATTERN_RULES,
    POSITIVE_MARKERS,
    RATING_RE,
    REVIEW_SITES,
    RISK_GREEN,
    RISK_RED,
    RISK_RU,
    RISK_UNKNOWN,
    RISK_YELLOW,
    SITE_NAMES,
    SITE_TRUST,
    STALE_AFTER_DAYS,
    STARS_RE,
)
from dossier_store import (  # noqa: F401 — реэкспорт для старых вызовов
    SCHEMA,
    _now,
    coverage,
    ensure_schema,
    is_fresh,
    list_dossiers,
    load,
    load_reviews,
    row_to_lines,
    store,
)

log = logging.getLogger(__name__)


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
        "не выдумывай цифр; не упоминай имён людей.\n\n{body}"
    ).format(company=dossier.company, body="\n\n".join(excerpts))

    try:
        answer = gateway.complete(  # type: ignore[attr-defined]
            # Этап «dossier», а не «company»: в отзывах встречаются имена
            # сотрудников, и профиль этапа (LOCAL, PERSONAL_STAGES) должен
            # решать, уходит ли это на внешний прокси [CORE-012].
            "dossier",
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
