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


# Мета-обновление перезагружает документ целиком, и браузер отматывает и
# страницу, и консоль в начало: следить за живым логом становится нельзя.
# Скрипт помнит обе позиции между обновлениями и, если консоль была внизу,
# держит её внизу — у бегущего лога это ожидаемое поведение.
CONSOLE_JS = """
<script>
(function () {
  var log = document.getElementById("log");
  var store = window.sessionStorage;
  if (!log || !store) { return; }
  var bottom = store.getItem("logbottom") !== "0";
  var at = parseInt(store.getItem("logtop") || "0", 10);
  log.scrollTop = bottom ? log.scrollHeight : at;
  log.addEventListener("scroll", function () {
    var near = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    store.setItem("logbottom", near ? "1" : "0");
    store.setItem("logtop", String(log.scrollTop));
  });
  var page = parseInt(store.getItem("pagetop") || "0", 10);
  if (page) { window.scrollTo(0, page); }
  window.addEventListener("scroll", function () {
    store.setItem("pagetop", String(window.scrollY));
  });
})();
</script>
"""

# По этим словам в логе последнего прогона видно, что hh.ru закрылся капчей.
BLOCK_MARKS = ("капч", "blockederror", "блокирует запросы")


def save_options(form: Mapping[str, Sequence[str]]) -> list[str]:
    """Сохраняет параметры прогона с главной страницы.

    Ключи те же, что на странице настроек: .env один, и прогон читает его на
    старте. Галочки приходят только в отмеченном виде, поэтому отсутствие
    ключа в форме — это «снято», а не «не трогали».
    """
    return settings.save(
        {
            "RUN_LIMIT": str(max(0, settings.as_int((form.get("limit") or ["0"])[0], 0))),
            "RUN_DETAILS": "1" if form.get("details") else "0",
            "TELEGRAM_ENABLED": "1" if form.get("telegram") else "0",
            "OUTREACH_LIMIT": str(
                max(0, settings.as_int((form.get("letters") or ["0"])[0], 0))
            ),
            "OUTREACH_MIN_SCORE": "{:g}".format(
                max(0.0, settings.as_float((form.get("min_score") or ["0"])[0], 0.0))
            ),
        }
    )


def save_cookie(form: Mapping[str, Sequence[str]]) -> list[str]:
    """Свежие cookie hh.ru из предупреждения о капче."""
    cookie = (form.get("cookie") or [""])[0].strip()
    return settings.save({"HH_COOKIE": cookie}) if cookie else []


def options_form() -> str:
    """«Сколько собираем» прямо у кнопки: это решение каждого запуска."""
    collect = settings.collect_options()
    outreach = settings.outreach_options()
    telegram = settings.flag("TELEGRAM_ENABLED")
    return (
        '<form method=post action="/runopts" class=tasks>'
        '<label>собрать вакансий <input type=number name=limit min=0 size=4 '
        'value="{limit}"></label> '
        "<label><input type=checkbox name=details value=1 {details}> "
        "догружать описания</label> "
        "<label><input type=checkbox name=telegram value=1 {telegram}> "
        "карточки в Telegram</label> "
        '<label>письма от скора <input type=number name=min_score min=0 size=3 '
        'value="{min_score:g}"></label> '
        '<label>до <input type=number name=letters min=0 size=3 value="{letters}"> '
        "штук</label> "
        "<button class=secondary>Сохранить</button></form>"
        "<p class=muted>Ноль вакансий — сбор без ограничения. Ноль писем — аутрич "
        "не работает вовсе.</p>"
    ).format(
        limit=collect.limit,
        details="checked" if collect.details else "",
        telegram="checked" if telegram else "",
        min_score=outreach.min_score,
        letters=outreach.limit,
    )


def cookie_block(job: "jobs.Job | None") -> str:
    """Поле cookie показывается там, где видно капчу, и только тогда.

    Постоянное поле секрета на главной — это приглашение его туда вставить
    просто так; в предупреждении оно отвечает на уже случившуюся беду.
    """
    if job is None:
        return ""
    tail = "\n".join(job.tail(200)).lower()
    if not any(mark in tail for mark in BLOCK_MARKS):
        return ""
    return (
        "<div class=warn><b>hh.ru закрылся капчей.</b> Открой hh.ru в браузере, "
        "пройди проверку, скопируй строку Cookie из инструментов разработчика и "
        "вставь сюда — прогон возьмёт её на следующем запуске."
        '<form method=post action="/cookie" class=tasks>'
        '<input type=text name=cookie size=60 placeholder="hhuid=…; hhtoken=…">'
        "<button>Сохранить cookie</button></form></div>"
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

    parts.append(options_form())

    collect = settings.collect_options()
    prefilter = settings.prefilter_options()
    detector_opts = settings.detector_options()
    parts.append(
        (
            "<p class=muted>Остальное: предфильтр {prefilter}, детектор брехни "
            "{detector}, модель {llm_state}. "
            '<a href="/settings">Изменить</a></p>'
        ).format(
            prefilter=(
                "от {:.0f}".format(prefilter.min_score)
                if prefilter.enabled
                else "выключен"
            ),
            detector="включён" if detector_opts.enabled else "выключен",
            llm_state="включена" if collect.use_llm else "выключена",
        )
    )

    job = jobs.runner.get(active_id) if active_id else jobs.runner.last()
    parts.append(cookie_block(job))
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
        '<pre class=console id=log>{}</pre>'.format(
            esc("\n".join(job.tail(400)) or "ждём вывод…")
        )
    )
    parts.append(CONSOLE_JS)
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


__all__ = (
    "CONSOLE_JS",
    "cookie_block",
    "loop_form",
    "options_form",
    "progress_block",
    "render_run",
    "save_cookie",
    "save_loop",
    "save_options",
    "stop",
)
