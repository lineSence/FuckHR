"""Досье на компанию: отзывы сотрудников, закономерности, красные флаги.

Зачем это отдельно от contacts.py. Контакт отвечает на вопрос «кому писать»,
досье — на вопрос «стоит ли писать вообще». Второе важнее: письмо нанимающему
менеджеру в контору с задержками зарплаты — потраченный вечер, а не шанс.

Где что лежит после разбиения по [CORE-024]:

- `dossier_rules.py` — площадки, признаки, маркеры, пороги;
- `dossier_store.py` — таблицы и запросы SQLite;
- `dossier_text.py` — тональность, цитаты, закономерности;
- `dossier_summary.py` — сводка словами и строки карточки;
- `reviewitems.py`, `fake_*.py` — отдельные отзывы и детекция накрутки;
- здесь — сборка досье и уровень риска.

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

Накрученные отзывы. Страница разбирается на отдельные отзывы (reviewitems.py),
каждый получает fake_score по детерминированным сигналам (fake_reviews.py), а
компания — метку накрутки по агрегатам (fake_company.py). Заказные отзывы не
участвуют в средней оценке и в доле негатива, сомнительные идут с половинным
весом. Средняя по всем тоже сохраняется: разница между ней и чистой средней —
и есть то, что видно владельцу. Проектное решение — docs/fake-reviews.md.

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
import fake_company
import aitext_llm
import embeddings_tasks
import fake_llm
import review_area
import reviewlegit
import reviewlegit_store
import fake_reviews
import fake_rules
import fake_store
import reviewpage
from dossier_summary import (  # noqa: F401 — реэкспорт для старых вызовов
    format_lines,
    format_summary,
    summarize,
)
from dossier_text import (  # noqa: F401 — реэкспорт для старых вызовов
    Pattern,
    average_rating,
    count_markers,
    find_patterns,
    is_negated,
    matched_needle,
    polarity_of,
)
from fake_company import CompanyMark
from fake_reviews import Verdict
from reviewitems import ReviewItem, extract_rating  # noqa: F401 — реэкспорт
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
    QUERY_SITES,
    REVIEW_SITES,  # noqa: F401 — реэкспорт для старых вызовов
    RISK_GREEN,
    RISK_RED,
    RISK_RU,
    RISK_THIN,
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


@dataclass
class Dossier:
    """Итог по компании. Собирается один раз и переиспользуется всеми вакансиями."""

    company: str
    domain: str | None = None
    site_url: str | None = None
    reviews: tuple[Review, ...] = ()
    items: tuple[ReviewItem, ...] = ()
    verdicts: tuple[Verdict, ...] = ()
    mark: CompanyMark = field(default_factory=CompanyMark)
    patterns: tuple[Pattern, ...] = ()
    avg_rating: float | None = None       # без заказных, сомнительные с весом 0.5
    avg_rating_all: float | None = None   # по всем отзывам, для сравнения
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
    def suspicious(self) -> tuple[Verdict, ...]:
        """Отзывы, помеченные как сомнительные или заказные."""
        return tuple(
            v for v in self.verdicts if v.label != fake_rules.LABEL_CLEAN
        )

    @property
    def item_by_index(self) -> dict[int, ReviewItem]:
        return {item.index: item for item in self.items}

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
    import reviewsites

    if reviewsites.only_selected():
        # Режим «только на этих площадках»: широкие запросы не идут вовсе.
        # Смысл режима — не тратить запросы на статьи, подборки и агрегаторы,
        # которые всё равно разберутся общим путём и без разметки [CORE-016].
        return [
            '"{}" отзывы сотрудников site:{}'.format(company, host)
            for host in reviewsites.selected()
        ]
    queries = [
        '"{}" отзывы сотрудников site:{}'.format(company, host)
        for host in QUERY_SITES
    ]
    queries.append('"{}" отзывы работодатель задержка зарплаты'.format(company))
    queries.append('"{}" как работать отзыв разработчика'.format(company))
    return queries


def dossier_queries(company: str, roles: Sequence[str] = ()) -> list[str]:
    """Всё, что нужно по компании за один проход: отзывы и контактные страницы."""
    return review_queries(company) + contacts_queries(company, roles)


def contacts_queries(company: str, roles: Sequence[str] = ()) -> list[str]:
    """Обёртка над websearch.contact_queries — чтобы не тянуть импорт в вызывающий код."""
    import websearch

    return websearch.contact_queries(company, roles)


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


def risk_level(
    reviews: Sequence[Review],
    patterns: Sequence[Pattern],
    avg_rating: float | None,
    mark: CompanyMark | None = None,
    negative_share: float | None = None,
) -> str:
    """Цвет светофора. Нет данных — значит нет данных, а не «всё хорошо» [CORE-019].

    Порядок проверок. Накрутка идёт первой: это красный флаг веса 4 (уровень
    серой зарплаты), только про поведение компании, а не про условия труда.
    Иначе получилось бы, что чем больше заказных отзывов, тем меньше осталось
    честных — и тем спокойнее выглядит работодатель.

    «Данных мало» — отдельный цвет: отзывы были, но после чистки их осталось
    меньше MIN_CLEAN_REVIEWS, и пересчитывать риск по остаткам нечестно.
    """
    if not reviews:
        return RISK_UNKNOWN
    if mark is not None and mark.flagged:
        return RISK_RED
    if mark is not None and mark.total and mark.clean_weight < fake_rules.MIN_CLEAN_REVIEWS:
        return RISK_THIN

    red = [p for p in patterns if p.polarity == "red" and p.confirmed]
    green = [p for p in patterns if p.polarity == "green" and p.confirmed]
    heavy = [p for p in red if p.weight >= 4]
    if negative_share is None:
        negative = sum(1 for r in reviews if r.polarity in ("negative", "mixed"))
        negative_share = negative / len(reviews)

    if heavy or negative_share >= 0.6 or (avg_rating is not None and avg_rating <= 2.5):
        return RISK_RED
    if red or negative_share >= 0.3 or (avg_rating is not None and avg_rating < 3.8):
        return RISK_YELLOW
    if green or (avg_rating is not None and avg_rating >= 4.0):
        return RISK_GREEN
    return RISK_YELLOW


def negative_share(
    items: Sequence[ReviewItem], verdicts: Sequence[Verdict]
) -> float | None:
    """Доля негатива с весом метки: заказной отзыв не тянет ни в одну сторону."""
    weights = {v.index: v.weight for v in verdicts}
    total = 0.0
    negative = 0.0
    for item in items:
        weight = weights.get(item.index, 1.0)
        if not weight:
            continue
        total += weight
        if polarity_of(item.text) in ("negative", "mixed"):
            negative += weight
    return round(negative / total, 2) if total else None


def analyze(
    company: str,
    reviews: Sequence[Review],
    site_url: str | None = None,
    items: Sequence[ReviewItem] = (),
    verdicts: Sequence[Verdict] = (),
    area: str = "",
) -> Dossier:
    """Детерминированная часть досье: без сети и без модели.

    Отдельных отзывов может не быть (старая база, страница без разделителей) —
    тогда всё считается по страницам, как раньше.
    """
    reviews = tuple(reviews)
    items = tuple(items)
    verdicts = tuple(verdicts)
    patterns = find_patterns(reviews)
    mark = fake_company.evaluate(items, verdicts, area=area or review_area.owner_code())
    avg = mark.avg_clean if items else average_rating(reviews)
    return Dossier(
        company=company,
        domain=contacts.domain_of(site_url),
        site_url=site_url,
        reviews=reviews,
        items=items,
        verdicts=verdicts,
        mark=mark,
        patterns=patterns,
        avg_rating=avg,
        avg_rating_all=mark.avg_all if items else avg,
        risk=risk_level(
            reviews,
            patterns,
            avg,
            mark=mark if items else None,
            negative_share=negative_share(items, verdicts) if items else None,
        ),
        sources=tuple(dict.fromkeys(r.url for r in reviews if r.url)),
    )


def items_from_reviews(
    reviews: Sequence[Review],
    fetcher: object | None,
    company: str = "",
    conn: object | None = None,
) -> tuple[ReviewItem, ...]:
    """Собирает отдельные отзывы всех прочитанных страниц в один список.

    Номера сквозные по компании: по ним потом сходятся вердикты, строки базы и
    интерфейс, а внутри страницы нумерация своя и совпала бы у разных площадок.
    """
    from dataclasses import replace

    pages = getattr(fetcher, "items", None) or {}
    out: list[ReviewItem] = []
    seen_text: set[str] = set()
    twins = 0
    for review in reviews:
        page_items = list(pages.get(review.url, ()))  # type: ignore[union-attr]
        if not page_items:
            continue
        # Страница без названия нашей компании — чужая: поиск часто приводит на
        # подборку «отзывы о работодателях города». Решение владельца —
        # выбрасывать такую страницу целиком, а не понижать доверие.
        haystack = " ".join(
            [review.title, review.body] + [str(getattr(i, "text", "")) for i in page_items]
        )
        if company and not reviewlegit.company_on_page(haystack, company):
            log.info("страница %s не про %s, пропускаю", review.url, company)
            _note_site(conn, review.site, pages=1, dropped=len(page_items))
            continue

        marks = _boilerplate(conn, review.site)
        kept, dropped = reviewlegit.filter_items(page_items, marks)
        for item in kept:
            # Один и тот же отзыв приходит с двух площадок: часть сайтов
            # пересобирает чужие отзывы. Дважды посчитанный отзыв портит и
            # среднюю оценку, и детекцию накрутки — «группа похожих» ловит
            # как раз копии.
            key = " ".join(str(getattr(item, "text", "")).lower().split())[:200]
            if key in seen_text:
                twins += 1
                continue
            seen_text.add(key)
            out.append(
                review_area.classify_item(
                    replace(item, index=len(out), site=review.site, url=review.url)
                )
            )
        for item, check in dropped:
            log.info("отброшен фрагмент со страницы %s: %s", review.url, check.why)
        _remember_lines(conn, review.site, company, page_items)
        _note_site(
            conn,
            review.site,
            pages=1,
            items=len(kept),
            dropped=len(dropped),
            no_date=sum(1 for i in kept if not getattr(i, "dated_at", None)),
        )
    if twins:
        log.info("копий одного отзыва на разных площадках: %s", twins)
    return tuple(out)


def _boilerplate(conn: object | None, site: str) -> frozenset[str]:
    """Шаблонные строки площадки. База недоступна — работаем без стоп-листа."""
    if conn is None or not site:
        return frozenset()
    try:
        return reviewlegit_store.boilerplate(conn, site)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — [CORE-017]
        log.warning("стоп-лист площадки %s не прочитан: %s", site, exc)
        return frozenset()


def _remember_lines(
    conn: object | None, site: str, company: str, items: Sequence[object]
) -> None:
    if conn is None or not site or not company:
        return
    try:
        reviewlegit_store.remember(
            conn,  # type: ignore[arg-type]
            site,
            company,
            [reviewlegit.line_hash(str(getattr(i, "text", ""))) for i in items],
        )
    except Exception as exc:  # noqa: BLE001 — [CORE-017]
        log.warning("строки площадки %s не записаны: %s", site, exc)


def _note_site(conn: object | None, site: str, **counters: int) -> None:
    if conn is None or not site:
        return
    try:
        reviewlegit_store.note(conn, site, **counters)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — [CORE-017]
        log.warning("итоги площадки %s не записаны: %s", site, exc)


def score_reviews(
    company: str,
    items: Sequence[ReviewItem],
    conn: object | None = None,
    gateway: object | None = None,
) -> tuple[Verdict, ...]:
    """Считает fake_score. Хэши чужих компаний берутся из базы, если она есть."""
    if not items:
        return ()
    known: dict[str, str] = {}
    if conn is not None:
        try:
            known = fake_store.known_hashes(conn, company)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — детекция важнее одного сигнала
            log.warning("хэши отзывов не прочитаны: %s", exc)
    return fake_reviews.score_items(
        items,
        known_hashes=known,
        llm_ads=fake_llm.ad_indexes(gateway, items),
        ai_texts=aitext_llm.generated_indexes(
            gateway, {item.index: item.text for item in items}
        ),
        near_pairs=embeddings_tasks.review_pairs(conn, gateway, items),
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
    urls = []
    for hit in hits:
        url = str(getattr(hit, "url", "") or "")
        if url and url not in seen and is_review_source(url):
            seen.add(url)
            urls.append(url)
    seen.clear()

    # Страницы читаются разом: по одной это восемь пауз подряд на компанию.
    bodies: dict[str, str] = {}
    if urls and fetcher is not None and getattr(fetcher, "enabled", False):
        if hasattr(fetcher, "fetch_many"):
            bodies = fetcher.fetch_many(urls)  # type: ignore[attr-defined]
        else:
            bodies = {url: str(fetcher.fetch(url) or "") for url in urls}  # type: ignore[attr-defined]

    for hit in hits:
        url = str(getattr(hit, "url", "") or "")
        if not url or url in seen:
            continue
        if not is_review_source(url):
            continue
        seen.add(url)
        title = str(getattr(hit, "title", "") or "")
        snippet = str(getattr(hit, "snippet", "") or "")
        body = str(bodies.get(url, "") or "")
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


def build(
    company: str,
    provider: object,
    gateway: object | None = None,
    site_url: str | None = None,
    limit: int = 5,
    fetcher: object | None = None,
    conn: object | None = None,
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

    if conn is None:
        conn = getattr(provider, "conn", None)
    reviews = reviews_from_hits(hits, fetcher=fetcher)
    items = items_from_reviews(reviews, fetcher, company=company, conn=conn)
    verdicts = score_reviews(company, items, conn=conn, gateway=gateway)
    dossier = analyze(company, reviews, site_url=site_url, items=items, verdicts=verdicts)
    summary, by = summarize(gateway, dossier)
    dossier.summary = summary
    dossier.summary_by = by
    log.info(
        "досье %s: отзывов %s (прочитано страниц %s, разобрано отзывов %s, "
        "похожи на заказные %s), риск %s, красных флагов %s, накрутка: %s",
        company,
        dossier.review_count,
        dossier.read_count,
        len(dossier.items),
        dossier.mark.fake,
        dossier.risk,
        len(dossier.red_flags),
        dossier.mark.level,
    )
    return dossier
