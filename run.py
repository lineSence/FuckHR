"""Один прогон пайплайна MVP (шаг 1 из docs/mvp-windows.md).

    hh.ru /vacancies -> предфильтр -> карточка вакансии -> скоринг -> SQLite -> Telegram

Ни одного LLM-вызова. Запускается из Task Scheduler через pythonw.exe (ADR-014).
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
import db
from hh import HHClient, Vacancy, enrich, from_search_item
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


def collect(client: HHClient, profile: Profile) -> dict[str, Vacancy]:
    """Собирает вакансии по всем запросам профиля.

    Детальная карточка запрашивается только для того, что прошло предфильтр по
    заголовку и вилке: это экономит сотни запросов к API на каждом прогоне.
    """
    found: dict[str, Vacancy] = {}
    for query in profile.queries:
        text = query.get("text")
        if not text:
            continue
        log.info("запрос: %s", text)
        for item in client.search(
            text=text,
            area=query.get("area") or profile.areas or None,
            period=int(query.get("period", 7)),
            max_pages=int(query.get("max_pages", 3)),
            extra=query.get("extra"),
        ):
            draft = from_search_item(item)
            rough = evaluate(draft, profile)
            if rough.rejected:
                log.debug("отброшено на предфильтре: %s (%s)", draft.title, rough.reject_reason)
                continue
            found.setdefault(draft.key, draft)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="FuckHR MVP: один прогон")
    parser.add_argument("--profile", default="profile.yaml")
    parser.add_argument("--limit", type=int, default=10, help="сколько карточек отправлять")
    parser.add_argument("--dry-run", action="store_true", help="без отправки в Telegram")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    db_path = Path(os.getenv("DB_PATH", "data/fuckhr.sqlite3"))
    setup_logging(Path(os.getenv("LOG_PATH", "data/fuckhr.log")), args.verbose)

    profile = Profile.load(args.profile)
    user_agent = os.getenv("HH_USER_AGENT", "")

    conn = db.connect(db_path)
    db.init_schema(conn)

    new_count = 0
    with HHClient(user_agent) as client:
        drafts = collect(client, profile)
        log.info("прошло предфильтр: %s", len(drafts))
        for draft in drafts.values():
            try:
                vacancy = enrich(draft, client.vacancy(draft.external_id))
            except Exception:  # noqa: BLE001 — вакансия могла быть уже закрыта
                log.warning("нет деталей по %s, берём черновик", draft.external_id)
                vacancy = draft
            verdict = evaluate(vacancy, profile)
            # Слепок пишется для всего, даже для отклонённого: история публикаций
            # нужна будущему детектору независимо от нашего интереса (ADR-009, ADR-010).
            db.add_snapshot(conn, vacancy)
            if verdict.rejected:
                continue
            if db.upsert_vacancy(conn, vacancy, verdict.score, verdict.reasons):
                new_count += 1

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

    log.info("итого в базе: %s", db.stats(conn))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
