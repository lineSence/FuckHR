"""Черновики писем: проверяется структура и границы, а не красота текста."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import contacts
import outreach
import websearch

CANDIDATE = contacts.Candidate(
    channel_kind="email",
    channel_value="ivan.petrov@acme.ru",
    person="Иван Петров",
    role="руководитель разработки",
    role_rank=1,
    source_url="https://acme.ru/team",
    guessed=True,
)


def _row(**values: object) -> sqlite3.Row:
    """Строка базы без самой базы: черновику нужны только поля."""
    data = {
        "key": "hh:1",
        "title": "Python разработчик",
        "company": "АКМЕ",
        "score": 72.0,
        "url": "https://hh.ru/vacancy/1",
        "description": "Python, FastAPI",
    }
    data.update(values)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = ", ".join(f'"{name}"' for name in data)
    placeholders = ", ".join("?" for _ in data)
    conn.execute(f"CREATE TABLE row ({columns})")
    conn.execute(f"INSERT INTO row ({columns}) VALUES ({placeholders})", tuple(data.values()))
    return conn.execute("SELECT * FROM row").fetchone()


def test_факты_берутся_только_из_профиля(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "facts:\n  - переписал импорт на asyncio, ускорил в 12 раз\n  -  \n",
        encoding="utf-8",
    )
    facts = outreach.load_facts(profile)
    assert facts == ("переписал импорт на asyncio, ускорил в 12 раз",)
    assert outreach.load_facts(tmp_path / "нету.yaml") == ()


def test_черновик_собирается_из_фактов_и_вакансии() -> None:
    draft = outreach.build_draft(
        _row(),
        CANDIDATE,
        facts=("ускорил импорт в 12 раз",),
        reason="взял контакт со страницы https://acme.ru/team",
    )
    assert "Иван" in draft.body
    assert "Python разработчик" in draft.body
    assert "ускорил импорт в 12 раз" in draft.body
    # Последний абзац из справочника: куда переадресовать письмо.
    assert "кто её ведёт" in draft.body
    assert len(draft.body) <= outreach.MAX_LETTER_CHARS


def test_без_фактов_черновик_не_выдумывает_достижений() -> None:
    draft = outreach.build_draft(_row(), CANDIDATE, facts=())
    assert outreach.NO_FACTS_HINT in draft.body


def test_follow_up_ровно_один() -> None:
    first = outreach.build_draft(_row(), CANDIDATE, facts=("факт",))
    follow = outreach.build_follow_up(_row(), first)
    assert follow.kind == "follow_up"
    assert follow.subject.startswith("Re: ")
    assert "один раз" in follow.body


def test_карточка_показывает_канал_и_признак_гадания() -> None:
    discovery = contacts.Discovery(key="hh:1", company="АКМЕ", candidates=(CANDIDATE,))
    draft = outreach.build_draft(_row(), CANDIDATE, facts=("факт",))
    card = outreach.format_card(_row(), discovery, draft, signal_lines=["⚠️ перепубликации: 4"])
    assert "Контакт: Иван Петров" in card
    assert "ivan.petrov@acme.ru" in card
    assert "адрес угадан по шаблону" in card
    assert "Черновик письма" in card
    assert "перепубликации: 4" in card


def test_заблокированная_компания_пропускается(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    contacts.store(
        conn,
        "hh:0",
        "АКМЕ",
        contacts.Candidate(channel_kind="email", channel_value="a@acme.ru"),
        status=contacts.BLOCKED,
    )

    provider = websearch.SearchProvider(api_key="")
    discovery, draft, reason = outreach.process_row(conn, _row(), ("факт",), provider)
    assert draft is None
    assert reason == "компания в блоке"
    assert discovery.candidates == ()


# --- Режим «контакта нет, но вакансия хорошая» --------------------------------
#
# На живой базе из 159 вакансий адрес почты нашёлся в одной: hh.ru вырезает
# контакты из текста. Поведение по умолчанию остаётся строгим, но терять всё
# молча — хуже, чем отдать сопроводительное с честной пометкой.


def test_без_флага_вакансия_без_контакта_пропускается(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    provider = websearch.SearchProvider(api_key="")
    row = _row(description="Ищем питониста. Откликайтесь на площадке.")

    discovery, draft, reason = outreach.process_row(conn, row, ("факт",), provider)
    assert draft is None
    assert reason == "прямого контакта не нашлось"
    assert discovery.candidates == ()


def test_allow_generic_даёт_сопроводительное_и_не_врёт(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    provider = websearch.SearchProvider(api_key="")
    row = _row(description="Ищем питониста. Откликайтесь на площадке.")

    discovery, draft, reason = outreach.process_row(
        conn, row, ("ускорил импорт в 12 раз",), provider, allow_generic=True
    )

    assert reason is None
    assert draft is not None
    fallback = discovery.candidates[0]
    # Заглушка не должна выглядеть найденным человеком.
    assert fallback.channel_kind == outreach.APPLY_CHANNEL
    assert fallback.person is None
    assert discovery.has_direct is False
    assert "ускорил импорт в 12 раз" in draft.body
    # Просить переадресации у отклика бессмысленно.
    assert "кто её ведёт" not in draft.body


def test_карточка_без_контакта_говорит_об_этом_прямо() -> None:
    row = _row()
    fallback = outreach.apply_candidate(row)
    discovery = contacts.Discovery(key="hh:1", company="АКМЕ", candidates=(fallback,))
    draft = outreach.build_draft(row, fallback, facts=("факт",))
    card = outreach.format_card(row, discovery, draft)

    assert outreach.NO_CONTACT_NOTE in card
    assert "Сопроводительное" in card
    # Никаких «Контакт: …» у вакансии без найденного человека быть не должно.
    assert "Контакт:" not in card
