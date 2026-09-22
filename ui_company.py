"""Карточка работодателя: всё, что известно про одну компанию.

Отделено от списка (`ui_companies.py`) по [CORE-024]: вместе они давно не
влезали в 25 КБ. Граница естественная — список отвечает на вопрос «кого
смотреть», карточка на вопрос «что с этой».

Карточка показывает два рода данных рядом: отзывы — это чужие слова, а история
публикаций (company_signals) — наши собственные наблюдения. Второе проверяемо
и потому весит больше, хоть и накапливается медленнее.

Блоки свёрнуты: и верхние (о компании, вакансии, контакты), и внутренние
(оценка, накрутка, сферы, источники). Иначе карточка крупного работодателя —
это экран на десять прокруток, где не найти нужную цифру.

Шаблоны — только str.format с заранее вычисленными переменными, без вложенных
ф-строк: однажды это уже стоило SyntaxError.
"""

from __future__ import annotations

import json
import sqlite3

import company_score_rules
import company_score_store
import company_signals
import contact_finds
import dossier
import fake_company
import fake_rules
import fake_store
import market_rules
import market_store
import profiles
import review_area
from ui_company_lists import (
    CONTACT_COLUMNS,
    CONTACT_SORTS,
    VACANCY_COLUMNS,
    VACANCY_SORTS,
    render_contacts_for,
    render_vacancies_for,
)
from ui_core import details, esc, table

RISK_CLASS = {
    dossier.RISK_RED: "danger",
    dossier.RISK_YELLOW: "warn",
    dossier.RISK_GREEN: "ok",
    dossier.RISK_UNKNOWN: "muted",
    dossier.RISK_THIN: "warn",
}

SCORE_CLASS = {
    company_score_rules.LEVEL_RED: "danger",
    company_score_rules.LEVEL_YELLOW: "warn",
    company_score_rules.LEVEL_GREEN: "ok",
    company_score_rules.LEVEL_UNKNOWN: "muted",
}

POLARITY_RU = {
    "negative": "негатив",
    "positive": "позитив",
    "mixed": "смешанный",
    "unknown": "не определён",
}

def _patterns(raw: object) -> list[dict]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [item for item in value if isinstance(item, dict)]




def render_signals(conn: sqlite3.Connection, name: str) -> str:
    """Блок фактов по истории публикаций — единственное место в UI, где данные наши.

    При короткой истории блок не скрывается, а говорит, что данных мало:
    иначе отсутствие фактов читалось бы как их благополучное отсутствие [CORE-019].
    """
    signals = company_signals.collect(conn, name)
    items = "".join(
        "<li>{}</li>".format(esc(line)) for line in company_signals.facts(signals)
    )
    note = (
        "Считается по слепкам вакансий этого работодателя — это наши наблюдения, "
        "а не чьи-то слова. Разные написания названия сводятся в одну компанию."
    )
    return "<ul>{items}</ul><p class=muted>{note}</p>".format(
        items=items, note=esc(note)
    )



def render_market(conn: sqlite3.Connection, name: str) -> str:
    """Метка работодателя по деньгам с раскрытием признаков и чисел.

    Метка без чисел непроверяема, поэтому рядом всегда стоит, по скольким
    вакансиям она посчитана и какова медиана отклонения [CORE-019].
    """
    row = market_store.load_company(conn, name)
    if row is None or str(row["level"]) == market_rules.MARK_NONE:
        return ""
    try:
        signs = json.loads(row["signs"] or "[]")
    except ValueError:
        signs = []
    if not signs:
        return ""
    level = str(row["level"])
    deviation = ""
    if row["deviation"] is not None:
        deviation = " · медиана отклонения {:+.0%}".format(float(row["deviation"]))
    head = '<div class="{cls}">{label} · вакансий с известным рынком: {count}{dev}</div>'.format(
        cls="danger" if level == market_rules.MARK_SET else "warn",
        label=esc(market_rules.MARK_RU.get(level, level)),
        count=esc(row["vacancies"]),
        dev=esc(deviation),
    )
    items = "".join(
        "<li>{}</li>".format(esc(str(sign.get("text") or ""))) for sign in signs
    )
    return "{}<ul>{}</ul>".format(head, items)


def render_score(conn: sqlite3.Connection, name: str) -> str:
    """Общая оценка работодателя с раскрытием: оси, улики, покрытие, вето.

    Уровень без улик — то же «плохая компания» без доказательств, поэтому
    таблица улик показывается всегда, а не прячется под уровнем [HRD-003].
    """
    row = company_score_store.load(conn, name)
    if row is None:
        return ""
    level = str(row["level"])
    try:
        axes = json.loads(row["axes"] or "{}")
        evidence = json.loads(row["evidence"] or "[]")
    except ValueError:
        axes, evidence = {}, []
    veto = str(row["veto"] or "")
    head = '<div class="{cls}">{label} · осей с данными: {covered} из {total}{veto}</div>'.format(
        cls=SCORE_CLASS.get(level, "muted"),
        label=esc(company_score_rules.LEVEL_RU.get(level, level)),
        covered=esc(row["covered"]),
        total=len(company_score_rules.AXES),
        veto=esc(
            " · вето: {}".format(company_score_rules.VETO_RU.get(veto, veto))
            if veto
            else ""
        ),
    )
    axis_line = " · ".join(
        "{}: {:.0f}".format(company_score_rules.AXIS_RU.get(axis, axis), value)
        for axis, value in axes.items()
    )
    if axis_line:
        head += "<p class=muted>{}</p>".format(esc(axis_line))
    rows = [
        [
            esc("—" if item.get("polarity") == "red" else "+"),
            esc(company_score_rules.AXIS_RU.get(str(item.get("axis")), item.get("axis"))),
            esc(item.get("text") or ""),
            esc(item.get("weight") or 0),
            esc("{:.2f}".format(float(item.get("trust") or 0))),
        ]
        for item in evidence
    ]
    table_html = "<p class=muted>Улик пока нет.</p>"
    if rows:
        table_html = table(("", "Ось", "Улика", "Вес", "Доверие"), rows)
    note = (
        "Уровень — худшая ось, а не среднее: плюсы не компенсируют невыплату "
        "зарплаты. Доверие 1.00 — наши наблюдения, ниже — отзывы, и оно падает "
        "при признаках накрутки. Оси без данных не считаются."
    )
    return "{}{}<p class=muted>{}</p>".format(head, table_html, esc(note))


def render_fake(conn: sqlite3.Connection, name: str, row: sqlite3.Row) -> str:
    """Метка накрутки и подозрительные отзывы.

    Метка всегда раскрывается: какие признаки сработали и сколько отзывов
    затронуто. Сами отзывы показываются, а не прячутся: скрытые данные нельзя
    перепроверить. Вердиктов «фейк» здесь нет — доказать заказной отзыв нельзя,
    поэтому формулировка «похоже на заказной» и ссылка на источник.
    """
    level = str(row["fake_level"] or fake_rules.MARK_NONE)
    try:
        signs = json.loads(row["fake_signs"] or "[]")
    except ValueError:
        signs = []
    подозрительные = [
        item
        for item in fake_store.load_items(conn, name)
        if str(item["label"]) != fake_rules.LABEL_CLEAN
    ]
    if level == fake_rules.MARK_NONE and not подозрительные:
        return ""

    head = '<div class="{cls}">{label}</div>'.format(
        cls="danger" if level == fake_rules.MARK_FAKE else "warn",
        label=esc(fake_rules.MARK_RU.get(level, level)),
    )
    if signs:
        head += "<ul>{}</ul>".format(
            "".join("<li>{}</li>".format(esc(sign.get("text") or "")) for sign in signs)
        )

    rows = []
    for item in подозрительные:
        try:
            signals = json.loads(item["signals"] or "[]")
        except ValueError:
            signals = []
        rows.append(
            [
                '<a href="{url}" target=_blank rel=noreferrer>{site}</a>'.format(
                    url=esc(item["url"]),
                    site=esc(dossier.SITE_NAMES.get(str(item["site"]), item["site"] or "источник")),
                ),
                esc(str(item["dated_at"] or "—")),
                esc("—" if item["rating"] is None else "{:.1f}".format(float(item["rating"]))),
                esc(fake_rules.LABEL_RU.get(str(item["label"]), item["label"])),
                esc("{:.2f}".format(float(item["fake_score"] or 0))),
                esc("; ".join(fake_rules.SIGNALS[c][1] for c in signals if c in fake_rules.SIGNALS)),
                esc(item["excerpt"] or ""),
            ]
        )
    table_html = "<p class=muted>Подозрительных отзывов нет.</p>"
    if rows:
        table_html = table(
            ("Площадка", "Дата", "Оценка", "Метка", "Счёт", "Сигналы", "Фрагмент"), rows
        )
    return "{}{}".format(head, table_html)


def render_areas(conn: sqlite3.Connection, name: str) -> str:
    """Отзывы по сферам: сколько их и какая оценка в каждой.

    Показывается всегда и целиком, а не только своя сфера. Разбивка «как в
    твоей сфере» считалась и раньше (`fake_company.by_area`), но видна была
    лишь в сводке досье и в Telegram, и только при заданной настройке — то
    есть у владельца её не было нигде. Пустая таблица здесь честнее тишины:
    видно, что сферу получил один отзыв из десяти, и почему [CORE-019].
    """
    items = fake_store.load_items(conn, name)
    if not items:
        return (
            "<p class=muted>Отдельных отзывов в базе нет: досье собрано по страницам "
            "целиком. Сфера ставится при разборе отзывов — пересобери досье.</p>"
        )
    counts: dict[str, int] = {}
    sums: dict[str, list[float]] = {}
    wide = 0
    for item in items:
        scope = str(item["area_scope"] or review_area.SCOPE_AREA)
        code = str(item["area"] or review_area.AREA_UNKNOWN)
        if scope == review_area.SCOPE_COMPANY:
            # Тема про компанию целиком: задержка зарплаты не бывает «чужой».
            wide += 1
            code = "wide"
        counts[code] = counts.get(code, 0) + 1
        if item["rating"] is not None:
            sums.setdefault(code, []).append(float(item["rating"]))

    def label_of(code: str) -> str:
        if code == "wide":
            return "про компанию целиком"
        if code == review_area.AREA_UNKNOWN:
            return "сфера не определена"
        return review_area.label(code)

    mine = review_area.owner_code()
    order = sorted(counts, key=lambda code: (-counts[code], label_of(code)))
    rows = []
    for code in order:
        marks = sums.get(code, [])
        rows.append(
            [
                "<b>{}</b>".format(esc(label_of(code)))
                if code == mine
                else esc(label_of(code)),
                esc(counts[code]),
                esc("—" if not marks else "{:.1f}".format(sum(marks) / len(marks))),
            ]
        )
    body = table(("Сфера", "Отзывов", "Средняя оценка"), rows)

    if not mine:
        note = (
            "Своя сфера не выбрана: настройка REVIEW_AREA на странице настроек, "
            "группа «Отзывы по сферам». Пока она пуста, в сводке досье и в "
            "Telegram разбивки нет — только эта таблица."
        )
    elif counts.get(mine, 0) < fake_company.AREA_MIN_ITEMS or len(items) < fake_company.AREA_MIN_TOTAL:
        note = (
            "Твоя сфера — «{label}», но отзывов по ней {mine} при {total} всего: "
            "для отдельной цифры нужно {need} в своей сфере и {all_need} в "
            "компании. «Оценка 1.0 по одному отзыву» — это шум, а не факт."
        ).format(
            label=review_area.label(mine),
            mine=counts.get(mine, 0),
            total=len(items),
            need=fake_company.AREA_MIN_ITEMS,
            all_need=fake_company.AREA_MIN_TOTAL,
        )
    else:
        note = (
            "Твоя сфера — «{label}»: её строка выделена. Расхождение с общей "
            "средней информативнее любой из двух цифр."
        ).format(label=review_area.label(mine))
    return "{}<p class=muted>{}</p><p class=muted>{}</p>".format(
        body,
        esc(note),
        esc(
            "Сфера — не отдел: оргструктуру никто не отдаёт, речь о том, кем "
            "работал автор. Ставится словарями по должности из шапки отзыва и "
            "лексике текста, неуверенный случай остаётся без сферы. Отзывы "
            "старых досье получают сферу только при пересборе."
        ),
    )


def render_company(
    conn: sqlite3.Connection,
    name: str,
    jobs_sort: str = "",
    contacts_sort: str = "",
) -> str:
    """Карточка работодателя: сначала компания, ниже её вакансии, в конце контакты.

    Блоки свёрнуты по умолчанию: порядок чтения здесь и есть смысл страницы —
    сперва решаем, стоит ли иметь дело с компанией, и только потом ищем, кому
    писать [OUT-002]. Развёрнутая простыня этот порядок ломает.
    """
    name = (name or "").strip()
    if not name:
        return "<div class=warn>Компания не указана.</div>"
    row = dossier.load(conn, name)
    if row is None:
        return (
            '<div class=warn>Досье на «{name}» ещё не собрано. Оно собирается '
            "автоматически, но только по компаниям вакансий от {threshold:.0f} "
            "баллов: остальные лежат в списке, а запросы поиска на них не "
            "тратятся.</div>"
            '<p><a href="/companies">К списку компаний</a></p>'
        ).format(name=esc(name), threshold=profiles.dossier_threshold())

    rating = "—"
    if row["avg_rating"] is not None:
        rating = "{:.1f} из 5".format(float(row["avg_rating"]))
        if row["avg_rating_all"] is not None and float(row["avg_rating_all"]) != float(
            row["avg_rating"]
        ):
            # Обе средние рядом: разница между ними и есть цена накрутки.
            rating += " (по всем отзывам {:.1f})".format(float(row["avg_rating_all"]))
    head = (
        '<div class="{cls}">Работодатель: {risk} · отзывов: {count} · средняя оценка: '
        "{rating}</div>"
    ).format(
        cls=RISK_CLASS.get(str(row["risk"]), "muted"),
        risk=esc(dossier.RISK_RU.get(str(row["risk"]), row["risk"])),
        count=esc(row["review_count"]),
        rating=esc(rating),
    )

    score_block = render_score(conn, name)
    fake_block = render_fake(conn, name, row)
    money_block = render_market(conn, name)

    summary = ""
    if row["summary"]:
        summary = (
            "<pre>{text}</pre>"
            "<p class=muted>Флаги и цифры считаются правилами, модель на них "
            "не влияет.</p>"
        ).format(text=esc(row["summary"]))

    pattern_rows = []
    for item in _patterns(row["patterns"]):
        mark = "—" if item.get("polarity") == "red" else "+"
        quotes = item.get("quotes") or []
        pattern_rows.append(
            [
                esc(mark),
                esc(item.get("label") or item.get("code") or ""),
                esc(item.get("hits") or 0),
                "<br>".join(esc(quote) for quote in quotes[:2]) or "<span class=muted>—</span>",
            ]
        )
    patterns_block = "<p class=muted>Закономерностей не найдено.</p>"
    if pattern_rows:
        patterns_block = table(
            ("", "Закономерность", "Упоминаний", "Цитаты"), pattern_rows
        )

    review_rows = []
    for review in dossier.load_reviews(conn, name):
        review_rows.append(
            [
                esc(dossier.SITE_NAMES.get(str(review["site"]), review["site"])),
                '<a href="{url}" target=_blank rel=noreferrer>{title}</a>'.format(
                    url=esc(review["url"]), title=esc(review["title"] or review["url"])
                ),
                esc(POLARITY_RU.get(str(review["polarity"]), review["polarity"])),
                esc("—" if review["rating"] is None else "{:.1f}".format(float(review["rating"]))),
                esc(review["snippet"] or ""),
            ]
        )
    reviews_block = "<p class=muted>Отзывов не найдено.</p>"
    if review_rows:
        reviews_block = table(
            ("Площадка", "Источник", "Тон", "Оценка", "Фрагмент"), review_rows
        )

    # Внутренние блоки тоже сворачиваются: у крупного работодателя таблица улик
    # и список подозрительных отзывов — это экран прокрутки каждый, и нужная
    # цифра тонет. Открыта только оценка: с неё читают карточку.
    inner = (
        ("Оценка работодателя", "", score_block, True),
        ("Деньги против рынка", "", money_block, False),
        ("Похоже на накрутку отзывов", "", fake_block, False),
        (
            "Отзывы по сферам",
            "кем работали авторы",
            render_areas(conn, name),
            False,
        ),
        ("Сводка", esc(row["summary_by"] or "правила"), summary, False),
        ("История публикаций", "наши наблюдения", render_signals(conn, name), False),
        ("Закономерности", "", patterns_block, False),
        ("Источники", "страниц: {}".format(len(review_rows)), reviews_block, False),
    )
    about = "".join(
        details(title, note, body, open_=open_)
        for title, note, body, open_ in inner
        if body
    )

    vacancies = company_signals.vacancy_rows(conn, name)
    found = contact_finds.for_company(conn, name)

    return (
        "<h2>{company}</h2>{head}"
        "{about}{jobs}{contacts}"
        '<p><a href="/companies">К компаниям и контактам</a></p>'
    ).format(
        company=esc(name),
        head=head,
        about=details("О компании", "отзывов: {}".format(row["review_count"]), about),
        jobs=details(
            "Вакансии компании",
            "в базе: {}".format(len(vacancies)),
            render_vacancies_for(conn, name, jobs_sort),
            # Пришли по ссылке сортировки — блок открыт, иначе клик уводил бы
            # на ту же свёрнутую страницу.
            open_=bool(jobs_sort),
        ),
        contacts=details(
            "Контакты",
            "найдено: {}".format(len(found)),
            render_contacts_for(conn, name, contacts_sort),
            open_=bool(contacts_sort),
        ),
    )


__all__ = (
    "CONTACT_COLUMNS",
    "CONTACT_SORTS",
    "POLARITY_RU",
    "RISK_CLASS",
    "SCORE_CLASS",
    "VACANCY_COLUMNS",
    "VACANCY_SORTS",
    "render_areas",
    "render_company",
    "render_contacts_for",
    "render_fake",
    "render_market",
    "render_score",
    "render_signals",
    "render_vacancies_for",
)
