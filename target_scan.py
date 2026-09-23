"""Шаги по цели: вакансии компании и отзывы с оценкой (ADR-025).

Отдельный файл, потому что интерфейс запускает задачи подпроцессами (jobs.py),
а в argv из браузера уходит только **id цели**: название компании берётся из
своей базы. Ввод владельца свободный, и подставлять его в командную строку
нельзя.

- `--step vacancies` — все вакансии работодателя на hh.ru. Описания страницами
  не догружаются: шаг должен быть быстрым, а текст вакансии подтянется, когда
  она понадобится письмом.
- `--step reviews` — досье по отзывам и общая оценка работодателя. Это те же
  `research.research_companies` и `company_score_store`, что и в прогоне.
- `--step all` — всё по очереди: вакансии, адреса для карты, отзывы с оценкой,
  глубокий ресёрч. Это и есть кнопка на странице цели: раньше их было три, и
  владелец всё равно жал все три подряд.

Шаг `all` идёт до конца: если вакансии не собрались или ресёрч выключен, в лог
уходит строка, а следующий шаг всё равно выполняется [CORE-017].

Адреса добираются только своим вакансиям (`geo.backfill(..., keys=...)`):
обычный сбор берёт их страницей вакансии, а шаг по цели страниц не открывает —
без этого цель на карте не появлялась.
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
import geo
import research
import research_deep
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


# Потолок страниц на адреса за один шаг: каждая — пауза в пару секунд, и шаг
# не должен идти часами. Остаток доберёт кнопка на карте.
GEO_LIMIT = 50


def scan_vacancies(conn, target: targets.Target, pages: int) -> list[str]:
    """Вакансии работодателя. Возвращает ключи вакансий цели."""
    with _client() as client:
        targets_hh.scan(conn, client, target, targets_hh.load_bundle(), pages)
    return targets.keys(conn, target.id)


def scan_geo(conn, keys: list[str], limit: int = GEO_LIMIT) -> tuple[int, int]:
    """Адреса для карты — только вакансиям этой цели."""
    geo.ensure_schema(conn)
    with _client() as client:
        return geo.backfill(conn, client, limit, keys)


def scan_deep(conn, target: targets.Target) -> bool:
    """Глубокий ресёрч. False — выключен настройкой."""
    if not deepresearch.options().enabled:
        log.warning("глубокий ресёрч выключен настройкой DEEP_ENABLED")
        return False
    deepresearch_store.ensure_schema(conn)
    report = deepresearch.research(conn, target.company)
    for line in research_deep.report_lines(report):
        log.info("%s", line)
    return True


def scan_all(conn, target: targets.Target, db_path: Path, pages: int, use_llm: bool) -> None:
    """Всё по цели по очереди. Упавший шаг не отменяет следующие [CORE-017]."""
    keys: list[str] = []
    if not target.resolved:
        log.warning(
            "у цели «%s» нет работодателя на hh.ru: вакансии и адреса пропускаю",
            target.company,
        )
    else:
        try:
            keys = scan_vacancies(conn, target, pages)
        except Exception as exc:  # noqa: BLE001 — дальше есть что делать
            log.warning("вакансии не собрались: %s", exc)
    if keys:
        try:
            filled, tried = scan_geo(conn, keys)
            log.info("адреса: точек %s из %s попыток", filled, tried)
        except Exception as exc:  # noqa: BLE001
            log.warning("адреса не добрались: %s", exc)
    try:
        scan_reviews(conn, target, db_path, use_llm)
    except Exception as exc:  # noqa: BLE001
        log.warning("отзывы и оценка не собрались: %s", exc)
    try:
        scan_deep(conn, target)
    except Exception as exc:  # noqa: BLE001
        log.warning("глубокий ресёрч не вышел: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Шаг по цели")
    parser.add_argument("--target", required=True, type=int, help="id цели")
    parser.add_argument("--step", required=True, choices=("all", "vacancies", "reviews"))
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
        use_llm = settings.flag("LLM_ENABLED")
        if args.step == "all":
            scan_all(conn, target, db_path, args.pages, use_llm)
        elif args.step == "vacancies":
            if not target.resolved:
                log.error(
                    "у цели «%s» нет работодателя на hh.ru: выбери компанию из "
                    "кандидатов на странице «Цели»",
                    target.company,
                )
                return 1
            scan_vacancies(conn, target, args.pages)
        else:
            scan_reviews(conn, target, db_path, use_llm=use_llm)
        if args.step in ("all", "reviews"):
            targets.mark_scan(conn, target.id)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
