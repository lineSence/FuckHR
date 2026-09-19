"""Кнопки карточки контакта: [OUT-006], [OUT-007], [OUT-009]."""

from __future__ import annotations

import sqlite3

import bot
import contacts


def test_кнопки_покрывают_статусы_из_правила() -> None:
    markup = bot.contact_keyboard(7)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["ct:sent:7", "ct:other:7", "ct:skip:7", "ct:block:7"]
    statuses = {status for status, _ in bot.CONTACT_ACTIONS.values()}
    assert statuses == {contacts.SENT, contacts.SKIPPED, contacts.BLOCKED}


def test_кнопка_переводит_контакт_в_новый_статус(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    contact_id = contacts.store(
        conn, "hh:1", "АКМЕ", contacts.Candidate(channel_kind="email", channel_value="a@acme.ru")
    )
    status, _ = bot.CONTACT_ACTIONS["block"]
    contacts.set_status(conn, contact_id, status)
    assert contacts.is_blocked(conn, "АКМЕ") is True
