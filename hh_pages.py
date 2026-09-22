"""Страницы выдачи hh.ru: кэш на прогон, остановка на известных, цена карточки.

Пункты 2, 3 и 4 аудита `docs/performance.md` (задача B-15). Все три про одно:
не запрашивать у hh.ru то, что ничего не добавит. Процессор и sqlite в прогоне
не при чём — время делают паузы перед запросами.

- `PageCache` живёт один прогон. Профилей около десятка (ADR-023), и запросы у
  них пересекаются: раньше одинаковые «(текст, регион, период)» качались заново
  для каждого профиля.
- `known_page_checker` останавливает пагинацию, когда страница целиком уже в
  базе и с той же датой публикации. Выдача отсортирована по дате публикации,
  значит дальше идёт только то, что мы уже видели.
- `worth_details` решает, нужна ли карточка вакансии: при `RUN_DETAILS=1` это
  ~70% времени первого прогона.

Отдельный файл, а не правка `hh_html.py`: тот уже близок к 25 КБ [CORE-024].
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Callable, Iterable, Sequence

import settings

log = logging.getLogger("fuckhr")


class PageCache:
    """Страницы выдачи в памяти процесса, со сроком годности.

    На диск не пишется: страница выдачи стареет за часы, а в базе она стала бы
    ещё одним источником несвежих данных. Но и жить ровно один прогон ей не
    обязательно: в режиме цикла соседние прогоны идут через пять минут и
    качают ровно те же первые страницы заново. Отсюда срок годности в минутах
    (`HH_SEARCH_CACHE_MINUTES`) и общий кэш процесса.
    """

    def __init__(self, enabled: bool = True, ttl: float = 0.0) -> None:
        self.enabled = enabled
        self.ttl = max(0.0, float(ttl))
        self.hits = 0
        self._pages: dict[str, dict[int, tuple[float, list[Any]]]] = {}

    def get(self, key: str, page: int) -> list[Any] | None:
        if not self.enabled:
            return None
        found = self._pages.get(key, {}).get(page)
        if found is None:
            return None
        stamp, vacancies = found
        if self.ttl and time.monotonic() - stamp > self.ttl:
            # Протухла — забываем, чтобы не держать память между циклами.
            del self._pages[key][page]
            return None
        self.hits += 1
        return list(vacancies)

    def put(self, key: str, page: int, vacancies: Iterable[Any]) -> None:
        if not self.enabled:
            return
        self._pages.setdefault(key, {})[page] = (time.monotonic(), list(vacancies))


def cache_key(params: dict[str, Any]) -> str:
    """Стабильный ключ запроса: номер страницы в него не входит."""
    rest = {key: value for key, value in params.items() if key != "page"}
    return json.dumps(rest, sort_keys=True, ensure_ascii=False, default=str)


# Один кэш на процесс: прогоны в режиме цикла живут в нём же, и второй круг
# через пять минут не платит за те же первые страницы.
_SHARED: "PageCache | None" = None


def cache_minutes() -> float:
    """Сколько минут страница выдачи считается свежей. Ноль — только этот прогон."""
    return max(0.0, settings.as_float(os.getenv("HH_SEARCH_CACHE_MINUTES"), 10.0))


def search_cache(shared: bool = True) -> PageCache:
    """Кэш по настройке. Выключенный кэш — это просто пустой объект."""
    global _SHARED
    enabled = settings.flag("HH_SEARCH_CACHE")
    ttl = cache_minutes() * 60.0
    if not shared or not ttl:
        return PageCache(enabled=enabled, ttl=ttl)
    if _SHARED is None or _SHARED.enabled != enabled or _SHARED.ttl != ttl:
        _SHARED = PageCache(enabled=enabled, ttl=ttl)
    return _SHARED


def page_is_known(conn: sqlite3.Connection | None, vacancies: Sequence[Any]) -> bool:
    """Вся страница уже в базе и дата публикации не менялась.

    Смена `published_at` означает перепубликацию: такую страницу пропускать
    нельзя, иначе детектор (ADR-009) не увидит свой главный сигнал. Вакансия без
    даты считается известной только по ключу — это осознанная слабость разбора
    HTML, а не допущение здесь.
    """
    if conn is None or not vacancies:
        return False
    keys = [item.key for item in vacancies]
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        "SELECT key, published_at FROM vacancies WHERE key IN ({})".format(placeholders),
        keys,
    ).fetchall()
    known = {row[0]: row[1] for row in rows}
    for item in vacancies:
        if item.key not in known:
            return False
        if item.published_at and known[item.key] != item.published_at:
            return False
    return True


def known_page_checker(
    conn: sqlite3.Connection | None,
) -> Callable[[Sequence[Any]], bool] | None:
    """Предикат для `HHHtmlClient.search` или None, если правило выключено."""
    if conn is None or not settings.flag("HH_STOP_ON_KNOWN"):
        return None
    return lambda vacancies: page_is_known(conn, vacancies)


def details_delta() -> float:
    """Дельта к порогу профиля, в пределах которой карточка ещё качается.

    Ноль — правило выключено и поведение прежнее. Пункт 2 аудита меняет
    наблюдаемость (детектор и разбор условий не увидят отсеянных), поэтому
    включается сознательно, а не втихую.
    """
    return max(0.0, settings.as_float(os.getenv("PREFILTER_DETAILS_DELTA"), 0.0))


def worth_details(
    vacancy: Any,
    bundle: Sequence[Any],
    owner_ids: Iterable[str] | None,
    fuzzy: int,
    delta: float,
) -> bool:
    """Стоит ли открывать карточку вакансии на hh.ru.

    Черновой скор считается по выдаче и занижен: описания ещё нет. Поэтому
    сравнение идёт не с порогом профиля, а с «порог минус дельта», и достаточно
    одного профиля, который вакансию почти пропускает.

    Вакансия не с hh.ru — сразу нет. Открывается `hh.ru/vacancy/<external_id>`,
    а id Работы.ру или Труда России на hh.ru ведёт на другую, вполне
    существующую вакансию: описание и адрес приехали бы от чужого объявления и
    выглядели бы настоящими. У внешних площадок описание и так приходит вместе
    с выдачей, второй запрос им не нужен.
    """
    if str(getattr(vacancy, "source", "") or "") != "hh.ru":
        return False
    if delta <= 0:
        return True
    import profiles  # локально: profiles тянет collector, а тот — этот модуль

    thresholds = {loaded.id: loaded.profile.min_score for loaded in bundle}
    matches = profiles.score_all(vacancy, bundle, owner_ids, fuzzy)
    return any(
        verdict.score >= thresholds.get(pid, 0.0) - delta for pid, verdict in matches
    )


__all__ = (
    "PageCache",
    "cache_minutes",
    "cache_key",
    "details_delta",
    "known_page_checker",
    "page_is_known",
    "search_cache",
    "worth_details",
)
