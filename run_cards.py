"""Строки под карточкой вакансии.

Вынесено из run.py по [CORE-024]. Здесь только сборка подписи: что известно
про работодателя и про саму вакансию, в том порядке, в каком это читают.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from typing import Sequence

import aitext
import company_score_store
import conditions
import db
import contact_finds
import contacts
import detector
import injection_store
import dossier
import market
import market_company
import market_store

log = logging.getLogger(__name__)


def card_lines(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    with_score: bool = True,
) -> dict[str, list[str]]:
    """Ключ вакансии → строки для Telegram.

    Порядок не косметика: сначала работодатель, потом утверждения вакансии.
    Красные флаги конторы отменяют смысл читать дальше.
    """
    signals: dict[str, list[str]] = {}
    for row in rows:
        lines: list[str] = []
        if row["market_label"]:
            lines.append(market.row_line(row))
        ai_line = aitext.row_line(row)
        if ai_line:
            lines.append(ai_line)
        if with_score:
            lines += company_score_store.row_lines(
                company_score_store.load(conn, row["company"])
            )
        saved = dossier.load(conn, row["company"]) if row["company"] else None
        if saved is not None:
            lines += dossier.row_to_lines(saved)
        money = market_store.load_company(conn, row["company"]) if row["company"] else None
        if money is not None:
            lines += market_company.row_lines(money)
        lines += injection_store.lines(conn, row["key"])
        lines += detector.load_lines(conn, row["key"])
        # Контакт в карточке: без него владелец не видит, есть ли вообще вход
        # мимо HR-воронки.
        find = contact_finds.load(conn, row["key"], row["company"])
        if find is not None:
            lines += contacts.format_contact_lines(find, limit=1)
        signals[row["key"]] = lines
    return signals


def deliver(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    signals: dict[str, list[str]],
    dry_run: bool = False,
) -> int:
    """Отправка готовых карточек в Telegram. Возвращает число доставленных.

    Вынесено из run.py вместе с card_lines: тот снова упёрся в 25 КБ
    [CORE-024]. Поведение прежнее — в сухом прогоне карточки только пишутся в
    лог, а ненастроенный Telegram не считается ошибкой: собранное уже в базе и
    видно в интерфейсе [CORE-017].
    """
    if dry_run:
        for row in rows:
            log.info("%5.1f  %s — %s", row["score"], row["title"], row["company"])
            log.info("        %s", row["url"])
            for line in conditions.lines(conn, row["key"]):
                log.info("        %s", line)
            for line in signals.get(row["key"], []):
                log.info("        %s", line)
        return 0
    if not rows:
        return 0
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning(
            "Telegram не настроен: %s карточек ждут в базе, смотри их в интерфейсе",
            len(rows),
        )
        return 0
    import bot as tg

    republished = {row["key"]: db.republish_count(conn, row["key"]) for row in rows}
    delivered = asyncio.run(tg.send_cards(token, chat_id, rows, republished, signals))
    db.mark_notified(conn, delivered)
    log.info("отправлено карточек: %s", len(delivered))
    return len(delivered)


__all__ = ("card_lines", "deliver")
