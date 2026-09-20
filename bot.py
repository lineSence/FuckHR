"""Telegram-слой: отправка карточек, служебные сообщения и сбор реакций.

Два режима работы:
- send_cards() / send_alert() — одноразовая отправка из run.py, без polling;
- python bot.py — долгоживущий polling, чтобы кнопки писали feedback в базу.

Кнопки в MVP меняют только оценку релевантности. Никакой отправки писем нет
и не планируется — см. ADR-012 и [CORE-023].

API Telegram из RU-сегмента напрямую недоступен, поэтому трафик идёт через
прокси из TELEGRAM_PROXY / HTTPS_PROXY: aiohttp, в отличие от httpx, сам переменные
окружения не читает — именно отсюда брались плавающие WinError 121.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Sequence

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import contacts
import db
import settings
import bot_buttons
from bot_buttons import CONTACT_ACTIONS  # noqa: F401 — публичное имя остаётся в bot

log = logging.getLogger(__name__)

EXPERIENCE_RU = {
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "больше 6 лет",
}


def proxy_url() -> str | None:
    """Прокси для api.telegram.org.

    TELEGRAM_PROXY имеет приоритет; иначе берываем стандартные переменные, которые
    уже использует httpx в сборщике — чтобы один VPN работал для всего проекта.
    """
    for name in ("TELEGRAM_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = os.getenv(name)
        if value:
            return value
    return None


def format_card(
    row: sqlite3.Row,
    republished: int = 1,
    signal_lines: Sequence[str] = (),
) -> str:
    """Карточка вакансии с выводами детектора.

    signal_lines приходят из detector.load_lines() и содержат цитаты из вакансии,
    то есть произвольный текст с чужого сайта — отсюда html.escape на каждой
    строке: одинокий < в описании иначе ломает отправку всей карточки.
    """
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
        lines.append(f"⚠\ufe0f Публиковалась раз в базе: {republished}")
    for line in signal_lines:
        lines.append(html.escape(line))
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


def contact_keyboard(contact_id: int) -> InlineKeyboardMarkup:
    """Статусы из [OUT-007]. Отправку система не видит, её отмечает владелец."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправил", callback_data=f"ct:sent:{contact_id}"),
                InlineKeyboardButton(
                    text="🔁 Другой контакт", callback_data=f"ct:other:{contact_id}"
                ),
            ],
            [
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"ct:skip:{contact_id}"),
                InlineKeyboardButton(
                    text="🚫 Не писать", callback_data=f"ct:block:{contact_id}"
                ),
            ],
        ]
    )


def hold_reason(options: "settings.TelegramOptions | None" = None) -> str:
    """Почему сейчас не отправляем. Пустая строка — отправлять можно.

    Оба запрета мягкие: карточка не помечается отправленной и уйдёт следующим
    прогоном, работа прогона не теряется [CORE-017].
    """
    opts = options or settings.telegram_options()
    if not opts.enabled:
        return "отправка в Telegram выключена в настройках"
    now = datetime.now()
    if opts.quiet_at(now.hour * 60 + now.minute):
        return "тихие часы: карточки уйдут следующим прогоном"
    return ""


def _bot(token: str) -> Bot:
    proxy = proxy_url()
    if proxy:
        log.info("Telegram через прокси %s", proxy)
    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    return Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def send_cards(
    token: str,
    chat_id: str | int,
    rows: Sequence[sqlite3.Row],
    republished: dict[str, int] | None = None,
    signals: dict[str, Sequence[str]] | None = None,
    attempts: int = 3,
) -> list[str]:
    """Отправляет карточки и возвращает ключи тех, которые дошли.

    Сетевые ошибки в RU-сегменте нормальны и почти всегда лечатся повтором,
    поэтому каждая карточка получает три попытки с паузой 2 → 4 с. Недошедшая
    карточка не помечается отправленной и уйдёт в следующий прогон.
    """
    hold = hold_reason()
    if hold:
        log.info("%s: %s карточек ждут в интерфейсе", hold, len(rows))
        return []
    delay = settings.telegram_options().delay
    republished = republished or {}
    signals = signals or {}
    delivered: list[str] = []
    failed: list[str] = []
    bot = _bot(token)
    try:
        for row in rows:
            for attempt in range(1, attempts + 1):
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=format_card(
                            row,
                            republished.get(row["key"], 1),
                            signals.get(row["key"], ()),
                        ),
                        reply_markup=keyboard(row["key"]),
                        disable_web_page_preview=True,
                    )
                    delivered.append(row["key"])
                    # Задержка из настроек: лимит Telegram на сообщения в один чат.
                    await asyncio.sleep(delay)
                    break
                except TelegramNetworkError as exc:
                    log.warning(
                        "сеть подвела на %s, попытка %s/%s: %s",
                        row["key"],
                        attempt,
                        attempts,
                        exc,
                    )
                    if attempt == attempts:
                        failed.append(row["key"])
                        break
                    await asyncio.sleep(2.0 * attempt)
                except Exception:  # noqa: BLE001 — одна карточка не должна рвать прогон
                    log.exception("не удалось отправить карточку %s", row["key"])
                    failed.append(row["key"])
                    break
    finally:
        await bot.session.close()
    if failed:
        log.warning("не дошло карточек: %s (уйдут в следующий прогон)", len(failed))
    return delivered


async def send_alert(token: str, chat_id: str | int, text: str) -> bool:
    """Служебное сообщение владельцу — без кнопок и без HTML-разметки.

    Текст приходит из canary.py и содержит пути файлов, поэтому parse_mode снят:
    одинокие < и & в путях иначе сломают отправку именно тогда, когда она нужна.

    Тихие часы на тревоги не распространяются: сломанный сбор тем и важен, что
    о нём узнают сразу. Полный выключатель отправки их всё же глушит.
    """
    if not settings.telegram_options().enabled:
        log.info("отправка в Telegram выключена: тревога только в логе")
        return False
    bot = _bot(token)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=None,
            disable_web_page_preview=True,
        )
        return True
    except Exception:  # noqa: BLE001 — канарейка не должна ронять прогон
        log.exception("не удалось отправить служебное сообщение")
        return False
    finally:
        await bot.session.close()


async def send_contact_card(
    token: str, chat_id: str | int, text: str, contact_id: int | None = None
) -> bool:
    """Карточка контакта с кнопками статуса.

    Без contact_id (dry-run или follow-up) уходит как обычное сообщение:
    менять статус нечему. parse_mode снят — в тексте письма живут < и &.
    """
    hold = hold_reason()
    if hold:
        log.info("%s: карточка контакта осталась в интерфейсе", hold)
        return False
    bot = _bot(token)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=None,
            disable_web_page_preview=True,
            reply_markup=contact_keyboard(contact_id) if contact_id else None,
        )
        return True
    except Exception:  # noqa: BLE001 — одна карточка не должна рвать прогон
        log.exception("не удалось отправить карточку контакта")
        return False
    finally:
        await bot.session.close()


async def _respond(call: CallbackQuery, reply: "bot_buttons.Reply") -> None:
    """Ответ на нажатие: всплывашка плюс «✓» на нажатой кнопке.

    Без правки разметки кнопка выглядит мёртвой: всплывашка гаснет за секунду,
    и владелец жмёт ещё раз (B-10). Обе операции терпимы к отказу: «query is
    too old» на вчерашней карточке и «message is not modified» на повторном
    нажатии — это норма, а не повод ронять обработчик [CORE-017].
    """
    try:
        await call.answer(reply.text, show_alert=reply.alert)
    except TelegramBadRequest as exc:
        log.warning("не смог ответить на нажатие: %s", exc)
    if reply.mark is None or call.message is None:
        return
    markup = getattr(call.message, "reply_markup", None)
    if markup is None:
        return
    rows = [
        [(button.text, button.callback_data or "") for button in row]
        for row in markup.inline_keyboard
    ]
    updated = bot_buttons.marked(rows, reply.mark)
    if updated == rows:
        return
    try:
        await call.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=text, callback_data=data)
                        for text, data in row
                    ]
                    for row in updated
                ]
            )
        )
    except TelegramBadRequest as exc:
        log.warning("не смог обновить кнопки: %s", exc)


async def run_polling(token: str, db_path: str) -> None:
    """Собирает нажатия кнопок в vacancies.feedback."""
    bot = _bot(token)
    dp = Dispatcher()
    conn = db.connect(db_path)
    db.init_schema(conn)
    contacts.ensure_schema(conn)

    @dp.callback_query(F.data.startswith("fb:"))
    async def on_feedback(call: CallbackQuery) -> None:
        parts = bot_buttons.parse(call.data)
        if parts is None:
            log.error("непонятный callback_data: %r", call.data)
            await _respond(call, bot_buttons.Reply("Не разобрал кнопку", alert=True))
            return
        _, value, key = parts
        reply = bot_buttons.feedback(conn, key, value)
        log.info("feedback %s -> %s: %s", key, value, reply.text)
        await _respond(call, reply)

    @dp.callback_query(F.data.startswith("ct:"))
    async def on_contact(call: CallbackQuery) -> None:
        """[OUT-007]: статус контакта меняет владелец, система его не угадывает."""
        parts = bot_buttons.parse(call.data)
        if parts is None or not parts[2].isdigit():
            log.error("непонятный callback_data: %r", call.data)
            await _respond(call, bot_buttons.Reply("Не разобрал кнопку", alert=True))
            return
        _, action, raw_id = parts
        reply = bot_buttons.contact(conn, int(raw_id), action)
        log.info("контакт %s: %s -> %s", raw_id, action, reply.text)
        await _respond(call, reply)

    @dp.callback_query()
    async def on_unknown_callback(call: CallbackQuery) -> None:
        log.warning("неизвестный callback: %r", call.data)
        await call.answer("Не моя кнопка")

    @dp.message()
    async def on_message(message: Message) -> None:
        """Ответ на любой текст — простая проверка, жив ли polling."""
        counts = db.stats(conn)
        await message.answer(
            "Слушаю кнопки. В базе: "
            f"вакансий {counts.get('vacancies', 0)}, "
            f"отправлено {counts.get('notified', 0)}.\n"
            f"chat_id: {message.chat.id}"
        )

    me = await bot.get_me()
    log.info("polling запущен для @%s, база %s", me.username, db_path)
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
