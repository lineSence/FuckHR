"""Верх страницы «Профиль и резюме»: разговор о поиске.

Раньше это были два раздела с тридцатью полями, и заполнять их надо было, уже
зная ответ. Здесь владелец пишет своими словами, модель задаёт недостающие
вопросы и предлагает готовые критерии и блоки резюме. Применяет владелец
галочками: предложение модели — это черновик, а не решение [CORE-019].

Диалог и разбор живут в `intake.py`, здесь только HTML.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Sequence

import intake
import profile_form
import settings
from ui_core import esc, table

PLACEHOLDER = (
    "Например: ищу backend на Python, восемь лет опыта, последние три года "
    "highload в финтехе. Хочу удалёнку, от 250 тысяч на руки, без 1С и "
    "поддержки легаси."
)


def _bubble(role: str, text: str) -> str:
    who = "Ты" if role == "owner" else "Модель"
    color = "warn" if role == "owner" else "ok"
    return '<div class={cls}><b>{who}.</b> {text}</div>'.format(
        cls=color, who=who, text=esc(text).replace("\n", "<br>")
    )


def render_plan(plan: intake.Plan) -> str:
    """Предложение: вопросы, критерии, блоки резюме — с галочками."""
    if plan.questions:
        items = "".join("<li>{}</li>".format(esc(q)) for q in plan.questions)
        questions = (
            "<div class=warn><b>Уточню, чтобы не выдумывать:</b>"
            "<ul>{}</ul></div>".format(items)
        )
    else:
        questions = ""

    if plan.empty:
        return questions

    rows = []
    if plan.profile:
        rows.append(
            [
                '<label><input type=checkbox name=apply value="profile" checked> '
                "Критерии поиска</label>",
                esc(", ".join(_profile_lines(plan.profile))),
            ]
        )
    if plan.facts:
        rows.append(
            [
                '<label><input type=checkbox name=apply value="facts" checked> '
                "Факты о себе ({})</label>".format(len(plan.facts)),
                "<br>".join(esc(f) for f in plan.facts),
            ]
        )
    if plan.blocks:
        rows.append(
            [
                '<label><input type=checkbox name=apply value="resume" checked> '
                "Блоки резюме ({})</label>".format(len(plan.blocks)),
                "<br>".join(
                    "<b>{}</b>: {}".format(esc(b["section"]), esc(b["body"][:200]))
                    for b in plan.blocks
                ),
            ]
        )

    dropped = ""
    if plan.dropped:
        items = "".join("<li>{}</li>".format(esc(d)) for d in plan.dropped)
        dropped = (
            "<div class=danger><b>Выброшено, потому что ты этого не говорил:</b>"
            "<ul>{}</ul></div>".format(items)
        )

    summary = (
        "<p class=muted>{}</p>".format(esc(plan.summary)) if plan.summary else ""
    )
    return (
        "{questions}{summary}{dropped}"
        '<form method=post action="/intake/apply">'
        '<input type=hidden name="plan" value="{blob}">'
        "{table}<button>Применить отмеченное</button></form>"
    ).format(
        questions=questions,
        summary=summary,
        dropped=dropped,
        blob=esc(json.dumps(_plan_payload(plan), ensure_ascii=False)),
        table=table(["Что применить", "Что именно"], rows),
    )


def _plan_payload(plan: intake.Plan) -> dict:
    return {
        "profile": plan.profile,
        "facts": list(plan.facts),
        "resume": list(plan.blocks),
    }


def _profile_lines(patch: dict) -> list[str]:
    out = []
    for key, value in patch.items():
        if isinstance(value, list):
            out.append("{}: {}".format(key, ", ".join(str(v) for v in value)))
        elif isinstance(value, bool):
            out.append("{}: {}".format(key, "да" if value else "нет"))
        else:
            out.append("{}: {}".format(key, value))
    return out


def render_intake(
    conn: sqlite3.Connection,
    profile_path: str = "profile.yaml",
    plan: intake.Plan | None = None,
    note: str = "",
) -> str:
    """Чат-бокс, история разговора и последнее предложение."""
    parts = [note] if note else []
    parts.append(
        "<p class=muted>Напиши своими словами, кого ищешь и что за спиной. "
        "Модель задаст недостающие вопросы, предложит критерии поиска и блоки "
        "резюме — применишь галочками. Формы ниже остаются: разговор их "
        "заполняет, а не заменяет.</p>"
    )

    if not settings.flag("LLM_ENABLED"):
        parts.append(
            '<div class=warn>Модель выключена в <a href="/settings">настройках</a>. '
            "Разговор не заработает, но формы ниже полностью рабочие.</div>"
        )

    lines = intake.history(conn, 12)
    if lines:
        parts.append("".join(_bubble(role, text) for role, text in lines[-6:]))

    parts.append(
        '<form method=post action="/intake">'
        '<div class=field><textarea name="text" placeholder="{}"></textarea></div>'
        "<button>Отправить</button> "
        '<button class=secondary name="action" value="clear">Начать заново</button>'
        "</form>".format(esc(PLACEHOLDER))
    )

    if plan is not None:
        parts.append(render_plan(plan))
    return "".join(parts)


def apply_plan(
    conn: sqlite3.Connection,
    profile_path: str,
    payload: str,
    wanted: Sequence[str],
) -> str:
    """Применяет отмеченные части предложения. Возвращает строку для страницы."""
    try:
        data = json.loads(payload or "{}")
    except ValueError:
        return "Предложение устарело, напиши ещё раз."

    done: list[str] = []
    profile = profile_form.load(profile_path)
    touched = False

    if "profile" in wanted and data.get("profile"):
        changed = intake.apply_profile(profile, data["profile"])
        touched = touched or bool(changed)
        done.extend(changed)
    if "facts" in wanted and data.get("facts"):
        added = intake.apply_facts(profile, data["facts"])
        touched = touched or bool(added)
        if added:
            done.append("факты: +{}".format(added))
    if touched:
        profile_form.save(profile_path, profile)
    if "resume" in wanted and data.get("resume"):
        added = intake.apply_blocks(conn, data["resume"])
        if added:
            done.append(
                "блоков в резюме: +{} (ждут подтверждения ниже)".format(added)
            )

    if not done:
        return "Нечего применять: ничего не отмечено или всё уже стоит."
    return "Применено — " + "; ".join(done)


__all__ = ("apply_plan", "render_intake", "render_plan")
