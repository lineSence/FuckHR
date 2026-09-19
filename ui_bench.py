"""Страница сравнения моделей: форма, живой статус прогона и отчёт таблицами.

Отдельный файл, потому что ui_forms уже близок к лимиту [CORE-024], а сравнение
моделей — самостоятельный экран: форма, ход прогона, разбор результата.

Результат читается из JSON, который пишет bench.py (`data/bench/last.json`).
Раньше отчёт показывался хвостом лога: markdown-таблица в <pre> нечитаема, а
хвост в 120 строк обрезал начало. Вторая копия в базе всё равно не нужна.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Sequence

import bench
import jobs
import llm
import settings
from ui_core import details, esc, table

ROOT = Path(__file__).resolve().parent
REPORT_PATH = ROOT / "data" / "bench" / "last.json"

# Ключи .env, куда можно подставить модель. Список закрытый: из браузера
# приходит имя ключа, и записать он должен только настройку модели.
ENV_KEYS = frozenset(llm.PROXY_MODEL_ENV.values())

# Границы окраски балла: 0.8 — рабочая модель, 0.5 — «иногда врёт», ниже — брак.
GOOD, SO_SO = 0.8, 0.5


def score_cell(value: float | None) -> str:
    if value is None:
        return "<span class=muted>—</span>"
    cls = "ok" if value >= GOOD else ("warn" if value >= SO_SO else "danger")
    return '<span class="{}">{:.2f}</span>'.format(cls, value)


def load_report(path: Path | str = REPORT_PATH) -> dict[str, Any] | None:
    """Последний отчёт или None. Битый файл — не повод ронять страницу."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("rows") else None


def refresh_seconds() -> int:
    """Пока сравнение идёт, страница обновляет себя сама."""
    job = jobs.runner.last("bench")
    return 2 if job is not None and job.running else 0


def render_status(job: jobs.Job) -> str:
    """Ход прогона прямо на странице: сколько сделано и что сейчас считается."""
    pair = job.progress
    if pair is None:
        bar = "<progress></progress>"
        label = "готовлю прогон"
    else:
        done, total = pair
        bar = '<progress value="{}" max="{}"></progress>'.format(done, total)
        label = "кейс {} из {} · {}%".format(done, total, job.percent)
    now = [line for line in job.tail(3) if line.strip()]
    return (
        "<div class=ok><b>Идёт сравнение.</b> Страница обновляется сама, можно "
        "её не трогать.</div>"
        "<div class=bar>{bar}<span class=muted>{label} · {seconds:.0f} с</span></div>"
        "<p class=muted>{now}</p>"
        '<form method=post action="/stop">'
        '<input type=hidden name=job value="{id}">'
        "<button class=secondary>Остановить</button></form>"
    ).format(
        bar=bar,
        label=esc(label),
        seconds=job.duration,
        now=esc(now[-1] if now else "ждём первый ответ модели"),
        id=job.id,
    )


def render_report(report: dict[str, Any], job: jobs.Job | None = None) -> str:
    """Сводка по этапам, рекомендация и разбор кейсов."""
    rows = [bench.Row(**row) for row in report.get("rows", [])]
    if not rows:
        return ""
    models = sorted({row.model for row in rows})
    stages = [s for s in bench.STAGES if any(r.stage == s for r in rows)]
    means = bench.by_stage(rows)

    head = ["Этап"] + models
    body = [
        [esc(stage)] + [score_cell(means.get((stage, model))) for model in models]
        for stage in stages
    ]

    best = "; ".join(
        "{} → {} ({:.2f})".format(stage, model, score)
        for stage, (model, score) in sorted(bench.winners(rows).items())
    )

    when = report.get("finished_at")
    meta = "{when} · {models} · прогонов на кейс: {repeat}".format(
        when=(
            time.strftime("%d.%m %H:%M", time.localtime(when))
            if isinstance(when, (int, float))
            else "прогон"
        ),
        models=", ".join(models),
        repeat=report.get("repeat", 1),
    )

    failures = [row for row in rows if row.score < GOOD]
    case_rows = [
        [
            esc(row.model),
            esc(row.case),
            score_cell(row.score),
            "{:.1f} с".format(row.seconds),
            esc(row.note or "—"),
        ]
        for row in sorted(rows, key=lambda r: (r.score, r.model))
    ]

    parts = [
        "<h3>Результат</h3>",
        "<p class=muted>{}</p>".format(esc(meta)),
        table(head, body, raw_head=True),
        "<div class=ok><b>Лучший балл по этапам:</b> {}</div>".format(
            esc(best or "не вышло")
        ),
        render_apply_form(rows),
        details(
            "Разбор кейсов",
            "{} из {} ниже {:.1f}".format(len(failures), len(rows), GOOD),
            table(
                ["Модель", "Кейс", "Балл", "Время", "Что вышло"], case_rows
            ),
        ),
    ]
    if job is not None:
        parts.append(
            '<p class=muted>Состояние: {status} · '
            '<a href="/?job={id}">полный лог</a></p>'.format(
                status=esc(job.status), id=job.id
            )
        )
    return "".join(parts)


def render_apply_form(rows: Sequence[bench.Row]) -> str:
    """Подстановка победителей в .env: профиль → LLM_PROXY_MODEL_*.

    Выбор делает bench.recommend: сначала балл, при почти равном балле —
    скорость. Галочки сняты у профилей, где и так стоит эта модель: незачем
    предлагать запись, которая ничего не меняет.
    """
    picks = bench.recommend(rows)
    if not picks:
        return ""
    values = settings.load()
    body, hidden = [], []
    for profile, (model, score, seconds) in sorted(picks.items()):
        env_key = llm.PROXY_MODEL_ENV.get(profile)
        if not env_key:
            continue
        current = values.get(env_key, "")
        same = current == model
        hidden.append(
            '<input type=hidden name="model:{key}" value="{model}">'.format(
                key=esc(env_key), model=esc(model)
            )
        )
        body.append(
            [
                '<label><input type=checkbox name=apply value="{key}"{on}> '
                "{profile}</label>".format(
                    key=esc(env_key), on="" if same else " checked", profile=esc(profile)
                ),
                esc(env_key),
                esc(current or "не задана"),
                "<b>{}</b>".format(esc(model)),
                "{} · {:.1f} с".format(score_cell(score), seconds),
            ]
        )
    if not body:
        return ""
    personal = (
        "<p class=muted>Профиль local-only — это персональные этапы (contacts, "
        "dossier, draft). Имя модели на прокси для них работает только при "
        "включённом LLM_PERSONAL_VIA_PROXY [CORE-012].</p>"
        if any(row[1] == esc(llm.PROXY_MODEL_ENV[llm.LOCAL]) for row in body)
        else ""
    )
    return (
        "<h3>Подставить в настройки</h3>"
        "<p class=muted>Победитель профиля: сначала балл, при разнице меньше "
        "0.05 — тот, кто отвечает быстрее. Запись идёт в .env, как со страницы "
        "«Настройки».</p>"
        "{personal}"
        '<form method=post action="/llm/apply">{hidden}{table}'
        "<button>Записать отмеченные</button></form>"
    ).format(
        personal=personal,
        hidden="".join(hidden),
        table=table(
            ["Профиль", "Ключ", "Сейчас", "Ставим", "Балл и время"], body
        ),
    )


def render_bench_form(note: str = "", known: Sequence[str] = ()) -> str:
    """Форма сравнения: чекбоксы известных моделей плюс ручной ввод.

    Имена моделей приходят из браузера, поэтому в команду они попадают только
    после проверки bench_models(): argv собирается из закрытого списка задач,
    а не из строки формы.
    """
    stages = "".join(
        '<label><input type=checkbox name=stage value="{s}" checked> {s}</label>'.format(
            s=esc(stage)
        )
        for stage in bench.STAGES
    )
    picker = ""
    if known:
        boxes = "".join(
            '<label><input type=checkbox name=models value="{m}"> {m}</label>'.format(
                m=esc(name)
            )
            for name in known[:30]
        )
        picker = (
            "<div class=field><label>Модели с прокси</label>"
            "<div class=checks>{}</div></div>".format(boxes)
        )

    job = jobs.runner.last("bench")
    live = render_status(job) if job is not None and job.running else ""
    report = "" if live else render_report(load_report() or {}, job)

    return (
        "<h2>Сравнение моделей</h2>"
        "<p class=muted>Одни и те же задачи пайплайна на нескольких моделях. "
        "Балл считают правила, а не модель: дословная цитата, никаких новых "
        "чисел, адресат из списка. Половина кейсов — ловушки. "
        "1.00 — ответ без единой выдумки, ниже 0.50 — модель для этапа не годится."
        "</p>"
        "{note}{live}"
        '<form method=post action="/bench">'
        "{picker}"
        '<div class=field><label>Другие модели через запятую</label>'
        '<input type=text name=models placeholder="qwen2.5-7b, gpt-4o-mini">'
        "<div class=hint>Имена как в config.yaml прокси. Список живых имён — "
        "по ссылке «Спросить список моделей» выше.</div></div>"
        '<div class=field><label>Этапы</label><div class=checks>{stages}</div></div>'
        '<div class=field><label>Прогонов на кейс</label>'
        '<input type=number name=repeat value="1" min="1" max="5">'
        "<div class=hint>Больше одного нужно, когда модели отвечают нестабильно: "
        "каждый прогон — это реальные вызовы и время.</div></div>"
        "<button>Сравнить</button></form>{report}"
    ).format(
        note=note, live=live, picker=picker, stages=stages, report=report
    )


__all__ = (
    "ENV_KEYS",
    "REPORT_PATH",
    "load_report",
    "refresh_seconds",
    "render_apply_form",
    "render_bench_form",
    "render_report",
    "render_status",
    "score_cell",
)
