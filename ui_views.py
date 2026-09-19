"""Страницы: запуск, настройки, вакансии, одна вакансия, контакты.

Ни одна функция здесь не знает про HTTP: на вход — соединение с базой и параметры,
на выход — готовый HTML. За счёт этого страницы проверяются тестами без сервера.

В шаблонах только str.format и только одинарные кавычки снаружи: внутри HTML живут
двойные, и смешивание двух видов кавычек в одной склейке уже давало SyntaxError.
"""

from __future__ import annotations

import sqlite3
import time
import urllib.parse
from typing import Sequence

import conditions
import contact_finds
import contacts
import db
import detector
import jobs
import llm
import market
import market_rules
import outreach
import settings
import websearch
from ui_core import esc, sort_head, sort_pick, table


# ———— запуск ————


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


def render_run(active_id: int | None = None, note: str = "") -> tuple[str, int]:
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
            limit=collect.limit,
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
                '<input type=hidden name=job value="{}">'
                "<button class=secondary>Остановить</button></form>"
            ).format(job.id)
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


# ———— настройки ————


def render_settings(saved: Sequence[str] = ()) -> str:
    values = settings.load()
    parts = []

    if saved:
        parts.append("<div class=ok>Сохранено: {}</div>".format(esc(", ".join(saved))))

    notes = settings.missing_required()
    if notes:
        items = "".join("<li>{}</li>".format(esc(item)) for item in notes)
        parts.append("<div class=warn><ul>{}</ul></div>".format(items))

    parts.append(
        "<p class=muted>Всё сохраняется в {}. Секреты показаны маской: пустое поле "
        "оставляет текущее значение, слово «очистить» стирает его.</p>".format(
            esc(settings.ENV_PATH)
        )
    )
    parts.append('<form method=post action="/settings">')

    for group, fields in settings.groups():
        parts.append("<h2>{}</h2>".format(esc(group)))
        for field in fields:
            current = values.get(field.key, "")
            hint = field.help
            if field.kind == settings.BOOL:
                on = settings.as_bool(current, settings.as_bool(field.default))
                control = (
                    '<label><input type=checkbox name="{key}" value="1"{checked}> '
                    "{label}</label>"
                ).format(
                    key=esc(field.key),
                    checked=" checked" if on else "",
                    label=esc(field.label),
                )
                parts.append(
                    (
                        "<div class=field>{control}"
                        "<div class=hint>{key} · {hint}</div></div>"
                    ).format(control=control, key=esc(field.key), hint=esc(hint))
                )
                continue

            if field.is_secret:
                shown = ""
                placeholder = settings.mask(current)
                input_type = "password"
            else:
                shown = current or field.default
                placeholder = field.default
                input_type = (
                    "number" if field.kind in (settings.INT, settings.FLOAT) else "text"
                )

            step = ""
            if field.kind == settings.INT:
                step = ' step="1"'
            elif field.kind == settings.FLOAT:
                step = ' step="any"'

            parts.append(
                (
                    "<div class=field><label>{label}</label>"
                    '<input type="{input_type}" name="{key}" value="{value}" '
                    'placeholder="{placeholder}"{step}>'
                    "<div class=hint>{key} · {hint}</div></div>"
                ).format(
                    label=esc(field.label),
                    input_type=input_type,
                    key=esc(field.key),
                    value=esc(shown),
                    placeholder=esc(placeholder),
                    step=step,
                    hint=esc(hint),
                )
            )

    parts.append("<p><button>Сохранить</button></p></form>")
    parts.append(
        "<p class=muted>Новые значения подхватываются со следующего запуска задачи: "
        "каждая задача — отдельный процесс со свежим .env.</p>"
    )
    return "".join(parts)


# ———— выдача ————


def vacancy_rows(
    conn: sqlite3.Connection, min_score: float = 0.0, limit: int = 50
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT key, title, company, score, url, published_at, last_seen_at, notified_at
        FROM vacancies
        WHERE score >= ?
        ORDER BY score DESC, last_seen_at DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()


def vacancy_one(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vacancies WHERE key = ?", (key,)).fetchone()


def contact_rows(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT key, company, person, role, role_rank, channel_kind, channel_value,
               confidence, guessed, status, created_at, notes
        FROM contacts
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def draft_button(key: str, label: str = "Подготовить письмо") -> str:
    """Кнопка подготовки письма. POST, потому что шаг ходит в поиск и модель."""
    return (
        '<form method=post action="/vacancy" class=inline>'
        '<input type=hidden name=key value="{key}">'
        '<button type=submit>{label}</button></form>'
    ).format(key=esc(key), label=esc(label))


def contacts_block(conn: sqlite3.Connection, key: str, company: str | None) -> str:
    """Найденные каналы по вакансии. Ничего не запускает: показывает собранное."""
    find = contact_finds.load(conn, key, company)
    if find is None:
        return (
            "<h2>Контакты</h2><p class=muted>Контакты по этой вакансии ещё не искали: "
            'они собираются вместе с вакансиями на <a href="/">сборе</a>, '
            "после досье на компанию.</p>"
        )
    if not find.candidates:
        lines = "".join("<li>{}</li>".format(esc(text)) for text in find.dropped[:5])
        return (
            "<h2>Контакты</h2><p class=muted>Рабочего канала не нашлось — остаётся "
            "отклик через площадку.</p>" + ("<ul>{}</ul>".format(lines) if lines else "")
        )
    body = []
    for cand in find.candidates:
        source = "—"
        if cand.source_url:
            source = '<a href="{url}" target=_blank rel=noreferrer>источник</a>'.format(
                url=esc(cand.source_url)
            )
        body.append(
            [
                esc(cand.person or "—"),
                esc(cand.role or "—"),
                "<span class=pill>{}</span>{}".format(
                    esc(cand.channel_kind), esc(cand.channel_value)
                ),
                esc(contacts.CONFIDENCE_RU.get(cand.confidence, cand.confidence))
                + (" · угадан" if cand.guessed else ""),
                source,
            ]
        )
    dropped = ""
    if find.dropped:
        dropped = "<p class=muted>Отброшено: {}</p>".format(
            esc("; ".join(find.dropped[:3]))
        )
    return (
        "<h2>Контакты</h2>"
        + table(["Человек", "Роль", "Канал", "Уверенность", "Откуда"], body)
        + dropped
    )


VACANCY_SORTS: dict[str, object] = {
    "score": lambda r: -float(r["score"] or 0),
    "title": lambda r: str(r["title"] or "").lower(),
    "company": lambda r: str(r["company"] or "").lower(),
    "published": lambda r: str(r["published_at"] or ""),
}
VACANCY_COLUMNS = (
    ("score", "Скор"),
    ("title", "Вакансия"),
    ("company", "Компания"),
    ("published", "Опубликована"),
    ("", "В TG"),
    ("", "Письмо"),
)

LOG_SORTS: dict[str, object] = {
    "company": lambda r: str(r["company"] or "").lower(),
    "person": lambda r: str(r["person"] or "я").lower(),
    "role": lambda r: int(r["role_rank"] or 99),
    "status": lambda r: str(r["status"] or ""),
    "created": lambda r: str(r["created_at"] or ""),
}
LOG_COLUMNS = (
    ("company", "Компания"),
    ("person", "Человек"),
    ("role", "Роль"),
    ("", "Канал"),
    ("", "Уверенность"),
    ("status", "Статус"),
    ("created", "Записан"),
)


def sort_rows(rows: list, keys: dict, sort: str) -> list:
    """Даты и числа читаются сверху вниз, поэтому у них порядок обратный."""
    rows = sorted(rows, key=keys[sort])
    if sort in ("published", "created"):
        rows.reverse()
    return rows


def render_vacancies(
    conn: sqlite3.Connection, min_score: float, limit: int, sort: str = "score"
) -> str:
    sort = sort_pick(sort, tuple(VACANCY_SORTS), "score")
    rows = sort_rows(list(vacancy_rows(conn, min_score, limit)), VACANCY_SORTS, sort)
    stats = db.stats(conn)
    direct, total = contacts.coverage(conn)
    with_conditions, scanned = conditions.coverage(conn)

    form = (
        '<form method=get action="/vacancies">'
        'Скоринг от <input type=number step=1 name=min_score value="{min_score}" '
        'style="width:90px"> '
        'показать <input type=number step=10 name=limit value="{limit}" '
        'style="width:90px"> '
        "<button>Применить</button></form>"
    ).format(min_score=int(min_score), limit=int(limit))

    summary = (
        "<p class=muted>В базе: {vacancies} вакансий. Разобраны условия: {conds} из "
        "{scanned}. Прямых контактов: {direct} из {total}. Найдено по фильтру: {found}.</p>"
    ).format(
        vacancies=stats.get("vacancies", 0),
        conds=with_conditions,
        scanned=scanned,
        direct=direct,
        total=total,
        found=len(rows),
    )

    if not rows:
        return (
            form
            + summary
            + "<div class=warn>Нет вакансий под фильтр. Если база пуста — сначала "
            '<a href="/">сбор</a>.</div>'
        )

    body = []
    for row in rows:
        score = float(row["score"] or 0)
        link = '<a href="/vacancy?key={}">{}</a>'.format(
            urllib.parse.quote(row["key"] or ""), esc(row["title"])
        )
        body.append(
            [
                "<span class=score>{:.0f}</span>".format(score),
                link,
                esc(row["company"]),
                esc((row["published_at"] or "")[:10]),
                "✓" if row["notified_at"] else "",
                draft_button(row["key"] or "", "Письмо"),
            ]
        )
    base = "/vacancies?min_score={}&limit={}".format(int(min_score), int(limit))
    return (
        form
        + summary
        + table(sort_head(VACANCY_COLUMNS, base, "sort", sort), body, raw_head=True)
    )


MARKET_CLASS = {
    market_rules.BELOW: "danger",
    market_rules.IN_MARKET: "ok",
    market_rules.ABOVE: "warn",
    market_rules.NO_SALARY: "muted",
    market_rules.UNKNOWN: "muted",
}


def render_vacancy(conn: sqlite3.Connection, key: str, with_draft: bool) -> str:
    row = vacancy_one(conn, key)
    if row is None:
        return "<p>Вакансия не найдена.</p>"

    parts = [
        (
            "<p><b>{company}</b> · скоринг <span class=score>{score:.0f}</span> · "
            '<a href="{url}" target=_blank rel=noreferrer>открыть на hh.ru</a></p>'
        ).format(
            company=esc(row["company"]),
            score=float(row["score"] or 0),
            url=esc(row["url"]),
        )
    ]

    market_line = market.row_line(row)
    if market_line:
        parts.append(
            '<h2>Рынок</h2><div class="{cls}">{line}</div>'
            "<p class=muted>Медиана считается по нашим наблюдениям с hh.ru за окно, "
            "а не по рынку труда целиком. Срез и число вакансий — на странице компании."
            "</p>".format(
                cls=MARKET_CLASS.get(str(row["market_label"] or ""), "muted"),
                line=esc(market_line),
            )
        )

    if row["score_reasons"]:
        parts.append(
            "<h2>Почему такой скор</h2><pre>{}</pre>".format(esc(row["score_reasons"]))
        )

    condition_lines = conditions.lines(conn, key)
    if condition_lines:
        parts.append(
            "<h2>Условия из описания</h2><pre>{}</pre>".format(
                esc("\n".join(condition_lines))
            )
        )
        parts.append(
            "<p class=muted>Каждая строка осталась только потому, что цитата нашлась "
            "в тексте дословно.</p>"
        )

    signal_lines = detector.load_lines(conn, key)
    if signal_lines:
        parts.append(
            "<h2>HR-флаги</h2><pre>{}</pre>".format(esc("\n".join(signal_lines)))
        )

    parts.append(contacts_block(conn, key, row["company"]))

    if with_draft:
        skip_precondition = outreach.precondition(conn, row)
    if with_draft and skip_precondition:
        parts.append(
            "<h2>Черновик</h2><div class=warn>Пропуск: {}</div>".format(
                esc(skip_precondition)
            )
        )
    elif with_draft:
        provider = websearch.SearchProvider.from_env(conn)
        gateway = llm.Gateway.from_env(conn) if settings.flag("LLM_ENABLED") else None
        # Те же факты, что у CLI: сначала подтверждённые блоки резюме (B-01).
        facts = outreach.collect_facts(conn, settings.get("RUN_PROFILE", "profile.yaml"))
        options = settings.outreach_options()
        discovery, draft, skip_reason = outreach.process_row(
            conn,
            row,
            facts,
            provider,
            check_mx=options.check_mx,
            allow_generic=options.allow_generic,
            gateway=gateway,
        )
        if skip_reason:
            parts.append(
                "<h2>Черновик</h2><div class=warn>Пропуск: {}</div>".format(
                    esc(skip_reason)
                )
            )
        else:
            card = outreach.format_card(
                row, discovery, draft, signal_lines, condition_lines
            )
            parts.append("<h2>Карточка и черновик</h2><pre>{}</pre>".format(esc(card)))
            if not facts:
                parts.append(
                    "<div class=warn>Блок facts пуст — в письме заглушка вместо повода "
                    'писать. <a href="/profile">Заполнить</a></div>'
                )
    else:
        parts.append(
            "<h2>Письмо</h2>"
            + draft_button(key)
            + "<p class=muted>Собирает черновик по найденным контактам: может дёрнуть "
            "модель, ничего не отправляет.</p>"
        )

    parts.append("<h2>Описание</h2><pre>{}</pre>".format(esc(row["description"])))
    return "".join(parts)


def render_contacts(conn: sqlite3.Connection, sort: str = "created") -> str:
    """Лог аутрича: что уже ушло в работу. Находки сборщика — в карточке вакансии."""
    sort = sort_pick(sort, tuple(LOG_SORTS), "created")
    contacts.ensure_schema(conn)
    rows = sort_rows(list(contact_rows(conn)), LOG_SORTS, sort)
    direct, total = contacts.coverage(conn)
    found_direct, found_total = contact_finds.coverage(conn)
    header = (
        "<h2>Контакты</h2>"
        "<p class=muted>Каналы найдены у {found_direct} из {found_total} вакансий "
        "(ищутся при общем сборе). В работе: {direct} из {total}.</p>"
    ).format(
        found_direct=found_direct, found_total=found_total, direct=direct, total=total
    )

    if not rows:
        return header + (
            "<div class=warn>В работу ещё ничего не брали. Письмо готовится кнопкой "
            '«Письмо» рядом с вакансией на <a href="/vacancies">странице вакансий</a>.</div>'
        )

    body = []
    for r in rows:
        channel = "<span class=pill>{}</span>{}".format(
            esc(r["channel_kind"]), esc(r["channel_value"])
        )
        confidence = esc(r["confidence"]) + (" · угадан" if r["guessed"] else "")
        body.append(
            [
                esc(r["company"]),
                esc(r["person"] or "—"),
                esc(r["role"] or "—"),
                channel,
                confidence,
                esc(r["status"]),
                esc((r["created_at"] or "")[:10]),
            ]
        )
    return header + table(
        sort_head(LOG_COLUMNS, "/companies", "ksort", sort), body, raw_head=True
    )


__all__ = (
    "contact_rows",
    "progress_block",
    "render_contacts",
    "sort_rows",
    "render_run",
    "render_settings",
    "render_vacancies",
    "render_vacancy",
    "vacancy_one",
    "vacancy_rows",
)
