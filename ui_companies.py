"""Страницы «Компании» и «Очистка».

Две разные задачи в одном файле по одной причине: и та, и другая работают с
данными о работодателях как с накопленным активом: одна показывает, что
накопилось, вторая — единственное место, где это можно уничтожить.

Карточка компании показывает два рода данных рядом: отзывы — это чужие слова,
а история публикаций (company_signals) — наши собственные наблюдения. Второе
проверяемо и потому весит больше, хоть и накапливается медленнее.

Шаблоны — только str.format с заранее вычисленными переменными, без вложенных
ф-строк: однажды это уже стоило SyntaxError.
"""

from __future__ import annotations

import json
import sqlite3

import company_signals
import dossier
import maintenance
from ui_core import esc, table

RISK_CLASS = {
    dossier.RISK_RED: "danger",
    dossier.RISK_YELLOW: "warn",
    dossier.RISK_GREEN: "ok",
    dossier.RISK_UNKNOWN: "muted",
}

POLARITY_RU = {
    "negative": "негатив",
    "positive": "позитив",
    "mixed": "смешанный",
    "unknown": "не определён",
}

CONFIRM_WORD = "УДАЛИТЬ"


def _flags(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(item) for item in value]


def _patterns(raw: object) -> list[dict]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [item for item in value if isinstance(item, dict)]


def company_rows(conn: sqlite3.Connection, limit: int = 200) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in dossier.list_dossiers(conn, limit):
        company = str(row["company"])
        red = _flags(row["red_flags"])
        rating = "—"
        if row["avg_rating"] is not None:
            rating = "{:.1f}".format(float(row["avg_rating"]))
        rows.append(
            [
                '<a href="/company?name={link}">{name}</a>'.format(
                    link=esc(company), name=esc(company)
                ),
                '<span class="{cls}">{label}</span>'.format(
                    cls=RISK_CLASS.get(str(row["risk"]), "muted"),
                    label=esc(dossier.RISK_RU.get(str(row["risk"]), row["risk"])),
                ),
                esc(row["review_count"]),
                esc(rating),
                esc("; ".join(red[:3]) or "—"),
                esc(str(row["updated_at"])[:10]),
            ]
        )
    return rows


def render_companies(conn: sqlite3.Connection) -> str:
    total, red, empty = dossier.coverage(conn)
    rows = company_rows(conn)
    if not rows:
        return (
            "<div class=warn>Досье пока нет. Они собираются автоматически при сканировании — "
            "по тем компаниям, чьи вакансии прошли порог. Нужен настроенный поиск: "
            "смотри страницу «Поиск».</div>"
        )
    summary = (
        "<p class=muted>Досье: {total} · с красными флагами: {red} · без единого отзыва: "
        "{empty}</p>"
    ).format(total=total, red=red, empty=empty)
    hint = (
        "<p class=muted>Красный статус ставится только по повторяющимся жалобам или "
        "тяжёлым признакам вроде задержки зарплаты. Один злой отзыв — ещё не "
        "закономерность.</p>"
    )
    return summary + table(
        ("Компания", "Работодатель", "Отзывов", "Оценка", "Закономерности", "Обновлено"),
        rows,
    ) + hint


def render_signals(conn: sqlite3.Connection, name: str) -> str:
    """Блок фактов по истории публикаций — единственное место в UI, где данные наши.

    При короткой истории блок не скрывается, а говорит, что данных мало:
    иначе отсутствие фактов читалось бы как их благополучное отсутствие [CORE-019].
    """
    signals = company_signals.collect(conn, name)
    items = "".join(
        "<li>{}</li>".format(esc(line)) for line in company_signals.facts(signals)
    )
    note = (
        "Считается по слепкам вакансий этого работодателя — это наши наблюдения, "
        "а не чьи-то слова. Разные написания названия сводятся в одну компанию."
    )
    return "<ul>{items}</ul><p class=muted>{note}</p>".format(
        items=items, note=esc(note)
    )


def render_company(conn: sqlite3.Connection, name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "<div class=warn>Компания не указана.</div>"
    row = dossier.load(conn, name)
    if row is None:
        return (
            '<div class=warn>Досье на «{}» ещё не собрано.</div>'
            '<p><a href="/companies">К списку компаний</a></p>'
        ).format(esc(name))

    rating = "—"
    if row["avg_rating"] is not None:
        rating = "{:.1f} из 5".format(float(row["avg_rating"]))
    head = (
        '<div class="{cls}">Работодатель: {risk} · отзывов: {count} · средняя оценка: '
        "{rating}</div>"
    ).format(
        cls=RISK_CLASS.get(str(row["risk"]), "muted"),
        risk=esc(dossier.RISK_RU.get(str(row["risk"]), row["risk"])),
        count=esc(row["review_count"]),
        rating=esc(rating),
    )

    summary = ""
    if row["summary"]:
        summary = (
            "<h2>Сводка</h2><pre>{text}</pre>"
            "<p class=muted>Собрала: {by}. Флаги и цифры считаются правилами, модель на них "
            "не влияет.</p>"
        ).format(text=esc(row["summary"]), by=esc(row["summary_by"] or "правила"))

    pattern_rows = []
    for item in _patterns(row["patterns"]):
        mark = "—" if item.get("polarity") == "red" else "+"
        quotes = item.get("quotes") or []
        pattern_rows.append(
            [
                esc(mark),
                esc(item.get("label") or item.get("code") or ""),
                esc(item.get("hits") or 0),
                "<br>".join(esc(quote) for quote in quotes[:2]) or "<span class=muted>—</span>",
            ]
        )
    patterns_block = "<p class=muted>Закономерностей не найдено.</p>"
    if pattern_rows:
        patterns_block = table(
            ("", "Закономерность", "Упоминаний", "Цитаты"), pattern_rows
        )

    review_rows = []
    for review in dossier.load_reviews(conn, name):
        review_rows.append(
            [
                esc(dossier.SITE_NAMES.get(str(review["site"]), review["site"])),
                '<a href="{url}" target=_blank rel=noreferrer>{title}</a>'.format(
                    url=esc(review["url"]), title=esc(review["title"] or review["url"])
                ),
                esc(POLARITY_RU.get(str(review["polarity"]), review["polarity"])),
                esc("—" if review["rating"] is None else "{:.1f}".format(float(review["rating"]))),
                esc(review["snippet"] or ""),
            ]
        )
    reviews_block = "<p class=muted>Отзывов не найдено.</p>"
    if review_rows:
        reviews_block = table(
            ("Площадка", "Источник", "Тон", "Оценка", "Фрагмент"), review_rows
        )

    return (
        "<h2>{company}</h2>{head}{summary}"
        "<h2>История публикаций</h2>{signals}"
        "<h2>Закономерности</h2>{patterns}"
        "<h2>Источники</h2>{reviews}"
        '<p><a href="/companies">К списку компаний</a></p>'
    ).format(
        company=esc(name),
        head=head,
        summary=summary,
        signals=render_signals(conn, name),
        patterns=patterns_block,
        reviews=reviews_block,
    )


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


__all__ = (
    "CONFIRM_WORD",
    "apply_cleanup",
    "company_rows",
    "render_cleanup",
    "render_companies",
    "render_company",
    "render_signals",
)
