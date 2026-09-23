"""Этап контактов: компании изучаются параллельно.

Внутри компании запросы к поиску и страницам и так шли пулом, а сами компании
стояли в очереди — при десятке компаний это минуты пустого ожидания сети
(docs/performance.md, пункт 5). Здесь компании расходятся по потокам.

Границы те же, что у `research.py`: поток открывает своё соединение sqlite и
свой провайдер поиска (кэш провайдера пишется в базу, а соединение между
потоками не делится), в общие таблицы пишет главный поток — находки сохраняет
`collect_contacts` после сбора.

Отдельный файл, потому что `outreach.py` уже перешёл 25 КБ [CORE-024].
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

import db
import settings
import websearch

log = logging.getLogger("fuckhr")

MAX_CONTACT_WORKERS = 8  # дальше упираемся в потолок запросов к поиску


def contact_workers() -> int:
    """Сколько компаний изучается одновременно."""
    raw = int(settings.as_float(settings.get("CONTACT_WORKERS", "4"), 4.0))
    return max(1, min(MAX_CONTACT_WORKERS, raw))


def _one_company(
    db_path: Path, rows: Sequence[Any], check_mx: bool
) -> list[tuple[str, Any]]:
    """Работа одного потока: хиты на компанию и находки по её вакансиям."""
    import outreach  # noqa: PLC0415 — обратный импорт только в потоке

    conn = db.connect(db_path)
    try:
        provider = websearch.SearchProvider.from_env(conn)
        best = max(
            rows,
            key=lambda r: r["score"] if "score" in r.keys() and r["score"] is not None else 0,
        )
        name = (best["company"] or "").strip()
        hits = outreach.company_hits(provider, name, best["title"]) if name else []
        out: list[tuple[str, Any]] = []
        for row in rows:
            discovery, _ = outreach.find_contacts(
                conn, row, provider, check_mx=check_mx, hits=hits
            )
            out.append((row["key"], discovery))
        return out
    finally:
        conn.close()


def discover_all(
    db_path: str | Path,
    rows_by_company: dict[str, list[Any]],
    check_mx: bool = False,
    workers: int | None = None,
) -> dict[str, Any]:
    """Находки по всем компаниям. Ключ вакансии → Discovery.

    Одна упавшая компания не роняет этап: без её контактов карточка просто
    останется без прямого канала [CORE-017].
    """
    if not rows_by_company:
        return {}
    size = min(workers or contact_workers(), len(rows_by_company))
    log.info("контакты: компаний %s, потоков %s", len(rows_by_company), size)
    out: dict[str, Any] = {}
    done = 0
    path = Path(db_path)
    with ThreadPoolExecutor(max_workers=size, thread_name_prefix="contacts") as pool:
        futures = {
            pool.submit(_one_company, path, rows, check_mx): company
            for company, rows in rows_by_company.items()
        }
        for future in as_completed(futures):
            company = futures[future]
            done += 1
            try:
                found = future.result()
            except Exception as exc:  # noqa: BLE001 — одна компания не роняет этап
                log.warning("контакты по %s не собрались: %s", company, exc)
                continue
            out.update(dict(found))
            # Счётчик в квадратных скобках — по нему интерфейс рисует полоску.
            log.info(
                "[%s/%s] контакты: %s — %s",
                done,
                len(futures),
                company or "компания не указана",
                "каналов {}".format(
                    sum(1 for _, d in found if d.candidates)
                )
                if any(d.candidates for _, d in found)
                else "ничего",
            )
    return out


def discover_here(
    conn: Any,
    rows_by_company: dict[str, list[Any]],
    provider: Any,
    check_mx: bool = False,
) -> dict[str, Any]:
    """Последовательный обход одним соединением: хиты один раз на компанию.

    Нужен там, где потоки завести нечем: провайдер и его кэш принадлежат
    соединению вызывающего, а делить соединение между потоками нельзя.
    """
    import outreach  # noqa: PLC0415 — обратный импорт

    out: dict[str, Any] = {}
    for name, rows in rows_by_company.items():
        best = max(
            rows,
            key=lambda r: r["score"] if "score" in r.keys() and r["score"] is not None else 0,
        )
        hits = outreach.company_hits(provider, name, best["title"]) if name else []
        for row in rows:
            discovery, _ = outreach.find_contacts(
                conn, row, provider, check_mx=check_mx, hits=hits
            )
            out[row["key"]] = discovery
    return out


__all__ = ("MAX_CONTACT_WORKERS", "contact_workers", "discover_all", "discover_here")
