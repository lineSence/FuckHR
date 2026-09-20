"""Шаги по цели: вакансии, отзывы с оценкой и глубокий ресёрч (ADR-025).

Отдельный файл, потому что интерфейс запускает задачи подпроцессами (jobs.py),
а в argv из браузера уходит только **id цели**: название компании берётся из
своей базы. Ввод владельца свободный, и подставлять его в командную строку
нельзя.

`--step all` — то, что запускает единственная кнопка «Собрать все данные»
(поправка к ADR-025 от 20.09.2026). Шаги идут подряд в одном процессе, потому
что задача в jobs.py всё равно одна: тремя кнопками владелец просто ждал три
раза. Цена шагов от этого не исчезла, поэтому запуск остаётся ручным
[CORE-016]. Отдельные шаги остались флагами: ими удобно перезапускать то, что
упало.

- `--step vacancies` — все вакансии работодателя на hh.ru. Описания страницами
  не догружаются: шаг должен быть быстрым, а текст вакансии подтянется, когда
  она понадобится письмом.
- `--step reviews` — досье по отзывам и общая оценка работодателя. Это те же
  `research.research_companies` и `company_score_store`, что и в прогоне.
- `--step research` — реестр, суды, долги, банкротство, новости
  (`deepresearch.py`).

В режиме `all` невозможный шаг пропускается, а не роняет остальные: у цели по
ИНН нет работодателя на hh.ru, а `DEEP_ENABLED=0` выключает ресёрч. Молчать об
этом нельзя, поэтому пропуск виден строкой в логе [CORE-017].
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

import company_score_store
import db
import deepresearch
import deepresearch_store
import research
import research_deep
import settings
import targets
import targets_hh
from hh_html import HHHtmlClient
from run_setup import setup_logging

log = logging.getLogger("fuckhr")

STEPS = ("vacancies", "reviews", "research")


def _client() -> HHHtmlClient:
    return HHHtmlClient(
        pause=settings.as_float(os.getenv("HH_PAUSE"), 2.0),
        pause_min=settings.as_float(os.getenv("HH_PAUSE_MIN"), 0.8),
        cookie=os.getenv("HH_COOKIE") or None,
        proxy=os.getenv("HH_PROXY") or None,
        failure_dir=settings.get("FAILURE_DIR", "data/failures"),
    )


def scan_reviews(conn, target: targets.Target, db_path: Path, use_llm: bool) -> int:
    """Отзывы и общая оценка работодателя по одной компании."""
    research.research_companies(
        conn, db_path, {target.company: target.site or None}, use_llm=use_llm, force=True
    )
    return company_score_store.refresh(conn, [target.company])


def scan_deep(conn, company: str) -> bool:
    """Глубокий ресёрч по компании. False — шаг пропущен настройкой."""
    opts = deepresearch.options()
    if not opts.enabled:
        log.warning("глубокий ресёрч выключен настройкой DEEP_ENABLED — пропускаю")
        return False
    deepresearch_store.ensure_schema(conn)
    log.info("глубокий ресёрч: «%s», бюджет времени %.0f с", company, opts.seconds)
    report = deepresearch.research(conn, company)
    for line in research_deep.report_lines(report):
        log.info("%s", line)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Шаг по цели")
    parser.add_argument("--target", required=True, type=int, help="id цели")
    parser.add_argument(
        "--step", required=True, choices=("vacancies", "reviews", "research", "all")
    )
    parser.add_argument("--pages", type=int, default=0, help="потолок страниц выдачи")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(Path(settings.get("LOG_PATH", "data/fuckhr.log")), args.verbose)
    db_path = Path(settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    conn = db.connect(db_path)
    try:
        db.init_schema(conn)
        targets.ensure_schema(conn)
        target = targets.get(conn, args.target)
        if target is None:
            log.error("цели %s нет в базе", args.target)
            return 1
        every = args.step == "all"
        plan = STEPS if every else (args.step,)
        for number, step in enumerate(plan, 1):
            # Счётчик в логе не украшение: по нему интерфейс рисует полоску.
            log.info("[%s/%s] цель «%s»: %s", number, len(plan), target.company, step)
            if step == "vacancies":
                if not target.resolved:
                    log.warning(
                        "у цели «%s» нет работодателя на hh.ru: выбери компанию из "
                        "кандидатов на странице «Цели»",
                        target.company,
                    )
                    if not every:
                        return 1
                    continue
                with _client() as client:
                    targets_hh.scan(
                        conn, client, target, targets_hh.load_bundle(), args.pages
                    )
            elif step == "reviews":
                scan_reviews(conn, target, db_path, use_llm=settings.flag("LLM_ENABLED"))
                targets.mark_scan(conn, target.id)
            else:
                if not scan_deep(conn, target.company) and not every:
                    return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
