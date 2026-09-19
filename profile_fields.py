"""Справочники полей формы профиля и пересчёт важности в веса.

Выделено из profile_form.py по [CORE-024]. Здесь только то, что не знает про
HTTP-форму: списки вариантов, шкала важности и её перевод в веса скоринга.
Все имена реэкспортируются из profile_form, поэтому вызывающий код не правлен.

Важность хранится рядом с весами, под ключом importance. score.py про неё не
знает и читает weights — именно поэтому веса всё равно записываются в файл, а не
вычисляются на лету. Если importance в файле нет (профиль старый или правлен
руками), она восстанавливается из весов.
"""

from __future__ import annotations

from typing import Any, Mapping

# Опыт в обозначениях hh.ru и по-человечески.
EXPERIENCE: tuple[tuple[str, str], ...] = (
    ("noExperience", "без опыта"),
    ("between1And3", "1–3 года"),
    ("between3And6", "3–6 лет"),
    ("moreThan6", "более 6 лет"),
)

# Критерии скоринга: ключ в weights и подпись.
WEIGHTS: tuple[tuple[str, str], ...] = (
    ("skills", "навыки"),
    ("salary", "зарплата"),
    ("nice_to_have", "желательные"),
    ("remote", "удалёнка"),
    ("experience", "опыт"),
    ("market", "рынок"),
)

WEIGHT_HINTS: dict[str, str] = {
    "skills": "Совпадение с обязательными навыками.",
    "salary": "Насколько вилка выше твоего минимума.",
    "nice_to_have": "Совпадение с желательными навыками.",
    "remote": "Удалённый или гибридный формат.",
    "market": "Вилка относительно медианы по нашим наблюдениям с hh.ru.",
    "experience": "Совпадение требуемого опыта с твоим.",
}

# Шкала важности. Слова вместо цифр: «вес 20» ни о чём не говорит, а «важно» — говорит.
IMPORTANCE_LEVELS: tuple[tuple[int, str], ...] = (
    (0, "не учитывать"),
    (1, "едва важно"),
    (2, "немного важно"),
    (3, "важно"),
    (4, "очень важно"),
    (5, "решает всё"),
)

MAX_IMPORTANCE = 5

# Списки, которые редактируются построчно или через запятую: ключ и подпись.
LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("skills", "Обязательные навыки"),
    ("nice_to_have", "Желательные навыки"),
    ("stop_words", "Стоп-слова"),
)

# Регионы hh.ru для выбора галочками. Список короткий сознательно: полный
# справочник — сотни строк, и в форме он бесполезен. Редкие регионы вводятся
# числом в отдельном поле; сверить ID можно в справочнике hh.ru (/areas).
AREA_CHOICES: tuple[tuple[int, str], ...] = (
    (113, "Вся Россия"),
    (1, "Москва"),
    (2, "Санкт-Петербург"),
    (3, "Екатеринбург"),
    (4, "Новосибирск"),
    (66, "Нижний Новгород"),
    (88, "Казань"),
)

AREA_NAMES: dict[int, str] = {code: name for code, name in AREA_CHOICES}

# За какой срок смотреть вакансии.
PERIOD_CHOICES: tuple[tuple[int, str], ...] = (
    (1, "за сутки"),
    (3, "за три дня"),
    (7, "за неделю"),
    (14, "за две недели"),
    (30, "за месяц"),
)

# Сколько страниц выдачи брать. Одна страница — до 50 вакансий. Пустой выбор —
# все страницы: потолок в три страницы отдавал половину запрошенного лимита.
PAGE_CHOICES: tuple[tuple[int, str], ...] = (
    (1, "1 страница (до 50)"),
    (2, "2 страницы (до 100)"),
    (3, "3 страницы (до 150)"),
    (5, "5 страниц (до 250)"),
)

CURRENCY_CHOICES: tuple[tuple[str, str], ...] = (
    ("RUR", "рубли"),
    ("USD", "доллары"),
    ("EUR", "евро"),
    ("KZT", "тенге"),
)

# Сколько слотов запросов показывать: все заполненные плюс два пустых, но не
# меньше трёх и не больше десяти. Пустой слот — это кнопка «добавить» без JS.
MIN_SLOTS = 3
MAX_SLOTS = 10
SPARE_SLOTS = 2

DEFAULT_PROFILE: dict[str, Any] = {
    "queries": [],
    "salary": {"min_net": 0, "currency": "RUR", "allow_missing": True},
    "skills": [],
    "nice_to_have": [],
    "stop_words": [],
    "geo": {"areas": [113], "remote_ok": True},
    "experience_ok": [],
    "importance": {key: 3 for key, _ in WEIGHTS},
    "weights": {key: 20 for key, _ in WEIGHTS},
    "min_score": 45,
    "facts": [],
}


def area_label(code: Any) -> str:
    """Имя региона для показа; неизвестный ID показывается как есть."""
    try:
        return AREA_NAMES[int(code)]
    except (TypeError, ValueError, KeyError):
        return "регион {}".format(code)


def normalize_weights(importance: Mapping[str, Any]) -> dict[str, int]:
    """Переводит важность 0–5 в веса с суммой ровно 100.

    Остаток от округления отдаётся самому важному критерию, иначе сумма трёх
    одинаковых важностей даст 99 и порог станет слегка недостижим.
    Все нули — равные веса: скоринг без весов вообще не работает.
    """
    keys = [key for key, _ in WEIGHTS]
    levels = {}
    for key in keys:
        try:
            levels[key] = max(0, min(MAX_IMPORTANCE, int(importance.get(key, 0) or 0)))
        except (TypeError, ValueError):
            levels[key] = 0

    total = sum(levels.values())
    if total <= 0:
        share = 100 // len(keys)
        weights = {key: share for key in keys}
        weights[keys[0]] += 100 - share * len(keys)
        return weights

    weights = {key: int(round(100.0 * levels[key] / total)) for key in keys}
    drift = 100 - sum(weights.values())
    if drift:
        leader = max(keys, key=lambda key: (levels[key], -keys.index(key)))
        weights[leader] += drift
    return weights


def importance_from_weights(weights: Mapping[str, Any]) -> dict[str, int]:
    """Восстанавливает важность из весов старого профиля.

    Самый тяжёлый критерий становится «решает всё», остальные шкалируются от
    него. Точность здесь неважна: первое же сохранение запишет явные значения.
    """
    values: dict[str, float] = {}
    for key, _ in WEIGHTS:
        try:
            values[key] = float(weights.get(key, 0) or 0)
        except (TypeError, ValueError):
            values[key] = 0.0
    top = max(values.values()) if values else 0.0
    if top <= 0:
        return {key: 3 for key, _ in WEIGHTS}
    out: dict[str, int] = {}
    for key, value in values.items():
        if value <= 0:
            out[key] = 0
            continue
        out[key] = max(1, int(round(MAX_IMPORTANCE * value / top)))
    return out


def importance_of(data: Mapping[str, Any]) -> dict[str, int]:
    """Важность из профиля: явная, иначе выведенная из весов."""
    explicit = (data or {}).get("importance")
    if isinstance(explicit, Mapping) and explicit:
        out = {}
        for key, _ in WEIGHTS:
            try:
                out[key] = max(0, min(MAX_IMPORTANCE, int(explicit.get(key, 0) or 0)))
            except (TypeError, ValueError):
                out[key] = 0
        return out
    return importance_from_weights((data or {}).get("weights") or {})
