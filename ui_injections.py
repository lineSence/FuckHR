"""Страница «Инъекции»: что чужой текст пытался сказать нашей модели.

Отдельный файл по [CORE-024] и по смыслу: остальные страницы показывают
вакансии и компании, а эта — попытки ими управлять.

Зачем на это вообще смотреть. Детектор (ADR-020) вырезает найденное молча, до
модели, и владельцу видна только строка в карточке. Но список находок — это
ещё и способ проверить сам детектор: если в цитатах окажется обычный текст
вакансии, порог ложных срабатываний надо снижать, а не радоваться улову.
Цифры отсюда никуда не идут: страница только читает [CORE-015].

Цитата — чужой текст, поэтому экранируется целиком и показывается коротко:
это улика, а не чтение.
"""

from __future__ import annotations

import sqlite3
import urllib.parse

import injection_rules as R
import injection_store
from ui_core import esc, table

LIMIT = 200


def render_injections(conn: sqlite3.Connection, level: str = "") -> str:
    """Список находок: свежие сверху, с фильтром по уровню."""
    level = level if level in (R.RED, R.YELLOW) else ""
    total, red = injection_store.counts(conn)
    parts = [
        "<p>Найдено объектов с подозрительным текстом: <b>{total}</b>, "
        "из них с настоящей инъекцией: <b>{red}</b>. Текст вычищен до того, "
        "как его увидела модель.</p>".format(total=total, red=red)
    ]

    tabs = []
    for code, name in (("", "все находки"), (R.RED, "только инъекции"),
                       (R.YELLOW, "только подозрения")):
        href = "/injections" + ("?level=" + code if code else "")
        mark = "<b>{}</b>" if code == level else "{}"
        tabs.append('<a href="{}">{}</a>'.format(href, mark.format(esc(name))))
    parts.append("<p class=muted>{}</p>".format(" · ".join(tabs)))

    rows = []
    for row in injection_store.recent(conn, LIMIT, level):
        code = str(row["code"])
        title = R.CODES.get(code, (code, 0))[0]
        level_ru = R.LEVEL_RU.get(str(row["level"]), str(row["level"]))
        mark = "🧨" if str(row["level"]) == R.RED else "⚠️"
        rows.append(
            [
                esc(str(row["seen_at"] or "")[:16].replace("T", " ")),
                "{} {}".format(mark, esc(level_ru)),
                esc(title),
                _where(row),
                "<code>{}</code>".format(esc(str(row["quote"] or "")[:R.QUOTE_CHARS])),
            ]
        )

    if not rows:
        parts.append(
            "<div class=ok>Пока ничего не поймано. Это нормально: спрятанные "
            "команды встречаются у единиц работодателей.</div>"
        )
        return "".join(parts)

    parts.append(table(["Когда", "Уровень", "Что нашли", "Где", "Цитата"], rows))
    if len(rows) >= LIMIT:
        parts.append(
            "<p class=muted>Показаны последние {} находок.</p>".format(LIMIT)
        )
    parts.append(
        "<p class=muted>Уровень «инъекция» добавляет компании улику по оси "
        "правдивости весом {}: подозрение, а не приговор — текст мог вставить "
        "агрегатор.</p>".format(injection_store.EVIDENCE_WEIGHT)
    )
    return "".join(parts)


def _where(row: sqlite3.Row) -> str:
    """Ссылки на вакансию и компанию. Чего уже нет в базе — просто текстом."""
    out = []
    title = str(row["title"] or "") if "title" in row.keys() else ""
    if str(row["kind"]) == "vacancy" and title:
        out.append(
            '<a href="/vacancy?key={key}">{title}</a>'.format(
                key=urllib.parse.quote(str(row["key"])), title=esc(title)
            )
        )
    else:
        out.append(esc(str(row["key"])[:80]))
    company = str(row["company"] or "")
    if company:
        out.append(
            '<a href="/company?name={name}">{company}</a>'.format(
                name=urllib.parse.quote(company), company=esc(company)
            )
        )
    return "<br>".join(out)


__all__ = ("LIMIT", "render_injections")
