"""Telegram-слой: отправка карточек и сбор реакций.

Два режима работы:
- send_cards() — одноразовая отправка из run.py, без polling;
- python bot.py — долгоживущий polling, чтобы кнопки писали feedback в базу.

Кнопки в MVP меняют только оценку релевантности. Никакой отправки писем нет
и не планируется — см. ADR-012 и [CORE-023].
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sqlite3
from typing import Sequence

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import db

log = logging.getLogger(__name__)

EXPERIENCE_RU = {
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "больше 6 лет",
}


def format_card(row: sqlite3.Row, republished: int = 1) -> str:
    reasons = json.loads(row["score_reasons"] or "[]")
    title = html.escape(row["title"] or "без названия")
    company = html.escape(row["company"] or "компания не указана")
    lines = [
        f"<b>{title}</b>",
        f"{company} · {html.escape(row['area'] or 'гео не указано')}",
        f"Скор: <b>{row['score']:.0f}</b>/100",
    ]
    if row["experience"]:
        lines.append(f"Опыт: {EXPERIENCE_RU.get(row['experience'], row['experience'])}")
    if reasons:
        lines.append("За что: " + html.escape("; ".join(reasons)))
    if republished > 1:
        # Зародыш детектора HR-брехни: сигнал появляется сам по мере накопления слепков.
        lines.append(f"⚠\ufe0f Публиковалась раз в базе: {republished}")
    lines.append(f"<a href=\"{row['url']}\">Открыть вакансию</a>")
    return "\n".join(lines)


def keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Интересно", callback_data=f"fb:good:{key}"),
                InlineKeyboardButton(text="👎 Мимо", callback_data=f"fb:bad:{key}"),
            ]
        ]
    )


def _bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def send_cards(
    token: str,
    chat_id: str | int,
    rows: Sequence[sqlite3.Row],
    republished: dict[str, int] | None = None,
) -> list[str]:
    """Отправляет карточки и возвращает ключи тех, которые дошли."""
    republished = republished or {}
    delivered: list[str] = []
    bot = _bot(token)
    try:
        for row in rows:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=format_card(row, republished.get(row["key"], 1)),
                    reply_markup=keyboard(row["key"]),
                    disable_web_page_preview=True,
                )
                delivered.append(row["key"])
                await asyncio.sleep(0.6)  # лимит Telegram на сообщения в один чат
            except Exception:  # noqa: BLE001 — одна упавшая карточка не должна рвать прогон
                log.exception("не удалось отправить карточку %s", row["key"])
    finally:
        await bot.session.close()
    return delivered


async def run_polling(token: str, db_path: str) -> None:
    """Опциональный режим: собирает нажатия кнопок в vacancies.feedback."""
    bot = _bot(token)
    dp = Dispatcher()
    conn = db.connect(db_path)
    db.init_schema(conn)

    @dp.callback_query(F.data.startswith("fb:"))
    async def on_feedback(call: CallbackQuery) -> None:
        _, value, key = (call.data or "").split(":", 2)
        db.set_feedback(conn, key, value)
        await call.answer("Записал" if value == "good" else "Понятно")

    try:
        await dp.start_polling(bot)
    finally:
        conn.close()
        await bot.session.close()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    token_env = os.environ["TELEGRAM_BOT_TOKEN"]
    asyncio.run(run_polling(token_env, os.getenv("DB_PATH", "data/fuckhr.sqlite3")))
