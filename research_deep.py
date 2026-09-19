"""Запуск глубокого ресёрча по одной компании.

Отдельный файл, потому что интерфейс запускает задачи подпроцессами (jobs.py),
а argv собирается из закрытого списка команд. Здесь только разбор аргументов:
вся работа — в deepresearch.py.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

import db
import deepresearch
import deepresearch_store
import settings
from run_setup import setup_logging

log = logging.getLogger("fuckhr")


def main() -> int:
    parser = argparse.ArgumentParser(description="Глубокий ресёрч по компании")
    parser.add_argument("--company", required=True, help="название работодателя")
    parser.add_argument("--force", action="store_true", help="не смотреть на свежесть отчёта")
    parser.add_argument("--verbose", action="store_true", help="подробный лог (DEBUG)")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(Path(settings.get("LOG_PATH", "data/fuckhr.log")), args.verbose)
    opts = deepresearch.options()
    if not opts.enabled:
        log.warning("глубокий ресёрч выключен настройкой DEEP_ENABLED")
        return 1

    conn = db.connect(Path(settings.get("DB_PATH", "data/fuckhr.sqlite3")))
    try:
        deepresearch_store.ensure_schema(conn)
        company = args.company.strip()
        if not args.force and deepresearch_store.is_fresh(conn, company, opts.ttl_days):
            log.info(
                "отчёт по «%s» моложе %s дней — беру сохранённый", company, opts.ttl_days
            )
            return 0
        log.info("глубокий ресёрч: «%s», бюджет времени %.0f с", company, opts.seconds)
        report = deepresearch.research(conn, company)
        for line in report_lines(report):
            log.info("%s", line)
        return 0
    finally:
        conn.close()


def report_lines(report) -> list[str]:
    """Короткий итог в лог: сколько нашли и где [HRD-003]."""
    if not report.findings:
        return ["недостаточно данных: ни одна тема не подтвердилась источником"]
    out = []
    for item in report.findings:
        out.append("{} — {} · {}".format(item.title, item.domain, item.url))
    if report.blocked:
        out.append("пропущены источники: {}".format(", ".join(report.blocked)))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
