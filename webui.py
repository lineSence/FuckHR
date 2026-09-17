"""Локальный веб-интерфейс: единственный пульт управления программой.

Здесь три вещи, которые раньше жили в терминале:

- запуск: сбор, письма, проверка модели и тесты — кнопками, с живым логом;
- настройки: весь .env формой, с пояснениями и масками для секретов;
- выдача: вакансии, условия, HR-флаги, контакты, поиск, маршруты модели.

В командной строке остаётся только запуск самих программ: python run.py и
python outreach.py без флагов, параметры они берут из .env. Так же их запускает
планировщик Windows, и настройки у них одни и те же.

Границы, которые не нарушаются:

- слушает только 127.0.0.1: ни авторизации, ни CSRF-защиты здесь нет, и выставлять
  его наружу нельзя;
- ничего не отправляет — ни писем, ни сообщений [CORE-023]; кнопки запуска
  запускают те же программы, что и руками, с теми же правилами;
- на диск пишет только .env, блок facts в profile.yaml и логи задач;
- без новых зависимостей: http.server из стандартной библиотеки справляется с одним
  пользователем, а Flask и FastAPI тянут за собой стек, который потом обновлять.

Про стиль шаблонов: только str.format с заранее вычисленными переменными, без
вложенных f-строк: однажды это уже стоило SyntaxError на ровном месте.

Запуск:
    python webui.py                 # http://127.0.0.1:8765
    python webui.py --port 9000
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sqlite3
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Sequence

import yaml

import conditions
import contacts
import db
import detector
import jobs
import llm
import outreach
import settings
import websearch

log = logging.getLogger("webui")

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
textarea { width: 100%; min-height: 160px; font: 14px/1.5 ui-monospace, Consolas, monospace;
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
"""

NAV_ITEMS = (
    ("/", "Запуск"),
    ("/vacancies", "Вакансии"),
    ("/contacts", "Контакты"),
    ("/search", "Поиск"),
    ("/llm", "Модель"),
    ("/profile", "Профиль"),
    ("/settings", "Настройки"),
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
    """Новое соединение на запрос: sqlite3 не любит передачи между потоками."""
    conn = db.connect(db_path())
    db.init_schema(conn)
    contacts.ensure_schema(conn)
    detector.ensure_schema(conn)
    conditions.ensure_schema(conn)
    return conn


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Ячейки приходят уже готовым HTML: экранирует вызывающая сторона."""
    head = "".join("<th>{}</th>".format(esc(h)) for h in headers)
    body = "".join(
        "<tr>" + "".join("<td>{}</td>".format(cell) for cell in row) + "</tr>"
        for row in rows
    )
    return "<table><tr>{}</tr>{}</table>".format(head, body)


# ———— запуск ————


def render_run(active_id: int | None = None, note: str = "") -> tuple[str, int]:
    """Главная страница: кнопки запуска и лог последней задачи.

    Возвращает тело и интервал автообновления: пока задача идёт, страница
    обновляется сама. Мета-обновление вместо JS — чтобы не тащить фронтенд.
    """
    parts = [note] if note else []

    buttons = []
    for key, title, hint in jobs.task_list():
        buttons.append(
            (
                '<form method=post action="/run">'
                '<input type=hidden name=task value="{key}">'
                '<button title="{hint}">{title}</button></form>'
            ).format(key=esc(key), hint=esc(hint), title=esc(title))
        )
    parts.append("<div class=tasks>{}</div>".format("".join(buttons)))

    notes = settings.missing_required()
    if notes:
        items = "".join("<li>{}</li>".format(esc(item)) for item in notes)
        parts.append(
            '<div class=warn><b>Перед запуском стоит знать:</b><ul>{}</ul>'
            '<a href="/settings">Открыть настройки</a></div>'.format(items)
        )

    collect = settings.collect_options()
    outreach_opts = settings.outreach_options()
    parts.append(
        (
            "<p class=muted>Сейчас так: сбор {limit} вакансий, письма от скора {min_score:.0f} "
            "до {letters} штук, модель {llm_state}. "
            '<a href="/settings">Изменить</a></p>'
        ).format(
            limit=collect.limit,
            min_score=outreach_opts.min_score,
            letters=outreach_opts.limit,
            llm_state="включена" if collect.use_llm else "выключена",
        )
    )

    job = jobs.runner.get(active_id) if active_id else jobs.runner.last()
    if job is None:
        parts.append("<p class=muted>Запусков ещё не было.</p>")
        return "".join(parts), 0

    head = (
        "<h2>{title}</h2>"
        "<p class=muted>Состояние: {status} · длится {duration:.0f} с · строк в логе: {lines}</p>"
    ).format(
        title=esc(job.title),
        status=esc(job.status),
        duration=job.duration,
        lines=len(job.lines),
    )
    parts.append(head)

    if job.running:
        parts.append(
            (
                '<form method=post action="/stop">'
                '<input type=hidden name=job value="{}">'
                "<button class=secondary>Остановить</button></form>"
            ).format(job.id)
        )

    parts.append(
        "<pre class=console>{}</pre>".format(esc("\n".join(job.tail(400)) or "ждём вывод…"))
    )

    history = [item for item in jobs.runner.history() if item.id != job.id]
    if history:
        rows = []
        for item in history[:8]:
            rows.append(
                [
                    '<a href="/?job={}">{}</a>'.format(item.id, esc(item.title)),
                    esc(item.status),
                    "{:.0f} с".format(item.duration),
                    esc(time.strftime("%H:%M:%S", time.localtime(item.started_at))),
                ]
            )
        parts.append("<h2>Прошлые запуски</h2>")
        parts.append(table(["Задача", "Итог", "Длительность", "Начало"], rows))

    return "".join(parts), 3 if job.running else 0


# ———— настройки ————


def render_settings(saved: Sequence[str] = ()) -> str:
    values = settings.load()
    parts = []

    if saved:
        parts.append(
            "<div class=ok>Сохранено: {}</div>".format(esc(", ".join(saved)))
        )

    notes = settings.missing_required()
    if notes:
        items = "".join("<li>{}</li>".format(esc(item)) for item in notes)
        parts.append("<div class=warn><ul>{}</ul></div>".format(items))

    parts.append(
        "<p class=muted>Всё сохраняется в {}. Секреты показаны маской: пустое поле оставляет "
        "текущее значение, слово «очистить» стирает его.</p>".format(
            esc(settings.ENV_PATH)
        )
    )
    parts.append('<form method=post action="/settings">')

    for group, fields in settings.groups():
        parts.append("<h2>{}</h2>".format(esc(group)))
        for field in fields:
            current = values.get(field.key, "")
            hint = field.help
            if field.kind == settings.BOOL:
                checked = " checked" if settings.as_bool(current, settings.as_bool(field.default)) else ""
                control = (
                    '<label><input type=checkbox name="{key}" value="1"{checked}> {label}</label>'
                ).format(key=esc(field.key), checked=checked, label=esc(field.label))
                parts.append(
                    '<div class=field>{control}<div class=hint>{key} · {hint}</div></div>'.format(
                        control=control, key=esc(field.key), hint=esc(hint)
                    )
                )
                continue

            if field.is_secret:
                shown = ""
                placeholder = settings.mask(current)
                input_type = "password"
            else:
                shown = current or field.default
                placeholder = field.default
                input_type = "number" if field.kind in (settings.INT, settings.FLOAT) else "text"

            step = ""
            if field.kind == settings.INT:
                step = ' step="1"'
            elif field.kind == settings.FLOAT:
                step = ' step="any"'

            parts.append(
                (
                    "<div class=field><label>{label}</label>"
                    '<input type="{input_type}" name="{key}" value="{value}" '
                    'placeholder="{placeholder}"{step}>'
                    "<div class=hint>{key} · {hint}</div></div>"
                ).format(
                    label=esc(field.label),
                    input_type=input_type,
                    key=esc(field.key),
                    value=esc(shown),
                    placeholder=esc(placeholder),
                    step=step,
                    hint=esc(hint),
                )
            )

    parts.append("<p><button>Сохранить</button></p></form>")
    parts.append(
        "<p class=muted>Новые значения подхватываются со следующего запуска задачи: "
        "каждая задача — отдельный процесс со свежим .env.</p>"
    )
    return "".join(parts)


# ———— выдача ————


def vacancy_rows(
    conn: sqlite3.Connection, min_score: float = 0.0, limit: int = 50
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT key, title, company, score, url, published_at, last_seen_at, notified_at
        FROM vacancies
        WHERE score >= ?
        ORDER BY score DESC, last_seen_at DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()


def vacancy_one(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vacancies WHERE key = ?", (key,)).fetchone()


def contact_rows(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT key, company, person, role, role_rank, channel_kind, channel_value,
               confidence, guessed, status, created_at, notes
        FROM contacts
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def render_vacancies(conn: sqlite3.Connection, min_score: float, limit: int) -> str:
    rows = vacancy_rows(conn, min_score, limit)
    stats = db.stats(conn)
    direct, total = contacts.coverage(conn)
    with_conditions, scanned = conditions.coverage(conn)

    form = (
        '<form method=get action="/vacancies">'
        'Скоринг от <input type=number step=1 name=min_score value="{min_score}" style="width:90px"> '
        'показать <input type=number step=10 name=limit value="{limit}" style="width:90px"> '
        "<button>Применить</button></form>"
    ).format(min_score=int(min_score), limit=int(limit))

    summary = (
        '<p class=muted>В базе: {vacancies} вакансий. Разобраны условия: {conds} из {scanned}. '
        "Прямых контактов: {direct} из {total}. Найдено по фильтру: {found}.</p>"
    ).format(
        vacancies=stats.get("vacancies", 0),
        conds=with_conditions,
        scanned=scanned,
        direct=direct,
        total=total,
        found=len(rows),
    )

    if not rows:
        return (
            form
            + summary
            + '<div class=warn>Нет вакансий под фильтр. Если база пуста — сначала '
            '<a href="/">сбор</a>.</div>'
        )

    body = []
    for row in rows:
        score = float(row["score"] or 0)
        link = '<a href="/vacancy?key={}">{}</a>'.format(
            urllib.parse.quote(row["key"] or ""), esc(row["title"])
        )
        body.append(
            [
                "<span class=score>{:.0f}</span>".format(score),
                link,
                esc(row["company"]),
                esc((row["published_at"] or "")[:10]),
                "✓" if row["notified_at"] else "",
            ]
        )
    return (
        form
        + summary
        + table(["Скор", "Вакансия", "Компания", "Опубликована", "В TG"], body)
    )


def render_vacancy(conn: sqlite3.Connection, key: str, with_draft: bool) -> str:
    row = vacancy_one(conn, key)
    if row is None:
        return "<p>Вакансия не найдена.</p>"

    head = (
        "<p><b>{company}</b> · скоринг <span class=score>{score:.0f}</span> · "
        '<a href="{url}" target=_blank rel=noreferrer>открыть на hh.ru</a></p>'
    ).format(
        company=esc(row["company"]),
        score=float(row["score"] or 0),
        url=esc(row["url"]),
    )
    parts = [head]

    if row["score_reasons"]:
        parts.append(
            "<h2>Почему такой скор</h2><pre>{}</pre>".format(esc(row["score_reasons"]))
        )

    condition_lines = conditions.lines(conn, key)
    if condition_lines:
        parts.append(
            "<h2>Условия из описания</h2><pre>{}</pre>".format(
                esc("\n".join(condition_lines))
            )
        )
        parts.append(
            "<p class=muted>Каждая строка осталась только потому, что цитата нашлась "
            "в тексте дословно.</p>"
        )

    signal_lines = detector.load_lines(conn, key)
    if signal_lines:
        parts.append(
            "<h2>HR-флаги</h2><pre>{}</pre>".format(esc("\n".join(signal_lines)))
        )

    if with_draft:
        provider = websearch.SearchProvider.from_env(conn)
        gateway = llm.Gateway.from_env(conn) if settings.flag("LLM_ENABLED") else None
        facts = outreach.load_facts(settings.get("RUN_PROFILE", "profile.yaml"))
        options = settings.outreach_options()
        discovery, draft, skip_reason = outreach.process_row(
            conn,
            row,
            facts,
            provider,
            check_mx=options.check_mx,
            allow_generic=options.allow_generic,
            gateway=gateway,
        )
        if skip_reason:
            parts.append(
                "<h2>Черновик</h2><div class=warn>Пропуск: {}</div>".format(esc(skip_reason))
            )
        else:
            card = outreach.format_card(
                row, discovery, draft, signal_lines, condition_lines
            )
            parts.append("<h2>Карточка и черновик</h2><pre>{}</pre>".format(esc(card)))
            if not facts:
                parts.append(
                    "<div class=warn>Блок facts пуст — в письме заглушка вместо повода писать. "
                    '<a href="/profile">Заполнить</a></div>'
                )
    else:
        parts.append(
            (
                '<p><a href="/vacancy?key={}&draft=1">Собрать черновик и найти контакт</a> '
                "<span class=muted>(может дёрнуть внешний поиск и модель, ничего не отправляет)</span></p>"
            ).format(urllib.parse.quote(key))
        )

    parts.append("<h2>Описание</h2><pre>{}</pre>".format(esc(row["description"])))
    return "".join(parts)


def render_contacts(conn: sqlite3.Connection) -> str:
    rows = contact_rows(conn)
    direct, total = contacts.coverage(conn)
    header = "<p class=muted>Прямых контактов {} из {}.</p>".format(direct, total)

    if not rows:
        return header + (
            '<div class=warn>Лог контактов пуст. Он заполняется задачей '
            '«Подготовка писем» на <a href="/">странице запуска</a>.</div>'
        )

    body = []
    for r in rows:
        channel = "<span class=pill>{}</span>{}".format(
            esc(r["channel_kind"]), esc(r["channel_value"])
        )
        confidence = esc(r["confidence"]) + (" · угадан" if r["guessed"] else "")
        body.append(
            [
                esc(r["company"]),
                esc(r["person"] or "—"),
                esc(r["role"] or "—"),
                channel,
                confidence,
                esc(r["status"]),
                esc((r["created_at"] or "")[:10]),
            ]
        )
    return header + table(
        ["Компания", "Человек", "Роль", "Канал", "Уверенность", "Статус", "Записан"],
        body,
    )


def render_search(conn: sqlite3.Connection, query: str, company: str) -> str:
    """Живая проверка выдачи: видно, что именно отдаёт поиск до ранжирования."""
    provider = websearch.SearchProvider.from_env(conn)
    state = "готов" if provider.enabled else esc(provider.disabled_reason)

    head = (
        '<form method=get action="/search">'
        'Запрос <input type=text name=q value="{query}" style="width:60%"> '
        "<button>Искать</button></form>"
        '<form method=get action="/search">'
        'Или запросы по компании <input type=text name=company value="{company}" style="width:40%"> '
        "<button>Показать и выполнить</button></form>"
        "<p class=muted>Провайдер: {provider} · адрес: {base_url} · {state}</p>"
    ).format(
        query=esc(query),
        company=esc(company),
        provider=esc(provider.provider),
        base_url=esc(provider.base_url or "не задан"),
        state=state,
    )

    if not provider.enabled:
        return head + (
            '<div class=warn>Внешний поиск выключен, искать негде. '
            'Адрес инстанса задаётся в <a href="/settings">настройках</a>.</div>'
        )

    queries = [query] if query else []
    if company:
        queries = list(websearch.contact_queries(company))
    if not queries:
        return head + "<p class=muted>Введи запрос или название компании.</p>"

    parts = [head, "<h2>Запросы</h2><pre>{}</pre>".format(esc("\n".join(queries)))]
    hits = provider.search_many(queries, limit=5)
    usage = provider.usage
    parts.append(
        (
            "<p class=muted>Вызовов: {calls} · из кэша: {cached} · "
            "ошибок: {failures} · пропущено: {skipped}</p>"
        ).format(
            calls=usage.calls,
            cached=usage.cached,
            failures=usage.failures,
            skipped=usage.skipped,
        )
    )

    if not hits:
        parts.append(
            "<div class=warn>Пустая выдача. Проверь, жив ли туннель и отвечает ли "
            "инстанс форматом json.</div>"
        )
        return "".join(parts)

    body = []
    for hit in hits:
        link = '<a href="{}" target=_blank rel=noreferrer>{}</a>'.format(
            esc(hit.url), esc(hit.title or hit.url)
        )
        body.append(
            [link, esc(contacts.domain_of(hit.url) or ""), esc(hit.snippet[:300])]
        )
    parts.append(table(["Страница", "Домен", "Сниппет"], body))
    return "".join(parts)


def render_llm(conn: sqlite3.Connection, probe: bool = False) -> str:
    """Какой этап на какую модель уходит — без запуска пайплайна."""
    if not settings.flag("LLM_ENABLED"):
        return (
            '<div class=warn>Модель выключена в <a href="/settings">настройках</a>. '
            "Пайплайн работает без неё целиком, просто грубее.</div>"
        )

    gateway = llm.Gateway.from_env(conn)
    parts = []
    if not gateway.enabled:
        parts.append(
            "<div class=danger>Шлюз не готов: {}</div>".format(esc(gateway.disabled_reason))
        )

    rows = []
    for stage, profile, route, model in gateway.describe_routes():
        personal = stage in llm.PERSONAL_STAGES
        marker = ""
        if personal and route == llm.ROUTE_PROXY:
            marker = ' <span class=pill>персональные данные уходят наружу</span>'
        rows.append(
            [
                esc(stage),
                esc(profile),
                esc(route) + marker,
                esc(model or "не задана"),
            ]
        )
    parts.append(table(["Этап", "Профиль", "Маршрут", "Модель"], rows))

    if settings.flag("LLM_PERSONAL_VIA_PROXY"):
        parts.append(
            "<div class=danger>Включён режим, при котором ФИО и адреса живых людей "
            "уходят на внешний прокси. Если прокси ходит наружу, а не в твою Ollama, "
            "это утечка чужих персональных данных [CORE-012].</div>"
        )

    parts.append(
        '<p><a href="/llm?probe=1">Спросить список моделей у прокси</a> '
        '<span class=muted>(один запрос к /v1/models)</span></p>'
    )

    if probe:
        try:
            names = gateway.models()
            if names:
                items = "".join("<li>{}</li>".format(esc(name)) for name in names)
                parts.append("<h2>Модели на прокси</h2><ul>{}</ul>".format(items))
            else:
                parts.append(
                    "<div class=warn>Прокси не отдал список моделей. Чаще всего это отсутствующий "
                    "ключ: без него LiteLLM отвечает 401.</div>"
                )
        except Exception as exc:  # noqa: BLE001 — сеть может лежать, это не повод падать
            parts.append("<div class=danger>{}</div>".format(esc(exc)))

    parts.append(
        "<p class=muted>Живой вызов на выдуманном тексте — задача «Проверка модели» "
        'на <a href="/">странице запуска</a>.</p>'
    )
    return "".join(parts)


# ———— профиль ————


def save_facts(profile_path: str | Path, text: str) -> tuple[str, ...]:
    """Перезаписывает только блок facts, остальное в профиле не трогает.

    Пустые строки отбрасываются здесь же: именно они разбирались в None и уезжали
    в письмо как факт о себе.
    """
    path = Path(profile_path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    facts = [line.strip() for line in text.splitlines() if line.strip()]
    data["facts"] = facts
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return tuple(facts)


def profile_summary(profile_path: str | Path) -> list[tuple[str, str]]:
    """Короткая сводка профиля для просмотра — без редактирования."""
    path = Path(profile_path)
    if not path.exists():
        return [("файл", "{} не найден".format(path))]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    queries = data.get("queries") or []
    salary = data.get("salary") or {}
    skills = data.get("skills") or []
    query_names = []
    for item in queries:
        if isinstance(item, dict):
            query_names.append(str(item.get("text", "")))
        else:
            query_names.append(str(item))
    return [
        ("запросы", ", ".join(q for q in query_names if q) or "не заданы"),
        ("минимум на руки", str(salary.get("min_net", "не задан"))),
        ("порог скоринга", str(data.get("min_score", "не задан"))),
        ("навыки", ", ".join(str(s) for s in skills) or "не заданы"),
    ]


def render_profile(profile_path: str, saved: int | None = None) -> str:
    facts = outreach.load_facts(profile_path)
    rows = [[esc(name), esc(value)] for name, value in profile_summary(profile_path)]

    note = ""
    if saved is not None:
        note = "<div class=ok>Сохранено фактов: {}</div>".format(saved)
    elif not facts:
        note = (
            "<div class=warn>Блок facts пуст. Без него каждое письмо собирается с заглушкой "
            "вместо повода писать.</div>"
        )

    return (
        note
        + "<h2>Факты о себе</h2>"
        + "<p class=muted>По одному на строку, с цифрами. Только эти строки попадают в письмо: "
        + "ничего кроме них система о вас не напишет.</p>"
        + '<form method=post action="/profile">'
        + "<textarea name=facts>{}</textarea>".format(esc("\n".join(facts)))
        + "<p><button>Сохранить</button></p></form>"
        + "<h2>Остальное в профиле</h2>"
        + "<p class=muted>Чтение; запросы и навыки правятся в profile.yaml.</p>"
        + table(["Параметр", "Значение"], rows)
    )


# ———— сервер ————


class Handler(BaseHTTPRequestHandler):
    server_version = "FuckHR-webui"
    profile_path = "profile.yaml"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.debug("%s", format % args)

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        """После POST всегда редирект: иначе F5 повторяет запуск задачи."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        def one(name: str, default: str = "") -> str:
            return (params.get(name) or [default])[0]

        try:
            if parsed.path == "/favicon.ico":
                self._send("", 404)
                return
            if parsed.path == "/":
                job_id = settings.as_int(one("job"), 0) or None
                body, refresh = render_run(job_id)
                self._send(page("Запуск", body, refresh))
                return
            if parsed.path == "/settings":
                self._send(page("Настройки", render_settings()))
                return
            if parsed.path == "/profile":
                self._send(page("Профиль", render_profile(self.profile_path)))
                return

            conn = open_db()
            try:
                if parsed.path == "/vacancies":
                    min_score = settings.as_float(one("min_score", "0"), 0.0)
                    limit = min(settings.as_int(one("limit", "50"), 50), 500)
                    self._send(page("Вакансии", render_vacancies(conn, min_score, limit)))
                elif parsed.path == "/vacancy":
                    body = render_vacancy(conn, one("key"), with_draft=one("draft") == "1")
                    self._send(page("Вакансия", body))
                elif parsed.path == "/contacts":
                    self._send(page("Контакты", render_contacts(conn)))
                elif parsed.path == "/search":
                    body = render_search(conn, one("q"), one("company"))
                    self._send(page("Проверка поиска", body))
                elif parsed.path == "/llm":
                    self._send(page("Модель", render_llm(conn, one("probe") == "1")))
                else:
                    self._send(page("Не найдено", "<p>Такой страницы нет.</p>"), 404)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — интерфейс не должен падать целиком
            log.exception("ошибка при обработке %s", self.path)
            self._send(page("Ошибка", "<pre>{}</pre>".format(esc(exc))), 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            form = self._form()

            if parsed.path == "/run":
                task = (form.get("task") or [""])[0]
                try:
                    job = jobs.runner.start(task)
                except (KeyError, RuntimeError) as exc:
                    body, refresh = render_run(
                        None, "<div class=warn>{}</div>".format(esc(exc))
                    )
                    self._send(page("Запуск", body, refresh))
                    return
                self._redirect("/?job={}".format(job.id))
                return

            if parsed.path == "/stop":
                job_id = settings.as_int((form.get("job") or [""])[0], 0)
                jobs.runner.stop(job_id)
                self._redirect("/?job={}".format(job_id))
                return

            if parsed.path == "/settings":
                updates = settings.form_updates(form)
                saved = settings.save(updates)
                self._send(page("Настройки", render_settings(saved)))
                return

            if parsed.path == "/profile":
                text = (form.get("facts") or [""])[0]
                facts = save_facts(self.profile_path, text)
                self._send(
                    page("Профиль", render_profile(self.profile_path, saved=len(facts)))
                )
                return

            self._send(page("Не найдено", "<p>Такой страницы нет.</p>"), 404)
        except Exception as exc:  # noqa: BLE001
            log.exception("ошибка при обработке POST %s", self.path)
            self._send(page("Ошибка", "<pre>{}</pre>".format(esc(exc))), 500)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальный интерфейс FuckHR")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        log.warning("python-dotenv не установлен: читаю только переменные окружения")

    Handler.profile_path = settings.get("RUN_PROFILE", "profile.yaml")
    server = HTTPServer((HOST, args.port), Handler)
    log.info("интерфейс здесь: http://%s:%s (Ctrl+C чтобы остановить)", HOST, args.port)
    log.info("база: %s · настройки: %s", db_path(), settings.ENV_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("остановлен")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
