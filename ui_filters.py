"""Рисование фильтров: быстрые виды, форма, чипы активного, ссылки сортировки.

Отделено от `filters.py` намеренно: там правила и SQL, здесь только HTML.
Одно меняется, когда появляется новый признак, другое — когда меняется вкус к
вёрстке, и смешивать эти два повода в одном файле дорого [CORE-024].

Почему форма свёрнута, а сверху стоят быстрые виды. Пятнадцать полей в
развёрнутом виде — это не «богатый фильтр», а стена, мимо которой человек
пролистывает к таблице. Виды отвечают на готовые вопросы одним кликом, а
форма нужна тем редким случаям, когда вопрос свой.

Чипы активного показываются всегда, когда фильтр задан. Без них короткий
список читается как «в базе пусто», и это самая частая причина недоверия к
собственному инструменту.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import filters
from ui_core import esc


def _url(action: str, params: Mapping[str, str], drop: str = "", **extra) -> str:
    merged = {key: value for key, value in params.items()}
    merged.update({key: str(value) for key, value in extra.items()})
    query = filters.query_string(merged, drop=drop)
    return action + ("?" + query if query else "")


def presets_line(
    presets: Sequence[filters.Preset],
    params: Mapping[str, str],
    action: str,
    default: str = "",
) -> str:
    """Быстрые виды ссылками. Активный выделен, у каждого — подпись зачем он.

    `default` — вид, который страница показывает при пустом адресе. Без него
    подсвечивался «Все», хотя список открывался «Подходящими», и выделение
    врало про то, что человек видит.
    """
    current = str(params.get("view", "") or "") or default
    out = []
    for item in presets:
        active = item.key == current
        label = esc(item.label)
        href = action + ("?view=" + item.key if item.key != default else "")
        out.append(
            '<a class="{cls}" href="{href}" title="{why}">{label}</a>'.format(
                cls="chip on" if active else "chip",
                href=esc(href),
                why=esc(item.why),
                label="<b>{}</b>".format(label) if active else label,
            )
        )
    return "<div class=chips>{}</div>".format("".join(out))


def chips(
    active: Sequence[tuple[str, str]], params: Mapping[str, str], action: str
) -> str:
    """Что сейчас отфильтровано. Крестик снимает один фильтр, не трогая остальные."""
    if not active:
        return ""
    out = []
    for key, text in active:
        out.append(
            '<span class="chip on">{text} <a href="{href}" title="снять">×</a></span>'.format(
                text=esc(text), href=esc(_url(action, params, drop=key))
            )
        )
    return "<div class=chips>{}<a class=chip href=\"{}\">сбросить всё</a></div>".format(
        "".join(out), esc(action)
    )


def form(
    definitions: Sequence[filters.Filter],
    params: Mapping[str, str],
    action: str,
    active_count: int = 0,
    hidden: Mapping[str, str] | None = None,
) -> str:
    """Свёрнутая форма со всеми полями. Раскрыта, если хоть один фильтр задан."""
    fields = []
    for item in definitions:
        value = str(params.get(item.key, "") or "")
        if item.kind == "choice":
            options = "".join(
                '<option value="{value}"{sel}>{label}</option>'.format(
                    value=esc(value_),
                    sel=" selected" if value_ == value else "",
                    label=esc(label),
                )
                for value_, label in item.options
            )
            control = '<select name="{key}">{options}</select>'.format(
                key=esc(item.key), options=options
            )
        else:
            control = (
                '<input type="{type}" name="{key}" value="{value}" style="width:{w}">'
            ).format(
                type="number" if item.kind == "number" else "text",
                key=esc(item.key),
                value=esc(value),
                w=esc(item.width),
            )
        fields.append(
            '<label class=filt>{label}{hint}<br>{control}</label>'.format(
                label=esc(item.label),
                hint=' <span class=muted title="{}">?</span>'.format(esc(item.hint))
                if item.hint
                else "",
                control=control,
            )
        )
    hidden_inputs = "".join(
        '<input type=hidden name="{}" value="{}">'.format(esc(key), esc(str(value)))
        for key, value in (hidden or {}).items()
        if str(value or "").strip()
    )
    return (
        '<details class=setgroup{open}><summary>Фильтры{count}</summary>'
        '<form method=get action="{action}">{hidden}<div class=filters>{fields}</div>'
        "<button>Применить</button> "
        '<a class=muted href="{action}">сбросить</a></form></details>'
    ).format(
        open=" open" if active_count else "",
        count=" <span class=pill>{}</span>".format(active_count) if active_count else "",
        action=esc(action),
        hidden=hidden_inputs,
        fields="".join(fields),
    )


def sort_line(
    sorts: Mapping[str, tuple[str, str]],
    params: Mapping[str, str],
    action: str,
    param: str = "sort",
    default: str = "score",
) -> str:
    """Сортировки строкой, а не только заголовками таблицы.

    Часть ключей нельзя повесить на колонку: «по зарплате» и «по отклонению от
    рынка» показываются не в каждой таблице, но сортировать по ним осмысленно.
    Второй стрелки (по возрастанию) нет по той же причине, что и раньше: у
    скора, денег и даты осмысленно только убывание [CORE-025].
    """
    current = str(params.get(param, "") or default)
    if current not in sorts:
        current = default
    out = []
    for key, (label, _sql) in sorts.items():
        if key == current:
            out.append("<b>{} ↓</b>".format(esc(label)))
        else:
            out.append(
                '<a href="{href}">{label}</a>'.format(
                    href=esc(_url(action, params, **{param: key})), label=esc(label)
                )
            )
    return "<p class=muted>Сортировка: {}</p>".format(" · ".join(out))


def apply_preset(
    presets: Sequence[filters.Preset], params: Mapping[str, str]
) -> dict[str, str]:
    """Параметры вида подставляются как умолчания: явное из адреса сильнее."""
    out = {key: str(value) for key, value in params.items() if str(value or "").strip()}
    preset = filters.preset_of(presets, str(params.get("view", "") or ""))
    if preset is None:
        return out
    for key, value in preset.params.items():
        out.setdefault(key, str(value))
    return out


__all__ = ("apply_preset", "chips", "form", "presets_line", "sort_line")
