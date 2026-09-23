"""Раздел «Цели»: компании, выбранные владельцем (ADR-025).

Страница устроена как список карточек — те же карточки, что и у профилей, и по
той же причине: целей около десятка, и первый вопрос к странице «кто у меня в
работе», а не «что известно про третью».

У цели одна кнопка «Собрать всё»: вакансии, адреса для карты, отзывы с оценкой
и глубокий ресёрч по очереди. Раньше кнопок было три, но владелец всё равно
жал их одну за другой, а три задачи вместо одной — три захода в интерфейс.
Упавший шаг не отменяет следующие: за это отвечает `target_scan.scan_all`
[CORE-017].

Внутри цели вакансий бывает тысяча: Москва, Казань и Урюпинск вперемешку, скор
от нуля до восьмидесяти. Поэтому над таблицей стоит отбор: город, порог
скора, слово в названии и порядок. Отбор ничего не удаляет и ничего не
запрашивает в сети — это только про то, что видно на экране.

Выбранный отбор хранится в памяти процесса (`_VIEW`), как и список кандидатов:
интерфейс однопользовательский, и заводить таблицу в базе ради временного
состояния экрана не стоит [CORE-012]. После перезапуска отбор сбрасывается.

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
from ui_core import esc, sort_pick, table

# Кандидаты ищутся сетью, поэтому их держим в памяти процесса между двумя
# запросами страницы: интерфейс однопользовательский, база для этого не нужна.
_CANDIDATES: dict[str, list[hh_employer.Employer]] = {}

# Отбор вакансий по каждой цели: {id цели: {sort, area, min, q}}.
_VIEW: dict[int, dict[str, object]] = {}

STEPS = (
    (
        "all",
        "Собрать всё по цели",
        "Вакансии с hh.ru, адреса для карты, отзывы с оценкой, глубокий ресёрч — "
        "по очереди.",
    ),
)

# Подписи к порядкам из targets.VAC_SORTS. Сами выражения SQL живут там же, где
# запрос, а здесь только слова для человека.
VAC_SORT_LABELS = (
    ("fresh", "Сначала новые и свежие"),
    ("score", "По скору"),
    ("date", "По дате публикации"),
    ("area", "По городу"),
    ("title", "По названию"),
)

# Пороги скора ступеньками: точное число здесь никому не нужно, а выбор из
# пяти вариантов быстрее любого поля ввода.
SCORE_STEPS = (
    (0.0, "любой скор"),
    (20.0, "скор от 20"),
    (40.0, "скор от 40"),
    (60.0, "скор от 60"),
    (80.0, "скор от 80"),
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
            "реестрам, а не к работодателю. Запусти у цели «Глубокий ресёрч», а "
            "чтобы собрать вакансии, добавь ту же компанию ещё раз по названию "
            "или ссылке — цель не раздвоится, а дополнится."
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
        if step == "research":
            if not deepresearch.options().enabled:
                return "Глубокий ресёрч выключен настройкой DEEP_ENABLED."
            jobs.runner.start("research-deep", ["--company", target.company, "--force"])
        elif step in ("all", "vacancies", "reviews"):
            if step == "all" and not target.resolved:
                return (
                    "У цели нет работодателя на hh.ru: вакансии и адреса собрать "
                    "не выйдет. Добавь её заново по названию и выбери компанию "
                    "из списка."
                )
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


def view_of(target_id: int) -> dict[str, object]:
    """Текущий отбор вакансий у цели. По умолчанию — всё, как раньше."""
    saved = _VIEW.get(int(target_id))
    if not saved:
        return {"sort": targets.VAC_SORT_DEFAULT, "area": "", "min": 0.0, "q": ""}
    return dict(saved)


def set_view(target_id: int, form: Mapping[str, Sequence[str]]) -> None:
    """Запомнить выбор из формы отбора. Пустая форма равна сбросу."""

    def one(key: str) -> str:
        return (form.get(key) or [""])[0]

    _VIEW[int(target_id)] = {
        # Имя порядка из браузера проверяется по белому списку, а город и
        # слово уходят в SQL только параметрами.
        "sort": sort_pick(
            one("sort"), tuple(targets.VAC_SORTS), targets.VAC_SORT_DEFAULT
        ),
        "area": one("area").strip()[:120],
        "min": settings.as_float(one("min"), 0.0),
        "q": one("q").strip()[:80],
    }


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
        # Отбор вакансий — тоже POST сюда: это единственный путь, после которого
        # снова рисуется страница самой цели, а не список целей. Никакой задачи
        # отбор не запускает и в сеть не ходит.
        if one("step") == "view":
            set_view(int(one("id") or 0), form)
            return ""
        problem = start_step(conn, int(one("id") or 0), one("step"))
        return problem or "Шаг запущен: смотри лог на странице «Запуск»."
    return ""


CARD = (
    # Цель без слежения не «выключена»: гасить её нечестно, слежение — это
    # только про новые вакансии.
    '<div class="card">'
    "<div class=cardtop><b>{company}</b>{flag}</div>"
    "<div class=muted>{meta}</div>"
    "<div class=cardbtns>"
    '<a class=chip href="/target?id={tid}">Открыть</a>'
    '<form class=inline method=post action="/targets/watch">'
    '<input type=hidden name="id" value="{tid}"><input type=hidden name="on" value="{next_watch}">'
    "<button class=secondary>{watch_label}</button></form>"
    '<form class=inline method=post action="/targets/remove">'
    '<input type=hidden name="id" value="{tid}">'
    "<button class=secondary>Убрать</button></form>"
    "</div></div>"
)


def _meta(conn: sqlite3.Connection, target: targets.Target) -> str:
    total, fresh = targets.counts(conn, target.id)
    bits = ["вакансий {}".format(total)]
    if fresh:
        bits.append("новых {}".format(fresh))
    if target.inn:
        bits.append("ИНН {}".format(target.inn))
    if not target.resolved:
        bits.append("работодатель hh.ru не выбран")
    if target.last_scan_at:
        bits.append("смотрели {}".format(target.last_scan_at[:10]))
    return esc(" · ".join(bits))


def render_targets(conn: sqlite3.Connection, note: str = "", query: str = "") -> str:
    """Список целей, форма добавления и — если нужно — выбор кандидата."""
    parts = []
    if note:
        parts.append("<div class=ok>{}</div>".format(esc(note)))
    parts.append(
        "<p class=muted>Цель — компания, которую выбрал ты, а не сбор. Можно "
        "вписать название, вставить ссылку на компанию или вакансию с hh.ru "
        "или указать ИНН. Шаги запускаются кнопками: каждый стоит времени и "
        "внешних запросов.</p>"
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

    found = targets.all_targets(conn)
    if not found:
        parts.append("<p class=muted>Целей пока нет.</p>")
        return "".join(parts)
    cards = []
    for target in found:
        cards.append(
            CARD.format(
                company=esc(target.company),
                flag='<span class="ok">следим</span>' if target.watch else "",
                meta=_meta(conn, target),
                tid=target.id,
                next_watch="0" if target.watch else "1",
                watch_label="Не следить" if target.watch else "Следить",
            )
        )
    parts.append('<div class=cards>{}</div>'.format("".join(cards)))
    if len(found) > targets.EXPECTED_MAX:
        parts.append(
            "<p class=muted>Целей больше десятка: каждая со слежением — "
            "отдельный обход hh.ru в прогоне.</p>"
        )
    return "".join(parts)


def _option(value: str, label: str, current: str) -> str:
    return '<option value="{value}"{sel}>{label}</option>'.format(
        value=esc(value),
        sel=" selected" if value == current else "",
        label=esc(label),
    )


def _filters(
    conn: sqlite3.Connection, target: targets.Target, view: dict[str, object], total: int
) -> str:
    """Форма отбора вакансий внутри цели.

    Города перечисляются со счётчиками: видно, что из тысячи вакансий в твоём
    городе две — и что искать там больше нечего.
    """
    area = str(view["area"])
    cities = [_option("", "все города ({})".format(total), area)]
    for name, count in targets.areas(conn, target.id):
        cities.append(
            _option(name, "{} ({})".format(name or "без города", count), area)
        )
    current_min = "{:g}".format(float(view["min"]))
    scores = [
        _option("{:g}".format(step), label, current_min) for step, label in SCORE_STEPS
    ]
    sorts = [_option(key, label, str(view["sort"])) for key, label in VAC_SORT_LABELS]
    return (
        '<form method=post action="/targets/step" class=filters>'
        '<input type=hidden name="id" value="{tid}">'
        '<input type=hidden name="step" value="view">'
        '<label class=filt>Город<br><select name="area">{cities}</select></label>'
        '<label class=filt>Скор<br><select name="min">{scores}</select></label>'
        '<label class=filt>Порядок<br><select name="sort">{sorts}</select></label>'
        '<label class=filt>В названии<br>'
        '<input type=search name="q" value="{q}" maxlength="80" placeholder="python"></label>'
        "<label class=filt><br><button>Показать</button></label>"
        "</form>"
    ).format(
        tid=target.id,
        cities="".join(cities),
        scores="".join(scores),
        sorts="".join(sorts),
        q=esc(str(view["q"])),
    )


def _reset(target: targets.Target) -> str:
    return (
        '<form class=inline method=post action="/targets/step">'
        '<input type=hidden name="id" value="{tid}">'
        '<input type=hidden name="step" value="view">'
        "<button class=secondary>Сбросить отбор</button></form>"
    ).format(tid=target.id)


def render_target(conn: sqlite3.Connection, target_id: int, note: str = "") -> str:
    """Одна цель: шаги, вакансии с отбором и ссылка на досье."""
    target = targets.get(conn, int(target_id))
    if target is None:
        return "<p>Такой цели нет.</p>"
    total, _fresh = targets.counts(conn, target.id)
    parts = ['<p><a href="/targets">← Все цели</a></p>']
    if note:
        parts.append("<div class=ok>{}</div>".format(esc(note)))
    parts.append("<h1>{}</h1>".format(esc(target.company)))
    parts.append(
        '<p class=muted>{meta} · <a href="/company?name={name}">досье и контакты</a></p>'.format(
            meta=_meta(conn, target), name=esc(target.company)
        )
    )

    buttons = []
    for step, label, hint in STEPS:
        buttons.append(
            (
                '<form class=inline method=post action="/targets/step">'
                '<input type=hidden name="id" value="{tid}">'
                '<input type=hidden name="step" value="{step}">'
                '<button title="{hint}">{label}</button></form>'
            ).format(tid=target.id, step=step, hint=esc(hint), label=esc(label))
        )
    parts.append("<div class=tasks>{}</div>".format("".join(buttons)))
    parts.append(
        "<p class=muted>{}</p>".format(
            " · ".join("{}: {}".format(esc(label), esc(hint)) for _, label, hint in STEPS)
        )
    )

    view = view_of(target.id)
    picked = bool(view["area"] or view["min"] or view["q"])
    found = targets.vacancies(
        conn,
        target.id,
        sort=str(view["sort"]),
        area=str(view["area"]),
        min_score=float(view["min"]),
        query=str(view["q"]),
    )
    rows = []
    for row in found:
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
    # Отметка «новая» гаснет после того, как строки уже посчитаны: иначе владелец
    # никогда не увидит, что именно пришло нового.
    targets.seen(conn, target.id)

    parts.append("<h2>Вакансии компании</h2>")
    if not total:
        parts.append(
            "<p class=muted>Вакансии ещё не собирались. Кнопка «Собрать вакансии» "
            "возьмёт всё, что у компании открыто.</p>"
        )
        return "".join(parts)

    parts.append(_filters(conn, target, view, total))
    parts.append(
        "<p class=muted>Показано {shown} из {total}. Отбор только прячет строки на "
        "экране: из базы ничего не пропадает, порог профиля к цели не "
        "применяется, а скор рядом — справочно, по лучшему профилю.{reset}</p>".format(
            shown=len(rows), total=total, reset=" " + _reset(target) if picked else ""
        )
    )
    if not rows:
        parts.append(
            "<p class=muted>Под этот отбор не попала ни одна вакансия. Ослабь порог "
            "скора или выбери другой город.</p>"
        )
    else:
        parts.append(
            table(["Вакансия", "Город", "Скор", "Опубликована"], rows, raw_head=True)
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
    "SCORE_STEPS",
    "STEPS",
    "VAC_SORT_LABELS",
    "add_from_input",
    "handle",
    "last_query",
    "pick",
    "render_target",
    "render_targets",
    "set_view",
    "star_form",
    "start_step",
    "view_of",
)
