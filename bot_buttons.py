"""Логика кнопок карточки: разбор callback_data, статусы, отметка нажатого.

Отдельный файл, потому что раньше вся логика жила замыканиями внутри
`bot.run_polling` и её нельзя было проверить ни одним тестом без сети. Баг
B-10 («кнопки не работают») прожил там ровно поэтому. Здесь нет ни aiogram, ни
сети: на входе соединение с базой и строка из кнопки, на выходе — что ответить
и какую кнопку пометить.

Три правила, на которых всё держится:

- **Повтор безвреден.** Владелец жмёт «Отправил» дважды, потому что первый раз
  «ничего не произошло». Второе нажатие не меняет статус и честно говорит, что
  отметка уже стоит.
- **Нажатие видно.** Telegram сам по себе ничего не рисует: без правки разметки
  кнопка выглядит мёртвой, даже когда база обновилась. Поэтому нажатая кнопка
  получает «✓».
- **«Другой контакт» — не «Пропустить».** Статус `retry` говорит следующему
  прогону: этот канал владельцу не подошёл, возьми следующего кандидата
  [OUT-006]. Со `skipped` они были неотличимы, и прогон присылал того же
  человека — из-за этого кнопка и считалась сломанной.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import contacts

# Кнопки карточки контакта [OUT-006], [OUT-007], [OUT-009].
CONTACT_ACTIONS = {
    "sent": (contacts.SENT, "Отметил: отправлено"),
    "other": (contacts.RETRY, "Поищу другой контакт в следующем прогоне"),
    "skip": (contacts.SKIPPED, "Пропустил"),
    "block": (contacts.BLOCKED, "Больше не пишем этой компании"),
}

FEEDBACK_REPLY = {"good": "Записал", "bad": "Понятно"}


@dataclass(frozen=True)
class Reply:
    """Что сказать владельцу и что сделать с разметкой сообщения."""

    text: str
    alert: bool = False
    mark: str | None = None


def parse(data: str | None) -> tuple[str, str, str] | None:
    """«ct:sent:7» → ('ct', 'sent', '7'). Неизвестная форма — None."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def feedback(conn: sqlite3.Connection, key: str, value: str) -> Reply:
    """Оценка вакансии. Ноль обновлённых строк — обычно разные DB_PATH."""
    if value not in FEEDBACK_REPLY:
        return Reply("Не разобрал кнопку", alert=True)
    row = conn.execute(
        "SELECT feedback FROM vacancies WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return Reply("Карточка не найдена в базе", alert=True)
    data = f"fb:{value}:{key}"
    if row[0] == value:
        return Reply("Уже отмечено", mark=data)
    conn.execute("UPDATE vacancies SET feedback = ? WHERE key = ?", (value, key))
    conn.commit()
    return Reply(FEEDBACK_REPLY[value], mark=data)


def contact(conn: sqlite3.Connection, contact_id: int, action: str) -> Reply:
    """Статус контакта. Меняет владелец, система его не угадывает [OUT-006]."""
    known = CONTACT_ACTIONS.get(action)
    if known is None:
        return Reply("Не моя кнопка")
    status, text = known
    row = conn.execute(
        "SELECT status FROM contacts WHERE id = ?", (contact_id,)
    ).fetchone()
    if row is None:
        return Reply("Контакт не найден в базе", alert=True)
    data = f"ct:{action}:{contact_id}"
    if row[0] == status:
        return Reply("Уже отмечено", mark=data)
    contacts.set_status(conn, contact_id, status)
    return Reply(text, mark=data)


def marked(rows: list[list[tuple[str, str]]], chosen: str) -> list[list[tuple[str, str]]]:
    """Та же разметка, но нажатая кнопка с «✓». Список пар (текст, data)."""
    out = []
    for row in rows:
        out.append(
            [
                (text if data != chosen or text.endswith(" ✓") else text + " ✓", data)
                for text, data in row
            ]
        )
    return out


__all__ = ("CONTACT_ACTIONS", "FEEDBACK_REPLY", "Reply", "contact", "feedback", "marked", "parse")
