"""Страницы: настройки, вакансии, одна вакансия, контакты.

Страница запуска живёт в ui_run.py и реэкспортируется отсюда.

Ни одна функция здесь не знает про HTTP: на вход — соединение с базой и параметры,
на выход — готовый HTML. За счёт этого страницы проверяются тестами без сервера.

В шаблонах только str.format и только одинарные кавычки снаружи: внутри HTML живут
двойные, и смешивание двух видов кавычек в одной склейке уже давало SyntaxError.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from typing import Sequence

import conditions
import contact_finds
import contacts
import db
import detector
import llm
import aitext
import market
import market_rules
import outreach
import settings
import websearch
from ui_core import esc, sort_head, sort_pick, table
from ui_run import (  # noqa: F401 — реэкспорт: страница запуска живёт в ui_run.py
    loop_form,
    progress_block,
    render_run,
)


# ———— настройки ————


SETTINGS_SEARCH = """
<div class=field>
<input type=search id=setq autocomplete=off
 placeholder="Поиск по настройкам: название, ключ или слово из подсказки">
<div class=hint id=setq-note>Группы свёрнуты: разверни нужную или начни искать.</div>
</div>
"""

# Скрипт идёт после формы: на момент выполнения подкаты должны уже существовать.
SETTINGS_SEARCH_JS = """
<script>
(function () {
  var box = document.getElementById("setq");
  var note = document.getElementById("setq-note");
  var groups = [].slice.call(document.querySelectorAll("details.setgroup"));
  function apply() {
    var q = box.value.trim().toLowerCase();
    var found = 0;
    groups.forEach(function (group) {
      var shown = 0;
      [].slice.call(group.querySelectorAll("[data-find]")).forEach(function (field) {
        var hit = !q || field.dataset.find.indexOf(q) >= 0;
        field.hidden = !hit;
        if (hit) { shown += 1; }
      });
      group.hidden = Boolean(q) && !shown;
      // Пустой запрос возвращает исходное состояние, а не «всё открыто»:
      // иначе после стирания строки страница остаётся километровой.
      group.open = q ? shown > 0 : group.dataset.open === "1";
      found += shown;
    });
    if (!q) {
      note.textContent = "Группы свёрнуты: разверни нужную или начни искать.";
    } else {
      note.textContent = found ? "Найдено настроек: " + found : "Ничего не нашлось.";
    }
  }
  box.addEventListener("input", apply);
  apply();
})();
</script>
"""


def settings_field(field: "settings.Field", current: str) -> str:
    """Одна настройка в форме. data-find — то, по чему её ищет строка поиска."""
    found = " ".join((field.label, field.key, field.help, field.group)).lower()

    if field.kind == settings.BOOL:
        on = settings.as_bool(current, settings.as_bool(field.default))
        control = (
            '<label><input type=checkbox name="{key}" value="1"{checked}> {label}</label>'
        ).format(
            key=esc(field.key),
            checked=" checked" if on else "",
            label=esc(field.label),
        )
        return (
            '<div class=field data-find="{found}">{control}'
            "<div class=hint>{key} · {hint}</div></div>"
        ).format(
            found=esc(found), control=control, key=esc(field.key), hint=esc(field.help)
        )

    if field.is_secret:
        shown = ""
        placeholder = settings.mask(current)
        input_type = "password"
    else:
        shown = current or field.default
        placeholder = field.default
        input_type = "number" if field.kind in (settings.INT, settings.FLOAT) else "text"

    step = ""
    if field.kind == settings.INT:
        step = ' step="1"'
    elif field.kind == settings.FLOAT:
        step = ' step="any"'

    return (
        '<div class=field data-find="{found}"><label>{label}</label>'
        '<input type="{input_type}" name="{key}" value="{value}" '
        'placeholder="{placeholder}"{step}>'
        "<div class=hint>{key} · {hint}</div></div>"
    ).format(
        found=esc(found),
        label=esc(field.label),
        input_type=input_type,
        key=esc(field.key),
        value=esc(shown),
        placeholder=esc(placeholder),
        step=step,
        hint=esc(field.help),
    )


def render_settings(saved: Sequence[str] = ()) -> str:
    """Настройки подкатами: шесть десятков полей одним списком не читаются.

    Раскрыта только группа запуска — та, куда ходят чаще всего. Остальные
    разворачиваются руками или сами, когда их поля попали в поиск. Поля
    свёрнутых групп остаются в форме и сохраняются как обычно: details прячет
    их визуально, браузер их всё равно отправляет.
    """
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
    parts.append(SETTINGS_SEARCH)
    parts.append('<form method=post action="/settings">')

    for position, (group, fields) in enumerate(settings.groups()):
        opened = position == 0
        parts.append(
            (
                '<details class=setgroup data-open="{flag}"{attr}>'
                "<summary>{group} <span class=muted>· настроек: {count} · {hint}</span>"
                "</summary>"
            ).format(
                flag="1" if opened else "0",
                attr=" open" if opened else "",
                group=esc(group),
                count=len(fields),
                hint=esc(settings.GROUP_HINTS.get(group, "")),
            )
        )
        for field in fields:
            parts.append(settings_field(field, values.get(field.key, "")))
        parts.append("</details>")

    parts.append("<p><button>Сохранить</button></p></form>")
    parts.append(SETTINGS_SEARCH_JS)
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

    ai_line = aitext.row_line(row)
    if ai_line:
        parts.append(
            '<h2>Текст описания</h2><div class="warn">{line}</div>'
            "<p class=muted>Это свойство текста, а не вывод о происхождении: "
            "детекторы сгенерированного текста ненадёжны. Смысл сигнала в том, "
            "что проверять в описании нечего.</p>".format(line=esc(ai_line))
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
