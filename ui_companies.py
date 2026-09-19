"""Страницы «Компании» и «Очистка».

Две разные задачи в одном файле по одной причине: и та, и другая работают с
данными о работодателях как с накопленным активом: одна показывает, что
накопилось, вторая — единственное место, где это можно уничтожить.

Карточка компании показывает два рода данных рядом: отзывы — это чужие слова,
а история публикаций (company_signals) — наши собственные наблюдения. Второе
проверяемо и потому весит больше, хоть и накапливается медленнее.

Шаблоны — только str.format с заранее вычисленными переменными, без вложенных
ф-строк: однажды это уже стоило SyntaxError.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse

import company_signals
import contact_finds
import contacts
import dossier
import fake_rules
import fake_store
import maintenance
from ui_core import details, esc, sort_head, sort_pick, table
from ui_views import draft_button

RISK_CLASS = {
    dossier.RISK_RED: "danger",
    dossier.RISK_YELLOW: "warn",
    dossier.RISK_GREEN: "ok",
    dossier.RISK_UNKNOWN: "muted",
    dossier.RISK_THIN: "warn",
}

POLARITY_RU = {
    "negative": "негатив",
    "positive": "позитив",
    "mixed": "смешанный",
    "unknown": "не определён",
}

CONFIRM_WORD = "УДАЛИТЬ"


def _flags(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(item) for item in value]


def _patterns(raw: object) -> list[dict]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [item for item in value if isinstance(item, dict)]


# Порядок «тяжёлое сверху»: смотрят обычно на худших работодателей.
RISK_ORDER = {
    dossier.RISK_RED: 3,
    dossier.RISK_YELLOW: 2,
    dossier.RISK_GREEN: 1,
    dossier.RISK_UNKNOWN: 0,
}
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}

COMPANY_SORTS: dict[str, object] = {
    "name": lambda r: str(r["company"] or "").lower(),
    "risk": lambda r: -RISK_ORDER.get(str(r["risk"]), 0),
    "reviews": lambda r: -int(r["review_count"] or 0),
    "rating": lambda r: -float(r["avg_rating"] or 0),
    "updated": lambda r: str(r["updated_at"] or ""),
}
COMPANY_COLUMNS = (
    ("name", "Компания"),
    ("risk", "Работодатель"),
    ("reviews", "Отзывов"),
    ("rating", "Оценка"),
    ("", "Закономерности"),
    ("updated", "Обновлено"),
)

CONTACT_SORTS: dict[str, object] = {
    "person": lambda r: str(r["person"] or "я").lower(),
    "role": lambda r: int(r["role_rank"] or 99),
    "channel": lambda r: (str(r["channel_kind"] or ""), str(r["channel_value"] or "")),
    "confidence": lambda r: CONFIDENCE_ORDER.get(str(r["confidence"]), 9),
}
CONTACT_COLUMNS = (
    ("", "Откуда"),
    ("person", "Человек"),
    ("role", "Роль"),
    ("channel", "Канал"),
    ("confidence", "Уверенность"),
    ("", "Источник"),
)

VACANCY_SORTS: dict[str, object] = {
    "score": lambda r: -float(r["score"] or 0),
    "title": lambda r: str(r["title"] or "").lower(),
    "published": lambda r: str(r["published_at"] or ""),
}
VACANCY_COLUMNS = (
    ("score", "Скор"),
    ("title", "Вакансия"),
    ("published", "Опубликована"),
    ("", "В TG"),
    ("", "Письмо"),
)


def _sorted(rows: list, keys: dict, sort: str) -> list:
    """Даты и числа читаются сверху вниз, поэтому у них порядок обратный."""
    rows = sorted(rows, key=keys[sort])
    if sort in ("updated", "published"):
        rows.reverse()
    return rows


def company_rows(
    conn: sqlite3.Connection, limit: int = 200, sort: str = "updated"
) -> list[list[str]]:
    sort = sort_pick(sort, tuple(COMPANY_SORTS), "updated")
    rows: list[list[str]] = []
    for row in _sorted(list(dossier.list_dossiers(conn, limit)), COMPANY_SORTS, sort):
        company = str(row["company"])
        red = _flags(row["red_flags"])
        rating = "—"
        if row["avg_rating"] is not None:
            rating = "{:.1f}".format(float(row["avg_rating"]))
        rows.append(
            [
                '<a href="/company?name={link}">{name}</a>'.format(
                    link=esc(company), name=esc(company)
                ),
                '<span class="{cls}">{label}</span>'.format(
                    cls=RISK_CLASS.get(str(row["risk"]), "muted"),
                    label=esc(dossier.RISK_RU.get(str(row["risk"]), row["risk"])),
                ),
                esc(row["review_count"]),
                esc(rating),
                esc("; ".join(red[:3]) or "—"),
                esc(str(row["updated_at"])[:10]),
            ]
        )
    return rows


def render_companies(conn: sqlite3.Connection, sort: str = "updated") -> str:
    sort = sort_pick(sort, tuple(COMPANY_SORTS), "updated")
    total, red, empty = dossier.coverage(conn)
    rows = company_rows(conn, sort=sort)
    if not rows:
        return (
            "<div class=warn>Досье пока нет. Они собираются автоматически при сканировании — "
            "по тем компаниям, чьи вакансии прошли порог. Нужен настроенный поиск: "
            "смотри страницу «Поиск».</div>"
        )
    summary = (
        "<p class=muted>Досье: {total} · с красными флагами: {red} · без единого отзыва: "
        "{empty}</p>"
    ).format(total=total, red=red, empty=empty)
    hint = (
        "<p class=muted>Красный статус ставится только по повторяющимся жалобам или "
        "тяжёлым признакам вроде задержки зарплаты. Один злой отзыв — ещё не "
        "закономерность.</p>"
    )
    return summary + table(
        sort_head(COMPANY_COLUMNS, "/companies", "csort", sort), rows, raw_head=True
    ) + hint


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


def render_contacts_for(
    conn: sqlite3.Connection, name: str, sort: str = "role"
) -> str:
    """Каналы, найденные по вакансиям этого работодателя.

    По умолчанию сверху те, кто ближе к работе: сортировка по рангу роли, а не
    по дате находки — писать всё равно одному человеку [OUT-007].
    """
    sort = sort_pick(sort, tuple(CONTACT_SORTS), "role")
    rows = contact_finds.for_company(conn, name)
    if not rows:
        return (
            "<p class=muted>Рабочих каналов по этому работодателю пока не нашлось. "
            "Они ищутся при общем сборе, после досье.</p>"
        )
    body = []
    for row in _sorted(list(rows), CONTACT_SORTS, sort):
        source = "—"
        if row["source_url"]:
            source = '<a href="{url}" target=_blank rel=noreferrer>источник</a>'.format(
                url=esc(row["source_url"])
            )
        body.append(
            [
                '<a href="/vacancy?key={key}">вакансия</a>'.format(
                    key=esc(str(row["key"]))
                ),
                esc(row["person"] or "—"),
                esc(row["role"] or "—"),
                "<span class=pill>{}</span>{}".format(
                    esc(row["channel_kind"]), esc(row["channel_value"])
                ),
                esc(contacts.CONFIDENCE_RU.get(str(row["confidence"]), row["confidence"]))
                + (" · угадан" if row["guessed"] else ""),
                source,
            ]
        )
    base = "/company?name={}".format(urllib.parse.quote(name))
    return table(sort_head(CONTACT_COLUMNS, base, "ksort", sort), body, raw_head=True)


def render_vacancies_for(
    conn: sqlite3.Connection, name: str, sort: str = "published"
) -> str:
    """Вакансии этого работодателя, свежие сверху.

    Список тот же, что на странице вакансий, но без фильтра по скорингу: в
    карточке важно видеть всё, что компания публиковала, включая слабые
    позиции — они и есть материал для выводов о работодателе.
    """
    sort = sort_pick(sort, tuple(VACANCY_SORTS), "published")
    rows = company_signals.vacancy_rows(conn, name)
    if not rows:
        return (
            "<p class=muted>Вакансий этого работодателя в базе нет. Они попадают сюда "
            'при сборе — смотри <a href="/">сбор</a>.</p>'
        )
    body = []
    for row in _sorted(list(rows), VACANCY_SORTS, sort):
        link = '<a href="/vacancy?key={key}">{title}</a>'.format(
            key=urllib.parse.quote(str(row["key"] or "")), title=esc(row["title"])
        )
        body.append(
            [
                "<span class=score>{:.0f}</span>".format(float(row["score"] or 0)),
                link,
                esc(str(row["published_at"] or "")[:10]),
                "✓" if row["notified_at"] else "",
                draft_button(str(row["key"] or ""), "Письмо"),
            ]
        )
    base = "/company?name={}".format(urllib.parse.quote(name))
    return table(sort_head(VACANCY_COLUMNS, base, "jsort", sort), body, raw_head=True)


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
    return "<h3>Похоже на накрутку отзывов</h3>{}{}".format(head, table_html)


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
            '<div class=warn>Досье на «{}» ещё не собрано.</div>'
            '<p><a href="/companies">К списку компаний</a></p>'
        ).format(esc(name))

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

    fake_block = render_fake(conn, name, row)

    summary = ""
    if row["summary"]:
        summary = (
            "<h3>Сводка</h3><pre>{text}</pre>"
            "<p class=muted>Собрала: {by}. Флаги и цифры считаются правилами, модель на них "
            "не влияет.</p>"
        ).format(text=esc(row["summary"]), by=esc(row["summary_by"] or "правила"))

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

    about = (
        "{fake}{summary}"
        "<h3>История публикаций</h3>{signals}"
        "<h3>Закономерности</h3>{patterns}"
        "<h3>Источники</h3>{reviews}"
    ).format(
        fake=fake_block,
        summary=summary,
        signals=render_signals(conn, name),
        patterns=patterns_block,
        reviews=reviews_block,
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


def render_cleanup(
    conn: sqlite3.Connection,
    removed: dict[str, int] | None = None,
    problems: tuple[str, ...] = (),
) -> str:
    """Форма очистки. Каждая цель подписана последствиями и числом строк."""
    counts = maintenance.counts(conn)
    blocks = []
    for code, label, warning, danger in maintenance.describe():
        blocks.append(
            (
                '<div class="field {cls}"><label>'
                '<input type=checkbox name=target value="{code}"> {label} '
                "<span class=pill>{count}</span></label>"
                '<div class=hint>{warning}</div></div>'
            ).format(
                cls="danger" if danger else "",
                code=esc(code),
                label=esc(label),
                count=esc(counts.get(code, 0)),
                warning=esc(warning),
            )
        )

    notice = ""
    if problems:
        notice += "".join(
            "<div class=warn>{}</div>".format(esc(text)) for text in problems
        )
    if removed:
        lines = "; ".join(
            "{}: {}".format(maintenance.target_label(code), count)
            for code, count in removed.items()
        )
        notice += "<div class=ok>Удалено — {}</div>".format(esc(lines))

    return (
        "{notice}"
        "<p>Отметь, что удалить. Стираются строки, сама база и настройки остаются на "
        "месте. Профиль и .env не трогаются никогда.</p>"
        '<form method=post action="/cleanup">{blocks}'
        '<div class=field><label>Подтверждение</label>'
        '<input type=text name=confirm placeholder="{word}">'
        "<div class=hint>Для контактов и истории публикаций нужно ввести {word}: "
        "эти данные повторным сбором не восстанавливаются.</div></div>"
        '<div class=field><label><input type=checkbox name=vacuum value="1"> '
        "Сжать файл базы после очистки</label>"
        "<div class=hint>Медленно на большой базе, но освобождает место на диске.</div></div>"
        "<button>Удалить выбранное</button></form>"
    ).format(notice=notice, blocks="".join(blocks), word=CONFIRM_WORD)


def apply_cleanup(
    conn: sqlite3.Connection, form: dict[str, list[str]]
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Разбирает форму и удаляет. Возвращает (что удалено, жалобы).

    Подтверждение требуется только там, где потеря необратима. Спрашивать его на
    каждый кэш — верный способ научить владельца подтверждать не глядя.
    """
    targets = [value for value in (form.get("target") or []) if value]
    if not targets:
        return {}, ("Ничего не выбрано — удалять нечего.",)

    unknown = [code for code in targets if code not in maintenance.TARGET_CODES]
    if unknown:
        return {}, ("Неизвестная цель: {}".format(", ".join(unknown)),)

    risky = [code for code in targets if code in maintenance.DANGEROUS]
    confirm = (form.get("confirm") or [""])[0].strip().upper()
    if risky and confirm != CONFIRM_WORD:
        labels = ", ".join(maintenance.target_label(code) for code in risky)
        return {}, (
            "Не удалено: {labels} — слишком ценные данные. Введи {word} в поле "
            "подтверждения.".format(labels=labels, word=CONFIRM_WORD),
        )

    removed = maintenance.wipe(conn, tuple(targets))
    if (form.get("vacuum") or [""])[0] == "1":
        maintenance.vacuum(conn)
    return removed, ()


__all__ = (
    "CONFIRM_WORD",
    "apply_cleanup",
    "company_rows",
    "render_cleanup",
    "render_companies",
    "render_company",
    "render_signals",
    "render_vacancies_for",
)
