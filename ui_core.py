"""Общие детали интерфейса: шаблон страницы, экранирование, поля форм, база.

Раньше всё это жило в webui.py и файл разросся до сорока килобайт. Разделение
простое: здесь нет ни одной страницы и ни одного знания о пайплайне — только то,
чем пользуются страницы.

Правило шаблонов то же: только str.format с заранее вычисленными переменными, без
вложенных ф-строк: однажды это уже стоило SyntaxError.
"""

from __future__ import annotations

import html
import os
import sqlite3
from typing import Sequence

import conditions
import contacts
import db
import detector
import dossier
import resume

# Адрес зашит намеренно: интерфейс без авторизации не должен слушать сеть.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

STYLE = """
body { font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0 auto;
       max-width: 1000px; padding: 24px; color: #1d1d1f; }
a { color: #0b62d6; }
nav { display: flex; gap: 16px; margin-bottom: 24px; padding-bottom: 12px;
      border-bottom: 1px solid #e3e3e6; flex-wrap: wrap; }
h1 { font-size: 22px; margin: 0 0 16px; }
h2 { font-size: 17px; margin: 24px 0 8px; }
h3 { font-size: 15px; margin: 18px 0 6px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #ececef;
         vertical-align: top; }
th { font-weight: 600; font-size: 13px; color: #6b6b70; }
.score { font-variant-numeric: tabular-nums; font-weight: 600; }
.muted { color: #6b6b70; }
.warn { background: #fff6e5; border: 1px solid #f0d9a8; padding: 10px 12px;
        border-radius: 6px; margin: 12px 0; }
.danger { background: #fdecec; border: 1px solid #f0b9b9; padding: 10px 12px;
        border-radius: 6px; margin: 12px 0; }
.ok { background: #eaf7ee; border: 1px solid #b6e0c2; padding: 10px 12px;
      border-radius: 6px; margin: 12px 0; }
pre { background: #f6f6f8; padding: 12px; border-radius: 6px; white-space: pre-wrap;
      word-break: break-word; }
.console { background: #1d1f23; color: #e6e6e6; max-height: 460px; overflow: auto;
           font: 13px/1.45 ui-monospace, Consolas, monospace; }
textarea { width: 100%; min-height: 120px; font: 14px/1.5 ui-monospace, Consolas, monospace;
           padding: 10px; border: 1px solid #d2d2d7; border-radius: 6px; }
input[type=text], input[type=number], input[type=password] { padding: 7px 9px;
           border: 1px solid #d2d2d7; border-radius: 6px; font-size: 14px; width: 100%;
           box-sizing: border-box; }
button { padding: 8px 14px; border: 0; border-radius: 6px; background: #0b62d6;
         color: #fff; font-size: 14px; cursor: pointer; }
button.secondary { background: #e9ebef; color: #1d1d1f; }
.tasks { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.tasks form { margin: 0; }
.field { margin: 12px 0; }
.field label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 3px; }
.field .hint { font-size: 13px; color: #6b6b70; margin-top: 3px; }
.pill { display: inline-block; padding: 1px 7px; border-radius: 99px; font-size: 12px;
        background: #eef1f5; margin-right: 6px; }
.bar { display: flex; align-items: center; gap: 12px; margin: 10px 0; }
.bar progress { width: 380px; height: 14px; }
.cols { display: flex; gap: 18px; flex-wrap: wrap; }
.cols .field { flex: 1 1 220px; margin: 8px 0; }
.checks { display: flex; gap: 18px; flex-wrap: wrap; margin: 6px 0 2px; }
.checks label { font-weight: 400; }
"""

NAV_ITEMS = (
    ("/", "Запуск"),
    ("/vacancies", "Вакансии"),
    ("/companies", "Компании"),
    ("/contacts", "Контакты"),
    ("/search", "Поиск"),
    ("/llm", "Модель"),
    ("/resume", "Резюме"),
    ("/profile", "Профиль"),
    ("/settings", "Настройки"),
    ("/cleanup", "Очистка"),
)

NAV = "<nav>{}</nav>".format(
    "".join('<a href="{}">{}</a>'.format(href, name) for href, name in NAV_ITEMS)
)


def esc(value: object) -> str:
    """Всё, что пришло из базы, поиска или лога, попадает в HTML только через это.

    В описаниях вакансий и сниппетах выдачи регулярно приезжает сырой HTML.
    """
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, refresh: int = 0) -> str:
    meta = ""
    if refresh:
        meta = '<meta http-equiv=refresh content="{}">'.format(int(refresh))
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        "{meta}<title>{title} — FuckHR</title><style>{style}</style></head><body>"
        "{nav}<h1>{title}</h1>{body}</body></html>"
    ).format(meta=meta, title=esc(title), style=STYLE, nav=NAV, body=body)


def db_path() -> str:
    return os.getenv("DB_PATH", "data/fuckhr.sqlite3")


def open_db() -> sqlite3.Connection:
    """Новое соединение на запрос: sqlite3 не любит передачи между потоками.

    Схемы всех этапов создаются здесь: интерфейс часто открывают до первого сбора,
    и страница не должна падать из-за отсутствующей таблицы.
    """
    conn = db.connect(db_path())
    db.init_schema(conn)
    contacts.ensure_schema(conn)
    detector.ensure_schema(conn)
    conditions.ensure_schema(conn)
    dossier.ensure_schema(conn)
    resume.ensure_schema(conn)
    return conn


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Ячейки приходят уже готовым HTML: экранирует вызывающая сторона."""
    head = "".join("<th>{}</th>".format(esc(h)) for h in headers)
    body = "".join(
        "<tr>" + "".join("<td>{}</td>".format(cell) for cell in row) + "</tr>"
        for row in rows
    )
    return "<table><tr>{}</tr>{}</table>".format(head, body)


def hint_block(hint: str) -> str:
    return "<div class=hint>{}</div>".format(esc(hint)) if hint else ""


def text_field(
    name: str, label: str, value: object, hint: str = "", placeholder: object = ""
) -> str:
    return (
        "<div class=field><label>{label}</label>"
        '<input type=text name="{name}" value="{value}" placeholder="{placeholder}">'
        "{hint}</div>"
    ).format(
        label=esc(label),
        name=esc(name),
        value=esc(value),
        placeholder=esc(placeholder),
        hint=hint_block(hint),
    )


def number_field(name: str, label: str, value: object, hint: str = "") -> str:
    return (
        "<div class=field><label>{label}</label>"
        '<input type=number step=any name="{name}" value="{value}">'
        "{hint}</div>"
    ).format(
        label=esc(label), name=esc(name), value=esc(value), hint=hint_block(hint)
    )


def area_field(name: str, label: str, value: object, hint: str = "") -> str:
    return (
        "<div class=field><label>{label}</label>"
        '<textarea name="{name}">{value}</textarea>'
        "{hint}</div>"
    ).format(
        label=esc(label), name=esc(name), value=esc(value), hint=hint_block(hint)
    )


def checkbox_field(name: str, label: str, checked: bool, hint: str = "") -> str:
    return (
        "<div class=field>"
        '<label><input type=checkbox name="{name}" value="1"{checked}> {label}</label>'
        "{hint}</div>"
    ).format(
        name=esc(name),
        checked=" checked" if checked else "",
        label=esc(label),
        hint=hint_block(hint),
    )


__all__ = (
    "DEFAULT_PORT",
    "HOST",
    "NAV",
    "NAV_ITEMS",
    "STYLE",
    "area_field",
    "checkbox_field",
    "db_path",
    "esc",
    "hint_block",
    "number_field",
    "open_db",
    "page",
    "table",
    "text_field",
)
