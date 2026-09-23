"""Карточки вакансий hh.ru пулом потоков.

Раньше описание качалось прямо в цикле обработки: запрос → пауза → скоринг →
следующий запрос. Пауза перед hh.ru обязательна, но простаивать в ней главному
потоку незачем. Здесь карточки заказываются заранее и качаются несколькими
потоками, а цикл забирает готовое.

Темп к hh.ru при этом прежний: очередь держит общий бакет (`net_rate`), а не
число потоков [CORE-014]. Соединение sqlite между потоками не делится — сюда
оно и не попадает: потоки только ходят в сеть, в базу пишет главный поток.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import settings
from hh_html import BlockedError

log = logging.getLogger("fuckhr")

MAX_DETAIL_WORKERS = 4  # выше смысла нет: темп всё равно держит бакет hh.ru


def detail_workers() -> int:
    """Сколько карточек качается одновременно."""
    raw = int(settings.as_float(settings.get("HH_DETAIL_WORKERS", "3"), 3.0))
    return max(1, min(MAX_DETAIL_WORKERS, raw))


class Details:
    """Очередь карточек. `submit` заказывает, `get` забирает готовую."""

    def __init__(self, client: Any, workers: int | None = None) -> None:
        self.client = client
        self.workers = workers if workers is not None else detail_workers()
        self.pool = ThreadPoolExecutor(
            max_workers=max(1, self.workers), thread_name_prefix="detail"
        )
        self._jobs: dict[str, Future] = {}
        self.blocked = False
        self.fetched = 0
        self.failed = 0

    def __len__(self) -> int:
        return len(self._jobs)

    def submit(self, key: str, vacancy_id: str) -> None:
        if key in self._jobs:
            return
        self._jobs[key] = self.pool.submit(self._one, vacancy_id)

    def _one(self, vacancy_id: str) -> dict[str, Any] | None:
        # Капча закрывает не одну карточку, а весь поход: остальные заказы
        # добирать бессмысленно, и пусть они закончатся быстро.
        if self.blocked:
            return None
        try:
            return self.client.vacancy(vacancy_id)
        except BlockedError:
            self.blocked = True
            log.error("hh.ru закрылся капчей на деталях, добирать остальное не будем")
            return None
        except Exception as exc:  # noqa: BLE001 — вакансия могла быть уже закрыта
            log.warning("нет деталей по %s: %s", vacancy_id, exc)
            return None

    def get(self, key: str) -> dict[str, Any] | None:
        """Ждёт свою карточку. None — не вышло, работаем с черновиком."""
        future = self._jobs.get(key)
        if future is None:
            return None
        try:
            detail = future.result()
        except Exception as exc:  # noqa: BLE001 — один сбой не стоит прогона
            log.warning("карточка %s не приехала: %s", key, exc)
            detail = None
        if detail is None:
            self.failed += 1
        else:
            self.fetched += 1
        return detail

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


def plan(
    conn: Any,
    drafts: Any,
    details: "Details",
    bundle: Any,
    owners: dict[str, list[str]],
    fuzzy: int,
    delta: float,
    with_details: bool,
) -> tuple[dict[str, Any], int]:
    """Кому описание уже есть в базе, кому его качать, а кому не стоит.

    Возвращает карту «ключ → описание из базы» и число вакансий, которым
    карточку не качаем из-за ворот (`PREFILTER_DETAILS_DELTA`). Заказы уходят
    в пул сразу: к началу цикла первые карточки уже в пути.
    """
    import db  # noqa: PLC0415 — модуль сети не должен тянуть базу при импорте
    import hh_pages
    import sources

    cached_details: dict[str, Any] = {}
    skipped = 0
    for draft in drafts:
        cached = db.cached_details(conn, draft.key) if with_details else None
        if cached and (not draft.published_at or cached[2] == draft.published_at):
            cached_details[draft.key] = cached
        elif with_details and hh_pages.worth_details(
            draft, bundle, owners.get(draft.key), fuzzy, delta
        ):
            details.submit(draft.key, draft.external_id)
        elif with_details and draft.source == sources.SOURCE_HH:
            skipped += 1
    if len(details):
        log.info("карточек к загрузке: %s, потоков %s", len(details), details.workers)
    return cached_details, skipped


__all__ = ("MAX_DETAIL_WORKERS", "Details", "detail_workers", "plan")
