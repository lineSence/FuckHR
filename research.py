"""Параллельный сбор досье на компании.

Выделено из run.py по [CORE-024]. Имена реэкспортируются из run, поэтому
`run.research_companies` и `run.research_workers` работают как раньше.

Почему это единственное место с потоками. Сбор с hh.ru последователен сознательно:
паузы между запросами — единственная защита от капчи. А внешний поиск по
компаниям идёт на другие домены, стоит секунды на компанию и зависит только от
сети — его можно вести в несколько потоков.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db
import dossier
import llm
import settings
import websearch

log = logging.getLogger("fuckhr")

MAX_RESEARCH_WORKERS = 8  # выше — верный способ получить бан у апстримов SearXNG


def research_workers() -> int:
    """Сколько компаний изучается одновременно."""
    raw = int(settings.as_float(settings.get("RESEARCH_WORKERS", "4"), 4.0))
    return max(1, min(MAX_RESEARCH_WORKERS, raw))


def _research_one(
    db_path: Path,
    company: str,
    site_url: str | None,
    use_llm: bool,
    limit: int,
    force: bool = False,
) -> dossier.Dossier:
    """Работа одного потока: своё соединение, свой провайдер, свой шлюз.

    Соединение sqlite нельзя делить между потоками, поэтому каждый открывает
    своё. Запись в общие таблицы из потоков не идёт — только кэши поиска и
    модели, где конкурентная запись безопасна при WAL. Само досье сохраняет
    главный поток.
    """
    conn = db.connect(db_path)
    try:
        # «Собрать заново» обходит кэш поиска и кэш страниц: иначе кнопка
        # пересобирает досье из тех же самых страниц [CORE-016 наоборот —
        # здесь владелец сознательно платит за свежесть].
        provider = websearch.SearchProvider.from_env(conn, refresh=force)
        gateway: llm.Gateway | None = None
        if use_llm:
            candidate = llm.Gateway.from_env(conn)
            gateway = candidate if candidate.enabled else None
        return dossier.build(
            company,
            provider,
            gateway=gateway,
            site_url=site_url,
            limit=limit,
            force=force,
        )
    finally:
        conn.close()


def research_companies(
    conn,
    db_path: Path,
    companies: dict[str, str | None],
    use_llm: bool,
    force: bool = False,
) -> dict[str, dossier.Dossier]:
    """Собирает досье на список компаний в несколько потоков.

    Свежие досье не пересобираются: за месяц отзывы о работодателе меняются
    медленнее, чем исчерпывается потолок запросов к поиску.
    """
    dossier.ensure_schema(conn)
    todo: dict[str, str | None] = {}
    for company, site_url in companies.items():
        if not company:
            continue
        if not force and dossier.is_fresh(dossier.load(conn, company)):
            log.debug("досье на %s свежее, пропускаем", company)
            continue
        todo[company] = site_url

    if not todo:
        log.info("досье на все компании уже есть и свежее")
        return {}

    probe = websearch.SearchProvider.from_env(None)
    if not probe.enabled:
        log.warning(
            "досье на %s компаний не собрать: %s",
            len(todo),
            probe.disabled_reason,
        )
        return {}

    workers = min(research_workers(), len(todo))
    log.info("изучаем компаний: %s, потоков: %s", len(todo), workers)

    out: dict[str, dossier.Dossier] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dossier") as pool:
        futures = {
            pool.submit(
                _research_one, db_path, company, site_url, use_llm, 5, force
            ): company
            for company, site_url in todo.items()
        }
        for future in as_completed(futures):
            company = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — одна компания не роняет прогон
                log.warning("досье на %s не собралось: %s", company, exc)
                continue
            # Счётчик в квадратных скобках — по нему интерфейс рисует полоску.
            log.info(
                "[%s/%s] досье: %s — %s",
                done,
                len(todo),
                company,
                dossier.RISK_RU.get(result.risk, result.risk),
            )
            dossier.store(conn, result)
            out[company] = result
    return out
