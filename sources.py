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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Значение `Vacancy.source` у hh.ru. Отдельная константа, потому что по ней
# решается, можно ли открывать страницу вакансии клиентом hh: id чужой площадки
# на hh.ru ведёт на другую, существующую вакансию, и подмену никто не заметит.
SOURCE_HH = "hh.ru"
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


def hh_enabled(codes: Sequence[str] | None = None) -> bool:
    """Входит ли hh.ru в сбор. Снятая галочка означает «не ходить туда вовсе»."""
    chosen = tuple(codes) if codes is not None else selected()
    return CODE_HH in chosen


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


def workers() -> int:
    """Сколько площадок обходится одновременно. Максимум 8, как у досье."""
    return max(1, min(8, settings.as_int(settings.get("SOURCE_WORKERS", "4"), 4)))


def _collect_site(
    site: Site, bundle: Sequence[Any], limit: int, prefilter: Any
) -> tuple[dict[str, Vacancy], dict[str, list[str]], list[tuple[str, str, str, str]], int]:
    """Обход одной площадки: своя сессия, свои паузы, свой счётчик.

    Работает в отдельном потоке и базы не касается: находки возвращаются
    главному потоку, он и пишет. Ошибка площадки остаётся её ошибкой
    [CORE-017].
    """
    found = 0
    site_seen: dict[str, Vacancy] = {}
    owners: dict[str, list[str]] = {}
    marks: list[tuple[str, str, str, str]] = []
    with src_common.client(pause=pause()) as fetcher:
        for loaded in bundle:
            profile = getattr(loaded, "profile", loaded)
            for query in [q for q in profile.queries if q.get("text")]:
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
                        marks.append((draft.key, site.source, draft.external_id, draft.url))
                        site_seen.setdefault(draft.key, draft)
                        rough = evaluate(draft, profile, getattr(prefilter, "fuzzy", None))
                        if prefilter.enabled and (
                            rough.rejected or rough.score < prefilter.min_score
                        ):
                            continue
                        owners.setdefault(draft.key, []).append(getattr(loaded, "id", "профиль"))
                        if limit and found >= limit:
                            break
                except Exception as exc:  # noqa: BLE001 — чужой сайт [CORE-017]
                    log.warning(
                        "%s: сбор по запросу «%s» сорвался: %s", site.label, query["text"], exc
                    )
    log.info("%s: увидел %s, прошло предфильтр %s", site.label, found, len(owners))
    return site_seen, owners, marks, found


def collect_external(
    bundle: Sequence[Any],
    limit: int = 0,
    prefilter: Any = None,
    conn: sqlite3.Connection | None = None,
    known: Sequence[str] = (),
    codes: Sequence[str] | None = None,
    marks_out: list[tuple[str, str, str, str]] | None = None,
) -> tuple[dict[str, Vacancy], dict[str, Vacancy], dict[str, list[str]]]:
    """Сбор со всех выбранных площадок, кроме hh.ru.

    Возвращает то же, что `profiles.collect_all`: увиденное, прошедшее
    предфильтр и кто из профилей забрал вакансию. Вакансии с ключом из `known`
    в черновики не попадают — они уже собраны с площадки выше по приоритету,
    но их площадка запоминается: на этом стоит метрика уникальности.

    Площадки обходятся потоками: домены разные, чужая пауза нашей не мешает, а
    последовательный обход означал, что медленный API держит весь сбор. Внутри
    площадки порядок прежний, и пауза `SOURCE_PAUSE` считается на неё одну.

    `marks_out` — куда сложить «где видели», когда писать в базу нельзя:
    соединение sqlite нельзя делить между потоками, поэтому при работе в фоне
    записи отдаются главному потоку (`source_store.remember_many`).
    """
    prefilter = prefilter or settings.prefilter_options()
    chosen = tuple(codes) if codes is not None else selected()
    sites = [BY_CODE[code] for code in chosen if code in BY_CODE and BY_CODE[code].external]
    seen: dict[str, Vacancy] = {}
    passed: dict[str, Vacancy] = {}
    owners: dict[str, list[str]] = {}
    ready_sites: list[Site] = []
    for site in sites:
        ok, why = ready(site)
        if ok:
            ready_sites.append(site)
        else:
            log.info("%s пропущен: %s", site.label, why)
    if not ready_sites:
        return seen, passed, owners

    marks: list[tuple[str, str, str, str]] = []
    known_keys = set(known)
    count = min(workers(), len(ready_sites))
    log.info("площадок в обходе: %s, потоков: %s", len(ready_sites), count)
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="source") as pool:
        futures = {
            pool.submit(_collect_site, site, bundle, limit, prefilter): site
            for site in ready_sites
        }
        for future in as_completed(futures):
            site = futures[future]
            try:
                site_seen, site_owners, site_marks, _found = future.result()
            except Exception as exc:  # noqa: BLE001 — одна площадка не роняет сбор
                log.warning("%s: обход сорвался: %s", site.label, exc)
                continue
            marks += site_marks
            for key, draft in site_seen.items():
                seen.setdefault(key, draft)
            for key, ids in site_owners.items():
                if key in known_keys:
                    continue
                passed.setdefault(key, site_seen[key])
                owners.setdefault(key, []).extend(ids)

    if marks_out is not None:
        marks_out += marks
    elif conn is not None and marks:
        source_store.remember_many(conn, marks)
    return seen, passed, owners


@dataclass
class ExternalJob:
    """Обход площадок, запущенный в фоне: результат забирают после hh.ru."""

    future: Any
    marks: list[tuple[str, str, str, str]]
    pool: Any

    def result(
        self, conn: sqlite3.Connection | None = None, known: Sequence[str] = ()
    ) -> tuple[dict[str, Vacancy], dict[str, Vacancy], dict[str, list[str]]]:
        """Дожидается площадок и отдаёт находки. Сбой фона не роняет прогон."""
        try:
            seen, passed, owners = self.future.result()
        except Exception as exc:  # noqa: BLE001 — чужие сайты [CORE-017]
            log.warning("обход других площадок сорвался: %s", exc)
            seen, passed, owners = {}, {}, {}
        finally:
            self.pool.shutdown(wait=False)
        # Дедуп с hh.ru делается здесь: пока площадки шли в фоне, что именно
        # принёс hh.ru, известно не было.
        known_keys = set(known)
        passed = {key: value for key, value in passed.items() if key not in known_keys}
        owners = {key: value for key, value in owners.items() if key not in known_keys}
        if conn is not None and self.marks:
            source_store.remember_many(conn, self.marks)
        return seen, passed, owners


def start_external(
    bundle: Sequence[Any],
    limit: int = 0,
    prefilter: Any = None,
    codes: Sequence[str] | None = None,
) -> ExternalJob:
    """Пускает обход площадок параллельно с hh.ru.

    hh.ru собирается медленно намеренно: паузы между страницами — защита от
    капчи, и всё это время процесс просто ждёт. Другие площадки живут на других
    доменах, их очередь запросов никак не связана с очередью hh.ru, поэтому
    ждать их по очереди — терять минуты на пустом месте.
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="external")
    marks: list[tuple[str, str, str, str]] = []
    future = pool.submit(
        collect_external, bundle, limit, prefilter, None, (), codes, marks
    )
    return ExternalJob(future=future, marks=marks, pool=pool)


def queue_note(seen: dict[str, Vacancy], drafts: dict[str, Vacancy]) -> str:
    """Строка «кто сколько принёс» для лога прогона.

    Без неё «досье только по hh.ru» выглядит как поломка досье, хотя обычно
    площадка либо не ответила, либо принесла дубли и вакансии ниже порога.
    """
    counts: dict[str, list[int]] = {}
    for key, vacancy in seen.items():
        row = counts.setdefault(str(vacancy.source or "?"), [0, 0])
        row[0] += 1
        if key in drafts:
            row[1] += 1
    if not counts:
        return "никто ничего не принёс"
    return "; ".join(
        "{}: увидели {}, в прогон {}".format(label_of(source), row[0], row[1])
        for source, row in sorted(counts.items(), key=lambda item: -item[1][0])
    )


def main_source(current: str, other: str) -> str:
    """Какая площадка считается главной для вакансии из двух."""
    order = {source: index for index, source in enumerate(PRIORITY)}
    return min((current, other), key=lambda src: order.get(src, len(order)))


__all__ = (
    "CODE_HH",
    "SOURCE_HH",
    "PRIORITY",
    "SITES",
    "Site",
    "collect_external",
    "hh_enabled",
    "label_of",
    "main_source",
    "max_pages",
    "pause",
    "queue_note",
    "workers",
    "ready",
    "selected",
    "start_external",
)
