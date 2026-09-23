"""Списки внутри карточки компании: её вакансии и найденные контакты.

Отделено от самой карточки (`ui_company.py`) по [CORE-024]: карточка с
разбивкой по сферам перестала влезать в 25 КБ. Граница честная — это две
таблицы со своей сортировкой, и с остальной карточкой их связывает только
название компании.
"""

from __future__ import annotations

import sqlite3
import urllib.parse

import company_signals
import contact_finds
import contacts
from ui_core import esc, sort_head, sort_pick, table
from ui_views import draft_button

CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
CONTACT_SORTS: dict[str, object] = {
    "person": lambda r: str(r["person"] or "я").lower(),
    "role": lambda r: int(r["role_rank"] or 99),
    "channel": lambda r: (str(r["channel_kind"] or ""), str(r["channel_value"] or "")),
    "confidence": lambda r: CONFIDENCE_ORDER.get(str(r["confidence"]), 9),
}
CONTACT_COLUMNS = (
    ("", "Откуда"),
    ("person", "Человек"),
    ("role", "Роль"),
    ("channel", "Канал"),
    ("confidence", "Уверенность"),
    ("", "Источник"),
)

VACANCY_SORTS: dict[str, object] = {
    "score": lambda r: -float(r["score"] or 0),
    "title": lambda r: str(r["title"] or "").lower(),
    "published": lambda r: str(r["published_at"] or ""),
}
VACANCY_COLUMNS = (
    ("score", "Скор"),
    ("title", "Вакансия"),
    ("published", "Опубликована"),
    ("", "В TG"),
    ("", "Письмо"),
)


def _sorted(rows: list, keys: dict, sort: str) -> list:
    """Даты и числа читаются сверху вниз, поэтому у них порядок обратный."""
    rows = sorted(rows, key=keys[sort])
    if sort in ("updated", "published"):
        rows.reverse()
    return rows


def render_contacts_for(
    conn: sqlite3.Connection, name: str, sort: str = "role"
) -> str:
    """Каналы, найденные по вакансиям этого работодателя.

    По умолчанию сверху те, кто ближе к работе: сортировка по рангу роли, а не
    по дате находки — писать всё равно одному человеку [OUT-007].
    """
    sort = sort_pick(sort, tuple(CONTACT_SORTS), "role")
    rows = contact_finds.for_company(conn, name)
    if not rows:
        return (
            "<p class=muted>Рабочих каналов по этому работодателю пока не нашлось. "
            "Они ищутся при общем сборе, после досье.</p>"
        )
    body = []
    for row in _sorted(list(rows), CONTACT_SORTS, sort):
        source = "—"
        if row["source_url"]:
            source = '<a href="{url}" target=_blank rel=noreferrer>источник</a>'.format(
                url=esc(row["source_url"])
            )
        body.append(
            [
                '<a href="/vacancy?key={key}">вакансия</a>'.format(
                    key=esc(str(row["key"]))
                ),
                esc(row["person"] or "—"),
                esc(row["role"] or "—"),
                "<span class=pill>{}</span>{}".format(
                    esc(row["channel_kind"]), esc(row["channel_value"])
                ),
                esc(contacts.CONFIDENCE_RU.get(str(row["confidence"]), row["confidence"]))
                + (" · угадан" if row["guessed"] else ""),
                source,
            ]
        )
    base = "/company?name={}".format(urllib.parse.quote(name))
    return table(sort_head(CONTACT_COLUMNS, base, "ksort", sort), body, raw_head=True)


def render_vacancies_for(
    conn: sqlite3.Connection, name: str, sort: str = "published"
) -> str:
    """Вакансии этого работодателя, свежие сверху.

    Список тот же, что на странице вакансий, но без фильтра по скорингу: в
    карточке важно видеть всё, что компания публиковала, включая слабые
    позиции — они и есть материал для выводов о работодателе.
    """
    sort = sort_pick(sort, tuple(VACANCY_SORTS), "published")
    rows = company_signals.vacancy_rows(conn, name)
    if not rows:
        return (
            "<p class=muted>Вакансий этого работодателя в базе нет. Они попадают сюда "
            'при сборе — смотри <a href="/">сбор</a>.</p>'
        )
    body = []
    for row in _sorted(list(rows), VACANCY_SORTS, sort):
        link = '<a href="/vacancy?key={key}">{title}</a>'.format(
            key=urllib.parse.quote(str(row["key"] or "")), title=esc(row["title"])
        )
        body.append(
            [
                "<span class=score>{:.0f}</span>".format(float(row["score"] or 0)),
                link,
                esc(str(row["published_at"] or "")[:10]),
                "✓" if row["notified_at"] else "",
                draft_button(str(row["key"] or ""), "Письмо"),
            ]
        )
    base = "/company?name={}".format(urllib.parse.quote(name))
    return table(sort_head(VACANCY_COLUMNS, base, "jsort", sort), body, raw_head=True)


__all__ = (
    "CONTACT_COLUMNS",
    "CONTACT_SORTS",
    "VACANCY_COLUMNS",
    "VACANCY_SORTS",
    "render_contacts_for",
    "render_vacancies_for",
)
