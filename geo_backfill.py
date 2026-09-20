"""Дозаполнение адресов для уже собранных вакансий.

У вакансий, собранных до появления карты, координат нет: их никто не читал со
страницы. Ждать, пока каждая снова попадётся в выдаче, долго, а часть уже
закрыта навсегда.

Задача сознательно ручная и с потолком: каждая страница — это запрос к hh.ru и
пауза; тысяча страниц подряд — прямая дорога к капче [CORE-016].

    python geo_backfill.py --limit 200

Если точек не появилось вообще, виновата не база, а страница: есть режим
разбора одной вакансии — он ничего не пишет в базу и показывает, что именно
нашлось на странице:

    python geo_backfill.py --probe 12345678
    python geo_backfill.py --probe https://hh.ru/vacancy/12345678
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

import db
import geo
import settings
from hh_html import VACANCY_PREFIX, HHHtmlClient
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


def probe(target: str) -> int:
    """Разбирает одну страницу и показывает найденные адреса. Базу не трогает."""
    import hh_html

    ident = geo.vacancy_id(target) or target.strip()
    with _client() as client:
        page = client.fetch(VACANCY_PREFIX + ident)
    try:
        state = hh_html.extract_state(page)
    except hh_html.ExtractionError as exc:
        path = hh_html.dump_failure(page, "probe-no-state-" + ident)
        log.error("состояние страницы не разобралось (%s), сырая страница: %s", exc, path)
        return 1

    log.info("корневые ключи состояния: %s", list(state)[:20])
    candidates = geo.find_address_nodes(state)
    log.info("похожих на адрес узлов: %s", len(candidates))
    for index, raw in enumerate(candidates[:5], start=1):
        log.info(
            "кандидат %s: %s",
            index,
            json.dumps(raw, ensure_ascii=False)[:400],
        )
    point = geo.from_page(page, ident)
    if point is None:
        path = hh_html.dump_failure(page, "probe-no-address-" + ident)
        log.error("адрес не найден; сырая страница сохранена: %s", path)
        return 1
    log.info(
        "адрес: %s | метро: %s | точка: %s",
        point.address or "—",
        point.metro or "—",
        "{}, {}".format(point.lat, point.lng) if point.mappable else "нет",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Адреса для карты")
    parser.add_argument(
        "--limit",
        type=int,
        default=settings.as_int(os.getenv("GEO_BACKFILL_LIMIT"), 100),
        help="сколько вакансий обойти за раз",
    )
    parser.add_argument(
        "--probe",
        metavar="ID|URL",
        help="разобрать одну страницу и показать, что нашлось (база не меняется)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(Path(settings.get("LOG_PATH", "data/fuckhr.log")), args.verbose)

    if args.probe:
        return probe(args.probe)

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
        if not filled and tried:
            log.warning(
                "ни одной точки: запусти python geo_backfill.py --probe <id вакансии> "
                "и пришли вывод — либо hh.ru отдаёт страницу без адреса без cookie, "
                "либо поля адреса переименованы"
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
