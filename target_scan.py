"""Шаги по цели: вакансии компании и отзывы с оценкой (ADR-025).

Отдельный файл, потому что интерфейс запускает задачи подпроцессами (jobs.py),
а в argv из браузера уходит только **id цели**: название компании берётся из
своей базы. Ввод владельца свободный, и подставлять его в командную строку
нельзя.

Шаги нарочно раздельны, как решил владелец: каждый стоит времени и внешних
запросов, и решать, тратить ли их, должен человек [CORE-016].

- `--step vacancies` — все вакансии работодателя на hh.ru. Описания страницами
  не догружаются: шаг должен быть быстрым, а текст вакансии подтянется, когда
  она понадобится письмом.
- `--step reviews` — досье по отзывам и общая оценка работодателя. Это те же
  `research.research_companies` и `company_score_store`, что и в прогоне.

Глубокий ресёрч (реестр, суды, долги) остался отдельной задачей
`research_deep.py`: он и до целей запускался кнопкой.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

import company_score_store
import db
import research
import settings
import targets
import targets_hh
from hh_html import HHHtmlClient
from run_setup import setup_logging

log = logging.getLogger("fuckhr")


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Шаг по цели")
    parser.add_argument("--target", required=True, type=int, help="id цели")
    parser.add_argument("--step", required=True, choices=("vacancies", "reviews"))
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
        if args.step == "vacancies":
            if not target.resolved:
                log.error(
                    "у цели «%s» нет работодателя на hh.ru: выбери компанию из "
                    "кандидатов на странице «Цели»",
                    target.company,
                )
                return 1
            with _client() as client:
                targets_hh.scan(
                    conn, client, target, targets_hh.load_bundle(), args.pages
                )
        else:
            scan_reviews(
                conn, target, db_path, use_llm=settings.flag("LLM_ENABLED")
            )
        if args.step == "reviews":
            targets.mark_scan(conn, target.id)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
