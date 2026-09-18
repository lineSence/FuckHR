"""Страница «Резюме»: интервью по секциям, подтверждение блоков, экспорт.

Сбор сделан диалогом, а не одной большой формой: на вопрос «что изменилось от
твоей работы» люди отвечают конкретно, а в пустое поле «Опыт работы» пишут
должностную инструкцию. Ответ сохраняется всегда, даже если модель молчит.

Главное решение интерфейса: предложенное моделью показано рядом с исходным
ответом владельца. Без исходника подтверждение превращается в формальность:
человек не помнит свой ответ через пять секций и жмёт «ок» на всё подряд.

Противоречия показываются предупреждением и ничего не блокируют: решение за
владельцем. Переход в лиды «через голову» бывает осознанным.

Шаблоны — только str.format с заранее вычисленными переменными, как во всём ui_*.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Mapping, Sequence

import llm
import resume
import resume_llm
from ui_core import area_field, esc, table, text_field

log = logging.getLogger(__name__)

FALSY = {"0", "false", "no", "off", ""}

PERIOD_SECTIONS = frozenset({"experience", "projects", "education"})


def _gateway(conn: sqlite3.Connection) -> llm.Gateway | None:
    """Шлюз или None. None — штатный режим, а не ошибка [CORE-017].

    LLM_ENABLED считается включённым, пока не выключен явно: страница резюме не
    должна молча терять помощника только из-за незаданной переменной.
    """
    if (os.getenv("LLM_ENABLED", "1") or "").strip().lower() in FALSY:
        return None
    gateway = llm.Gateway.from_env(conn)
    return gateway if gateway.enabled else None


def _status_cell(block: resume.Block) -> str:
    if block.pending:
        return '<span class=pill>предложено ИИ, подтвердите</span>'
    if block.by_ai:
        return '<span class=muted>подтверждено</span>'
    return '<span class=muted>ваш текст</span>'


def _actions(block: resume.Block) -> str:
    parts = []
    if block.pending:
        parts.append(
            '<form method=post action="/resume" class=tasks>'
            '<input type=hidden name=action value=confirm>'
            '<input type=hidden name=block value="{}">'
            "<button>Подтвердить</button></form>".format(block.id)
        )
    parts.append(
        '<form method=post action="/resume" class=tasks>'
        '<input type=hidden name=action value=delete>'
        '<input type=hidden name=block value="{}">'
        '<button class=secondary>Удалить</button></form>'.format(block.id)
    )
    return "".join(parts)


def _block_rows(items: Sequence[resume.Block]) -> list[list[str]]:
    rows = []
    for block in items:
        body = esc(block.body).replace("\n", "<br>")
        original = ""
        if block.answer and block.answer.strip() != block.body.strip():
            # Исходный ответ нужен рядом: иначе подтверждение становится формальностью.
            original = (
                '<div class=muted style="margin-top:6px">Вы говорили: {}</div>'
            ).format(esc(block.answer).replace("\n", "<br>"))
        rows.append(
            [
                esc(resume.SECTION_TITLES.get(block.section, block.section)),
                "<b>{}</b><br>{}{}".format(
                    esc(block.label), body, original
                )
                if block.heading or block.period
                else "{}{}".format(body, original),
                _status_cell(block),
                _actions(block),
            ]
        )
    return rows


def _interview(section: str, gateway_on: bool) -> str:
    """Форма одного вопроса интервью."""
    fields = area_field(
        "answer",
        resume.QUESTIONS.get(section, ""),
        "",
        "Пишите как говорится. Оформление — не ваша забота."
        if gateway_on
        else "Модель выключена: текст сохранится дословно.",
    )
    if section in PERIOD_SECTIONS:
        fields += '<div class=cols>{}{}{}</div>'.format(
            text_field("heading", "Название", "", "", "Компания, должность"),
            text_field("started", "Начало", "", "", "2021-03"),
            text_field(
                "finished", "Окончание", "", "Пусто — работаю сейчас", "2024-08"
            ),
        )
    return (
        '<form method=post action="/resume">'
        '<input type=hidden name=action value=add>'
        '<input type=hidden name=section value="{section}">'
        "{fields}<button>Добавить в резюме</button></form>"
    ).format(section=esc(section), fields=fields)


def render_resume(
    conn: sqlite3.Connection,
    profile_path: str = "profile.yaml",
    saved: str = "",
    problems: Sequence[str] = (),
) -> str:
    """Тело страницы /resume."""
    resume_id = resume.get_or_create(conn)
    items = resume.blocks(conn, resume_id)
    counts = resume.stats(conn, resume_id)
    gateway_on = _gateway(conn) is not None

    parts: list[str] = []
    if saved:
        parts.append("<div class=ok>{}</div>".format(esc(saved)))

    warnings = list(problems) or resume.contradictions(conn, resume_id, profile_path)
    if warnings:
        parts.append(
            "<div class=warn><b>На что стоит посмотреть</b><ul>{}</ul>"
            "<div class=hint>Это предупреждения, а не запреты — решаете вы.</div>"
            "</div>".format(
                "".join("<li>{}</li>".format(esc(text)) for text in warnings)
            )
        )

    months = resume.experience_months(
        [b for b in items if b.confirmed]
    )
    parts.append(
        "<p class=muted>Блоков: {blocks}, подтверждено {confirmed}, ждут проверки "
        "{pending}. Стаж по срокам: {years:.1f} лет. Модель: {llm}.</p>".format(
            blocks=counts["blocks"],
            confirmed=counts["confirmed"],
            pending=counts["pending"],
            years=months / 12.0,
            llm="помогает" if gateway_on else "выключена, работает обычная форма",
        )
    )

    if items:
        parts.append("<h2>Блоки</h2>")
        parts.append(
            table(
                ("Секция", "Текст", "Статус", ""), _block_rows(items)
            )
        )
    else:
        parts.append(
            "<p class=muted>Резюме пустое. Ответьте на вопросы ниже — по одному "
            "за раз, в любом порядке.</p>"
        )

    parts.append("<h2>Интервью</h2>")
    for key in resume.SECTION_KEYS:
        filled = sum(1 for b in items if b.section == key)
        parts.append(
            "<h3>{title} <span class=muted>({filled})</span></h3>{form}".format(
                title=esc(resume.SECTION_TITLES[key]),
                filled=filled,
                form=_interview(key, gateway_on),
            )
        )

    markdown = resume.export(conn, resume_id)
    parts.append(
        "<h2>Экспорт в Markdown</h2>"
        "<p class=muted>Только подтверждённые блоки. Скопируйте целиком.</p>"
        "<pre>{}</pre>".format(esc(markdown.strip() or "Пока нечего экспортировать."))
    )
    return "".join(parts)


def save_resume(
    conn: sqlite3.Connection, form: Mapping[str, str], profile_path: str = "profile.yaml"
) -> str:
    """Обработка POST /resume. Возвращает сообщение для страницы."""
    action = (form.get("action") or "").strip()
    resume_id = resume.get_or_create(conn)

    if action == "confirm":
        block_id = _int(form.get("block"))
        if block_id is None:
            return "Не понял, какой блок подтвердить."
        resume.confirm_block(conn, block_id)
        return "Блок подтверждён и теперь уйдёт в экспорт и письма."

    if action == "delete":
        block_id = _int(form.get("block"))
        if block_id is None:
            return "Не понял, какой блок удалить."
        resume.delete_block(conn, block_id)
        return "Блок удалён."

    if action != "add":
        return ""

    section = (form.get("section") or "").strip()
    answer = (form.get("answer") or "").strip()
    if section not in resume.SECTION_TITLES:
        return "Неизвестная секция, ничего не сохранено."
    if not answer:
        return "Пустой ответ сохранять нечего."

    block_id = resume_llm.save_draft(
        conn,
        resume_id,
        section,
        answer,
        gateway=_gateway(conn),
        heading=(form.get("heading") or "").strip(),
        started=(form.get("started") or "").strip(),
        finished=(form.get("finished") or "").strip(),
    )
    saved = next(
        (b for b in resume.blocks(conn, resume_id) if b.id == block_id), None
    )
    if saved is not None and saved.pending:
        return (
            "Модель предложила формулировку. Сравните с тем, что вы говорили, "
            "и подтвердите — без этого текст никуда не попадёт."
        )
    return "Ответ сохранён."


def _int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = ("render_resume", "save_resume")
