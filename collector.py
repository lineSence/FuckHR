"""Сбор вакансий с выдачи и предфильтр.

Выделено из run.py по [CORE-024]; `run.collect` продолжает работать через
реэкспорт. Здесь заканчивается всё, что зависит от площадки: дальше прогон
работает с уже собранными объектами.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Sequence

import hh_pages
import market
import market_store
import settings
from hh import Vacancy
from hh_html import HHHtmlClient
import query_plan
from score import Profile, ceiling, evaluate

log = logging.getLogger("fuckhr")


def _report_capped(capped: int) -> None:
    """Строка про отсев по потолку. Молчит, когда отсекать было нечего."""
    if not capped:
        return
    log.info(
        "потолок отсёк %s: с таким названием порог профиля недостижим "
        "при любом описании",
        capped,
    )


def collect(
    client: HHHtmlClient,
    profile: Profile,
    limit: int = 0,
    prefilter: settings.PrefilterOptions | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Vacancy], dict[str, Vacancy]]:
    """Собирает вакансии по запросам профиля, но не больше limit штук.

    Возвращает две карты: всё увиденное и то, что прошло предфильтр. Первая нужна
    истории: «вакансия видна в выдаче» — факт о рынке, независимый от нашего интереса.

    limit останавливает обход сразу как только набралось нужное число: генератор
    поиска бросается недочитанным, и остальные страницы не запрашиваются. Каждая
    незапрошенная страница — это сэкономленные две-три секунды паузы и шаг от капчи.

    Вторая причина не запрашивать страницу — она уже известна целиком (B-15,
    пункт 4 в `docs/performance.md`). Выдача отсортирована по дате публикации,
    поэтому после такой страницы идёт только то, что мы уже видели. Правило
    работает при подключённой базе и снимается настройкой `HH_STOP_ON_KNOWN`.

    Зарплатные наблюдения снимаются здесь же, со всей выдачи и до предфильтра.
    Считать рынок по прошедшим профиль нельзя: порог владельца обрезает выборку
    снизу, и метки «ниже рынка» не существовало бы в принципе. Страница уже
    скачана, новых запросов к hh.ru это не добавляет [CORE-016].

    Предфильтр настраивается: его можно выключить целиком или поднять порог
    чернового скора. Скор на выдаче занижен — описания ещё нет, поэтому по
    умолчанию порог нулевой и отсев идёт только по стоп-словам и вилке.
    """
    prefilter = prefilter or settings.prefilter_options()
    known_page = hh_pages.known_page_checker(conn)
    stop_kwargs = {"known_page": known_page} if known_page is not None else {}
    observations: list[market.Observation] = []
    seen: dict[str, Vacancy] = {}
    passed: dict[str, Vacancy] = {}
    capped = 0
    stopped_by_limit = False
    queries = [q for q in profile.queries if q.get("text")]
    for index, query in enumerate(queries, start=1):
        if limit and len(passed) >= limit:
            log.info("лимит %s набран, остальные запросы не трогаем", limit)
            break
        text = query["text"]
        log.info("[%s/%s] запрос: %s", index, len(queries), text)
        pages = client.search(
            text=text,
            area=query.get("area") or profile.areas or None,
            period=int(query.get("period", 7)),
            max_pages=int(query.get("max_pages") or 0),
            extra=query.get("extra"),
            **stop_kwargs,
        )
        try:
            for draft in pages:
                if draft.key not in seen:
                    point = market.observe(draft)
                    if point is not None:
                        observations.append(point)
                seen.setdefault(draft.key, draft)
                rough = evaluate(draft, profile, prefilter.fuzzy)
                if prefilter.enabled and rough.rejected:
                    log.debug(
                        "отброшено на предфильтре: %s (%s)",
                        draft.title,
                        rough.reject_reason,
                    )
                    continue
                if prefilter.enabled and rough.score < prefilter.min_score:
                    log.debug(
                        "отброшено на предфильтре: %s (черновой скор %.1f < %.1f)",
                        draft.title,
                        rough.score,
                        prefilter.min_score,
                    )
                    continue
                if prefilter.enabled:
                    top = ceiling(draft, profile, prefilter.fuzzy)
                    if top < profile.min_score:
                        capped += 1
                        log.debug(
                            "отброшено на предфильтре: %s (потолок %.1f < порога %.1f)",
                            draft.title,
                            top,
                            profile.min_score,
                        )
                        continue
                passed.setdefault(draft.key, draft)
                if limit and len(passed) >= limit:
                    stopped_by_limit = True
                    log.info(
                        "собрали %s вакансий при лимите %s, больше страниц не запрашиваем",
                        len(passed),
                        limit,
                    )
                    break
        finally:
            # Генератор закрываем явно: иначе он доживает до сборки мусора и не
            # очевидно когда отпустит соединение.
            pages.close()
    # Лимит не набран, а страницы кончились — это не сбой сбора, а конец
    # выдачи: ниже по прогону число вакансий объяснять больше нечем.
    if limit and not stopped_by_limit and len(passed) < limit:
        log.warning(
            "вакансии в выдаче кончились: найдено %s из лимита %s "
            "(увидели всего %s). Больше по этим запросам hh.ru не отдаёт — "
            "расширь срок, географию или добавь запрос.",
            len(passed),
            limit,
            len(seen),
        )
    _report_capped(capped)
    if conn is not None and observations:
        # Наблюдения пишутся одним куском после обхода: держать транзакцию
        # открытой на всё время пауз hh.ru незачем.
        market_store.record(conn, observations)
        log.info("зарплатных наблюдений записано: %s", len(observations))
    return seen, passed


def collect_plan(
    client: HHHtmlClient,
    bundle: Sequence[Any],
    limit: int = 0,
    prefilter: settings.PrefilterOptions | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Vacancy], dict[str, Vacancy], dict[str, list[str]]]:
    """Сбор по плану: каждый запрос выполняется один раз на все профили.

    Отличие от `collect` — не в сборе, а в том, кому достаётся страница. Раньше
    обход шёл по профилям, и одинаковый запрос двух профилей стоил двух обходов
    (кэш снимал только дословные повторы внутри своего срока). Теперь запросы
    сведены в план (`query_plan`), а предфильтр считается по каждому профилю,
    который этот запрос заказывал.

    Лимит по-прежнему на профиль: он ограничивает, сколько вакансий профиль
    забирает в прогон. Запрос перестаёт читаться, когда все его профили набрали
    своё.
    """
    prefilter = prefilter or settings.prefilter_options()
    known_page = hh_pages.known_page_checker(conn)
    stop_kwargs = {"known_page": known_page} if known_page is not None else {}
    observations: list[market.Observation] = []
    seen: dict[str, Vacancy] = {}
    passed: dict[str, Vacancy] = {}
    owners: dict[str, list[str]] = {}
    capped = 0
    taken: dict[str, int] = {loaded.id: 0 for loaded in bundle}

    tasks = query_plan.build(bundle)
    for index, task in enumerate(tasks, start=1):
        hungry = [lo for lo in task.owners if not limit or taken[lo.id] < limit]
        if not hungry:
            log.info("запрос %r пропущен: его профили уже набрали лимит", task.text)
            continue
        log.info(
            "[%s/%s] запрос: %s (профилей: %s)",
            index,
            len(tasks),
            task.text,
            ", ".join(lo.id for lo in hungry),
        )
        pages = client.search(**task.kwargs(), **stop_kwargs)
        try:
            for draft in pages:
                if draft.key not in seen:
                    point = market.observe(draft)
                    if point is not None:
                        observations.append(point)
                seen.setdefault(draft.key, draft)
                for loaded in hungry:
                    if limit and taken[loaded.id] >= limit:
                        continue
                    if loaded.id in owners.get(draft.key, []):
                        continue
                    rough = evaluate(draft, loaded.profile, prefilter.fuzzy)
                    if prefilter.enabled and rough.rejected:
                        log.debug(
                            "профиль %s отбросил на предфильтре: %s (%s)",
                            loaded.id,
                            draft.title,
                            rough.reject_reason,
                        )
                        continue
                    if prefilter.enabled and rough.score < prefilter.min_score:
                        log.debug(
                            "профиль %s отбросил на предфильтре: %s (%.1f < %.1f)",
                            loaded.id,
                            draft.title,
                            rough.score,
                            prefilter.min_score,
                        )
                        continue
                    if prefilter.enabled:
                        top = ceiling(draft, loaded.profile, prefilter.fuzzy)
                        if top < loaded.profile.min_score:
                            capped += 1
                            log.debug(
                                "профиль %s отбросил на предфильтре: %s "
                                "(потолок %.1f < порога %.1f)",
                                loaded.id,
                                draft.title,
                                top,
                                loaded.profile.min_score,
                            )
                            continue
                    passed.setdefault(draft.key, draft)
                    owners.setdefault(draft.key, []).append(loaded.id)
                    taken[loaded.id] += 1
                if limit and all(taken[lo.id] >= limit for lo in hungry):
                    log.info(
                        "профили этого запроса набрали по %s вакансий, страницы дальше не нужны",
                        limit,
                    )
                    break
        finally:
            # Генератор закрываем явно: иначе он доживает до сборки мусора и не
            # очевидно когда отпустит соединение.
            pages.close()

    for loaded in bundle:
        log.info("профиль %s: забрал %s вакансий", loaded.id, taken[loaded.id])
    _report_capped(capped)
    if conn is not None and observations:
        market_store.record(conn, observations)
        log.info("зарплатных наблюдений записано: %s", len(observations))
    return seen, passed, owners
