"""Запуск этапа контактов: CLI, лог, шлюз и карточки в Telegram.

Отделено от outreach.py, чтобы логика черновиков не зависела от Telegram:
отправка — дело пайплайна, а не предметного модуля ([CORE-023]: система
сама писем не шлёт, в Telegram уходит только карточка владельцу).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from typing import Sequence

import conditions
import contacts
import db
import detector
import llm
import resume
import settings
import websearch
from outreach import (
    APPLY_CHANNEL,
    collect_facts,
    follow_up_cards,
    format_card,
    process_row,
    top_rows,
)

log = logging.getLogger("outreach")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from logs import quiet_libraries  # noqa: PLC0415 — логи настраивает только запуск

    quiet_libraries(verbose)


def build_gateway(conn: sqlite3.Connection, disabled: bool) -> llm.Gateway | None:
    """Шлюз или None. None — штатный режим, а не авария [CORE-017]."""
    if disabled:
        log.info("модель выключена в настройках (LLM_ENABLED)")
        return None
    candidate = llm.Gateway.from_env(conn)
    if not candidate.enabled:
        log.info("модель не настроена (%s), идём без неё", candidate.disabled_reason)
        return None
    for stage, profile, route, model, source in candidate.describe_routes():
        if stage in {"company", "contacts", "draft"}:
            log.info(
                "этап %s: профиль %s, маршрут %s, модель %s (имя из: %s)",
                stage, profile, route, model, source,
            )
    return candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Поиск нанимающего менеджера и черновик письма. Настройки — в webui.py"
    )
    parser.add_argument("--dry-run", action="store_true", help="ничего не писать и не шлать")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    options = settings.outreach_options()
    log.info(
        "настройки: лимит %s, порог %s, профиль %s, без прямого контакта %s, MX %s, модель %s",
        options.limit,
        options.min_score,
        options.profile,
        "да" if options.allow_generic else "нет",
        "да" if options.check_mx else "нет",
        "да" if options.use_llm else "нет",
    )

    conn = db.connect(settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    db.init_schema(conn)
    contacts.ensure_schema(conn)
    detector.ensure_schema(conn)
    conditions.ensure_schema(conn)
    resume.ensure_schema(conn)

    # Факты читаются после открытия базы: главный их источник теперь резюме.
    facts = collect_facts(conn, options.profile)
    if not facts:
        log.warning(
            "ни подтверждённых блоков резюме, ни фактов в %s — в черновике будет "
            "заглушка; заполните резюме на /resume",
            options.profile,
        )

    gateway = build_gateway(conn, not options.use_llm)

    provider = websearch.SearchProvider.from_env(conn)
    if not provider.enabled:
        log.info(
            "внешний поиск выключен (%s): ищем только в том, что уже собрано",
            provider.disabled_reason,
        )

    rows = top_rows(conn, options.min_score, options.limit)
    if not rows:
        log.info("нет вакансий со скором >= %s", options.min_score)
        return 0

    cards: list[tuple[str, int | None]] = []
    prepared = 0
    generic_cards = 0
    for row in rows:
        discovery, draft, skip_reason = process_row(
            conn,
            row,
            facts,
            provider,
            check_mx=options.check_mx,
            allow_generic=options.allow_generic,
            gateway=gateway,
        )
        if skip_reason:
            log.info("%s: пропуск — %s", row["key"], skip_reason)
            continue

        best = discovery.candidates[0]
        if best.channel_kind == APPLY_CHANNEL:
            generic_cards += 1
        contact_id = None
        if not args.dry_run:
            contact_id = contacts.store(conn, row["key"], row["company"], best)
        cards.append(
            (
                format_card(
                    row,
                    discovery,
                    draft,
                    detector.load_lines(conn, row["key"]),
                    conditions.lines(conn, row["key"]),
                ),
                contact_id,
            )
        )
        prepared += 1

    cards.extend(follow_up_cards(conn, facts, args.dry_run))

    for card, _ in cards:
        print(card)
        print("-" * 40)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if cards and not args.dry_run and token and chat_id:
        import bot as tg

        for card, contact_id in cards:
            # Кнопки статуса [OUT-006]: без них контакт навсегда остаётся в drafted.
            asyncio.run(tg.send_contact_card(token, chat_id, card, contact_id))

    direct, total = contacts.coverage(conn)
    log.info(
        "подготовлено: %s (из них без прямого контакта %s); "
        "в логе контактов вакансий с прямым контактом %s из %s",
        prepared,
        generic_cards,
        direct,
        total,
    )
    if gateway is not None:
        usage = gateway.usage
        log.info(
            "модель: вызовов %s, из кэша %s, ошибок %s, пропущено %s",
            usage.calls,
            usage.cached,
            usage.failures,
            usage.skipped,
        )
    if not options.allow_generic and prepared == 0:
        log.info(
            "прямых контактов в тексте вакансий почти не бывает: "
            "включи внешний поиск и режим «сопроводительное к отклику» в настройках"
        )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
