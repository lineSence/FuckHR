"""Контакты собираются общим прогоном и живут в карточке вакансии."""

from __future__ import annotations

import sqlite3

import contact_finds
import contacts
import outreach
import ui_views
import websearch


def _vacancy(conn: sqlite3.Connection, key: str = "hh:1", company: str = "АКМЕ") -> sqlite3.Row:
    conn.execute(
        "INSERT OR REPLACE INTO vacancies "
        "(key, source, external_id, url, title, company, description, score, "
        "first_seen_at, last_seen_at) "
        "VALUES (?, 'hh', '1', 'https://hh.ru/vacancy/1', 'Python разработчик', ?, ?, 80, "
        "'2026-09-19', '2026-09-19')",
        (key, company, "Пишите на lead@acme.ru"),
    )
    conn.commit()
    return conn.execute("SELECT * FROM vacancies WHERE key = ?", (key,)).fetchone()


def _ready(conn: sqlite3.Connection, key: str, company: str = "АКМЕ") -> None:
    import detector
    import dossier_store

    dossier_store.ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO company_dossier (company, risk, updated_at) VALUES (?, ?, ?)",
        (company, "yellow", "2026-09-18T00:00:00+00:00"),
    )
    conn.commit()
    detector.ensure_schema(conn)
    detector.store(conn, detector.Report(key=key))


def test_находки_переживают_перезапись(conn: sqlite3.Connection) -> None:
    discovery = contacts.Discovery(
        key="hh:1",
        company="АКМЕ",
        candidates=(
            contacts.Candidate(
                channel_kind="email",
                channel_value="lead@acme.ru",
                person="Иван Петров",
                role_rank=2,
                source_url="https://acme.ru/team",
            ),
        ),
        dropped=("телефон пропущен",),
    )
    assert contact_finds.save(conn, discovery) == 1
    assert contact_finds.save(conn, discovery) == 1  # перезапись, а не дубль

    loaded = contact_finds.load(conn, "hh:1")
    assert [c.channel_value for c in loaded.candidates] == ["lead@acme.ru"]
    assert loaded.dropped == ("телефон пропущен",)
    assert contact_finds.coverage(conn) == (1, 1)
    assert contact_finds.load(conn, "hh:404") is None


def test_общий_сбор_ищет_контакты_после_досье(conn: sqlite3.Connection) -> None:
    row = _vacancy(conn)
    provider = websearch.SearchProvider(api_key="")

    # Без досье этап молчит [OUT-002].
    assert outreach.collect_contacts(conn, [row], provider) == 0
    assert contact_finds.load(conn, "hh:1") is None

    _ready(conn, "hh:1")
    assert outreach.collect_contacts(conn, [row], provider) == 1
    found = contact_finds.load(conn, "hh:1")
    assert [c.channel_value for c in found.candidates] == ["lead@acme.ru"]


def test_подготовка_письма_берёт_готовые_находки(conn: sqlite3.Connection) -> None:
    """Второй поиск по той же вакансии не оплачивается."""
    row = _vacancy(conn)
    contacts.ensure_schema(conn)
    contact_finds.save(
        conn,
        contacts.Discovery(
            key="hh:1",
            company="АКМЕ",
            candidates=(
                contacts.Candidate(
                    channel_kind="email",
                    channel_value="teamlead@acme.ru",
                    person="Иван Петров",
                    role="тимлид",
                    role_rank=2,
                    source_url="https://acme.ru/team",
                ),
            ),
        ),
    )

    discovery, draft, reason = outreach.process_row(
        conn, row, ("факт",), websearch.SearchProvider(api_key="")
    )
    assert reason is None
    assert discovery.candidates[0].channel_value == "teamlead@acme.ru"
    assert draft is not None


def test_карточка_вакансии_показывает_контакты_и_кнопку(conn: sqlite3.Connection) -> None:
    import conditions
    import detector

    conditions.ensure_schema(conn)
    detector.ensure_schema(conn)
    _vacancy(conn)
    contact_finds.save(
        conn,
        contacts.Discovery(
            key="hh:1",
            company="АКМЕ",
            candidates=(
                contacts.Candidate(
                    channel_kind="email",
                    channel_value="lead@acme.ru",
                    source_url="https://acme.ru/team",
                ),
            ),
        ),
    )

    html = ui_views.render_vacancy(conn, "hh:1", with_draft=False)
    assert "lead@acme.ru" in html
    assert "Подготовить письмо" in html
    # Кнопка письма стоит и рядом с вакансией в списке.
    assert "Письмо" in ui_views.render_vacancies(conn, 0.0, 10)
