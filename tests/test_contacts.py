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
    # Отрасль роли не важна: маркеры руководства общие.
    assert contacts.role_rank("Бригадир уборщиков")[0] == 2
    assert contacts.role_rank("Заведующий складом")[0] == 1
    # Коллега на той же роли опознаётся по предмету вакансии, не по словарю.
    assert contacts.role_rank("Уборщик помещений", "уборщик помещений")[0] == 4
    assert contacts.role_rank("Бухгалтер", "уборщик помещений")[0] == 99


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


def test_локальная_часть_адреса_не_telegram() -> None:
    """Голая @ в регулярке делала «рабочий канал» из любого email [OUT-003]."""
    found, _ = contacts.extract_channels(
        "Пишите на ivan.petrov@romashka.ru", source_url="https://romashka.ru/team"
    )
    assert [(c.channel_kind, c.channel_value) for c in found] == [
        ("email", "ivan.petrov@romashka.ru")
    ]


def test_telegram_берётся_только_из_опубликованного_канала() -> None:
    found, _ = contacts.extract_channels(
        "Канал t.me/acmelead, telegram: @teamhead, пишите @acmechat",
        source_url="https://acme.ru/team",
    )
    nicks = {c.channel_value for c in found if c.channel_kind == "telegram"}
    assert nicks == {"@acmelead", "@teamhead", "@acmechat"}


def test_слова_стека_не_делают_человека_руководителем() -> None:
    """Ранг 4 — это «python» и «разработчик», должности по ним не бывает."""
    assert contacts.extract_names("Наша команда Python: Иван Петров и Мария Сидорова") == ()
    leads = contacts.extract_names("Руководитель разработки — Иван Петров")
    assert leads == (("Иван Петров", 1, "руководитель направления"),)


def test_одна_компания_один_адресат() -> None:
    page = "Руководитель разработки — Иван Петров\nТимлид Мария Сидорова"
    discovery = contacts.discover(
        key="hh:1",
        company="АКМЕ",
        vacancy_text="Ищем Python-разработчика",
        company_pages=[("https://acme.ru/team", page)],
    )
    guessed = [c for c in discovery.candidates if c.guessed]
    assert len(guessed) == 1
    assert guessed[0].person == "Иван Петров"
    assert guessed[0].role_rank == 1
    assert any("один адресат" in d for d in discovery.dropped)


def test_контакт_без_ссылки_на_публикацию_не_используется() -> None:
    """[LEG-004]: адрес без источника не показывается как контакт."""
    without = contacts.discover(key="hh:1", company="АКМЕ", vacancy_text="почта lead@acme.ru")
    assert without.candidates == ()
    assert any("нет ссылки на публикацию" in d for d in without.dropped)

    with_source = contacts.discover(
        key="hh:1",
        company="АКМЕ",
        vacancy_text="почта lead@acme.ru",
        vacancy_url="https://hh.ru/vacancy/1",
    )
    assert with_source.candidates[0].source_url == "https://hh.ru/vacancy/1"


def test_блок_закрывает_компанию_в_любом_написании(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    contacts.store(
        conn,
        "hh:1",
        "Ромашка",
        contacts.Candidate(channel_kind="email", channel_value="a@romashka.ru"),
        status=contacts.BLOCKED,
    )
    assert contacts.is_blocked(conn, 'ООО «Ромашка»') is True
    assert contacts.is_blocked(conn, "Ландыш") is False


def test_повторный_прогон_не_плодит_дубли(conn: sqlite3.Connection) -> None:
    contacts.ensure_schema(conn)
    candidate = contacts.Candidate(channel_kind="email", channel_value="lead@acme.ru")
    first = contacts.store(conn, "hh:1", "АКМЕ", candidate)
    second = contacts.store(conn, "hh:1", "АКМЕ", candidate)
    assert first == second
    assert len(contacts.load(conn, "hh:1")) == 1


def test_follow_up_ждёт_отметки_владельца(conn: sqlite3.Connection) -> None:
    """Напоминать о письме, которого не отправляли, незачем [OUT-006]."""
    contacts.ensure_schema(conn)
    contact_id = contacts.store(
        conn,
        "hh:1",
        "АКМЕ",
        contacts.Candidate(channel_kind="email", channel_value="lead@acme.ru"),
    )
    later = datetime.now(timezone.utc) + timedelta(days=30)
    assert contacts.due_follow_ups(conn, 6, now=later) == []

    contacts.set_status(conn, contact_id, contacts.SENT)
    due = contacts.due_follow_ups(conn, 6, now=later)
    assert [row["id"] for row in due] == [contact_id]

    contacts.mark_follow_up(conn, contact_id)
    assert contacts.due_follow_ups(conn, 6, now=later) == []
