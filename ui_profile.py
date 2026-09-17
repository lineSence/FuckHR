"""Страница профиля: не редактор YAML, а фильтр вакансий.

Раньше форма повторяла структуру файла: запросы строкой «python | 113 | 7 | 3»,
регионы числами, пять весов с требованием суммы 100. Такую форму невозможно
заполнить, не зная формата файла и справочника hh.ru.

Здесь три решения, которые делают страницу фильтром.

1. Запрос — строка из четырёх полей, где три из них — выпадающие списки.
   Город выбирается именем, срок — словами («за неделю»), глубина — сразу с
   оценкой в вакансиях. Пустой слот внизу заменяет кнопку «добавить», которую
   без JavaScript всё равно не сделать.
2. Вместо весов — важность от «не учитывать» до «решает всё». Сумму 100
   считает код (profile_form.normalize_weights), а посчитанные веса показаны рядом
   справочно — видно, что именно увидит скоринг.
3. Порог показан вместе с предпросмотром по уже собранной базе: сколько
   вакансий прошло бы его сейчас. Цифра 45 сама по себе ни о чём не говорит,
   а «прошло 12 из 159» — говорит.

Шаблоны — только str.format с заранее вычисленными частями, без вложенных f-строк
и без смешения кавычек: одинарные снаружи, двойные в HTML. Каждое отступление
от этого правила уже стоило нам сломанного запуска.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

import profile_form
from ui_core import area_field, checkbox_field, esc, hint_block, open_db

# Заготовки текстов запросов: пустая страница не должна выглядеть задачей.
PLACEHOLDERS = (
    "python разработчик",
    "backend инженер",
    "тимлид разработки",
)

LIST_HINTS = {
    "skills": "Главный источник скора. Через запятую или по строкам.",
    "nice_to_have": "Добавляют баллы, но не обязательны.",
    "stop_words": "Вакансия с таким словом отбрасывается до скоринга.",
}

LIST_PLACEHOLDERS = {
    "skills": "Python, PostgreSQL, Docker",
    "nice_to_have": "Kubernetes, Kafka",
    "stop_words": "1С, стажировка, продажи",
}


def _select(
    name: str,
    value: Any,
    choices: Sequence[tuple[Any, str]],
    empty: str = "",
    width: str = "",
) -> str:
    """Выпадающий список. Пустой первый пункт — «не задано, решает сбор»."""
    current = "" if value is None else str(value)
    options = []
    if empty:
        options.append(
            '<option value=""{selected}>{label}</option>'.format(
                selected=" selected" if current == "" else "", label=esc(empty)
            )
        )
    for code, label in choices:
        options.append(
            '<option value="{code}"{selected}>{label}</option>'.format(
                code=esc(code),
                selected=" selected" if current == str(code) else "",
                label=esc(label),
            )
        )
    style = ' style="{}"'.format(esc(width)) if width else ""
    return '<select name="{name}"{style}>{options}</select>'.format(
        name=esc(name), style=style, options="".join(options)
    )


def _query_row(number: int, slot: Mapping[str, Any]) -> str:
    """Один запрос: текст и три списка в одной строке таблицы."""
    placeholder = PLACEHOLDERS[(number - 1) % len(PLACEHOLDERS)]
    text = (
        '<input type=text name="q{number}_text" value="{value}" '
        'placeholder="{placeholder}">'
    ).format(
        number=number,
        value=esc(slot.get("text", "")),
        placeholder=esc(placeholder),
    )
    area = _select(
        "q{}_area".format(number),
        slot.get("area"),
        profile_form.AREA_CHOICES,
        "как в географии ниже",
    )
    period = _select(
        "q{}_period".format(number),
        slot.get("period"),
        profile_form.PERIOD_CHOICES,
        "без ограничения",
    )
    pages = _select(
        "q{}_pages".format(number),
        slot.get("max_pages"),
        profile_form.PAGE_CHOICES,
        "по умолчанию",
    )
    return "<tr><td>{text}</td><td>{area}</td><td>{period}</td><td>{pages}</td></tr>".format(
        text=text, area=area, period=period, pages=pages
    )


def queries_block(slots: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(_query_row(number, slot) for number, slot in enumerate(slots, 1))
    return (
        "<table><tr><th>Что искать на hh.ru</th><th>Где</th>"
        "<th>За какой срок</th><th>Сколько смотреть</th></tr>{rows}</table>"
        "<div class=hint>Пустые строки игнорируются. Чтобы удалить запрос, "
        "очисти его текст и сохрани. Больше страниц — больше времени сбора: "
        "между запросами есть паузы, чтобы hh.ru не считал нас ботом.</div>"
    ).format(rows=rows)


def importance_block(importance: Mapping[str, int]) -> str:
    """Важность словами плюс посчитанные веса рядом."""
    weights = profile_form.normalize_weights(importance)
    rows = []
    for key, label in profile_form.WEIGHTS:
        select = _select(
            "importance_" + key,
            importance.get(key, 0),
            profile_form.IMPORTANCE_LEVELS,
        )
        rows.append(
            "<tr><td><b>{label}</b><br><span class=muted>{hint}</span></td>"
            "<td>{select}</td><td class=score>{weight}</td></tr>".format(
                label=esc(label),
                hint=esc(profile_form.WEIGHT_HINTS.get(key, "")),
                select=select,
                weight=weights.get(key, 0),
            )
        )
    return (
        "<table><tr><th>Критерий</th><th>Насколько важен</th>"
        "<th>Баллов из 100</th></tr>{rows}</table>"
        "<div class=hint>Правый столбец — текущее распределение; оно пересчитается "
        "после сохранения. Сумму не надо сводить руками.</div>"
    ).format(rows=rows_join(rows))


def rows_join(rows: Sequence[str]) -> str:
    return "".join(rows)


def areas_block(selected: Sequence[int], extra: str) -> str:
    """Города галочками; редкие регионы — числами в отдельном поле."""
    chosen = {int(code) for code in selected if str(code).lstrip("-").isdigit()}
    checks = []
    for code, label in profile_form.AREA_CHOICES:
        checks.append(
            (
                '<label><input type=checkbox name=geo_area value="{code}"{checked}> '
                "{label}</label>"
            ).format(
                code=code,
                checked=" checked" if code in chosen else "",
                label=esc(label),
            )
        )
    return (
        "<div class=field><label>Где искать</label>"
        "<div class=checks>{checks}</div>"
        '<div class=field><label>Другие регионы</label>'
        '<input type=text name="geo_areas_extra" value="{extra}" '
        'placeholder="например: 53, 76">{hint}</div></div>'
    ).format(
        checks=rows_join(checks),
        extra=esc(extra),
        hint=hint_block(
            "Числа через запятую для городов, которых нет в списке. "
            "ID берутся из адреса поиска hh.ru (параметр area)."
        ),
    )


def experience_block(selected: Sequence[str]) -> str:
    checks = []
    for key, label in profile_form.EXPERIENCE:
        checks.append(
            (
                '<label><input type=checkbox name=experience_ok value="{key}"{checked}> '
                "{label}</label>"
            ).format(
                key=esc(key),
                checked=" checked" if key in selected else "",
                label=esc(label),
            )
        )
    return (
        "<div class=field><label>Подходящий опыт</label>"
        "<div class=checks>{checks}</div>{hint}</div>"
    ).format(
        checks=rows_join(checks),
        hint=hint_block("Снять все галочки — значит не фильтровать по опыту вовсе."),
    )


def salary_block(values: Mapping[str, Any]) -> str:
    amount = (
        '<div class=field><label>Меньше этой суммы на руки не предлагать</label>'
        '<input type=number step=1000 name="salary_min_net" value="{value}" '
        'placeholder="250000">{hint}</div>'
    ).format(
        value=esc(values.get("salary_min_net", "")),
        hint=hint_block("Гросс из вакансий пересчитывается на руки с вычетом 13%."),
    )
    currency = (
        "<div class=field><label>Валюта</label>{select}</div>"
    ).format(
        select=_select(
            "salary_currency",
            values.get("salary_currency", "RUR"),
            profile_form.CURRENCY_CHOICES,
        )
    )
    allow = checkbox_field(
        "salary_allow_missing",
        "Смотреть и вакансии без указанной зарплаты",
        bool(values.get("salary_allow_missing")),
        "Зарплату не пишут в большей части вакансий. Снять галочку — "
        "потерять большую часть рынка.",
    )
    return "<div class=cols>{amount}{currency}</div>{allow}".format(
        amount=amount, currency=currency, allow=allow
    )


def score_preview(threshold: Any, conn: sqlite3.Connection | None = None) -> str:
    """Сколько уже собранных вакансий прошло бы порог.

    Без базы или до первого сбора блок просто молчит: страница настроек не
    должна падать из-за отсутствующей таблицы.
    """
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        return ""
    owned = conn is None
    try:
        if conn is None:
            conn = open_db()
        row = conn.execute(
            "SELECT count(*) AS total, "
            "sum(CASE WHEN score >= ? THEN 1 ELSE 0 END) AS passed "
            "FROM vacancies WHERE active = 1",
            (value,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        if owned and conn is not None:
            conn.close()
    total = int((row[0] if row else 0) or 0)
    passed = int((row[1] if row else 0) or 0)
    if not total:
        return (
            "<div class=hint>База пуста: после первого сбора здесь появится, "
            "сколько вакансий проходит порог.</div>"
        )
    share = 100.0 * passed / total
    note = ""
    if passed == 0:
        note = " При таком пороге письма не будут готовиться вообще."
    elif share > 60:
        note = " Порог мягкий: в отбор попадает большая часть базы."
    return (
        '<div class="hint">С текущим порогом {value:g} прошло бы {passed} из {total} '
        "собранных вакансий ({share:.0f}%).{note}</div>"
    ).format(value=value, passed=passed, total=total, share=share, note=esc(note))


def summary_line(values: Mapping[str, Any]) -> str:
    """Одна строка человеческим языком: что именно сейчас искает программа."""
    slots = [slot for slot in values.get("query_slots") or [] if slot.get("text")]
    if not slots:
        return (
            "<div class=warn>Запросы не заданы — искать нечего. Заполни хотя бы одну "
            "строку ниже.</div>"
        )
    names = ", ".join(str(slot.get("text")) for slot in slots[:4])
    areas = values.get("geo_area_codes") or []
    where = ", ".join(profile_form.area_label(code) for code in areas[:4]) or "везде"
    salary = values.get("salary_min_net")
    money = "любая зарплата"
    try:
        if float(salary) > 0:
            money = "от {:,.0f} на руки".format(float(salary)).replace(",", "\u00a0")
    except (TypeError, ValueError):
        pass
    return (
        '<div class="ok">Ищем: <b>{names}</b> · {where} · {money} · порог {score}</div>'
    ).format(
        names=esc(names),
        where=esc(where),
        money=esc(money),
        score=esc(values.get("min_score", "не задан")),
    )


def render_profile(
    profile_path: str,
    saved: int | None = None,
    problems: Sequence[str] = (),
) -> str:
    """Форма-фильтр. Неизвестные ключи файла сохраняются при записи."""
    data = profile_form.load(profile_path)
    values = profile_form.form_values(data)

    parts: list[str] = []
    if saved is not None:
        parts.append(
            "<div class=ok>Сохранено. Фактов о себе: {}</div>".format(saved)
        )
    for problem in problems:
        parts.append("<div class=warn>{}</div>".format(esc(problem)))

    parts.append(summary_line(values))

    if not values["facts"] and saved is None:
        parts.append(
            "<div class=warn>Не заполнен блок «Факты о себе». Без него письма "
            "собираются без конкретики о тебе.</div>"
        )

    parts.append('<form method=post action="/profile">')

    parts.append("<h2>1. Кого и где искать</h2>")
    parts.append(queries_block(values["query_slots"]))

    parts.append("<h2>2. География и формат</h2>")
    parts.append(areas_block(values["geo_area_codes"], values["geo_areas_extra"]))
    parts.append(
        checkbox_field(
            "geo_remote_ok",
            "Удалёнка подходит",
            values["geo_remote_ok"],
            "Влияет и на фильтр, и на баллы за формат работы.",
        )
    )
    parts.append(experience_block(values["experience_ok"]))

    parts.append("<h2>3. Деньги</h2>")
    parts.append(salary_block(values))

    parts.append("<h2>4. Навыки и стоп-слова</h2>")
    for key, label in profile_form.LIST_FIELDS:
        parts.append(
            area_field(
                key,
                label,
                values[key],
                "{} Пример: {}".format(
                    LIST_HINTS.get(key, ""), LIST_PLACEHOLDERS.get(key, "")
                ),
            )
        )

    parts.append("<h2>5. Что важнее при отборе</h2>")
    parts.append(importance_block(values["importance"]))

    parts.append("<h2>6. Порог отбора</h2>")
    parts.append(
        (
            '<div class=field><label>Письма готовить от скора</label>'
            '<input type=number step=5 min=0 max=100 name="min_score" value="{value}">'
            "{hint}{preview}</div>"
        ).format(
            value=esc(values["min_score"]),
            hint=hint_block(
                "Вакансии ниже порога остаются в базе, но не идут в письма и Telegram."
            ),
            preview=score_preview(values["min_score"]),
        )
    )

    parts.append("<h2>7. Факты о себе</h2>")
    parts.append(
        area_field(
            "facts",
            "По одному на строку, с цифрами",
            values["facts"],
            "Только эти строки попадают в письмо. Пример: сократил время сборки "
            "с 40 до 6 минут на проекте из 200 тысяч строк.",
        )
    )

    parts.append("<p><button>Сохранить фильтр</button></p></form>")
    parts.append(
        "<p class=muted>Всё сохраняется в {path} и применяется со следующего запуска "
        "сбора. Файл остаётся читаемым: ручные правки и дополнительные ключи "
        "не теряются.</p>".format(path=esc(profile_path))
    )
    return "".join(parts)


def save_profile(
    profile_path: str, form: dict[str, list[str]]
) -> tuple[int, list[str]]:
    """Принимает форму целиком и возвращает (сколько фактов, замечания)."""
    path = Path(profile_path)
    data = profile_form.load(path)
    updated, problems = profile_form.apply_form(data, form)
    profile_form.save(path, updated)
    return len(updated.get("facts") or []), problems


__all__ = (
    "LIST_HINTS",
    "areas_block",
    "experience_block",
    "importance_block",
    "queries_block",
    "render_profile",
    "salary_block",
    "save_profile",
    "score_preview",
    "summary_line",
)
