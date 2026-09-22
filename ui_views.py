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
import embeddings_tasks
import contacts
import db
import detector
import company_score_store
import filters
import injection_store
import llm
import ui_filters
import aitext
import market
import market_rules
import outreach
import profiles
import settings
import source_store
import sources
import websearch
from ui_core import esc, live_search, sort_head, sort_pick, table
from ui_run import (  # noqa: F401 — реэкспорт: страница запуска живёт в ui_run.py
    loop_form,
    progress_block,
    render_run,
)
from ui_settings import (  # noqa: F401 — реэкспорт: настройки живут в ui_settings.py
    render_settings,
    settings_field,
)


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


def filtered_vacancies(
    conn: sqlite3.Connection, params: dict, limit: int
) -> tuple[list[sqlite3.Row], int, list[tuple[str, str]]]:
    """(строки, сколько всего подошло, активные фильтры).

    Фильтр и сортировка уходят в SQL до LIMIT. Раньше страница брала первые N
    по скору и сортировала уже их: «по зарплате» показывало самую денежную из
    верхушки, а не из базы.
    """
    # Фильтры заглядывают в соседние таблицы (контакты, инъекции, сигналы,
    # оценки компаний). На базе, собранной версией без них, страница не должна
    # падать: CREATE IF NOT EXISTS дешевле, чем обработка «no such table».
    filters.ensure_tables(conn)
    where, args, active = filters.build_where(filters.VACANCY_FILTERS, params)
    order = filters.order_by(
        filters.VACANCY_SORTS, str(params.get("sort", "") or ""), "score"
    )
    total = conn.execute(
        "SELECT COUNT(*) FROM vacancies v WHERE {}".format(where), args
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT v.* FROM vacancies v WHERE {} ORDER BY {} LIMIT ?".format(where, order),
        [*args, limit],
    ).fetchall()
    return rows, int(total or 0), active


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


# Сортировки вакансий переехали в filters.VACANCY_SORTS: они стали кусками
# ORDER BY, потому что сортировать урезанный LIMIT-ом срез — значит показывать
# «самую денежную из верхушки по скору» и называть это «по зарплате».
VACANCY_COLUMNS = (
    ("score", "Скор"),
    ("title", "Вакансия"),
    ("company", "Компания"),
    ("", "Площадка"),
    ("salary", "Зарплата"),
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
    conn: sqlite3.Connection,
    min_score: float = 0.0,
    limit: int = 50,
    sort: str = "score",
    params: dict | None = None,
) -> str:
    """Список вакансий: быстрые виды, фильтры, сортировка.

    Старые позиционные аргументы оставлены: их зовут тесты и прежние ссылки.
    Всё остальное приходит словарём параметров адреса.
    """
    query = dict(params or {})
    if params is not None:
        # Адрес без параметров — это вид «Подходящие», а не «всё подряд». Иначе
        # первое, что видит владелец, — вакансии ниже порога профиля, на
        # компании которых досье никто не собирал: вакансия есть, работодателя
        # нет, и это выглядит поломкой. «Все» остаётся одним щелчком рядом.
        # params=None означает старый вызов «покажи от min_score»: его зовут
        # прежние ссылки и тесты, и вид им не навязывается.
        query.setdefault("view", filters.FIT)
    query = ui_filters.apply_preset(filters.VACANCY_PRESETS, query)
    if query.get("min_score") == filters.FIT:
        query["min_score"] = "{:.0f}".format(profiles.dossier_threshold())
    if min_score and "min_score" not in query:
        query["min_score"] = str(min_score)
    query.setdefault("sort", sort)
    limit = max(1, min(1000, int(settings.as_int(query.get("limit", ""), limit) or limit)))
    query["limit"] = str(limit)

    rows, found, active = filtered_vacancies(conn, query, limit)
    stats = db.stats(conn)
    direct, total = contacts.coverage(conn)

    head = (
        ui_filters.presets_line(
            filters.VACANCY_PRESETS, query, "/vacancies", default=filters.FIT
        )
        + live_search(
            "/vacancies",
            "vq",
            "vlist",
            value=str(query.get("q", "") or ""),
            hidden={
                key: value
                for key, value in query.items()
                if key not in ("q", "limit") and value
            },
        )
        + ui_filters.chips(active, query, "/vacancies")
        + ui_filters.form(
            filters.VACANCY_FILTERS,
            query,
            "/vacancies",
            len(active),
            hidden={"sort": query.get("sort", ""), "limit": query.get("limit", "")},
        )
        + ui_filters.sort_line(filters.VACANCY_SORTS, query, "/vacancies")
    )

    # Порог профиля здесь же: без него «19 вакансий, а досье 5» выглядит
    # поломкой, хотя это два разных порога (docs/dossier.md).
    threshold = profiles.dossier_threshold()
    summary = (
        "<p class=muted>В базе {vacancies} вакансий · под фильтр подошло "
        "{found} · показано {shown} · прямых контактов {direct} из {total}. "
        "Досье, контакты и письма — только от {threshold:.0f} баллов.</p>"
    ).format(
        vacancies=stats.get("vacancies", 0),
        found=found,
        shown=len(rows),
        direct=direct,
        total=total,
        threshold=threshold,
    )

    if not rows:
        return (
            head
            + summary
            + "<div class=warn>Под фильтр не попало ничего. Сними условия выше "
            'или начни со <a href="/">сбора</a>, если база пуста.</div>'
        )

    marks = _source_marks(conn, [str(row["key"] or "") for row in rows])
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
                _source_cell(row, marks),
                _salary_cell(row),
                esc((row["published_at"] or "")[:10]),
                "✓" if row["notified_at"] else "",
                draft_button(row["key"] or "", "Письмо"),
            ]
        )
    base = "/vacancies?" + filters.query_string(query, drop="sort")
    return (
        head
        + summary
        + '<div id=vlist>'
        + table(
            sort_head(VACANCY_COLUMNS, base, "sort", query.get("sort", "score")),
            body,
            raw_head=True,
        )
        + "</div>"
    )


def _source_marks(
    conn: sqlite3.Connection, keys: Sequence[str]
) -> dict[str, list[str]]:
    """Ключ вакансии → площадки, где её видели. Одним запросом на всю страницу."""
    out: dict[str, list[str]] = {}
    keys = [key for key in keys if key]
    if not keys:
        return out
    source_store.ensure_schema(conn)
    holes = ",".join("?" * len(keys))
    for key, source in conn.execute(
        "SELECT key, source FROM vacancy_sources WHERE key IN ({})".format(holes),
        list(keys),
    ):
        out.setdefault(str(key), []).append(str(source))
    return out


def _source_cell(row: sqlite3.Row, marks: dict[str, list[str]]) -> str:
    """Главная площадка и сколько ещё её видели.

    Главная — та же, что в ссылке вакансии (`sources.PRIORITY`). «+1» рядом
    значит, что вакансия висит не на одной площадке: это не дубль в базе, а
    одна запись с несколькими адресами (docs/sources.md). Пусто — запись
    старая, собранная до разметки площадок, и додумывать за неё нечего
    [CORE-019].
    """
    seen = marks.get(str(row["key"] or ""), [])
    main = str(row["source"] or "")
    if not main:
        for source in seen:
            main = sources.main_source(main, source) if main else source
    if not main:
        return '<span class=muted>—</span>'
    extra = len([source for source in seen if source != main])
    tail = ' <span class=pill title="{}">+{}</span>'.format(
        esc(", ".join(sources.label_of(source) for source in seen if source != main)),
        extra,
    ) if extra else ""
    return esc(sources.label_of(main)) + tail


def _salary_cell(row: sqlite3.Row) -> str:
    """Вилка как есть. Пусто — значит работодатель её не показал, и это факт."""
    low, high = row["salary_from"], row["salary_to"]
    if low is None and high is None:
        return '<span class=muted>скрыта</span>'
    parts = [format(int(value), ",d").replace(",", " ") for value in (low, high) if value]
    return esc(" — ".join(parts))


MARKET_CLASS = {
    market_rules.BELOW: "danger",
    market_rules.IN_MARKET: "ok",
    market_rules.ABOVE: "warn",
    market_rules.NO_SALARY: "muted",
    market_rules.UNKNOWN: "muted",
}


def _open_links(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Ссылки «открыть на …» — по всем площадкам, где вакансию видели.

    Раньше подпись была «открыть на hh.ru» литералом, и вакансия с Работы.ру
    выглядела как hh-евская. Главная ссылка идёт первой (она же в `vacancies`),
    остальные адреса берутся из `vacancy_sources`: одна и та же вакансия на двух
    сайтах — это одна запись с двумя адресами (`docs/sources.md`).
    """
    main = str(row["url"] or "")
    main_source = str(row["source"] or "")
    seen: list[tuple[str, str]] = []
    if main:
        seen.append((main_source, main))
    source_store.ensure_schema(conn)
    for site, url in conn.execute(
        "SELECT source, url FROM vacancy_sources WHERE key = ? ORDER BY source",
        (str(row["key"] or ""),),
    ):
        url = str(url or "")
        if url and url != main:
            seen.append((str(site), url))
    links = [
        '<a href="{url}" target=_blank rel=noreferrer>открыть на {label}</a>'.format(
            url=esc(url), label=esc(sources.label_of(site) if site else "площадке")
        )
        for site, url in seen
    ]
    return " · ".join(links)


def render_vacancy(conn: sqlite3.Connection, key: str, with_draft: bool) -> str:
    row = vacancy_one(conn, key)
    if row is None:
        return "<p>Вакансия не найдена.</p>"

    parts = [
        "<p><b>{company}</b> · скоринг <span class=score>{score:.0f}</span>{links}</p>".format(
            company=esc(row["company"]),
            score=float(row["score"] or 0),
            links=" · " + _open_links(conn, row) if row["url"] else "",
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

    parts.append(similar_block(conn, key))
    parts.append("<h2>Описание</h2><pre>{}</pre>".format(esc(row["description"])))
    return "".join(parts)


def similar_block(conn: sqlite3.Connection, key: str) -> str:
    """Похожие вакансии по векторам (ADR-021). Выключено — пустая строка.

    Показываем то, что уже посчитано прогоном: страница не будит эмбеддер.
    Полезно ровно одним — видно «ту же вакансию» под другим названием, которую
    ключ title+company считает новой.
    """
    gateway = llm.Gateway.from_env(conn) if settings.flag("LLM_ENABLED") else None
    found = embeddings_tasks.similar_vacancies(conn, gateway, key)
    if not found:
        return ""
    rows = []
    for other, score in found:
        row = vacancy_one(conn, other)
        if row is None:
            continue
        link = '<a href="/vacancy?key={}">{}</a>'.format(esc(other), esc(row["title"]))
        rows.append([link, esc(row["company"] or ""), "{:.0%}".format(score)])
    if not rows:
        return ""
    return (
        "<h2>Похожие вакансии</h2>"
        + table(["Вакансия", "Компания", "Близость"], rows)
        + "<p class=muted>Сравниваются векторы названия и описания. На скоринг и "
        "оценку работодателя не влияет.</p>"
    )


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
    "similar_block",
    "render_vacancies",
    "render_vacancy",
    "vacancy_one",
    "vacancy_rows",
)
