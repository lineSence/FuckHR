"""Дозаполнение адресов для уже собранных вакансий.

У вакансий, собранных до появления карты, координат нет: их никто не читал со
страницы. Ждать, пока каждая снова попадётся в выдаче, долго, а часть уже
закрыта навсегда.

Задача сознательно ручная и с потолком: каждая страница — это запрос к hh.ru и
пауза; тысяча страниц подряд — прямая дорога к капче [CORE-016].

    python geo_backfill.py --limit 200
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

import db
import geo
import settings
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Адреса для карты")
    parser.add_argument(
        "--limit",
        type=int,
        default=settings.as_int(os.getenv("GEO_BACKFILL_LIMIT"), 100),
        help="сколько вакансий обойти за раз",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(Path(settings.get("LOG_PATH", "data/fuckhr.log")), args.verbose)
    conn = db.connect(Path(settings.get("DB_PATH", "data/fuckhr.sqlite3")))
    try:
        db.init_schema(conn)
        geo.ensure_schema(conn)
        mapped, total = geo.coverage(conn)
        log.info("до запуска: точки есть у %s из %s вакансий", mapped, total)
        with _client() as client:
            filled, tried = geo.backfill(conn, client, args.limit)
        mapped, total = geo.coverage(conn)
        log.info(
            "готово: добавлено %s точек за %s попыток, теперь %s из %s",
            filled,
            tried,
            mapped,
            total,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
