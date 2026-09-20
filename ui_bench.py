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
import bench_metrics
import jobs
import llm
import settings
from ui_core import details, esc, table

ROOT = Path(__file__).resolve().parent
REPORT_PATH = ROOT / "data" / "bench" / "last.json"

# Ключи .env, куда можно подставить модель. Список закрытый: из браузера
# приходит имя ключа, и записать он должен только настройку модели.
CASCADE_KEYS = frozenset(llm.STAGE_MODELS_ENV.values())
ENV_KEYS = (
    frozenset(llm.PROXY_MODEL_ENV.values())
    | frozenset(llm.STAGE_MODEL_ENV.values())
    | CASCADE_KEYS
)

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

    levels = bench_metrics.levels_present(rows)
    level_block = ""
    if len(levels) > 1:
        profile = bench_metrics.by_level(rows)
        level_block = table(
            ["Модель"] + [bench_metrics.LEVEL_RU[level] for level in levels],
            [
                [esc(model)]
                + [score_cell(profile.get((model, level))) for level in levels]
                for model in models
            ],
            raw_head=True,
        )

    extra = bench_metrics.columns(rows)
    columns_block = table(
        ["Модель"] + list(extra),
        [
            [esc(model)]
            + [bench_metrics.fmt(extra[name].get(model)) for name in extra]
            for model in models
        ],
        raw_head=True,
    )
    warning = bench_metrics.separation(rows)

    parts = [
        "<h3>Результат</h3>",
        "<p class=muted>{}</p>".format(esc(meta)),
        table(head, body, raw_head=True),
        "<p class=muted>Балл — взвешенное среднее: сложный кейс весит втрое, "
        "иначе лёгкие задачи перевешивают и модели упираются в потолок.</p>",
    ]
    if level_block:
        parts += [
            "<h3>По уровням сложности</h3>",
            level_block,
        ]
    parts += [
        "<h3>Отдельные колонки</h3>",
        columns_block,
        "<p class=muted>В балл не входят: стабильность — разброс между "
        "повторами одного кейса, порядок — тот же список наоборот, отказы — "
        "кейсы, где верный ответ «никого» или «спросить», ловушки — где текст "
        "просит соврать.</p>",
    ]
    if warning:
        parts.append("<div class=warn>{}</div>".format(esc(warning)))
    parts += [
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
    """Подстановка каскада в .env: свой порядок кандидатов на каждый этап.

    Порядок делает bench.cascades: сначала балл, при почти равном балле —
    скорость. Пишется не одно имя, а до трёх через запятую: это и есть
    порядок фолбэка в рантайме (ADR-022). Галочки сняты у этапов, где тот же
    каскад уже стоит: незачем предлагать запись, которая ничего не меняет.
    """
    picks = bench.cascades(rows)
    if not picks:
        return ""
    values = settings.load()
    body, hidden = [], []
    personal = False
    for stage, chain in sorted(picks.items()):
        env_key = llm.STAGE_MODELS_ENV.get(stage)
        if not env_key:
            continue
        names = ",".join(model for model, _, _ in chain)
        model, score, seconds = chain[0]
        profile = llm.STAGE_PROFILES.get(stage, "")
        current = values.get(env_key, "")
        named = values.get(llm.PROXY_MODEL_ENV.get(profile, ""), "")
        fallback = "по профилю {}{}".format(
            profile, ": {}".format(named) if named else ""
        )
        personal = personal or stage in llm.PERSONAL_STAGES
        hidden.append(
            '<input type=hidden name="model:{key}" value="{model}">'.format(
                key=esc(env_key), model=esc(names)
            )
        )
        body.append(
            [
                '<label><input type=checkbox name=apply value="{key}"{on}> '
                "{stage}</label>".format(
                    key=esc(env_key),
                    on="" if current == names else " checked",
                    stage=esc(stage),
                ),
                esc(current or fallback),
                "<b>{}</b>".format(esc(names.replace(",", " → "))),
                "{} · {:.1f} с".format(score_cell(score), seconds),
            ]
        )
    if not body:
        return ""
    note = (
        "<p class=muted>Среди этапов есть персональные (contacts, dossier, "
        "draft): имя модели на прокси сработает только при включённом "
        "LLM_PERSONAL_VIA_PROXY [CORE-012].</p>"
        if personal
        else ""
    )
    return (
        "<h3>Подставить в настройки</h3>"
        "<p class=muted>На этап ставится каскад: до трёх кандидатов по порядку "
        "бенча, а последним слотом всегда идёт локальная модель. Первый "
        "кандидат перебивает модель профиля. Непроверенные этапы остаются на "
        "профиле. Запись идёт в .env, как со страницы «Настройки».</p>"
        "{note}"
        '<form method=post action="/llm/apply">{hidden}{table}'
        "<button>Записать отмеченные</button></form>"
    ).format(
        note=note,
        hidden="".join(hidden),
        table=table(["Этап", "Сейчас", "Каскад", "Первый: балл и время"], body),
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
    "CASCADE_KEYS",
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
