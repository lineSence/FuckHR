"""Страница «Компании»: список досье с фильтрами и видами.

Сама карточка работодателя живёт в `ui_company.py` (а две её таблицы — в
`ui_company_lists.py`): вместе они давно не влезали в 25 КБ [CORE-024]. Здесь
остался список — он отвечает на вопрос «кого смотреть», карточка на вопрос
«что с этой». Функции карточки реэкспортируются, чтобы старые вызовы
`ui_companies.render_company` продолжали работать.

Шаблоны — только str.format с заранее вычисленными переменными, без вложенных
ф-строк: однажды это уже стоило SyntaxError.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse

import company_score_rules
import filters
import ui_filters
import company_score_store
import contacts
import dossier
import profiles
import reviewlegit_store
from ui_cleanup import CONFIRM_WORD, apply_cleanup, render_cleanup
# Реэкспорт карточки: имена нужны интерфейсу и тестам по старым путям.
from ui_company import (  # noqa: F401
    RISK_CLASS,
    SCORE_CLASS,
    render_areas,
    render_company,
    render_contacts_for,
    render_fake,
    render_market,
    render_score,
    render_signals,
    render_vacancies_for,
)
from ui_core import esc, live_search, sort_head, table


def _flags(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(item) for item in value]


# Порядок «тяжёлое сверху»: смотрят обычно на худших работодателей.
RISK_ORDER = {
    dossier.RISK_RED: 3,
    dossier.RISK_YELLOW: 2,
    dossier.RISK_GREEN: 1,
    dossier.RISK_UNKNOWN: 0,
}

# Сортировки компаний живут в filters.COMPANY_SORTS: как и у вакансий, это
# теперь куски ORDER BY, а не ключи для sorted().
COMPANY_COLUMNS = (
    ("name", "Компания"),
    ("level", "Оценка"),
    ("", "Отзывы"),
    ("reviews", "Отзывов"),
    ("rating", "Оценка отзывов"),
    ("openings", "Вакансий"),
    ("", "Контакт"),
    ("", "Закономерности"),
    ("updated", "Обновлено"),
)


def filtered_companies(
    conn: sqlite3.Connection, params: dict, limit: int = 200
) -> tuple[list[sqlite3.Row], int, list[tuple[str, str]]]:
    """(строки досье с оценкой и счётчиками, сколько всего подошло, фильтры).

    Одним запросом, а не перебором в Python: считать «сколько у компании
    вакансий» в цикле по двумстам досье — это двести запросов на открытие
    страницы, и растёт это вместе с базой.
    """
    dossier.ensure_schema(conn)
    company_score_store.ensure_schema(conn)
    contacts.ensure_schema(conn)
    filters.register(conn)  # поиск по-русски без учёта регистра
    where, args, active = filters.build_where(filters.COMPANY_FILTERS, params)
    order = filters.order_by(
        filters.COMPANY_SORTS, str(params.get("csort", "") or ""), "updated"
    )
    base = (
        "FROM company_dossier d"
        " LEFT JOIN company_score s ON s.company = d.company"
        " WHERE {}".format(where)
    )
    total = conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
    rows = conn.execute(
        "SELECT d.*, COALESCE(s.level, 'unknown') AS score_level,"
        " (SELECT COUNT(*) FROM vacancies v WHERE v.company = d.company) AS openings,"
        " (SELECT COUNT(*) FROM contacts c WHERE c.company = d.company"
        "  AND COALESCE(c.guessed, 0) = 0) AS direct_contacts "
        + base
        + " ORDER BY {} LIMIT ?".format(order),
        [*args, limit],
    ).fetchall()
    return rows, int(total or 0), active


def company_rows(
    conn: sqlite3.Connection, limit: int = 200, sort: str = "updated", params: dict | None = None
) -> list[list[str]]:
    """Готовые ячейки таблицы компаний."""
    query = dict(params or {})
    query.setdefault("csort", sort)
    rows, _total, _active = filtered_companies(conn, query, limit)
    out: list[list[str]] = []
    for row in rows:
        company = str(row["company"])
        level = str(row["score_level"])
        red = _flags(row["red_flags"])
        rating = "—"
        if row["avg_rating"] is not None:
            rating = "{:.1f}".format(float(row["avg_rating"]))
        out.append(
            [
                '<a href="/company?name={link}">{name}</a>'.format(
                    link=urllib.parse.quote(company), name=esc(company)
                ),
                '<span class="{cls}">{label}</span>'.format(
                    cls=SCORE_CLASS.get(level, "muted"),
                    label=esc(company_score_rules.LEVEL_RU.get(level, level)),
                ),
                '<span class="{cls}">{label}</span>'.format(
                    cls=RISK_CLASS.get(str(row["risk"]), "muted"),
                    label=esc(dossier.RISK_RU.get(str(row["risk"]), row["risk"])),
                ),
                esc(row["review_count"]),
                esc(rating),
                esc(row["openings"]),
                "✓" if int(row["direct_contacts"] or 0) else "",
                esc("; ".join(red[:3]) or "—"),
                esc(str(row["updated_at"])[:10]),
            ]
        )
    return out


def render_companies(
    conn: sqlite3.Connection, sort: str = "updated", params: dict | None = None
) -> str:
    """Список досье: те же быстрые виды и фильтры, что у вакансий."""
    query = ui_filters.apply_preset(filters.COMPANY_PRESETS, dict(params or {}))
    query.setdefault("csort", sort)
    rows, found, active = filtered_companies(conn, query)
    total, red, empty = dossier.coverage(conn)

    head = (
        ui_filters.presets_line(filters.COMPANY_PRESETS, query, "/companies")
        + live_search(
            "/companies",
            "cq",
            "clist",
            value=str(query.get("cq", "") or ""),
            placeholder="название компании",
            name="cq",
            hidden={
                key: value for key, value in query.items() if key != "cq" and value
            },
        )
        + ui_filters.chips(active, query, "/companies")
        + ui_filters.form(filters.COMPANY_FILTERS, query, "/companies", len(active))
        + ui_filters.sort_line(
            filters.COMPANY_SORTS, query, "/companies", param="csort", default="updated"
        )
    )
    if not rows and not active:
        return (
            head
            + "<div class=warn>Досье пока нет. Они собираются автоматически при "
            "сканировании — по тем компаниям, чьи вакансии прошли порог. Нужен "
            'настроенный поиск: смотри страницу «Поиск».</div>'
        )
    # Почему компаний меньше, чем вакансий: порогов два. В список вакансий
    # попадает всё, что прошло предфильтр, а досье собирается только по тем,
    # что прошли порог профиля — за досье платят запросы поиска и время.
    threshold = profiles.dossier_threshold()
    vacancies = int(
        conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] or 0
    )
    summary = (
        "<p class=muted>Досье: {total} · с красными флагами: {red} · без единого "
        "отзыва: {empty} · под фильтр подошло: {found}</p>"
        "<p class=muted>Порогов два: в списке вакансий — всё, что прошло "
        "предфильтр ({vacancies} шт.), досье — только компании вакансий от "
        '{threshold:.0f} баллов (<a href="/profile">порог профиля</a>, '
        '<a href="/vacancies?min_score={threshold:.0f}">эти вакансии</a>). '
        "За досье платят запросы поиска, поэтому оно не на всех.</p>"
    ).format(
        total=total,
        red=red,
        empty=empty,
        found=found,
        threshold=threshold,
        vacancies=vacancies,
    )
    health = reviewlegit_store.health_line(reviewlegit_store.health(conn))
    if health:
        # Отброшенное показывается числом: поломку разбора иначе видно только
        # по внезапно опустевшим досье.
        summary += (
            "<p class=muted>Сбор отзывов — {}. Выброшенное не отзывы: меню, "
            "реклама, ответы работодателя и отзывы клиентов о товаре.</p>"
        ).format(esc(health))
    if not rows:
        return head + summary + (
            "<div class=warn>Под фильтр не попало ни одно досье.</div>"
        )
    hint = (
        "<p class=muted>Красный статус ставится только по повторяющимся жалобам или "
        "тяжёлым признакам вроде задержки зарплаты. Один злой отзыв — ещё не "
        "закономерность.</p>"
    )
    base = "/companies?" + filters.query_string(query, drop="csort")
    return (
        head
        + summary
        + "<div id=clist>"
        + table(
            sort_head(COMPANY_COLUMNS, base, "csort", query.get("csort", "updated")),
            company_rows(conn, params=query),
            raw_head=True,
        )
        + "</div>"
        + hint
    )



__all__ = (
    "CONFIRM_WORD",
    "COMPANY_COLUMNS",
    "RISK_CLASS",
    "SCORE_CLASS",
    "apply_cleanup",
    "company_rows",
    "render_cleanup",
    "render_areas",
    "render_companies",
    "render_company",
    "render_contacts_for",
    "render_fake",
    "render_market",
    "render_score",
    "render_signals",
    "render_vacancies_for",
)
