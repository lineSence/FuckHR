"""Галочки «какие агрегаторы входят в сбор» и метрика их пользы.

Место на главной странице выбрано сознательно: набор площадок — часть решения
«что сейчас собираем», как лимит вакансий, а не настройка на месяцы. Рядом
таблица, из-за которой всё это и затевалось: сколько вакансий площадка дала
**только** она. Источник без уникальных вакансий — это чужая капча в обмен на
дубли, и выключить его должно быть так же легко, как включить.
"""

from __future__ import annotations

import sqlite3
from typing import Mapping, Sequence

import source_store
import sources
from ui_core import esc, table


def save(form: Mapping[str, Sequence[str]]) -> list[str]:
    """Отмеченные галочки → настройка SOURCES. Ни одной — остаётся hh.ru."""
    import settings

    chosen = [
        site.code for site in sources.SITES if (form.get("source_" + site.code) or [""])[0]
    ]
    value = ",".join(chosen) if chosen else sources.CODE_HH
    return settings.save({"SOURCE_SITES": value})


def render_sources(conn: sqlite3.Connection, note: str = "") -> str:
    """Форма выбора площадок и что каждая принесла."""
    chosen = set(sources.selected())
    counts = source_store.counts(conn)
    unique = source_store.unique_counts(conn)

    boxes = []
    for site in sources.SITES:
        ok, why = sources.ready(site)
        mark = "" if ok else ' <span class=pill>{}</span>'.format(esc(why))
        boxes.append(
            '<label><input type=checkbox name="source_{code}" value="1"{on}{off}> '
            "{label}</label>{mark} <span class=muted>{note}</span><br>".format(
                code=esc(site.code),
                on=" checked" if site.code in chosen else "",
                off="" if ok else " disabled",
                label=esc(site.label),
                mark=mark,
                note=esc(site.note),
            )
        )

    rows = [
        [
            esc(site.label),
            "в сборе" if site.code in chosen else "выключена",
            str(counts.get(site.source, 0)),
            str(unique.get(site.source, 0)),
        ]
        for site in sources.SITES
    ]

    return "".join(
        [
            "<h2>Площадки в сборе</h2>",
            note,
            '<form method=post action="/sources">',
            "".join(boxes),
            "<button>Сохранить выбор</button></form>",
            table(["Площадка", "Состояние", "Вакансий видно", "Только здесь"], rows),
            "<p class=muted>«Только здесь» — вакансии, которых не нашлось ни на "
            "одной другой площадке. Если за месяц эта цифра осталась нулём, "
            "площадка приносит дубли, а платим мы её паузами и блокировками. "
            "Дубли этапы модели проходят один раз: ключ вакансии не зависит от "
            "площадки (<code>docs/sources.md</code>).</p>",
        ]
    )


__all__ = ("render_sources", "save")
