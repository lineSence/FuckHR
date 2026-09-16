"""Один прогон пайплайна MVP (шаг 1 из docs/mvp-windows.md).

    hh.ru (HTML поиска) -> предфильтр -> страница вакансии -> скоринг -> SQLite -> Telegram

Ни одного LLM-вызова. Запускается из Task Scheduler через pythonw.exe (ADR-014).
Источник данных — HTML страниц hh.ru: публичный API закрыт с апреля 2026 (ADR-015).

У прогона три обязанности, а не одна:
1. собрать и отправить карточки;
2. зафиксировать историю — и появление, и исчезновение вакансии (ADR-010);
3. пожаловаться, если сам сломался (canary.py), а не тихо вернуть ноль.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

import bot as tg
import canary
import db
from hh import Vacancy, enrich
from hh_html import BlockedError, HHHtmlClient
from score import Profile, evaluate

log = logging.getLogger("fuckhr")


def setup_logging(log_path: Path, verbose: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    ]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def collect(
    client: HHHtmlClient, profile: Profile
) -> tuple[dict[str, Vacancy], dict[str, Vacancy]]:
    """Собирает вакансии по всем запросам профиля.

    Возвращает две карты: всё увиденное и то, что прошло предфильтр. Первая нужна
    истории: «вакансия видна в выдаче» — факт о рынке, независимый от нашего интереса.

    Страница вакансии запрашивается только для того, что прошло предфильтр по
    заголовку и вилке: каждый лишний запрос приближает капчу.
    """
    seen: dict[str, Vacancy] = {}
    passed: dict[str, Vacancy] = {}
    for query in profile.queries:
        text = query.get("text")
        if not text:
            continue
        log.info("запрос: %s", text)
        for draft in client.search(
            text=text,
            area=query.get("area") or profile.areas or None,
            period=int(query.get("period", 7)),
            max_pages=int(query.get("max_pages", 3)),
            extra=query.get("extra"),
        ):
            seen.setdefault(draft.key, draft)
            rough = evaluate(draft, profile)
            if rough.rejected:
                log.debug("отброшено на предфильтре: %s (%s)", draft.title, rough.reject_reason)
                continue
            passed.setdefault(draft.key, draft)
    return seen, passed


def notify_if_broken(stats: canary.RunStats, dry_run: bool) -> list[canary.Alert]:
    """Считает поводы для тревоги и пишет в Telegram не чаще раза в сутки."""
    alerts = canary.check(stats)
    for alert in alerts:
        log.warning("канарейка [%s]: %s", alert.kind, alert.text.replace("\n", " "))
    if not alerts or dry_run:
        return alerts

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("канарейке некуда писать: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return alerts

    state_path = Path(os.getenv("ALERT_STATE_PATH", "data/alerts.json"))
    state = canary.load_state(state_path)
    due = canary.filter_due(
        state, alerts, cooldown_hours=float(os.getenv("ALERT_COOLDOWN_HOURS", "24"))
    )
    if not due:
        log.info("о этих сбоях уже писали недавно, молчим")
        return alerts
    if asyncio.run(tg.send_alert(token, chat_id, canary.format_message(due))):
        canary.save_state(state_path, state)
    return alerts


def main() -> int:
    parser = argparse.ArgumentParser(description="FuckHR MVP: один прогон")
    parser.add_argument("--profile", default="profile.yaml")
    parser.add_argument("--limit", type=int, default=10, help="сколько карточек отправлять")
    parser.add_argument("--dry-run", action="store_true", help="без отправки в Telegram")
    parser.add_argument(
        "--no-details", action="store_true", help="не ходить за страницами вакансий"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    db_path = Path(os.getenv("DB_PATH", "data/fuckhr.sqlite3"))
    setup_logging(Path(os.getenv("LOG_PATH", "data/fuckhr.log")), args.verbose)

    profile = Profile.load(args.profile)

    conn = db.connect(db_path)
    db.init_schema(conn)

    new_count = 0
    enriched = 0
    empty_descriptions = 0
    blocked = False
    seen: dict[str, Vacancy] = {}
    drafts: dict[str, Vacancy] = {}

    client = HHHtmlClient(
        pause=float(os.getenv("HH_PAUSE", "2.0")),
        cookie=os.getenv("HH_COOKIE") or None,
        proxy=os.getenv("HH_PROXY") or None,
        failure_dir=os.getenv("FAILURE_DIR", "data/failures"),
    )
    try:
        seen, drafts = collect(client, profile)
        log.info("увидели: %s, прошло предфильтр: %s", len(seen), len(drafts))
        for draft in drafts.values():
            vacancy = draft
            if not args.no_details:
                try:
                    vacancy = enrich(draft, client.vacancy(draft.external_id))
                    enriched += 1
                    if not vacancy.description.strip():
                        empty_descriptions += 1
                except BlockedError:
                    # Дальше ходить бессмысленно: сохраняем то, что уже собрали.
                    log.error("hh.ru закрылся капчей на деталях, добирать остальное не будем")
                    blocked = True
                    args.no_details = True
                except Exception:  # noqa: BLE001 — вакансия могла быть уже закрыта
                    log.warning("нет деталей по %s, берём черновик", draft.external_id)
            verdict = evaluate(vacancy, profile)
            # Слепок пишется для всего, даже для отклонённого: история публикаций
            # нужна будущему детектору независимо от нашего интереса (ADR-009, ADR-010).
            db.add_snapshot(conn, vacancy)
            if verdict.rejected:
                continue
            if db.upsert_vacancy(conn, vacancy, verdict.score, verdict.reasons):
                new_count += 1
    except BlockedError as exc:
        blocked = True
        log.error("%s", exc)
        print(f"hh.ru заблокировал сбор: {exc}")
    finally:
        client.close()

    # Отметка «видели сегодня» нужна и для отклонённых вакансий, иначе они будут
    # считаться пропавшими сразу после первого прогона.
    if seen:
        db.touch_seen(conn, seen.keys())

    # Вторая половина истории: что исчезло из выдачи. Только после чистого прогона:
    # при капче или сломанном парсере мы бы «закрыли» всю базу разом и испортили историю.
    if not blocked and not client.fallback_pages and seen:
        db.deactivate_missing(conn, seen.keys())

    notify_if_broken(
        canary.RunStats(
            collected=len(seen),
            passed=len(drafts),
            blocked=blocked,
            fallback_pages=client.fallback_pages,
            enriched=enriched,
            empty_descriptions=empty_descriptions,
            failures=list(client.failures),
        ),
        dry_run=args.dry_run,
    )

    rows = db.pending_cards(conn, profile.min_score, args.limit)
    log.info("новых вакансий: %s, к отправке: %s", new_count, len(rows))

    if args.dry_run:
        for row in rows:
            print(f"{row['score']:5.1f}  {row['title']} — {row['company']}")
            print(f"        {row['url']}")
    elif rows:
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        republished = {row["key"]: db.republish_count(conn, row["key"]) for row in rows}
        delivered = asyncio.run(tg.send_cards(token, chat_id, rows, republished))
        db.mark_notified(conn, delivered)
        log.info("отправлено карточек: %s", len(delivered))

    filled, total = db.published_at_coverage(conn)
    log.info("итого в базе: %s, слепков с датой публикации: %s из %s", db.stats(conn), filled, total)
    conn.close()
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
