"""Страница «Статистика»: воронка, динамика, деньги, работодатели, брехня.

Отдельный раздел, потому что вопросы здесь другие, чем на остальных страницах:
не «покажи эту вакансию», а «прогон отработал нормально?», «рынок по моему
профилю такой, как я думаю?» и «на что уходят вызовы модели?». Всё считается
по своей базе, сеть и модель не трогаются.

Счёт живёт в `stats.py`, графики — в `ui_chart.py`, здесь только сборка. Так
файл остаётся в пределах лимита [CORE-024], а запросы проверяются тестом.

Честность цифр [CORE-019]: воронка показывает, что уже случилось, и не
измеряет, сколько «потеряно» — отсев на этапе может быть правильным.
"""

from __future__ import annotations

import sqlite3

import company_score_rules
import injection_rules
import profiles
import settings
import stats
from ui_chart import BRAND, SECOND, bars, columns, legend, spark
from ui_core import details, esc, table

PERIODS = ((7, "неделя"), (30, "месяц"), (90, "три месяца"), (3650, "всё время"))
DEFAULT_DAYS = 30
AI_RU = {"generated": "похоже на нейросеть", "human": "писал человек"}


def pick_days(value: object) -> int:
    allowed = {days for days, _ in PERIODS}
    days = settings.as_int(str(value or ""), DEFAULT_DAYS)
    return days if days in allowed else DEFAULT_DAYS


def pick_profile(conn: sqlite3.Connection, value: object) -> str:
    """Чужой профиль молча заменяется на «все»: из адреса приходит что угодно."""
    known = {name for name, _ in stats.profiles_seen(conn)}
    name = str(value or "").strip()
    return name if name in known else ""


def switcher(conn: sqlite3.Connection, days: int, profile: str) -> str:
    """Два разреза страницы: профиль и окно."""
    seen = stats.profiles_seen(conn)
    links = []
    for value, title in PERIODS:
        cls = " class=active" if value == days else ""
        links.append(
            '<a href="/stats?days={}&profile={}"{}>{}</a>'.format(
                value, esc(profile), cls, esc(title)
            )
        )
    line = "<p>Период: {}</p>".format(" · ".join(links))
    if seen:
        items = [("", "все профили")] + [(name, "{} ({})".format(name, n)) for name, n in seen]
        picks = []
        for value, title in items:
            cls = " class=active" if value == profile else ""
            picks.append(
                '<a href="/stats?days={}&profile={}"{}>{}</a>'.format(
                    days, esc(value), cls, esc(title)
                )
            )
        line += "<p>Профиль: {}</p>".format(" · ".join(picks))
    return line


def render_funnel(rows: list[tuple[str, int]]) -> str:
    seen = rows[0][1] if rows else 0
    body = [bars(rows)]
    if seen:
        cells = [
            [esc(name), str(count), "{:.0f}%".format(100 * count / seen)]
            for name, count in rows
        ]
        body.append(table(["Шаг", "Штук", "От встреченных"], cells))
    body.append(
        "<p class=muted>Это статистика того, что уже случилось, а не замер "
        "качества: отсев на шаге чаще всего правильный. Пустой шаг означает, "
        "что этап выключен, а не что он сломался.</p>"
    )
    return "".join(body)


def render_daily(rows: list[tuple[str, int, int]]) -> str:
    if not rows:
        return "<p class=muted>Данных пока нет.</p>"
    total = sum(r[1] for r in rows)
    fit = sum(r[2] for r in rows)
    return "".join([
        spark(rows),
        legend([("встречено", SECOND), ("выше порога", BRAND)]),
        "<p class=muted>Всего за период {} вакансий, из них выше порога {}. "
        "Резкий провал обычно означает капчу или смену вёрстки площадки, а не "
        "затишье на рынке.</p>".format(total, fit),
    ])


def render_salaries(data: dict) -> str:
    total, shown = data["total"], data["shown"]
    if not total:
        return "<p class=muted>Данных пока нет.</p>"
    hidden = total - shown
    body = [columns(stats.histogram(data["points"]))]
    body.append(
        "<p>Медиана вилок: <b>{median}</b> ₽. Вилку показали у {shown} из "
        "{total} ({share:.0f}%), молчат {hidden}.</p>".format(
            median="{:,.0f}".format(data["median"]).replace(",", " "),
            shown=shown, total=total, hidden=hidden,
            share=100 * shown / total,
        )
    )
    body.append(
        "<p class=muted>Точка вилки — её середина. До вычета и на руки не "
        "приводятся друг к другу: коэффициент пришлось бы выдумать. «До "
        "вычета» отмечено у {} вакансий с вилкой. Отсутствие вилки — сама по "
        "себе характеристика работодателя.</p>".format(data["gross"])
    )
    return "".join(body)


def render_employers(top: list[tuple[str, int]], levels: list[tuple[str, int]]) -> str:
    body = ["<p class=muted>Кто чаще всех висит в выдаче:</p>", bars(top)]
    if levels:
        # Порядок светофора, а не по количеству: «зелёный, жёлтый, красный»
        # читается с одного взгляда, отсортированный по числу — нет.
        order = list(company_score_rules.LEVEL_ORDER) + [company_score_rules.LEVEL_UNKNOWN]
        named = [
            (company_score_rules.LEVEL_RU.get(level, level), count)
            for level, count in sorted(levels, key=lambda pair: (
                order.index(pair[0]) if pair[0] in order else len(order)))
        ]
        body.append("<p class=muted>Светофор по всем работодателям в базе:</p>")
        body.append(bars(named))
    return "".join(body)


def render_quality(data: dict) -> str:
    body = []
    if data["reports"]:
        body.append(
            "<p>Детектор разобрал {reports} описаний, хотя бы один флаг нашёлся "
            "у {flagged} ({share:.0f}%).</p>".format(
                reports=data["reports"], flagged=data["flagged"],
                share=100 * data["flagged"] / data["reports"],
            )
        )
        body.append(bars(data["top"]))
    else:
        body.append("<p class=muted>Детектор на этом окне ничего не разбирал.</p>")
    ai = [(AI_RU.get(name, name), count) for name, count in data["ai"]]
    body.append("<p class=muted>Кто писал описание, по мнению этапа ai_text:</p>")
    body.append(bars(ai))
    if data["injections"]:
        found = [
            (injection_rules.LEVEL_RU.get(level, level), count)
            for level, count in data["injections"]
        ]
        body.append("<p class=muted>Найденные промпт-инъекции по уровням:</p>")
        body.append(bars(found))
    return "".join(body)


def _times(label: str) -> str:
    """«2» → «2 раза»: без слова столбики читаются как годы или баллы."""
    if label.endswith("+"):
        return "{} раз и больше".format(label[:-1])
    return "{} {}".format(label, "раз" if label in ("1", "5", "6") else "раза")


def render_lifetime(data: dict) -> str:
    if not data["tracked"]:
        return "<p class=muted>Данных пока нет.</p>"
    return "".join([
        "<p>Медиана «сколько висит»: <b>{days}</b> дней. Перевывешено {many} "
        "и более раз: {count} вакансий.</p>".format(
            days=data["median_days"], many=stats.MANY_REPUBLISH, count=data["republished"]
        ),
        bars([(_times(label), count) for label, count in data["posts"]]),
        "<p class=muted>Считается по слепкам выдачи: разные даты публикации "
        "или разные идентификаторы у одной и той же вакансии. Долгая жизнь "
        "объявления — это либо текучка, либо вакансия-витрина.</p>",
    ])


def render_costs(model: list[tuple[str, int]], sources: list[tuple[str, int]], review: dict) -> str:
    body = ["<p class=muted>Ответы модели в кеше по этапам:</p>", bars(model)]
    body.append(
        "<p class=muted>Это не полный учёт вызовов: в кеш попадает не всё, а "
        "повтор одного и того же текста считается один раз. Настоящий счётчик "
        "вызовов — отдельная задача, и выдавать это за него нечестно.</p>"
    )
    if sources:
        body.append("<p class=muted>Откуда приходят вакансии:</p>")
        body.append(bars(sources))
    body.append(
        "<p>Отзывов собрано {items} по {companies} работодателям, помечено "
        "накруткой {marked}.</p>".format(**review)
    )
    return "".join(body)


def render_stats(conn: sqlite3.Connection, params: dict | None = None) -> str:
    params = params or {}
    days = pick_days(params.get("days"))
    profile = pick_profile(conn, params.get("profile"))
    threshold = profiles.dossier_threshold()
    parts = [
        switcher(conn, days, profile),
        "<h2>Воронка</h2>",
        render_funnel(stats.funnel(conn, profile, days, threshold)),
        "<h2>По дням</h2>",
        render_daily(stats.daily(conn, profile, days, threshold)),
        "<h2>Деньги</h2>",
        render_salaries(stats.salaries(conn, profile, days)),
        "<h2>Работодатели</h2>",
        render_employers(
            stats.employers(conn, profile, days), stats.company_levels(conn)
        ),
        "<h2>Качество текста</h2>",
        render_quality(stats.text_quality(conn, profile, days)),
        "<h2>Жизнь вакансии</h2>",
        render_lifetime(stats.lifetime(conn, profile, days)),
        "<h2>Расход и источники</h2>",
        render_costs(
            stats.model_usage(conn, days), stats.sources(conn, days), stats.reviews(conn)
        ),
        details(
            "Как это считается",
            "коротко",
            "<p>Всё берётся из своей базы одним запросом на блок: сеть и модель "
            "не трогаются, порог подходящих — порог профиля ({:.0f}), окно "
            "считается от сегодняшнего дня. Разрез по профилю берёт скор "
            "именно этого профиля, а не лучший по всем.</p>".format(threshold),
        ),
    ]
    return "".join(parts)


__all__ = (
    "PERIODS",
    "pick_days",
    "pick_profile",
    "render_stats",
    "switcher",
)
