"""Страница запуска: кнопки задач, режим цикла, полоска и живой лог.

Выделено из ui_views.py по [CORE-024]. Ни одна функция здесь не знает про HTTP:
на вход — параметры, на выход — готовый HTML.
"""

from __future__ import annotations

import time
import sqlite3
from typing import Mapping, Sequence

import jobs
import run_loop
import settings
from ui_core import esc, table


def save_loop(form: Mapping[str, Sequence[str]]) -> list[str]:
    """Сохраняет режим цикла из формы страницы запуска.

    Ключи те же, что на странице настроек: прогон — отдельный процесс и читает
    .env на старте, отдельного канала для «галочки» заводить незачем.
    """
    return settings.save(
        {
            "RUN_LOOP_ENABLED": "1" if form.get("enabled") else "0",
            "RUN_LOOP_CYCLES": str(
                max(0, settings.as_int((form.get("cycles") or ["0"])[0], 0))
            ),
            "RUN_LOOP_PAUSE": str(
                max(0, settings.as_int((form.get("pause") or ["300"])[0], 300))
            ),
        }
    )


def stop(job_id: int, soft: bool) -> None:
    """Останавливает задачу. Мягко — просьбой дописать текущий цикл, жёстко —
    убийством процесса."""
    if soft:
        run_loop.request_stop()
    else:
        jobs.runner.stop(job_id)


def progress_block(job: jobs.Job) -> str:
    """Полоска загрузки.

    Если в логах нашёлся счётчик вида «[3/30]» — показываем реальный процент.
    Если не нашёлся — неопределённая полоска без цифр: выдуманные проценты хуже,
    чем честное «шаги неизвестны» [CORE-019].
    """
    pair = job.progress
    if pair is None:
        if not job.running:
            return ""
        bar = "<progress></progress>"
        label = "шаги неизвестны, смотри лог"
    else:
        done, total = pair
        bar = '<progress value="{}" max="{}"></progress>'.format(done, total)
        label = "{} из {} · {}%".format(done, total, job.percent)
    return "<div class=bar>{bar}<span class=muted>{label}</span></div>".format(
        bar=bar, label=esc(label)
    )


def loop_form() -> str:
    """Галочка режима цикла прямо на странице запуска.

    Настройка живёт в .env, как и все прочие: прогон — отдельный процесс и
    читает её на старте. Форма здесь, а не только в настройках, потому что
    решение «гонять по кругу» принимается ровно в тот момент, когда жмёшь
    кнопку сбора.
    """
    loop = settings.loop_options()
    return (
        '<form method=post action="/loop" class=tasks>'
        "<label><input type=checkbox name=enabled value=1 {checked}> "
        "Повторять сбор по кругу</label> "
        '<label>циклов <input type=number name=cycles min=0 size=4 value="{cycles}">'
        "</label> "
        '<label>пауза, с <input type=number name=pause min=0 size=5 value="{pause}">'
        "</label> "
        "<button class=secondary>Сохранить</button></form>"
        "<p class=muted>Ноль циклов — сбор повторяется, пока не остановишь сам. "
        "Кнопка «Остановить» убивает прогон на месте, «Остановить после цикла» даёт "
        "ему дописать текущий круг.</p>"
    ).format(
        checked="checked" if loop.enabled else "",
        cycles=loop.cycles,
        pause=int(loop.pause),
    )


def render_run(
    active_id: int | None = None,
    note: str = "",
    conn: "sqlite3.Connection | None" = None,
) -> tuple[str, int]:
    """Главная страница: кнопки, полоска и живой лог.

    Вторым значением идёт интервал автообновления: пока задача идёт, страница
    обновляет себя каждые две секунды. Мета-обновление вместо JavaScript — чтобы
    не тащить фронтенд в проект из десяти файлов.
    """
    parts = [note] if note else []

    buttons = []
    for key, title, hint in jobs.task_list():
        buttons.append(
            (
                '<form method=post action="/run">'
                '<input type=hidden name=task value="{key}">'
                '<button title="{hint}">{title}</button></form>'
            ).format(key=esc(key), hint=esc(hint), title=esc(title))
        )
    parts.append("<div class=tasks>{}</div>".format("".join(buttons)))

    notes = settings.missing_required()
    if notes:
        items = "".join("<li>{}</li>".format(esc(item)) for item in notes)
        parts.append(
            "<div class=warn><b>Перед запуском стоит знать:</b><ul>{}</ul>"
            '<a href="/settings">Открыть настройки</a></div>'.format(items)
        )

    parts.append(loop_form())

    # Выбор площадок — часть решения «что сейчас собираем», поэтому он здесь, а
    # не в настройках. Без базы блок не рисуется: метрику брать негде.
    if conn is not None:
        import ui_sources

        parts.append(ui_sources.render_sources(conn))

    collect = settings.collect_options()
    outreach_opts = settings.outreach_options()
    prefilter = settings.prefilter_options()
    detector_opts = settings.detector_options()
    parts.append(
        (
            "<p class=muted>Сейчас так: сбор {limit} вакансий, предфильтр {prefilter}, "
            "детектор брехни {detector}, письма от скора {min_score:.0f} "
            "до {letters} штук, модель {llm_state}. "
            '<a href="/settings">Изменить</a></p>'
        ).format(
            limit="без ограничения" if not collect.limit else collect.limit,
            prefilter=(
                "от {:.0f}".format(prefilter.min_score)
                if prefilter.enabled
                else "выключен"
            ),
            detector="включён" if detector_opts.enabled else "выключен",
            min_score=outreach_opts.min_score,
            letters=outreach_opts.limit,
            llm_state="включена" if collect.use_llm else "выключена",
        )
    )

    job = jobs.runner.get(active_id) if active_id else jobs.runner.last()
    if job is None:
        parts.append("<p class=muted>Запусков ещё не было.</p>")
        return "".join(parts), 0

    parts.append(
        (
            "<h2>{title}</h2>"
            "<p class=muted>Состояние: {status} · длится {duration:.0f} с · "
            "строк в логе: {lines}</p>"
        ).format(
            title=esc(job.title),
            status=esc(job.status),
            duration=job.duration,
            lines=len(job.lines),
        )
    )
    parts.append(progress_block(job))

    if job.running:
        parts.append(
            (
                '<form method=post action="/stop">'
                '<input type=hidden name=job value="{id}">'
                "<button class=secondary>Остановить</button></form>"
            ).format(id=job.id)
        )
        if job.task.startswith("collect") and settings.loop_options().enabled:
            parts.append(
                (
                    '<form method=post action="/stop">'
                    '<input type=hidden name=job value="{id}">'
                    '<input type=hidden name=soft value="1">'
                    "<button class=secondary>Остановить после цикла</button></form>"
                ).format(id=job.id)
            )

    parts.append(
        "<pre class=console>{}</pre>".format(
            esc("\n".join(job.tail(400)) or "ждём вывод…")
        )
    )
    parts.append(
        "<p class=muted>Тот же вывод идёт в терминал, где запущен webui.py, и в файл "
        "внутри data/jobs.</p>"
    )

    history = [item for item in jobs.runner.history() if item.id != job.id]
    if history:
        rows = []
        for item in history[:8]:
            rows.append(
                [
                    '<a href="/?job={}">{}</a>'.format(item.id, esc(item.title)),
                    esc(item.status),
                    "{:.0f} с".format(item.duration),
                    esc(time.strftime("%H:%M:%S", time.localtime(item.started_at))),
                ]
            )
        parts.append("<h2>Прошлые запуски</h2>")
        parts.append(table(["Задача", "Итог", "Длительность", "Начало"], rows))

    return "".join(parts), 2 if job.running else 0


__all__ = ("loop_form", "progress_block", "render_run", "save_loop", "stop")
