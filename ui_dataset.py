"""Блок «Датасет для дообучения» на странице «Модель»: что есть и кнопка сборки.

Отдельный файл, потому что ui_forms уже близок к лимиту [CORE-024], а тема
самостоятельная: посчитать, что в базе годится в примеры, и запустить сборку.

Сама сборка идёт задачей в подпроцессе, как и все длинные операции: на паре
тысяч вакансий она занимает минуты, и держать её в потоке сервера нельзя.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import dataset_export
import jobs
import settings
from ui_core import esc, table

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "train"

# Что показываем как «из этого выйдут примеры». Подписи те же, что у этапов
# в таблице маршрутов выше: это одни и те же этапы пайплайна.
COUNTS = (
    (
        "extract",
        "вакансий с разобранными условиями",
        "SELECT COUNT(DISTINCT key) FROM vacancy_conditions",
    ),
    (
        "hr_filter",
        "отчётов детектора с утверждениями от модели",
        "SELECT COUNT(*) FROM vacancy_signals WHERE payload LIKE '%llm_claim%'",
    ),
    (
        "dossier",
        "досье со сводкой, которую писала модель",
        "SELECT COUNT(*) FROM company_dossier WHERE summary_by = 'модель'",
    ),
)


def available(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько строк базы годится в примеры. Нет таблицы — ноль, не падаем."""
    out: dict[str, int] = {}
    for stage, _, sql in COUNTS:
        try:
            out[stage] = int(conn.execute(sql).fetchone()[0] or 0)
        except sqlite3.Error:
            out[stage] = 0
    return out


def load_report(path: Path = OUT_DIR / dataset_export.REPORT_NAME) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def render_report(report: dict) -> str:
    when = report.get("finished_at")
    rows = [
        [esc(stage), str(count)]
        for stage, count in sorted(report.get("by_stage", {}).items())
    ]
    rows.append(["<b>всего</b>", "<b>{}</b>".format(report.get("total", 0))])
    files = ", ".join(
        "{} — {}".format(name, count)
        for name, count in sorted(report.get("files", {}).items())
    )
    parts = [
        "<div class=ok>Последняя сборка: {when} · {files}</div>".format(
            when=esc(
                time.strftime("%d.%m %H:%M", time.localtime(when))
                if isinstance(when, (int, float))
                else "когда-то"
            ),
            files=esc(files or "файлов нет"),
        ),
        table(["Этап", "Примеров"], rows),
        "<p class=muted>Файлы лежат в <code>{}</code>. Их и надо загрузить в "
        "Colab.</p>".format(esc(str(OUT_DIR))),
    ]
    rejected = report.get("rejected") or {}
    if rejected:
        items = "".join(
            "<li>{} × {}</li>".format(count, esc(why))
            for why, count in sorted(rejected.items())
        )
        parts.append(
            "<details><summary>Отбраковано: {}</summary><ul>{}</ul>"
            "<p class=muted>Отбраковка — это норма: пример не берётся, если "
            "пайплайн не принял свой же сохранённый ответ.</p></details>".format(
                sum(rejected.values()), items
            )
        )
    return "".join(parts)


def render_dataset(conn: sqlite3.Connection, note: str = "") -> str:
    """Что есть в базе, форма сборки и результат прошлой."""
    counts = available(conn)
    rows = [
        [esc(stage), esc(label), str(counts.get(stage, 0))]
        for stage, label, _ in COUNTS
    ]
    parts = [
        "<h2>Датасет для дообучения</h2>",
        "<p class=muted>Примеры собираются из своей базы: запрос — тот же самый, "
        "что уходит модели в проде, ответ — тот, что пайплайн уже принял и "
        "сохранил. Ни одного вызова модели и ни одного запроса в сеть.</p>",
        table(["Этап", "Что берём", "Есть в базе"], rows),
        note,
        '<form method=post action="/dataset">'
        "<label>Потолок примеров на этап "
        '<input type=number name=cap value="{cap}" min="10" max="5000"></label> '
        "<button>Собрать датасет</button></form>".format(cap=dataset_export.DEFAULT_CAP),
        "<p class=muted>Контакты в текстах заменяются масками, а персональные "
        "этапы — контакты, письма, резюме и анкета — не выгружаются вовсе: файл "
        "уезжает в облако [CORE-012], [CORE-021].</p>",
    ]
    report = load_report()
    if report:
        parts.append(render_report(report))
    return "".join(parts)


def start_dataset(form: dict) -> tuple[int | None, str]:
    """Запускает сборку: (номер задачи, объяснение отказа).

    Из браузера приходит только число: команда берётся из jobs.TASKS, как и у
    остальных задач.
    """
    cap = settings.as_int((form.get("cap") or [""])[0], dataset_export.DEFAULT_CAP)
    cap = max(10, min(5000, cap))
    try:
        job = jobs.runner.start("dataset", ["--cap", str(cap)])
    except (KeyError, RuntimeError) as exc:
        return None, "<div class=warn>{}</div>".format(esc(exc))
    return job.id, ""


def refused(note: str) -> str:
    """Тот же блок, но с объяснением отказа. Своё соединение: сервер держит
    по одному на запрос, а сюда мы попадаем уже после разбора формы."""
    from ui_core import open_db

    conn = open_db()
    try:
        return render_dataset(conn, note)
    finally:
        conn.close()


__all__ = (
    "available",
    "load_report",
    "refused",
    "render_dataset",
    "start_dataset",
)
