"""Блок глубокого ресёрча на странице компании (ADR-019).

Выделено по [CORE-024]: `ui_companies.py` уже на пределе размера. Здесь только
HTML и запуск задачи, вся работа — в `deepresearch.py`.

Кнопка, а не автоматика: ресёрч стоит минут и запросов, и решение о нём
принимает владелец [CORE-016].
"""

from __future__ import annotations

import sqlite3
from typing import Mapping, Sequence

import deepresearch
import deepresearch_store as store
import jobs
import ui_companies
from ui_core import esc, table


def companies_known(conn: sqlite3.Connection) -> set[str]:
    """Компании, про которые в базе вообще что-то есть.

    Название приходит из браузера и уходит в argv подпроцесса. В argv[0] оно
    не попадает никогда (команда берётся из jobs.TASKS), но сверка со своей
    базой отсекает и опечатки, и попытку запустить ресёрч по чему угодно.
    """
    names: set[str] = set()
    for query in (
        "SELECT DISTINCT company FROM vacancies WHERE company <> ''",
        "SELECT DISTINCT company FROM company_dossier WHERE company <> ''",
    ):
        try:
            names.update(str(row[0]) for row in conn.execute(query).fetchall())
        except sqlite3.Error:  # таблицы может не быть на пустой базе
            continue
    return names


def start(conn: sqlite3.Connection, company: str, force: bool = False) -> str:
    """Запускает задачу ресёрча. Возвращает пустую строку или текст ошибки."""
    company = (company or "").strip()
    if company not in companies_known(conn):
        return "Такой компании нет в базе."
    if not deepresearch.options().enabled:
        return "Глубокий ресёрч выключен настройкой DEEP_ENABLED."
    extra = ["--company", company] + (["--force"] if force else [])
    try:
        jobs.runner.start("research-deep", extra)
    except (KeyError, RuntimeError) as exc:
        return str(exc)
    return ""


def handle(conn: sqlite3.Connection, form: Mapping[str, Sequence[str]]) -> str:
    """Обработка формы «Собрать заново».

    Пустая строка — задача запущена, можно уводить на страницу запуска. Иначе
    возвращается готовая страница досье с объяснением, почему не вышло.
    """
    company = (form.get("company") or [""])[0]
    problem = start(conn, company, force=bool(form.get("force")))
    if not problem:
        return ""
    return ui_companies.render_company(conn, company) + render_research(
        conn, company, problem
    )


def render_research(conn: sqlite3.Connection, company: str, note: str = "") -> str:
    """Отчёт ресёрча: находки с источниками или честное «недостаточно данных»."""
    company = (company or "").strip()
    if not company:
        return ""
    opts = deepresearch.options()
    parts = ["<h2>Глубокий ресёрч</h2>"]
    if note:
        parts.append("<div class=warn>{}</div>".format(esc(note)))
    parts.append(
        (
            # Тумблер рядом с кнопкой: раньше выключенный ресёрч было видно
            # только текстом ошибки после нажатия.
            '<form method=post action="/deep" class=tasks>'
            '<input type=hidden name=company value="{company}">'
            "<label><input type=checkbox name=enabled value=1 {enabled}> "
            "глубокий ресёрч включён</label> "
            "<button class=secondary>Сохранить</button></form>"
            '<form method=post action="/research">'
            '<input type=hidden name=company value="{company}">'
            '<input type=hidden name=force value="1">'
            "<button>Собрать заново</button></form>"
            "<p class=muted>Реестр, суды, долги, банкротство и новости. Бюджет "
            "времени {seconds:.0f} с, отчёт считается свежим {ttl} дней. Капча на "
            "источнике не обходится: он пропускается, обход идёт дальше.</p>"
        ).format(
            company=esc(company),
            enabled="checked" if opts.enabled else "",
            seconds=opts.seconds,
            ttl=opts.ttl_days,
        )
    )

    try:
        report = store.load(conn, company)
    except sqlite3.Error:
        report = None
    if report is None:
        parts.append("<p class=muted>Ресёрч по этой компании ещё не запускали.</p>")
        return "".join(parts)

    parts.append(
        (
            "<p class=muted>Состояние: {status} · запросов {queries} · страниц "
            "{pages} · обновлено {finished}{inn}</p>"
        ).format(
            status=esc(report.status),
            queries=report.queries,
            pages=report.pages,
            finished=esc(report.finished_at[:16].replace("T", " ") or "—"),
            inn=" · ИНН {}".format(esc(report.inn)) if report.inn else "",
        )
    )
    if report.blocked:
        parts.append(
            "<p class=muted>Источники пропущены (капча или недоступны): {}</p>".format(
                esc(", ".join(report.blocked))
            )
        )
    if not report.findings:
        parts.append(
            "<div class=warn>Недостаточно данных: ни одна тема не подтвердилась "
            "источником. Это не справка о чистоте — это отсутствие находок.</div>"
        )
        return "".join(parts)

    rows = []
    for item in report.findings:
        rows.append(
            [
                esc(item.title),
                esc(item.quote or "—"),
                '<a href="{url}" rel="noreferrer">{domain}</a>'.format(
                    url=esc(item.url), domain=esc(item.domain)
                ),
                "{:.1f}".format(item.trust),
                esc(item.observed_at[:10]),
            ]
        )
    parts.append(table(["Тема", "Цитата", "Источник", "Доверие", "Дата"], rows))
    if not deepresearch.options().in_score:
        parts.append(
            "<p class=muted>Находки показаны справочно и в оценку работодателя не "
            "входят. Включается настройкой DEEP_IN_SCORE.</p>"
        )
    return "".join(parts)


__all__ = ("companies_known", "handle", "render_research", "start")
