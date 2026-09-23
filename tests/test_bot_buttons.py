"""Кнопки карточки контакта: [OUT-006], [OUT-007], [OUT-009]."""

from __future__ import annotations

import sqlite3

import bot
import bot_buttons
import contacts


def test_кнопки_покрывают_статусы_из_правила() -> None:
    markup = bot.contact_keyboard(7)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["ct:sent:7", "ct:other:7", "ct:skip:7", "ct:block:7"]
    statuses = {status for status, _ in bot.CONTACT_ACTIONS.values()}
    assert statuses == {contacts.SENT, contacts.SKIPPED, contacts.BLOCKED, contacts.RETRY}


def test_кнопка_переводит_контакт_в_новый_статус(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    contact_id = contacts.store(
        conn, "hh:1", "АКМЕ", contacts.Candidate(channel_kind="email", channel_value="a@acme.ru")
    )
    status, _ = bot.CONTACT_ACTIONS["block"]
    contacts.set_status(conn, contact_id, status)
    assert contacts.is_blocked(conn, "АКМЕ") is True


def test_повторное_нажатие_не_переворачивает_статус(conn: sqlite3.Connection) -> None:
    """Владелец жмёт дважды, потому что первый раз «ничего не произошло» (B-10)."""
    contacts.ensure_schema(conn)
    contact_id = contacts.store(
        conn, "hh:1", "АКМЕ", contacts.Candidate(channel_kind="email", channel_value="a@acme.ru")
    )
    first = bot_buttons.contact(conn, contact_id, "sent")
    second = bot_buttons.contact(conn, contact_id, "sent")
    assert first.text == "Отметил: отправлено"
    assert second.text == "Уже отмечено"
    assert first.mark == second.mark == f"ct:sent:{contact_id}"


def test_другой_контакт_не_то_же_что_пропустить() -> None:
    """Со skipped они были неотличимы, и прогон присылал того же человека."""
    assert bot_buttons.CONTACT_ACTIONS["other"][0] == contacts.RETRY
    assert bot_buttons.CONTACT_ACTIONS["skip"][0] == contacts.SKIPPED


def test_отклонённый_канал_не_предлагается_снова(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    first = contacts.store(
        conn, "hh:1", "АКМЕ", contacts.Candidate(channel_kind="email", channel_value="a@acme.ru")
    )
    contacts.set_status(conn, first, contacts.RETRY)
    assert contacts.rejected_channels(conn, "hh:1") == {"a@acme.ru"}
    assert contacts.rejected_channels(conn, "hh:2") == set()


def test_неизвестная_кнопка_не_роняет_обработчик(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    assert bot_buttons.parse("мусор") is None
    assert bot_buttons.parse("ct:sent:7") == ("ct", "sent", "7")
    assert bot_buttons.parse("fb:good:hh:abc") == ("fb", "good", "hh:abc")
    assert bot_buttons.contact(conn, 999, "sent").alert is True


def test_нажатая_кнопка_получает_галочку() -> None:
    """Telegram сам ничего не рисует: без этого кнопка выглядит мёртвой."""
    rows = [[("✅ Отправил", "ct:sent:7"), ("⏭ Пропустить", "ct:skip:7")]]
    once = bot_buttons.marked(rows, "ct:sent:7")
    assert once[0][0][0].endswith(" ✓")
    assert once[0][1][0] == "⏭ Пропустить"
    # Второе нажатие не плодит галочки: иначе Telegram ответит «not modified».
    assert bot_buttons.marked(once, "ct:sent:7") == once


def test_оценка_вакансии_пишется_и_повторяется(conn: sqlite3.Connection) -> None:
    import db

    db.init_schema(conn)
    conn.execute(
        "INSERT INTO vacancies (key, source, external_id, url, title, first_seen_at,"
        " last_seen_at) VALUES ('hh:7', 'hh', '7', 'u', 'т', 'сейчас', 'сейчас')"
    )
    conn.commit()
    assert bot_buttons.feedback(conn, "hh:7", "good").text == "Записал"
    assert bot_buttons.feedback(conn, "hh:7", "good").text == "Уже отмечено"
    # Ключа нет в базе — обычно это разные DB_PATH у run.py и bot.py.
    assert bot_buttons.feedback(conn, "hh:404", "good").alert is True
