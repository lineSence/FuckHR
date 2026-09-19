"""Строки под карточкой вакансии.

Вынесено из run.py по [CORE-024]. Здесь только сборка подписи: что известно
про работодателя и про саму вакансию, в том порядке, в каком это читают.
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

import aitext
import company_score_store
import contact_finds
import contacts
import detector
import dossier
import market
import market_company
import market_store


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
        lines += detector.load_lines(conn, row["key"])
        # Контакт в карточке: без него владелец не видит, есть ли вообще вход
        # мимо HR-воронки.
        find = contact_finds.load(conn, row["key"], row["company"])
        if find is not None:
            lines += contacts.format_contact_lines(find, limit=1)
        signals[row["key"]] = lines
    return signals


__all__ = ("card_lines",)
