"""Какие площадки входят в сбор: реестр, выбор галочками и обход.

hh.ru собирается своим путём (`collector.py`): у него есть капча, кэш страниц,
цели со слежением и адреса для карты. Остальные площадки проще и живут здесь
одним общим обходом — каждая своим адаптером `src_*.py`.

Три правила, без которых мультиисточник вредит, а не помогает:

1. Дубли не проходят этапы модели дважды. Ключ дедупа `Vacancy.key` не знает о
   площадке, поэтому вакансия, найденная и на hh.ru, и на SuperJob, остаётся
   одной записью, а факт «видели и там» уходит в `vacancy_sources` `[CORE-016]`.
2. Главная ссылка выбирается по приоритету (`PRIORITY`), а не по тому, кто
   пришёл последним: иначе url вакансии прыгал бы между площадками.
3. Падение площадки не роняет сбор. Адаптер отдал пусто — идём дальше
   `[CORE-017]`; и сбор остальных источников не зависит от того, ответил ли hh.

Источник, который месяц не приносит вакансий, которых нет больше нигде, — это
чужая капча в обмен на дубли. Считать это можно (`source_store.unique_counts`),
и метрика видна на главной странице.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

import settings
import source_store
import src_common
import src_hhlike
import src_rabota
import src_superjob
import src_trudvsem
from hh import Vacancy
from score import evaluate

log = logging.getLogger("fuckhr")

CODE_HH = "hh"
DEFAULT = (CODE_HH,)


@dataclass(frozen=True)
class Site:
    """Площадка: код для галочки, подпись, значение `Vacancy.source`."""

    code: str
    label: str
    source: str
    note: str = ""
    secret: str = ""
    module: Any = None

    @property
    def external(self) -> bool:
        """Собирается общим обходом, а не отдельным путём hh.ru."""
        return self.module is not None


SITES: tuple[Site, ...] = (
    Site(
        CODE_HH,
        "hh.ru",
        "hh.ru",
        "основной источник: капча, кэш страниц, адреса для карты, цели со слежением",
    ),
    Site(
        src_trudvsem.CODE,
        src_trudvsem.LABEL,
        src_trudvsem.CODE,
        "официальное API без ключа; вместе с вакансией приезжают ИНН и ОГРН компании",
        module=src_trudvsem,
    ),
    Site(
        src_superjob.CODE,
        src_superjob.LABEL,
        src_superjob.CODE,
        "официальное API, нужен бесплатный ключ приложения",
        secret="SUPERJOB_KEY",
        module=src_superjob,
    ),
    Site(
        src_hhlike.CODE,
        src_hhlike.LABEL,
        src_hhlike.CODE,
        "тот же движок, что у hh.ru: разбор состояния страницы",
        module=src_hhlike,
    ),
    Site(
        src_rabota.CODE,
        src_rabota.LABEL,
        src_rabota.CODE,
        "вакансии из JSON-LD на странице поиска",
        module=src_rabota,
    ),
)

# Чья ссылка считается главной, если вакансия нашлась на нескольких площадках.
PRIORITY: tuple[str, ...] = tuple(site.source for site in SITES)

BY_CODE: dict[str, Site] = {site.code: site for site in SITES}
BY_SOURCE: dict[str, Site] = {site.source: site for site in SITES}


def label_of(source: str) -> str:
    site = BY_SOURCE.get(source)
    return site.label if site else source


def selected() -> tuple[str, ...]:
    """Коды площадок из настройки. Пусто — только hh.ru, как было всегда."""
    raw = settings.get("SOURCE_SITES", "") or ""
    codes = tuple(
        code for code in (part.strip().lower() for part in raw.replace(";", ",").split(","))
        if code in BY_CODE
    )
    return codes or DEFAULT


def _key(site: Site) -> str:
    """Секрет площадки читает сам её адаптер: имя ключа там литералом."""
    getter = getattr(site.module, "key", None)
    return str(getter() if callable(getter) else "").strip()


def ready(site: Site) -> tuple[bool, str]:
    """Готова ли площадка к сбору. Второе значение — чего не хватает."""
    if site.secret and not _key(site):
        return False, "не задан {}".format(site.secret)
    return True, ""


def pause() -> float:
    return max(0.0, settings.as_float(settings.get("SOURCE_PAUSE", "1"), 1.0))


def max_pages() -> int:
    return max(1, min(20, settings.as_int(settings.get("SOURCE_MAX_PAGES", "3"), 3)))


def _search(site: Site, text: str, area: Any, period: int, limit: int, fetcher: Any) -> Iterator[Vacancy]:
    kwargs: dict[str, Any] = {
        "area": area,
        "period": period,
        "limit": limit,
        "fetcher": fetcher,
        "max_pages": max_pages(),
    }
    if site.secret:
        kwargs["key"] = _key(site)
    return site.module.search(text, **kwargs)


def collect_external(
    bundle: Sequence[Any],
    limit: int = 0,
    prefilter: Any = None,
    conn: sqlite3.Connection | None = None,
    known: Sequence[str] = (),
    codes: Sequence[str] | None = None,
) -> tuple[dict[str, Vacancy], dict[str, Vacancy], dict[str, list[str]]]:
    """Сбор со всех выбранных площадок, кроме hh.ru.

    Возвращает то же, что `profiles.collect_all`: увиденное, прошедшее
    предфильтр и кто из профилей забрал вакансию. Вакансии с ключом из `known`
    в черновики не попадают — они уже собраны с площадки выше по приоритету,
    но их площадка запоминается: на этом стоит метрика уникальности.
    """
    prefilter = prefilter or settings.prefilter_options()
    chosen = tuple(codes) if codes is not None else selected()
    sites = [BY_CODE[code] for code in chosen if code in BY_CODE and BY_CODE[code].external]
    seen: dict[str, Vacancy] = {}
    passed: dict[str, Vacancy] = {}
    owners: dict[str, list[str]] = {}
    if not sites:
        return seen, passed, owners

    marks: list[tuple[str, str, str, str]] = []
    known_keys = set(known)
    for site in sites:
        ok, why = ready(site)
        if not ok:
            log.info("%s пропущен: %s", site.label, why)
            continue
        found = 0
        with src_common.client(pause=pause()) as fetcher:
            for loaded in bundle:
                profile = getattr(loaded, "profile", loaded)
                queries = [q for q in profile.queries if q.get("text")]
                for query in queries:
                    if limit and found >= limit:
                        break
                    try:
                        stream = _search(
                            site,
                            str(query["text"]),
                            query.get("area") or profile.areas or None,
                            int(query.get("period", 7)),
                            max(0, limit - found) if limit else 0,
                            fetcher,
                        )
                        for draft in stream:
                            found += 1
                            marks.append(
                                (draft.key, site.source, draft.external_id, draft.url)
                            )
                            seen.setdefault(draft.key, draft)
                            if draft.key in known_keys:
                                continue
                            rough = evaluate(draft, profile, getattr(prefilter, "fuzzy", None))
                            if prefilter.enabled and (
                                rough.rejected or rough.score < prefilter.min_score
                            ):
                                continue
                            passed.setdefault(draft.key, draft)
                            owners.setdefault(draft.key, []).append(
                                getattr(loaded, "id", "профиль")
                            )
                            if limit and found >= limit:
                                break
                    except Exception as exc:  # noqa: BLE001 — чужой сайт [CORE-017]
                        log.warning("%s: сбор по запросу «%s» сорвался: %s", site.label, query["text"], exc)
        log.info("%s: увидел %s, прошло предфильтр %s", site.label, found, len(passed))

    if conn is not None and marks:
        source_store.remember_many(conn, marks)
    return seen, passed, owners


def main_source(current: str, other: str) -> str:
    """Какая площадка считается главной для вакансии из двух."""
    order = {source: index for index, source in enumerate(PRIORITY)}
    return min((current, other), key=lambda src: order.get(src, len(order)))


__all__ = (
    "CODE_HH",
    "PRIORITY",
    "SITES",
    "Site",
    "collect_external",
    "label_of",
    "main_source",
    "max_pages",
    "pause",
    "ready",
    "selected",
)
