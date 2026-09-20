"""Раздел «Цели»: компании, выбранные владельцем (ADR-025).

Список — сворачиваемые блоки, а не карточки: целей около десятка, и первый
вопрос к странице «кто у меня в работе», а не «что известно про третью».
Открыл цель — её вакансии видны прямо в списке, закрыл — страница снова
читается сверху одним взглядом.

Кнопка одна: «Собрать все данные» — вакансии, отзывы с оценкой и глубокий
ресёрч одной задачей (поправка к ADR-025 от 20.09.2026). Задача в jobs.py
всё равно идёт одна, и тремя кнопками владелец просто ждал три раза подряд.
Запуск остаётся ручным: цена шагов никуда не делась [CORE-016].

Порядок и фильтр списка приходят формой, а не адресом: у /targets нет
GET-параметров, и выбор живёт в памяти процесса — там же, где кандидаты.
Интерфейс однопользовательский. Имена сверяются со словарями `targets.SORTS`
и `targets.FILTERS`: из браузера в SQL не попадает ни один символ, чужое имя
молча заменяется умолчанием [CORE-017].

В браузер не уходит ничего, кроме id цели. Название компании для подпроцесса
берётся из базы: строку из формы владелец вводит свободную, и в argv ей
не место.
"""

from __future__ import annotations

import sqlite3
from typing import Mapping, Sequence

import deepresearch
import hh_employer
import jobs
import settings
import targets
from ui_core import details, esc, sort_pick, table

# Кандидаты ищутся сетью, поэтому их держим в памяти процесса между двумя
# запросами страницы: интерфейс однопользовательский, база для этого не нужна.
_CANDIDATES: dict[str, list[hh_employer.Employer]] = {}

# Как показан список. Тоже память процесса: это вид, а не данные.
_VIEW = {"sort": "added", "only": "all"}

# Что именно делает единственная кнопка. Список нужен и странице — сказать,
# за что владелец платит одним нажатием.
STEPS = (
    ("vacancies", "Собрать вакансии", "Все вакансии компании на hh.ru, без порога."),
    ("reviews", "Отзывы и оценка", "Досье по отзывам и общая оценка работодателя."),
    ("research", "Глубокий ресёрч", "Реестр, суды, долги, банкротство, новости."),
)

ALL_STEP = (
    "all",
    "Собрать все данные",
    "Вакансии, отзывы с оценкой и глубокий ресёрч — одной задачей.",
)

SORT_LABELS = (
    ("added", "по добавлению"),
    ("company", "по названию"),
    ("fresh", "по новым"),
    ("vacancies", "по вакансиям"),
    ("scan", "по последнему сбору"),
)

ONLY_LABELS = (
    ("all", "все"),
    ("watch", "со слежением"),
    ("fresh", "с новыми"),
    ("unresolved", "без работодателя"),
)


def _client() -> object:
    from hh_html import HHHtmlClient

    return HHHtmlClient(
        pause=settings.as_float(settings.get("HH_PAUSE", "2.0"), 2.0),
        cookie=settings.get("HH_COOKIE", "") or None,
        proxy=settings.get("HH_PROXY", "") or None,
        failure_dir=settings.get("FAILURE_DIR", "data/failures"),
    )


def add_from_input(conn: sqlite3.Connection, text: str) -> str:
    """Название, ссылка или ИНН → цель либо список кандидатов на выбор."""
    from hh_html import BlockedError

    ask = hh_employer.parse_input(text)
    if ask.kind == "inn":
        # По ИНН hh.ru не ищется: это ключ к реестрам, а не к работодателю.
        targets.add(conn, text.strip(), inn=ask.value, source="inn")
        return (
            "Цель добавлена по ИНН. На hh.ru по ИНН ничего не ищется — это ключ к "
            "реестрам, а не к работодателю. Кнопка «Собрать все данные» принесёт "
            "отзывы и ресёрч, а чтобы собрались вакансии, добавь ту же компанию "
            "ещё раз по названию или ссылке — цель не раздвоится, а дополнится."
        )
    client = _client()
    try:
        if ask.kind == "link":
            found = hh_employer.employer(client, ask.value)
        elif ask.kind == "vacancy":
            found = hh_employer.employer_of_vacancy(client, ask.value)
        else:
            options = hh_employer.candidates(client, ask.value)
            if not options:
                return (
                    "На hh.ru такой компании не нашлось. Проверь название или дай "
                    "ссылку на страницу компании или на любую её вакансию. Если "
                    "компания точно есть на hh.ru, сырая страница поиска лежит в "
                    "data/failures — по ней видно, что именно отдал hh.ru."
                )
            if len(options) == 1:
                found = options[0]
            else:
                _CANDIDATES[ask.value] = options
                return ""
    except BlockedError as exc:
        # Молчаливое «не нашлось» на капче — худший ответ: владелец начнёт
        # править название вместо того, чтобы обновить cookie [CORE-014].
        return "hh.ru не пускает: {}".format(exc)
    finally:
        getattr(client, "close", lambda: None)()
    if found is None:
        return (
            "Страница компании не открылась или имя с неё не вынулось: попробуй "
            "ещё раз или добавь по названию."
        )
    targets.add(
        conn, found.name, employer_id=found.id, site=found.link, source=ask.kind
    )
    return "Цель «{}» добавлена.".format(found.name)


def pick(conn: sqlite3.Connection, query: str, employer_id: str) -> str:
    """Владелец выбрал одного из кандидатов."""
    for item in _CANDIDATES.get(query, []):
        if item.id == employer_id:
            targets.add(
                conn, item.name, employer_id=item.id, site=item.link, source="name"
            )
            _CANDIDATES.pop(query, None)
            return "Цель «{}» добавлена.".format(item.name)
    return "Этого кандидата больше нет в списке: поищи заново."


def start_step(conn: sqlite3.Connection, target_id: int, step: str) -> str:
    """Запуск шага задачей. Пустая строка — пошло, иначе текст ошибки."""
    target = targets.get(conn, int(target_id))
    if target is None:
        return "Такой цели нет."
    try:
        if step == "all":
            # Невозможные шаги не проверяем здесь: у цели по ИНН нет
            # работодателя, а DEEP_ENABLED мог быть выключен — задача пропустит
            # такой шаг строкой в логе и доделает остальные.
            jobs.runner.start("target-scan", ["--target", str(target.id), "--step", "all"])
        elif step == "research":
            if not deepresearch.options().enabled:
                return "Глубокий ресёрч выключен настройкой DEEP_ENABLED."
            jobs.runner.start("research-deep", ["--company", target.company, "--force"])
        elif step in ("vacancies", "reviews"):
            if step == "vacancies" and not target.resolved:
                return (
                    "У цели нет работодателя на hh.ru. Добавь её заново по названию "
                    "и выбери компанию из списка."
                )
            jobs.runner.start(
                "target-scan", ["--target", str(target.id), "--step", step]
            )
        else:
            return "Неизвестный шаг."
    except (KeyError, RuntimeError) as exc:
        return str(exc)
    return ""


def last_query(form: Mapping[str, Sequence[str]]) -> str:
    """Что искали: нужно, чтобы показать кандидатов после добавления по имени."""
    text = (form.get("text") or [""])[0]
    ask = hh_employer.parse_input(text)
    return ask.value if ask.kind == "name" else ""


def handle(conn: sqlite3.Connection, path: str, form: Mapping[str, Sequence[str]]) -> str:
    """POST раздела. Возвращает сообщение для страницы (может быть пустым)."""

    def one(key: str) -> str:
        return (form.get(key) or [""])[0]

    if path == "/targets/add":
        return add_from_input(conn, one("text"))
    if path == "/targets/pick":
        return pick(conn, one("query"), one("employer"))
    if path == "/targets/view":
        # Порядок и фильтр: в SQL уходит не значение, а найденное по нему имя.
        _VIEW["sort"] = sort_pick(one("sort"), tuple(targets.SORTS), "added")
        _VIEW["only"] = sort_pick(one("only"), tuple(targets.FILTERS), "all")
        return ""
    if path == "/targets/star":
        # Звёздочка со страницы компании: название приходит из своей же базы.
        targets.add(conn, one("company"), source="run")
        return "Компания «{}» теперь цель.".format(one("company").strip())
    if path == "/targets/watch":
        targets.set_watch(conn, int(one("id") or 0), one("on") == "1")
        return "Слежение {}.".format("включено" if one("on") == "1" else "выключено")
    if path == "/targets/remove":
        targets.remove(conn, int(one("id") or 0))
        return "Цель убрана из списка. Всё собранное о компании осталось в базе."
    if path == "/targets/step":
        problem = start_step(conn, int(one("id") or 0), one("step"))
        return problem or "Шаг запущен: смотри лог на странице «Запуск»."
    return ""


COLLECT_FORM = (
    '<form class=inline method=post action="/targets/step">'
    '<input type=hidden name="id" value="{tid}">'
    '<input type=hidden name="step" value="all">'
    '<button title="{hint}">{label}</button></form>'
)

BLOCK_BTNS = (
    # Цель без слежения не «выключена»: гасить её нечестно, слежение — это
    # только про новые вакансии.
    "<div class=cardbtns>"
    '<a class=chip href="/target?id={tid}">Открыть</a>'
    "{collect}"
    '<form class=inline method=post action="/targets/watch">'
    '<input type=hidden name="id" value="{tid}"><input type=hidden name="on" value="{next_watch}">'
    "<button class=secondary>{watch_label}</button></form>"
    '<form class=inline method=post action="/targets/remove">'
    '<input type=hidden name="id" value="{tid}">'
    "<button class=secondary>Убрать</button></form>"
    "</div>"
)


def _collect_button(target_id: int) -> str:
    return COLLECT_FORM.format(
        tid=int(target_id), hint=esc(ALL_STEP[2]), label=esc(ALL_STEP[1])
    )


def _meta(target: targets.Target, total: int, fresh: int) -> str:
    """Строка под названием цели. Возвращает текст, а не HTML."""
    bits = ["вакансий {}".format(total)]
    if fresh:
        bits.append("новых {}".format(fresh))
    if target.watch:
        bits.append("следим")
    if target.inn:
        bits.append("ИНН {}".format(target.inn))
    if not target.resolved:
        bits.append("работодатель hh.ru не выбран")
    if target.last_scan_at:
        bits.append("смотрели {}".format(target.last_scan_at[:10]))
    return " · ".join(bits)


def _vacancy_table(conn: sqlite3.Connection, target_id: int) -> str:
    """Вакансии цели таблицей. Пусто — объясняем, а не показываем ничего."""
    rows = []
    for row in targets.vacancies(conn, target_id):
        rows.append(
            [
                '<a href="/vacancy?key={key}">{title}</a>{flag}'.format(
                    key=esc(row["key"]),
                    title=esc(row["title"]),
                    flag=' <span class="ok">новая</span>' if row["fresh"] else "",
                ),
                esc(row["area"] or ""),
                "{:.0f}".format(row["score"] or 0),
                esc((row["published_at"] or "")[:10]),
            ]
        )
    if not rows:
        return (
            "<p class=muted>Вакансии ещё не собирались. Кнопка «Собрать все данные» "
            "возьмёт всё, что у компании открыто.</p>"
        )
    return table(["Вакансия", "Город", "Скор", "Опубликована"], rows, raw_head=True)


def _view_button(param: str, key: str, label: str) -> str:
    """Кнопка порядка или фильтра. Формой, потому что у /targets нет параметров."""
    values = dict(_VIEW)
    values[param] = key
    return (
        '<form class=inline method=post action="/targets/view">'
        '<input type=hidden name="sort" value="{sort}">'
        '<input type=hidden name="only" value="{only}">'
        '<button class="{cls}">{label}</button></form>'
    ).format(
        sort=esc(values["sort"]),
        only=esc(values["only"]),
        cls="" if _VIEW[param] == key else "secondary",
        label=esc(label),
    )


def _view_row(title: str, param: str, items: Sequence[tuple[str, str]]) -> str:
    return '<div class=chips><span class=filt>{title}</span>{buttons}</div>'.format(
        title=esc(title),
        buttons="".join(_view_button(param, key, label) for key, label in items),
    )


def render_targets(conn: sqlite3.Connection, note: str = "", query: str = "") -> str:
    """Список целей, форма добавления и — если нужно — выбор кандидата."""
    parts = []
    if note:
        parts.append("<div class=ok>{}</div>".format(esc(note)))
    parts.append(
        "<p class=muted>Цель — компания, которую выбрал ты, а не сбор. Можно "
        "вписать название, вставить ссылку на компанию или вакансию с hh.ru "
        "или указать ИНН. Сбор запускается кнопкой: он стоит времени и внешних "
        "запросов, сам по себе ничего не тратится.</p>"
    )
    parts.append(
        '<form method=post action="/targets/add" class=addrow>'
        '<input type=text name="text" placeholder="Яндекс, hh.ru/employer/1455 или 7736207543" '
        'maxlength="200" required><button>Добавить цель</button></form>'
    )

    options = _CANDIDATES.get(query) or ([] if query else [])
    if options:
        rows = []
        for item in options:
            rows.append(
                [
                    esc(item.name),
                    '<a href="{link}" target=_blank rel=noopener>hh.ru</a>'.format(
                        link=esc(item.link)
                    ),
                    (
                        '<form class=inline method=post action="/targets/pick">'
                        '<input type=hidden name="query" value="{q}">'
                        '<input type=hidden name="employer" value="{eid}">'
                        "<button>Это она</button></form>"
                    ).format(q=esc(query), eid=esc(item.id)),
                ]
            )
        parts.append("<h2>Кандидаты на hh.ru</h2>")
        parts.append(
            "<p class=muted>Одноимённых контор много. Выбери нужную — ресёрч не по "
            "той компании хуже отсутствия ресёрча.</p>"
        )
        parts.append(table(["Компания", "Страница", ""], rows, raw_head=True))

    parts.append(_view_row("Порядок:", "sort", SORT_LABELS))
    parts.append(_view_row("Показать:", "only", ONLY_LABELS))

    found = targets.listing(conn, _VIEW["sort"], _VIEW["only"])
    if not found:
        parts.append(
            "<p class=muted>{}</p>".format(
                "Под этот фильтр ни одна цель не попала."
                if _VIEW["only"] != "all"
                else "Целей пока нет."
            )
        )
        return "".join(parts)
    for target, total, fresh in found:
        parts.append(
            details(
                target.company,
                _meta(target, total, fresh),
                BLOCK_BTNS.format(
                    tid=target.id,
                    collect=_collect_button(target.id),
                    next_watch="0" if target.watch else "1",
                    watch_label="Не следить" if target.watch else "Следить",
                )
                + _vacancy_table(conn, target.id),
            )
        )
    if len(found) > targets.EXPECTED_MAX:
        parts.append(
            "<p class=muted>Целей больше десятка: каждая со слежением — "
            "отдельный обход hh.ru в прогоне.</p>"
        )
    return "".join(parts)


def render_target(conn: sqlite3.Connection, target_id: int, note: str = "") -> str:
    """Одна цель: сбор, вакансии целиком и ссылка на досье."""
    target = targets.get(conn, int(target_id))
    if target is None:
        return "<p>Такой цели нет.</p>"
    total, fresh = targets.counts(conn, target.id)
    targets.seen(conn, target.id)
    parts = ['<p><a href="/targets">← Все цели</a></p>']
    if note:
        parts.append("<div class=ok>{}</div>".format(esc(note)))
    parts.append("<h1>{}</h1>".format(esc(target.company)))
    parts.append(
        '<p class=muted>{meta} · <a href="/company?name={name}">досье и контакты</a></p>'.format(
            meta=esc(_meta(target, total, fresh)), name=esc(target.company)
        )
    )

    parts.append("<div class=tasks>{}</div>".format(_collect_button(target.id)))
    parts.append(
        "<p class=muted>Одной задачей: {}</p>".format(
            " · ".join("{}: {}".format(esc(label), esc(hint)) for _, label, hint in STEPS)
        )
    )
    parts.append(
        "<p class=muted>Невозможный шаг пропускается строкой в логе, остальные "
        "доезжают: цель по ИНН остаётся без вакансий, выключенный DEEP_ENABLED — "
        "без ресёрча. Перезапустить один шаг: "
        "<code>python target_scan.py --target {tid} --step reviews</code>.</p>".format(
            tid=target.id
        )
    )

    parts.append(
        details(
            "Вакансии компании",
            "показаны все, порог профиля не применяется",
            "<p class=muted>Скор рядом — справочно, по лучшему профилю.</p>"
            + _vacancy_table(conn, target.id),
            open_=True,
        )
    )
    return "".join(parts)


def star_form(conn: sqlite3.Connection, company: str) -> str:
    """Звёздочка на странице компании: перенос из прогона в цели."""
    company = (company or "").strip()
    if not company:
        return ""
    if targets.by_company(conn, company) is not None:
        return '<p class=muted>★ Эта компания уже в целях: <a href="/targets">раздел «Цели»</a>.</p>'
    return (
        '<form class=inline method=post action="/targets/star">'
        '<input type=hidden name="company" value="{company}">'
        '<button class=secondary title="Добавить компанию в раздел «Цели»">'
        "★ В цели</button></form>"
    ).format(company=esc(company))


__all__ = (
    "ALL_STEP",
    "ONLY_LABELS",
    "SORT_LABELS",
    "STEPS",
    "add_from_input",
    "handle",
    "last_query",
    "pick",
    "render_target",
    "render_targets",
    "star_form",
    "start_step",
)
