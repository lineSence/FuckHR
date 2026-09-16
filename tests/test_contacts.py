"""Contact discovery без сети: всё ядро детерминировано."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import contacts

PAGE = (
    "Наша команда\n"
    "Руководитель разработки — Иван Петров\n"
    "Общая почта: info@acme.ru, резюме на hr@acme.ru\n"
)


def test_роли_ранжируются_к_нанимающему_менеджеру() -> None:
    assert contacts.role_rank("Руководитель разработки")[0] == 1
    assert contacts.role_rank("Тимлид backend")[0] == 2
    assert contacts.role_rank("CTO")[0] == 3
    # Рекрутер — последний вариант, а не первый [OUT-001].
    assert contacts.role_rank("HR-менеджер")[0] == 8


def test_домен_джобборда_не_домен_компании() -> None:
    assert contacts.domain_of("https://www.acme.ru/team") == "acme.ru"
    assert contacts.domain_of("https://hh.ru/employer/1") is None
    assert contacts.domain_of(None) is None


def test_угадывание_адреса_по_шаблону() -> None:
    guesses = contacts.guess_emails("Иван Петров", "acme.ru")
    assert guesses[0] == "ivan.petrov@acme.ru"
    # Перебор десятков вариантов запрещён [OUT-008].
    assert len(guesses) <= 5
    assert contacts.guess_emails("Иван", "acme.ru") == ()


def test_телефоны_и_соцсети_не_каналы() -> None:
    text = "Пишите lead@acme.ru или t.me/acmelead, звоните +7 999 123-45-67, vk.com/acme"
    found, dropped = contacts.extract_channels(text)
    kinds = {(c.channel_kind, c.channel_value) for c in found}
    assert ("email", "lead@acme.ru") in kinds
    assert ("telegram", "@acmelead") in kinds
    assert not any(c.channel_kind == "phone" for c in found)
    assert any("телефон" in d for d in dropped)
    assert any("vk.com" in d for d in dropped)


def test_hr_ящик_уезжает_в_конец_списка() -> None:
    found, _ = contacts.extract_channels("hr@acme.ru и lead@acme.ru")
    ranked = contacts.rank_candidates(found)
    assert ranked[0].channel_value == "lead@acme.ru"
    assert ranked[-1].channel_value == "hr@acme.ru"


def test_discover_выводит_адрес_руководителя_со_страницы_команды() -> None:
    discovery = contacts.discover(
        key="hh:1",
        company="АКМЕ",
        vacancy_text="Ищем Python-разработчика",
        company_pages=[("https://acme.ru/team", PAGE)],
    )
    best = discovery.candidates[0]
    assert best.person == "Иван Петров"
    assert best.channel_value == "ivan.petrov@acme.ru"
    # Угаданный адрес всегда помечен как гадание [OUT-008].
    assert best.guessed is True
    assert best.confidence == "low"
    assert discovery.has_direct is True


def test_без_контактов_карточка_говорит_прямо() -> None:
    discovery = contacts.discover(
        key="hh:2", company="АКМЕ", vacancy_text="Описание без контактов"
    )
    assert discovery.candidates == ()
    assert "прямого контакта нет" in contacts.format_contact_lines(discovery)[0]


def test_лог_контактов_и_статусы(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    candidate = contacts.Candidate(
        channel_kind="email",
        channel_value="ivan.petrov@acme.ru",
        person="Иван Петров",
        role="тимлид",
        role_rank=2,
    )
    contact_id = contacts.store(conn, "hh:1", "АКМЕ", candidate)
    assert contacts.load(conn, "hh:1")[0]["status"] == contacts.DRAFTED

    # Факт отправки ставит владелец, а не система [OUT-006].
    contacts.set_status(conn, contact_id, contacts.SENT)
    assert contacts.load(conn, "hh:1")[0]["status"] == contacts.SENT
    assert contacts.coverage(conn) == (1, 1)


def test_повторный_контакт_раньше_трёх_месяцев_запрещён(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    contacts.store(
        conn,
        "hh:1",
        "АКМЕ",
        contacts.Candidate(channel_kind="email", channel_value="a@acme.ru", person="Иван Петров"),
    )
    assert contacts.recently_contacted(conn, "Иван Петров", "АКМЕ") is True
    # Через четыре месяца писать можно снова.
    later = datetime.now(timezone.utc) + timedelta(days=120)
    assert contacts.recently_contacted(conn, "Иван Петров", "АКМЕ", now=later) is False


def test_отказ_закрывает_компанию_навсегда(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    contacts.store(
        conn,
        "hh:1",
        "АКМЕ",
        contacts.Candidate(channel_kind="email", channel_value="a@acme.ru"),
        status=contacts.BLOCKED,
    )
    assert contacts.is_blocked(conn, "АКМЕ") is True
    assert contacts.is_blocked(conn, "Другая компания") is False
