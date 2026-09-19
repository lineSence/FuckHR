"""Страница «Очистка»: единственное место, где накопленные данные уничтожают.

Отделена от страницы компаний по [CORE-024] (размер файла), но и по смыслу:
здесь важна не выдача данных, а цена их потери.
"""

from __future__ import annotations

import sqlite3

import maintenance
from ui_core import esc

CONFIRM_WORD = "УДАЛИТЬ"


def render_cleanup(
    conn: sqlite3.Connection,
    removed: dict[str, int] | None = None,
    problems: tuple[str, ...] = (),
) -> str:
    """Форма очистки. Каждая цель подписана последствиями и числом строк."""
    counts = maintenance.counts(conn)
    blocks = []
    for code, label, warning, danger in maintenance.describe():
        blocks.append(
            (
                '<div class="field {cls}"><label>'
                '<input type=checkbox name=target value="{code}"> {label} '
                "<span class=pill>{count}</span></label>"
                '<div class=hint>{warning}</div></div>'
            ).format(
                cls="danger" if danger else "",
                code=esc(code),
                label=esc(label),
                count=esc(counts.get(code, 0)),
                warning=esc(warning),
            )
        )

    notice = ""
    if problems:
        notice += "".join(
            "<div class=warn>{}</div>".format(esc(text)) for text in problems
        )
    if removed:
        lines = "; ".join(
            "{}: {}".format(maintenance.target_label(code), count)
            for code, count in removed.items()
        )
        notice += "<div class=ok>Удалено — {}</div>".format(esc(lines))

    return (
        "{notice}"
        "<p>Отметь, что удалить. Стираются строки, сама база и настройки остаются на "
        "месте. Профиль и .env не трогаются никогда.</p>"
        '<form method=post action="/cleanup">{blocks}'
        '<div class=field><label>Подтверждение</label>'
        '<input type=text name=confirm placeholder="{word}">'
        "<div class=hint>Для контактов и истории публикаций нужно ввести {word}: "
        "эти данные повторным сбором не восстанавливаются.</div></div>"
        '<div class=field><label><input type=checkbox name=vacuum value="1"> '
        "Сжать файл базы после очистки</label>"
        "<div class=hint>Медленно на большой базе, но освобождает место на диске.</div></div>"
        "<button>Удалить выбранное</button></form>"
    ).format(notice=notice, blocks="".join(blocks), word=CONFIRM_WORD)


def apply_cleanup(
    conn: sqlite3.Connection, form: dict[str, list[str]]
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Разбирает форму и удаляет. Возвращает (что удалено, жалобы).

    Подтверждение требуется только там, где потеря необратима. Спрашивать его на
    каждый кэш — верный способ научить владельца подтверждать не глядя.
    """
    targets = [value for value in (form.get("target") or []) if value]
    if not targets:
        return {}, ("Ничего не выбрано — удалять нечего.",)

    unknown = [code for code in targets if code not in maintenance.TARGET_CODES]
    if unknown:
        return {}, ("Неизвестная цель: {}".format(", ".join(unknown)),)

    risky = [code for code in targets if code in maintenance.DANGEROUS]
    confirm = (form.get("confirm") or [""])[0].strip().upper()
    if risky and confirm != CONFIRM_WORD:
        labels = ", ".join(maintenance.target_label(code) for code in risky)
        return {}, (
            "Не удалено: {labels} — слишком ценные данные. Введи {word} в поле "
            "подтверждения.".format(labels=labels, word=CONFIRM_WORD),
        )

    removed = maintenance.wipe(conn, tuple(targets))
    if (form.get("vacuum") or [""])[0] == "1":
        maintenance.vacuum(conn)
    return removed, ()


__all__ = ("CONFIRM_WORD", "apply_cleanup", "render_cleanup")
