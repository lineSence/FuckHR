"""Раздел «Профили»: страницы и обработка форм.

Вынесено из `webui.py` по двум причинам. Первая формальная: с приходом
нескольких профилей маршрутов стало шесть, и файл перешёл 25 КБ [CORE-024].
Вторая важнее: раздел перестал быть одной формой. Теперь это два экрана —
список карточек и редактор одного профиля, — и логика «какой профиль сейчас
открыт» не должна размазываться по общему роутеру.

Что здесь происходит:

- `/profile` без параметров — карточки всех профилей и кнопка «Добавить»;
- `/profile?id=<файл>` — редактор: разговор с моделью, свёрнутые разделы
  критериев, резюме;
- `/profiles/new` и `/profiles/toggle` — создать профиль и включить-выключить;
- `/profile`, `/intake`, `/intake/apply`, `/resume` POST — сохранение.

Все обработчики возвращают `(заголовок, тело, путь к профилям)`. Третье нужно
ровно один раз: когда владелец добавляет второй профиль, каталог заводится сам
и RUN_PROFILE меняется — сервер должен узнать об этом без перезапуска.
"""

from __future__ import annotations

import logging
import sqlite3

import intake
import llm
import profile_form
import settings
import ui_intake
import ui_profiles
from ui_core import esc, open_db
from ui_profile import render_profile, save_profile
from ui_resume import render_resume, save_resume

log = logging.getLogger(__name__)

TITLE = "Профили"
POST_PATHS = frozenset(
    {"/profile", "/profiles/new", "/profiles/toggle", "/intake", "/intake/apply", "/resume"}
)


def _one(form: dict[str, list[str]], key: str) -> str:
    return (form.get(key) or [""])[0]


def _wrap(title: str, body: str, open_: bool = False) -> str:
    return "<details{open}><summary>{title}</summary>{body}</details>".format(
        open=" open" if open_ else "", title=esc(title), body=body
    )


def body(
    conn: sqlite3.Connection,
    profile_path: str,
    pid: str = "",
    plan: object = None,
    note: str = "",
    saved: int | None = None,
    problems: tuple[str, ...] = (),
    resume_note: str = "",
) -> str:
    """Список карточек или редактор одного профиля."""
    if not pid:
        return ui_profiles.render_cards(profile_path, note) + _wrap(
            "Резюме", render_resume(conn, _path(profile_path, ""), saved=resume_note)
        )
    path = _path(profile_path, pid)
    return (
        '<p><a href="/profile">← Все профили</a></p>'
        + _wrap("Разговор о поиске", ui_intake.render_intake(conn, path, plan, note, pid))
        + "<h2>Критерии поиска</h2>"
        + render_profile(path, saved=saved, problems=problems, pid=pid)
        + _wrap("Резюме", render_resume(conn, path, saved=resume_note))
    )


def _path(profile_path: str, pid: str) -> str:
    """Файл профиля: по id из браузера, иначе первый включённый."""
    try:
        return str(ui_profiles.resolve(profile_path, pid))
    except KeyError as exc:
        log.warning("%s", exc)
        return str(ui_profiles.resolve(profile_path, ""))


def _page(profile_path: str, **kwargs: object) -> tuple[str, str, str]:
    conn = open_db()
    try:
        return TITLE, body(conn, profile_path, **kwargs), profile_path  # type: ignore[arg-type]
    finally:
        conn.close()


def _intake(profile_path: str, form: dict[str, list[str]], pid: str) -> tuple[str, str, str]:
    """Одна реплика владельца: один вызов модели, предложение с галочками."""
    conn = open_db()
    try:
        if _one(form, "action") == "clear":
            intake.clear(conn)
            return TITLE, body(
                conn, profile_path, pid, note="<div class=ok>Разговор очищен.</div>"
            ), profile_path
        said, dialogue = ui_intake.compose(
            form.get("question") or [], form.get("answer") or [], _one(form, "text")
        )
        if not said:
            return TITLE, body(
                conn, profile_path, pid, note="<div class=warn>Пустое сообщение.</div>"
            ), profile_path
        intake.log_message(conn, "owner", said)
        gateway = llm.Gateway.from_env(conn) if settings.flag("LLM_ENABLED") else None
        plan = intake.ask(
            gateway,
            intake.owner_words(conn),
            profile_form.load(_path(profile_path, pid)),
            context=dialogue,
        )
        reply = plan.summary or (
            "\n".join(plan.questions) if plan.questions else "Ответа нет."
        )
        intake.log_message(conn, "ai", reply)
        intake.save_plan(conn, plan)
        return TITLE, body(conn, profile_path, pid, plan=plan), profile_path
    finally:
        conn.close()


def handle(
    path: str, form: dict[str, list[str]], profile_path: str
) -> tuple[str, str, str]:
    """POST раздела. Возвращает (заголовок, тело, путь к профилям)."""
    pid = _one(form, "id")

    if path == "/profiles/new":
        new_id, profile_path, note = ui_profiles.create(profile_path, _one(form, "name"))
        # Сразу в редактор: пустой профиль бесполезен, пока не заданы запросы.
        return _page(profile_path, pid=new_id, note="<div class=ok>{}</div>".format(esc(note)))

    if path == "/profiles/toggle":
        note = ui_profiles.toggle(profile_path, pid, _one(form, "on") == "1")
        return _page(profile_path, note="<div class=ok>{}</div>".format(esc(note)))

    if path == "/profile":
        count, problems = save_profile(_path(profile_path, pid), form)
        return _page(profile_path, pid=pid, saved=count, problems=tuple(problems))

    if path == "/intake":
        return _intake(profile_path, form, pid)

    if path == "/intake/apply":
        conn = open_db()
        try:
            note = ui_intake.apply_plan(
                conn,
                _path(profile_path, pid),
                _one(form, "plan"),
                form.get("apply") or [],
            )
            return TITLE, body(
                conn, profile_path, pid, note="<div class=ok>{}</div>".format(esc(note))
            ), profile_path
        finally:
            conn.close()

    # /resume: ответ рисуется сразу, без редиректа — нужно показать, что
    # именно модель предложила и что ждёт подтверждения.
    flat = {key: values[0] for key, values in form.items() if values}
    conn = open_db()
    try:
        note = save_resume(conn, flat, _path(profile_path, pid))
        return TITLE, body(conn, profile_path, pid, resume_note=note), profile_path
    finally:
        conn.close()


__all__ = ("POST_PATHS", "TITLE", "body", "handle")
