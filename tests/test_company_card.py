"""Карточка компании: порядок блоков и их свёрнутость.

Проверяется не вёрстка, а решение: сначала компания, потом её вакансии, в
самом низу контакты — это порядок принятия решения [OUT-002]. И то, что
блоки закрыты: развёрнутая простыня этот порядок обнуляет.
"""

from __future__ import annotations

import sqlite3

import contact_finds
import contacts
import dossier_store
import ui_companies


def _dossier(conn: sqlite3.Connection, company: str = "ООО «Ромашка»") -> None:
    dossier_store.ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO company_dossier (company, risk, review_count, updated_at) "
        "VALUES (?, 'yellow', 3, '2026-09-18T00:00:00+00:00')",
        (company,),
    )
    conn.commit()


def _vacancy(conn: sqlite3.Connection, key: str, company: str, title: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO vacancies "
        "(key, source, external_id, url, title, company, description, score, "
        "published_at, first_seen_at, last_seen_at) "
        "VALUES (?, 'hh', ?, 'https://hh.ru/vacancy/1', ?, ?, '', 80, "
        "'2026-09-17', '2026-09-19', '2026-09-19')",
        (key, key, title, company),
    )
    conn.commit()


def test_блоки_идут_по_порядку_и_свёрнуты(conn: sqlite3.Connection) -> None:
    _dossier(conn)
    html = ui_companies.render_company(conn, "ООО «Ромашка»")

    about = html.index("О компании")
    jobs = html.index("Вакансии компании")
    people = html.index("Контакты")
    assert about < jobs < people
    assert "<details>" in html
    assert "<details open>" not in html


def test_вакансии_компании_попадают_в_карточку_при_разном_написании(
    conn: sqlite3.Connection,
) -> None:
    _dossier(conn)
    _vacancy(conn, "hh:1", "ООО «Ромашка»", "Python разработчик")
    _vacancy(conn, "hh:2", "Ромашка", "Backend разработчик")
    _vacancy(conn, "hh:3", "ООО Ландыш", "Frontend разработчик")

    html = ui_companies.render_company(conn, "ООО «Ромашка»")
    assert "Python разработчик" in html
    assert "Backend разработчик" in html
    assert "Frontend разработчик" not in html
    assert "в базе: 2" in html


def test_контакты_считаются_в_заголовке_блока(conn: sqlite3.Connection) -> None:
    _dossier(conn)
    _vacancy(conn, "hh:1", "Ромашка", "Python разработчик")
    contact_finds.save(
        conn,
        contacts.Discovery(
            key="hh:1",
            company="Ромашка",
            candidates=(
                contacts.Candidate(
                    channel_kind="email",
                    channel_value="lead@romashka.ru",
                    person="Иван Петров",
                    role_rank=2,
                    source_url="https://romashka.ru/team",
                ),
            ),
        ),
    )
    html = ui_companies.render_company(conn, "ООО «Ромашка»")
    assert "найдено: 1" in html
    assert "lead@romashka.ru" in html
