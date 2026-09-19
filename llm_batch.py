"""Этапы модели по собранным вакансиям — параллельно.

Выделено из run.py по [CORE-024]. Зачем вообще: extract и hr_filter висели
внутри цикла обхода hh.ru, и каждый вызов на 2–10 секунд останавливал сбор.
Модель ждёт сеть, а не процессор, поэтому вызовы идут пулом потоков, как сбор
досье в research.py.

Границы те же, что у последовательной версии: своё соединение и свой шлюз на
поток, в общие таблицы из потоков никто не пишет — результаты возвращаются
наверх, и записывает их главный поток.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

import db
import detector_llm
import llm
import llm_tasks
import settings

log = logging.getLogger("fuckhr")

MAX_LLM_WORKERS = 8  # выше шлюз начинает отвечать 429, и очередь только растёт


def llm_workers() -> int:
    """Сколько вызовов модели идёт одновременно."""
    raw = int(settings.as_float(settings.get("LLM_WORKERS", "4"), 4.0))
    return max(1, min(MAX_LLM_WORKERS, raw))


def _with_gateway(db_path: Path, work) -> Any:
    conn = db.connect(db_path)
    try:
        gateway = llm.Gateway.from_env(conn)
        if not gateway.enabled:
            return None
        return work(gateway)
    finally:
        conn.close()


def extract_all(
    db_path: Path, vacancies: Sequence[Any], workers: int | None = None
) -> dict[str, Any]:
    """Этап extract по списку вакансий. Ключ вакансии → разобранные условия."""
    return _fan_out(
        db_path,
        {v.key: v for v in vacancies},
        lambda gateway, vacancy: llm_tasks.extract_conditions(gateway, vacancy.description),
        workers,
        "условия",
    )


def claims_all(
    db_path: Path, reports: Sequence[tuple[Any, Any]], workers: int | None = None
) -> dict[str, Any]:
    """Этап hr_filter: отчёт детектора дополняется утверждениями из текста."""
    pairs = {vacancy.key: (vacancy, report) for vacancy, report in reports}
    return _fan_out(
        db_path,
        pairs,
        lambda gateway, pair: detector_llm.with_llm_claims(pair[1], pair[0], gateway),
        workers,
        "утверждения",
    )


def _fan_out(
    db_path: Path, items: dict[str, Any], work, workers: int | None, what: str
) -> dict[str, Any]:
    if not items:
        return {}
    size = min(workers or llm_workers(), len(items))
    log.info("модель: %s по %s вакансиям, потоков %s", what, len(items), size)
    out: dict[str, Any] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=size, thread_name_prefix="llm") as pool:
        futures = {
            pool.submit(_with_gateway, db_path, lambda gw, it=item: work(gw, it)): key
            for key, item in items.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — одна вакансия не роняет прогон
                log.warning("модель не ответила по %s: %s", key, exc)
                continue
            if result is not None:
                out[key] = result
            # Счётчик в квадратных скобках — по нему интерфейс рисует полоску.
            log.info("[%s/%s] модель: %s", done, len(items), what)
    return out


__all__ = ("MAX_LLM_WORKERS", "claims_all", "extract_all", "llm_workers")
