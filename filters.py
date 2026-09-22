"""Фильтры и сортировки списков: один словарь правил на все страницы.

Зачем отдельный модуль, а не по фильтру на страницу. Вопросы, которые владелец
задаёт списку, повторяются: «что стоит открыть сегодня», «где есть живой
контакт», «кто врёт», «что я ещё не видел». Если каждый такой вопрос писать
прямо в странице, он превращается в ещё один `if` в HTML, и через месяц
никто не скажет, какие фильтры вообще есть.

Три правила, на которых всё держится.

1. **Фильтр уходит в SQL, а не в список.** Раньше страница вакансий брала
   первые N по скору и сортировала уже их: сортировка «по зарплате» показывала
   самую денежную из верхушки, а не из базы. Это не украшение, а враньё.
2. **Из браузера в SQL не попадает ни один символ.** Имя фильтра ищется в этом
   словаре, кусок SQL берётся отсюда же, значение уходит параметром `?`.
   Неизвестное имя молча игнорируется [CORE-017].
3. **Пустое значение — не фильтр.** Пустая строка, ноль и «любой» просто не
   добавляют условие: иначе владелец, забывший поле, получал бы пустой список
   и решал, что база сломалась.

Быстрые виды (`PRESETS`) — это не новая механика, а именованные наборы тех же
параметров. Один клик = один заранее сформулированный вопрос.
"""

from __future__ import annotations

import sqlite3

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Mapping, Sequence

import aitext_rules
import company_score_rules as CSR
import injection_rules
import market_rules
import sources

ANY = ""  # значение «неважно» у выбора


def like(value: str) -> str:
    """Строка поиска как данные: % и _ от человека — буквы, а не джокеры."""
    safe = value.strip().replace("\\", "\\\\")
    safe = safe.replace("%", "\\%").replace("_", "\\_")
    return "%" + safe + "%"


def _days_ago(value: str) -> str:
    days = max(0, min(3650, int(float(value))))
    return (date.today() - timedelta(days=days)).isoformat()


@dataclass(frozen=True)
class Filter:
    """Одно поле фильтра: как его показать и во что превратить.

    `build` возвращает (кусок SQL, параметры) или None, если значение пустое.
    Ни один кусок SQL не собирается из пользовательского ввода.
    """

    key: str
    label: str
    kind: str  # text | number | choice
    build: Callable[[str], tuple[str, tuple] | None]
    options: tuple[tuple[str, str], ...] = ()
    hint: str = ""
    width: str = "160px"

    def apply(self, raw: str) -> tuple[str, tuple] | None:
        value = str(raw or "").strip()
        if not value:
            return None
        if self.kind == "choice" and value not in dict(self.options):
            return None  # чужое значение — как будто фильтр не задан
        try:
            return self.build(value)
        except (TypeError, ValueError):
            return None


def _choice(mapping: Mapping[str, str]) -> Callable[[str], tuple[str, tuple] | None]:
    """Выбор из закрытого списка: значение подбирает готовый кусок SQL."""

    def build(value: str) -> tuple[str, tuple] | None:
        sql = mapping.get(value)
        return (sql, ()) if sql else None

    return build


# ———— вакансии ————

_ACTIVE = (
    "(SELECT s.is_active FROM vacancy_snapshots s WHERE s.key = v.key"
    " ORDER BY s.seen_at DESC LIMIT 1)"
)
_HAS_CONTACT = "EXISTS (SELECT 1 FROM contacts c WHERE c.key = v.key{})"
_INJECTION = (
    "EXISTS (SELECT 1 FROM injection_hits i WHERE i.kind = 'vacancy'"
    " AND i.key = v.key AND i.level = '{}')".format(injection_rules.RED)
)
_FLAGS = (
    "EXISTS (SELECT 1 FROM vacancy_signals g WHERE g.key = v.key"
    " AND g.flags IS NOT NULL AND g.flags <> '')"
)
_COMPANY_LEVEL = (
    "EXISTS (SELECT 1 FROM company_score s WHERE s.company = v.company AND s.level = ?)"
)

VACANCY_FILTERS: tuple[Filter, ...] = (
    Filter(
        "q",
        "Слово в названии или компании",
        "text",
        lambda v: ("(v.title LIKE ? ESCAPE '\\' OR v.company LIKE ? ESCAPE '\\')", (like(v), like(v))),
        width="220px",
    ),
    Filter("company", "Компания целиком", "text", lambda v: ("v.company = ?", (v,))),
    Filter("area", "Город", "text", lambda v: ("v.area LIKE ? ESCAPE '\\'", (like(v),))),
    Filter(
        "min_score",
        "Скор не ниже",
        "number",
        lambda v: ("COALESCE(v.score, 0) >= ?", (float(v),)),
        width="90px",
    ),
    Filter(
        "salary",
        "Зарплата от, ₽",
        "number",
        lambda v: ("COALESCE(v.salary_from, v.salary_to) >= ?", (float(v),)),
        hint="сравнивается нижняя граница вилки как есть, без пересчёта gross",
        width="110px",
    ),
    Filter(
        "days",
        "Опубликована за, дней",
        "number",
        lambda v: ("COALESCE(v.published_at, v.first_seen_at) >= ?", (_days_ago(v),)),
        width="90px",
    ),
    Filter(
        "state",
        "Состояние",
        "choice",
        _choice(
            {
                "active": "COALESCE({}, 1) = 1".format(_ACTIVE),
                "closed": "{} = 0".format(_ACTIVE),
            }
        ),
        options=((ANY, "любое"), ("active", "висит"), ("closed", "снята")),
        hint="по последнему слепку: снятая вакансия остаётся в базе как история",
    ),
    Filter(
        "salary_shown",
        "Вилка",
        "choice",
        _choice(
            {
                "yes": "(v.salary_from IS NOT NULL OR v.salary_to IS NOT NULL)",
                "no": "(v.salary_from IS NULL AND v.salary_to IS NULL)",
            }
        ),
        options=((ANY, "неважно"), ("yes", "указана"), ("no", "скрыта")),
    ),
    Filter(
        "notified",
        "В Telegram",
        "choice",
        _choice(
            {"yes": "v.notified_at IS NOT NULL", "no": "v.notified_at IS NULL"}
        ),
        options=((ANY, "неважно"), ("yes", "отправлена"), ("no", "ещё нет")),
    ),
    Filter(
        "feedback",
        "Моя отметка",
        "choice",
        _choice(
            {
                "like": "v.feedback = 'like'",
                "dislike": "v.feedback = 'dislike'",
                "none": "(v.feedback IS NULL OR v.feedback = '')",
            }
        ),
        options=(
            (ANY, "любая"),
            ("like", "нравится"),
            ("dislike", "не нравится"),
            ("none", "без отметки"),
        ),
    ),
    Filter(
        "contact",
        "Контакт",
        "choice",
        _choice(
            {
                "any": _HAS_CONTACT.format(""),
                "direct": _HAS_CONTACT.format(" AND COALESCE(c.guessed, 0) = 0"),
                "none": "NOT " + _HAS_CONTACT.format(""),
            }
        ),
        options=(
            (ANY, "неважно"),
            ("any", "хоть какой"),
            ("direct", "не угаданный"),
            ("none", "нет совсем"),
        ),
    ),
    # Площадка, на которой вакансия нашлась. Значения — те же, что в
    # `vacancies.source`, плюс «есть на нескольких»: одна вакансия, висящая
    # сразу на четырёх сайтах, — это сама по себе улика (docs/sources.md).
    Filter(
        "source",
        "Площадка",
        "choice",
        _choice(
            {
                **{
                    site.source: "v.source = '{}'".format(site.source)
                    for site in sources.SITES
                },
                "many": (
                    "(SELECT COUNT(*) FROM vacancy_sources s WHERE s.key = v.key) > 1"
                ),
            }
        ),
        options=(
            (ANY, "любая"),
            *((site.source, site.label) for site in sources.SITES),
            ("many", "есть на нескольких"),
        ),
    ),
    Filter(
        "market",
        "Рынок",
        "choice",
        _choice(
            {
                market_rules.BELOW: "v.market_label = '{}'".format(market_rules.BELOW),
                market_rules.IN_MARKET: "v.market_label = '{}'".format(
                    market_rules.IN_MARKET
                ),
                market_rules.ABOVE: "v.market_label = '{}'".format(market_rules.ABOVE),
            }
        ),
        options=(
            (ANY, "неважно"),
            (market_rules.BELOW, "ниже рынка"),
            (market_rules.IN_MARKET, "в рынке"),
            (market_rules.ABOVE, "выше рынка"),
        ),
    ),
    Filter(
        "ai",
        "Текст описания",
        "choice",
        _choice(
            {
                aitext_rules.LABEL_LIKELY: "v.ai_label = '{}'".format(
                    aitext_rules.LABEL_LIKELY
                ),
                aitext_rules.LABEL_SUSPECT: "v.ai_label IN ('{}', '{}')".format(
                    aitext_rules.LABEL_SUSPECT, aitext_rules.LABEL_LIKELY
                ),
                aitext_rules.LABEL_HUMAN: "v.ai_label = '{}'".format(
                    aitext_rules.LABEL_HUMAN
                ),
            }
        ),
        options=(
            (ANY, "неважно"),
            (aitext_rules.LABEL_LIKELY, "похоже на генерацию"),
            (aitext_rules.LABEL_SUSPECT, "шаблонный и хуже"),
            (aitext_rules.LABEL_HUMAN, "обычный текст"),
        ),
    ),
    Filter(
        "company_level",
        "Работодатель",
        "choice",
        lambda v: (_COMPANY_LEVEL, (v,)),
        options=(
            (ANY, "неважно"),
            (CSR.LEVEL_RED, "красные флаги"),
            (CSR.LEVEL_YELLOW, "есть к чему придраться"),
            (CSR.LEVEL_GREEN, "претензий не видно"),
            (CSR.LEVEL_UNKNOWN, "данных мало"),
        ),
    ),
    Filter(
        "risk",
        "Подозрительное",
        "choice",
        _choice(
            {
                "injection": _INJECTION,
                "flags": _FLAGS,
                "any": "({} OR {})".format(_INJECTION, _FLAGS),
                "clean": "NOT ({} OR {})".format(_INJECTION, _FLAGS),
            }
        ),
        options=(
            (ANY, "неважно"),
            ("any", "хоть что-то"),
            ("injection", "промпт-инъекция"),
            ("flags", "необеспеченные обещания"),
            ("clean", "чисто"),
        ),
    ),
)

VACANCY_SORTS: dict[str, tuple[str, str]] = {
    "score": ("Скор", "COALESCE(v.score, 0) DESC, v.last_seen_at DESC"),
    "title": ("Вакансия", "LOWER(v.title) ASC"),
    "company": ("Компания", "LOWER(COALESCE(v.company, '')) ASC, COALESCE(v.score,0) DESC"),
    "published": ("Опубликована", "COALESCE(v.published_at, v.first_seen_at) DESC"),
    "seen": ("Замечена", "v.last_seen_at DESC"),
    "salary": (
        "Зарплата",
        "COALESCE(v.salary_from, v.salary_to) DESC NULLS LAST, COALESCE(v.score,0) DESC",
    ),
    "market": ("Рынок", "COALESCE(v.market_delta, -999) DESC"),
}

# ———— компании ————

_COMPANY_VACANCIES = "(SELECT COUNT(*) FROM vacancies v WHERE v.company = d.company)"
_COMPANY_CONTACTS = (
    "(SELECT COUNT(*) FROM contacts c WHERE c.company = d.company"
    " AND COALESCE(c.guessed, 0) = 0)"
)

COMPANY_FILTERS: tuple[Filter, ...] = (
    Filter(
        "cq",
        "Название",
        "text",
        lambda v: ("d.company LIKE ? ESCAPE '\\'", (like(v),)),
        width="220px",
    ),
    Filter(
        "level",
        "Оценка работодателя",
        "choice",
        lambda v: ("COALESCE(s.level, 'unknown') = ?", (v,)),
        options=(
            (ANY, "любая"),
            (CSR.LEVEL_RED, "красные флаги"),
            (CSR.LEVEL_YELLOW, "есть к чему придраться"),
            (CSR.LEVEL_GREEN, "претензий не видно"),
            (CSR.LEVEL_UNKNOWN, "данных мало"),
        ),
    ),
    Filter(
        "reviews",
        "Отзывов не меньше",
        "number",
        lambda v: ("COALESCE(d.review_count, 0) >= ?", (int(float(v)),)),
        width="90px",
    ),
    Filter(
        "rating",
        "Оценка отзывов от",
        "number",
        lambda v: ("COALESCE(d.avg_rating, 0) >= ?", (float(v),)),
        width="90px",
    ),
    Filter(
        "fake",
        "Похоже на накрутку",
        "choice",
        _choice(
            {
                "yes": "COALESCE(d.fake_level, '') IN ('suspect', 'likely')",
                "no": "COALESCE(d.fake_level, '') NOT IN ('suspect', 'likely')",
            }
        ),
        options=((ANY, "неважно"), ("yes", "да"), ("no", "нет")),
    ),
    Filter(
        "ccontact",
        "Прямой контакт",
        "choice",
        _choice({"yes": _COMPANY_CONTACTS + " > 0", "no": _COMPANY_CONTACTS + " = 0"}),
        options=((ANY, "неважно"), ("yes", "есть"), ("no", "нет")),
    ),
    Filter(
        "openings",
        "Вакансий не меньше",
        "number",
        lambda v: (_COMPANY_VACANCIES + " >= ?", (int(float(v)),)),
        width="90px",
    ),
)

COMPANY_SORTS: dict[str, tuple[str, str]] = {
    "updated": ("Обновлено", "d.updated_at DESC"),
    "name": ("Компания", "LOWER(d.company) ASC"),
    "level": (
        "Оценка",
        "CASE COALESCE(s.level, 'unknown') WHEN 'red' THEN 0 WHEN 'yellow' THEN 1"
        " WHEN 'unknown' THEN 2 ELSE 3 END ASC, d.updated_at DESC",
    ),
    "reviews": ("Отзывов", "COALESCE(d.review_count, 0) DESC"),
    "rating": ("Оценка отзывов", "COALESCE(d.avg_rating, 0) DESC"),
    "openings": ("Вакансий", _COMPANY_VACANCIES + " DESC"),
}


# ———— быстрые виды ————


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    why: str
    params: dict = field(default_factory=dict)


# Значение «порог профиля» вместо числа: жёсткая цифра в виде расходилась с
# настройкой профиля, и вид «Стоит открыть» показывал вакансии, на компании
# которых досье никто не собирал. Подстановку делает ui_views.render_vacancies.
FIT = "fit"

VACANCY_PRESETS: tuple[Preset, ...] = (
    Preset(
        "fit",
        "Подходящие",
        "прошли порог профиля: на их компании есть досье и искались контакты",
        {"min_score": FIT, "sort": "score"},
    ),
    Preset(
        "all",
        "Все",
        "весь список, включая те, что ниже порога профиля — досье на них нет",
        {},
    ),
    Preset(
        "open",
        "Стоит открыть",
        "висят, прошли порог профиля, я их ещё не отклонял",
        {"state": "active", "min_score": FIT, "feedback": "none", "sort": "score"},
    ),
    Preset(
        "fresh",
        "Свежие",
        "опубликованы за три дня",
        {"days": "3", "state": "active", "sort": "published"},
    ),
    Preset(
        "reachable",
        "С живым контактом",
        "есть не угаданный канал — письмо уйдёт человеку, а не в HR-ящик",
        {"contact": "direct", "sort": "score"},
    ),
    Preset(
        "suspicious",
        "Проблемные",
        "инъекция в тексте или необеспеченные обещания",
        {"risk": "any", "sort": "score"},
    ),
    Preset(
        "unseen",
        "Не доехали в Telegram",
        "прошли порог, но карточка не отправлялась",
        {"notified": "no", "min_score": FIT, "sort": "score"},
    ),
    Preset(
        "money",
        "По деньгам",
        "вилка указана, сверху самые крупные",
        {"salary_shown": "yes", "sort": "salary"},
    ),
)

COMPANY_PRESETS: tuple[Preset, ...] = (
    Preset("all", "Все", "весь список досье", {}),
    Preset(
        "bad",
        "Красные",
        "оценка работодателя — красные флаги",
        {"level": CSR.LEVEL_RED, "csort": "level"},
    ),
    Preset(
        "unknown",
        "Без данных",
        "досье есть, а судить не по чему: сюда стоит заглядывать руками",
        {"level": CSR.LEVEL_UNKNOWN, "csort": "openings"},
    ),
    Preset(
        "fake",
        "С накруткой отзывов",
        "отзывы похожи на заказные",
        {"fake": "yes", "csort": "reviews"},
    ),
    Preset(
        "reachable",
        "С прямым контактом",
        "есть канал к живому человеку",
        {"ccontact": "yes", "csort": "openings"},
    ),
)


# ———— сборка ————


def build_where(
    definitions: Sequence[Filter], params: Mapping[str, str]
) -> tuple[str, list, list[tuple[str, str]]]:
    """(SQL после WHERE, параметры, [(ключ, читаемое значение)]).

    Третьим значением идут активные фильтры для показа чипами: пустой список
    без объяснения — худший ответ, который может дать страница.
    """
    clauses, args, active = [], [], []
    for item in definitions:
        raw = str(params.get(item.key, "") or "").strip()
        built = item.apply(raw)
        if built is None:
            continue
        sql, values = built
        clauses.append(sql)
        args.extend(values)
        shown = dict(item.options).get(raw, raw) if item.kind == "choice" else raw
        active.append((item.key, "{}: {}".format(item.label, shown)))
    return (" AND ".join(clauses) or "1=1"), args, active


def order_by(sorts: Mapping[str, tuple[str, str]], name: str, default: str) -> str:
    """ORDER BY только из словаря. Чужое имя — умолчание."""
    return sorts.get(name, sorts[default])[1]


def preset_of(presets: Sequence[Preset], key: str) -> Preset | None:
    for item in presets:
        if item.key == key:
            return item
    return None


def query_string(params: Mapping[str, str], drop: str = "") -> str:
    """Параметры обратно в строку запроса — для ссылок сортировки и чипов."""
    import urllib.parse

    clean = {
        key: str(value)
        for key, value in params.items()
        if str(value or "").strip() and key != drop
    }
    return urllib.parse.urlencode(clean)


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Фильтры заглядывают в соседние таблицы; на старой базе их может не быть."""
    # Импорт внутри: хранилища сами тянут правила, а те — этот модуль.
    import company_score_store
    import contacts
    import detector
    import injection_store

    import source_store

    for store in (
        contacts,
        detector,
        injection_store,
        company_score_store,
        source_store,
    ):
        store.ensure_schema(conn)


__all__ = (
    "FIT",
    "ANY",
    "COMPANY_FILTERS",
    "COMPANY_PRESETS",
    "COMPANY_SORTS",
    "Filter",
    "Preset",
    "VACANCY_FILTERS",
    "VACANCY_PRESETS",
    "VACANCY_SORTS",
    "build_where",
    "ensure_tables",
    "like",
    "order_by",
    "preset_of",
    "query_string",
)
