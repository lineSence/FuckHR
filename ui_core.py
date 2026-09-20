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
*, *::before, *::after { box-sizing: border-box }
:root { --bg:#f2f3f5; --card:#fff; --line:#dfe1e6; --text:#172b4d; --muted:#6b778c;
        --brand:#0b5cd5; --ok:#006644; --okbg:#e3fcef; --warn:#974f0c; --warnbg:#fffae6;
        --danger:#bf2600; --dangerbg:#ffebe6 }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: var(--brand); text-decoration: none }
a:hover { text-decoration: underline }
/* Шапка липкая: на длинных списках вакансий переход в другой раздел не должен
   требовать прокрутки наверх. */
.topbar { background: #172b4d; color: #fff; padding: 0 20px; height: 52px;
          display: flex; align-items: center; gap: 24px; position: sticky; top: 0; z-index: 5 }
.brand { display: flex; align-items: center; gap: 9px; font-size: 15px; font-weight: 600 }
.brand .logo { display: grid; place-items: center; width: 26px; height: 26px;
               border-radius: 6px; background: #2684ff; font-size: 12px }
.topbar nav { display: flex; gap: 2px; overflow: auto }
.topbar nav a { color: #c1c7d0; padding: 6px 11px; border-radius: 5px; font-size: 13.5px;
                white-space: nowrap }
.topbar nav a:hover { background: #243858; color: #fff; text-decoration: none }
.topbar nav a.active { background: #2684ff; color: #fff; font-weight: 600 }
main { max-width: 1240px; margin: 0 auto; padding: 20px }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 4px;
         padding: 16px 18px }
h1 { font-size: 20px; margin: 0 0 14px }
h2 { font-size: 13px; margin: 22px 0 10px; color: var(--muted);
     text-transform: uppercase; letter-spacing: .04em }
h3 { font-size: 14px; margin: 16px 0 6px }
.muted { color: var(--muted); font-size: 13px }
table { border-collapse: collapse; width: 100% }
th { font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
     text-align: left; padding: 8px 10px; border-bottom: 2px solid var(--line) }
td { padding: 8px 10px; border-bottom: 1px solid #f4f5f7; vertical-align: top }
table tr:nth-child(even) td { background: #fafbfc }
.score { font-variant-numeric: tabular-nums; font-weight: 700; font-size: 15px }
details { background: var(--card); border: 1px solid var(--line); border-radius: 4px;
          padding: 10px 14px; margin: 10px 0 }
details[open] { padding-bottom: 14px }
summary { cursor: pointer; font-weight: 600; font-size: 13.5px }
summary .muted { font-weight: 400 }
form.inline { display: inline }
form.inline button { padding: 4px 10px; font-size: 12.5px }
.warn, .ok, .danger { border-radius: 4px; padding: 10px 12px; margin: 12px 0;
        font-size: 13.5px; border-left: 4px solid; color: #42526e }
.warn { background: var(--warnbg); border-color: #ffab00 }
.ok { background: var(--okbg); border-color: #36b37e }
.danger { background: var(--dangerbg); border-color: #ff5630 }
/* В таблице те же классы означают метку, а не блок-предупреждение:
   без этого padding и margin блока разносили строки и налезали друг на друга. */
td .warn, td .danger, td .ok { display: inline-block; padding: 1px 8px; margin: 0;
        border: 0; border-radius: 3px; font-size: 12px; font-weight: 700;
        text-transform: uppercase; letter-spacing: .03em; white-space: nowrap }
.pill { display: inline-block; padding: 2px 8px; margin-right: 6px; border-radius: 3px;
        background: #deebff; color: var(--brand); font-size: 12px; font-weight: 700;
        text-transform: uppercase }
pre { background: #f4f5f7; border-radius: 4px; padding: 12px; white-space: pre-wrap;
      word-break: break-word }
.console { background: #091e42; color: #b3d4ff; max-height: 460px; overflow: auto;
           font: 12.5px/1.55 ui-monospace, Consolas, monospace }
button { padding: 6px 12px; border: 0; border-radius: 4px; background: var(--brand);
         color: #fff; font-size: 13.5px; font-weight: 600; cursor: pointer }
button.secondary { background: #ebecf0; color: var(--text) }
.tasks { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px }
.tasks form { margin: 0 }
.bar { display: flex; align-items: center; gap: 12px; margin: 10px 0 }
.bar progress { width: 360px; height: 6px }
.field { margin: 10px 0 }
.field label { display: block; font-weight: 600; font-size: 13px; margin-bottom: 4px }
.field .hint { font-size: 12px; color: var(--muted); margin-top: 4px }
textarea { width: 100%; min-height: 110px; padding: 8px 10px; background: #fafbfc;
           border: 2px solid var(--line); border-radius: 4px;
           font: 13px/1.5 ui-monospace, Consolas, monospace }
input[type=text], input[type=number], input[type=password], input[type=search] {
           width: 100%; max-width: 560px;
           padding: 6px 10px;
           background: #fafbfc; border: 2px solid var(--line); border-radius: 4px;
           font-size: 13.5px; font-family: inherit }
select { padding: 5px 8px; background: #fafbfc; border: 2px solid var(--line);
         border-radius: 4px; font-size: 13.5px; font-family: inherit; color: var(--text) }
input:focus, textarea:focus, select:focus { outline: 0; background: #fff; border-color: var(--brand) }
.cols { display: flex; gap: 16px; flex-wrap: wrap }
.cols .field { flex: 1 1 220px; margin: 8px 0 }
.checks { display: flex; gap: 16px; flex-wrap: wrap; margin: 8px 0 2px }
.checks label, .field label:has(input) { font-weight: 400 }
@media (max-width: 720px) {
  .topbar { height: auto; flex-wrap: wrap; gap: 8px; padding: 10px 14px }
  main { padding: 12px 10px }
  .panel { padding: 12px }
  .bar progress { width: 100% }
}
"""

NAV_ITEMS = (
    ("/", "Запуск"),
    ("/vacancies", "Вакансии"),
    ("/companies", "Компании и контакты"),
    ("/search", "Поиск"),
    ("/llm", "Модель"),
    ("/profile", "Профиль и резюме"),
    ("/settings", "Настройки"),
    ("/cleanup", "Очистка"),
)

# Заголовок страницы служит и признаком активного пункта: отдельный параметр
# пришлось бы протаскивать через все три десятка вызовов page() [CORE-025].
NAV_BY_TITLE = {name: href for href, name in NAV_ITEMS}
NAV_BY_TITLE.update(
    {"Вакансия": "/vacancies", "Досье": "/companies", "Проверка поиска": "/search"}
)


def nav(title: str = "") -> str:
    active = NAV_BY_TITLE.get(title, "")
    items = []
    for href, name in NAV_ITEMS:
        cls = " class=active" if href == active else ""
        items.append('<a href="{}"{}>{}</a>'.format(href, cls, name))
    return "<nav>{}</nav>".format("".join(items))


NAV = nav()


def esc(value: object) -> str:
    """Всё, что пришло из базы, поиска или лога, попадает в HTML только через это.

    В описаниях вакансий и сниппетах выдачи регулярно приезжает сырой HTML.
    """
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, refresh: int = 0, refresh_url: str = "/") -> str:
    meta = ""
    if refresh:
        # Адрес обязателен. Без него браузер перезагружает текущий адрес, а
        # страница, отрисованная в ответ на POST, живёт по адресу вроде /run,
        # где GET-обработчика нет, — и самообновление уводило на «такой
        # страницы нет».
        meta = '<meta http-equiv=refresh content="{};url={}">'.format(
            int(refresh), esc(refresh_url)
        )
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        "{meta}<title>{title} — FuckHR</title><style>{style}</style></head><body>"
        '<header class=topbar><span class=brand><span class=logo>FH</span>FuckHR</span>'
        "{nav}</header>"
        "<main><h1>{title}</h1><div class=panel>{body}</div></main>"
        "</body></html>"
    ).format(
        meta=meta, title=esc(title), style=STYLE, nav=nav(title), body=body
    )


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


def table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], raw_head: bool = False
) -> str:
    """Ячейки приходят уже готовым HTML: экранирует вызывающая сторона.

    raw_head нужен заголовкам со ссылками сортировки — их собирает sort_head.
    """
    head = "".join("<th>{}</th>".format(h if raw_head else esc(h)) for h in headers)
    body = "".join(
        "<tr>" + "".join("<td>{}</td>".format(cell) for cell in row) + "</tr>"
        for row in rows
    )
    return "<table><tr>{}</tr>{}</table>".format(head, body)


def details(title: str, note: str, body: str, open_: bool = False) -> str:
    """Сворачиваемый блок. По умолчанию закрыт: карточка должна читаться сверху.

    Экранируется только заголовок: тело собирают вызывающие, у них уже HTML.
    """
    tail = ' <span class=muted>{}</span>'.format(esc(note)) if note else ""
    return (
        "<details{op}><summary>{title}{tail}</summary>{body}</details>"
    ).format(op=" open" if open_ else "", title=esc(title), tail=tail, body=body)


def sort_pick(value: object, allowed: Sequence[str], default: str) -> str:
    """Имя сортировки из запроса. Чужое значение молча заменяется умолчанием."""
    name = str(value or "").strip()
    return name if name in allowed else default


def sort_head(
    columns: Sequence[tuple[str, str]], base: str, param: str, current: str
) -> list[str]:
    """Заголовки-ссылки сортировки. У колонки без ключа — обычный текст.

    Переключения «по возрастанию/по убыванию» нет намеренно: у скора и даты
    осмысленно только убывание, у названий — только алфавит, вторая стрелка
    добавляла бы клик, не давая ни одного нового ответа [CORE-025].
    """
    cells = []
    for key, label in columns:
        if not key:
            cells.append(esc(label))
            continue
        sep = "&" if "?" in base else "?"
        link = '<a href="{base}{sep}{param}={key}">{label}</a>'.format(
            base=esc(base), sep=sep, param=esc(param), key=esc(key), label=esc(label)
        )
        cells.append(link + (" ↓" if key == current else ""))
    return cells


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
    "nav",
    "STYLE",
    "area_field",
    "checkbox_field",
    "db_path",
    "details",
    "esc",
    "hint_block",
    "number_field",
    "open_db",
    "page",
    "sort_head",
    "sort_pick",
    "table",
    "text_field",
)
