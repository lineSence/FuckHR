"""Карточка компании: порядок блоков, свёрнутость и разбивка по сферам.

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
    # Внутренние блоки сворачиваются тоже: иначе «О компании» — это экран
    # прокрутки, в котором не найти нужную цифру.
    for title in ("Отзывы по сферам", "История публикаций", "Закономерности", "Источники"):
        assert "<summary>{}".format(title) in html


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


def test_сортировка_вакансий_меняет_порядок_и_открывает_блок(
    conn: sqlite3.Connection,
) -> None:
    _dossier(conn)
    _vacancy(conn, "hh:1", "Ромашка", "Python разработчик")
    conn.execute("UPDATE vacancies SET score = 10 WHERE key = 'hh:1'")
    _vacancy(conn, "hh:2", "Ромашка", "Backend разработчик")
    conn.execute("UPDATE vacancies SET score = 90, published_at = '2026-01-01' "
                 "WHERE key = 'hh:2'")
    conn.commit()

    by_date = ui_companies.render_company(conn, "Ромашка", jobs_sort="published")
    assert by_date.index("Python") < by_date.index("Backend")
    by_score = ui_companies.render_company(conn, "Ромашка", jobs_sort="score")
    assert by_score.index("Backend") < by_score.index("Python")
    # По ссылке сортировки блок открыт, иначе клик уводил бы в свёрнутое.
    assert "<details open>" in by_score


def test_чужая_сортировка_не_роняет_страницу(conn: sqlite3.Connection) -> None:
    _dossier(conn)
    _vacancy(conn, "hh:1", "Ромашка", "Python разработчик")
    html = ui_companies.render_company(conn, "Ромашка", jobs_sort="'; DROP TABLE--")
    assert "Python разработчик" in html


def _item(
    conn: sqlite3.Connection,
    idx: int,
    company: str,
    text: str,
    rating: float,
) -> None:
    """Разобранный отзыв прямо в таблицу: сфера ставится теми же словарями."""
    import fake_store
    import review_area

    fake_store.ensure_schema(conn)
    area = review_area.classify(text)
    conn.execute(
        "INSERT INTO review_items (company, url, idx, site, text_hash, excerpt,"
        " rating, label, area, area_scope, area_hits, created_at)"
        " VALUES (?, 'https://x/1', ?, 'dreamjob', ?, ?, ?, 'clean', ?, ?, '[]',"
        " '2026-09-18T00:00:00+00:00')",
        (company, idx, "h{}".format(idx), text, rating, area.code, area.scope),
    )
    conn.commit()


def test_сферы_видны_на_странице_и_объясняют_молчание(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    """Разбивка считалась и раньше, но нигде не показывалась: в сводке её видно
    только при заданной настройке, а на странице компании не было вовсе."""
    _dossier(conn)
    _item(conn, 0, "ООО «Ромашка»", "Работала продавцом, недостачу вешают на нас", 2.0)
    _item(conn, 1, "ООО «Ромашка»", "Работал программистом, ревью и спринты", 4.0)
    _item(conn, 2, "ООО «Ромашка»", "Задерживают зарплату второй месяц", 1.0)
    _item(conn, 3, "ООО «Ромашка»", "Всё нормально, работаю второй год", 5.0)

    monkeypatch.setattr("settings.get", lambda key, default="": default)
    html = ui_companies.render_areas(conn, "ООО «Ромашка»")
    assert "розница, склад и линия" in html and "разработка и ИТ" in html
    assert "про компанию целиком" in html and "сфера не определена" in html
    # Настройка не задана — страница говорит об этом и даёт выбрать сферу
    # на месте, а не отправляет в настройки.
    assert "Своя сфера не выбрана" in html
    assert 'name=area' in html and 'action="/area"' in html

    monkeypatch.setattr("settings.get", lambda key, default="": "it" if key == "REVIEW_AREA" else default)
    html = ui_companies.render_areas(conn, "ООО «Ромашка»")
    assert "разработка и ИТ" in html and "Своя сфера не выбрана" not in html
