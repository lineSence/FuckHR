"""Разбор и сохранение profile.yaml для формы в интерфейсе (ADR-013).

Почему отдельный модуль, а не код внутри страницы. Профиль — единственный файл,
ошибка в котором тихо обессмысливает всю работу: затёрся queries — утренний прогон
вернёт пустоту, съехали веса — скоринг начнёт врать. Такой код должен быть
тестируем без браузера и без HTTP-сервера, поэтому здесь только чистые функции
над dict и словарём полей формы (тот, что отдаёт urllib.parse.parse_qs).

Два правила, которые важнее удобства:

1. Неизвестные ключи сохраняются. Если в profile.yaml руками добавили поле, о
   котором форма не знает, сохранение его не стирает.
2. Замечания вместо запрета. Пустой facts — повод предупредить, а не причина
   отказаться сохранять. Нечисло в числовом поле — наоборот, причина оставить
   старое значение: молча заменить min_score на ноль значит вывалить в Telegram
   всю базу.

Что поменялось в подаче. Раньше форма была пересказом YAML: запросы одной
строкой «текст | 113 | 7 | 3», регионы числами, веса с требованием суммы 100.
Всё это требовало знания формата файла. Теперь:

- каждый запрос — свой слот из четырёх полей (q1_text, q1_area, q1_period, q1_pages);
- регионы выбираются из списка городов, а ID вводятся только для редких случаев;
- вместо весов — важность от 0 до 5 для каждого критерия, а веса в 100 баллов
  пересчитываются сами (normalize_weights).

Важность хранится в профиле рядом с весами, под ключом importance. score.py про
неё не знает и продолжает читать weights — именно поэтому веса всё равно
записываются в файл, а не вычисляются на лету. Если importance в файле нет
(профиль старый или правлен руками), она восстанавливается из весов.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

# Опыт в обозначениях hh.ru и по-человечески.
EXPERIENCE: tuple[tuple[str, str], ...] = (
    ("noExperience", "без опыта"),
    ("between1And3", "1–3 года"),
    ("between3And6", "3–6 лет"),
    ("moreThan6", "более 6 лет"),
)

# Критерии скоринга: ключ в weights, подпись, пояснение для формы.
WEIGHTS: tuple[tuple[str, str], ...] = (
    ("skills", "навыки"),
    ("salary", "зарплата"),
    ("nice_to_have", "желательные"),
    ("remote", "удалёнка"),
    ("experience", "опыт"),
)

WEIGHT_HINTS: dict[str, str] = {
    "skills": "Совпадение с обязательными навыками.",
    "salary": "Насколько вилка выше твоего минимума.",
    "nice_to_have": "Совпадение с желательными навыками.",
    "remote": "Удалённый или гибридный формат.",
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

# Сколько страниц выдачи брать. Одна страница — до 50 вакансий.
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


def load(path: str | Path) -> dict[str, Any]:
    """Читает профиль. Отсутствие файла и битый YAML — пустой профиль, не падение."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def save(path: str | Path, data: Mapping[str, Any]) -> None:
    """Пишет профиль без сортировки ключей: файл читают люди, порядок привычен."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# --- строки и списки ---------------------------------------------------------


def split_items(text: str) -> list[str]:
    """Список из строк или запятых — как человеку удобнее, без повторов.

    Запятая разделитель потому, что навыки копируют из вакансий одной строкой.
    Составные навыки с запятой внутри встречаются реже, чем желание вставить
    «Python, FastAPI, PostgreSQL» одним движением.
    """
    seen: set[str] = set()
    out: list[str] = []
    normalized = (text or "").replace("\r\n", "\n").replace(",", "\n")
    for raw in normalized.split("\n"):
        item = raw.strip()
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        out.append(item)
    return out


def lines_to_list(text: str) -> list[str]:
    """Построчный список без пустых строк и без повторов, порядок сохраняется.

    Запятая здесь не разделитель: факт о себе и стоп-фраза спокойно её содержат.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def list_to_lines(items: Iterable[Any]) -> str:
    return "\n".join(str(item) for item in (items or []))


def parse_int_list(text: str) -> tuple[list[int], list[str]]:
    """Список регионов: числа через запятую или перевод строки."""
    problems: list[str] = []
    values: list[int] = []
    for chunk in (text or "").replace("\n", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            problems.append("регион «{}» не число, пропустил".format(item))
    return values, problems


def area_label(code: Any) -> str:
    """Имя региона для показа; неизвестный ID показывается как есть."""
    try:
        return AREA_NAMES[int(code)]
    except (TypeError, ValueError, KeyError):
        return "регион {}".format(code)


# --- важность и веса -----------------------------------------------------------


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


# --- запросы -------------------------------------------------------------------


def slot_count(queries: Sequence[Any]) -> int:
    """Сколько слотов показать в форме."""
    return max(MIN_SLOTS, min(MAX_SLOTS, len(queries or []) + SPARE_SLOTS))


def query_slots(queries: Sequence[Any], count: int | None = None) -> list[dict[str, Any]]:
    """Запросы в слоты формы; последние слоты пустые и готовы к заполнению."""
    items = list(queries or [])
    total = count or slot_count(items)
    slots: list[dict[str, Any]] = []
    for index in range(total):
        item = items[index] if index < len(items) else None
        if isinstance(item, Mapping):
            slots.append(
                {
                    "text": str(item.get("text") or ""),
                    "area": item.get("area"),
                    "period": item.get("period"),
                    "max_pages": item.get("max_pages"),
                }
            )
        elif item is None:
            slots.append({"text": "", "area": None, "period": None, "max_pages": None})
        else:
            slots.append(
                {"text": str(item), "area": None, "period": None, "max_pages": None}
            )
    return slots


def parse_query_slots(
    form: Mapping[str, Sequence[str]], count: int = MAX_SLOTS
) -> tuple[list[dict[str, Any]], list[str]]:
    """Собирает запросы из полей q{n}_text / q{n}_area / q{n}_period / q{n}_pages.

    Пустой текст — слот просто не заполнен, и это не ошибка: именно так запрос
    удаляют — стирают текст и сохраняют.
    """
    problems: list[str] = []
    queries: list[dict[str, Any]] = []
    for number in range(1, count + 1):
        text = _one(form, "q{}_text".format(number)).strip()
        if not text:
            continue
        query: dict[str, Any] = {"text": text}
        for suffix, name in (("area", "area"), ("period", "period"), ("pages", "max_pages")):
            raw = _one(form, "q{}_{}".format(number, suffix)).strip()
            if not raw:
                continue
            try:
                query[name] = int(raw)
            except ValueError:
                problems.append(
                    "запрос «{}»: значение «{}» не число, оставил пустым".format(
                        text[:40], raw
                    )
                )
        queries.append(query)
    return queries, problems


def render_queries(queries: Sequence[Mapping[str, Any]]) -> str:
    """Запросы в текст «текст | регион | дней | страниц».

    Форма им больше не пользуется, но формат остался удобным для показа в одну
    строку и для тестов.
    """
    lines: list[str] = []
    for item in queries or []:
        if not isinstance(item, Mapping):
            lines.append(str(item))
            continue
        lines.append(
            " | ".join(
                [
                    str(item.get("text") or ""),
                    str(item.get("area") if item.get("area") is not None else ""),
                    str(item.get("period") if item.get("period") is not None else ""),
                    str(item.get("max_pages") if item.get("max_pages") is not None else ""),
                ]
            ).rstrip(" |")
        )
    return "\n".join(lines)


def parse_queries(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Разбор строк «текст | регион | дней | страниц».

    Оставлен для совместимости: так профиль можно править скриптом или руками.
    """
    problems: list[str] = []
    queries: list[dict[str, Any]] = []
    for number, raw in enumerate((text or "").replace("\r\n", "\n").split("\n"), 1):
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        query: dict[str, Any] = {"text": parts[0]}
        if not query["text"]:
            problems.append("строка {}: пустой текст запроса, пропустил".format(number))
            continue
        for index, name in ((1, "area"), (2, "period"), (3, "max_pages")):
            if len(parts) <= index or not parts[index]:
                continue
            try:
                query[name] = int(parts[index])
            except ValueError:
                problems.append(
                    "строка {}: {} = «{}» не число, пропустил поле".format(
                        number, name, parts[index]
                    )
                )
        queries.append(query)
    return queries, problems


# --- форма ---------------------------------------------------------------------


def _one(form: Mapping[str, Sequence[str]], key: str, default: str = "") -> str:
    values = form.get(key) or []
    return values[0] if values else default


def _checked(form: Mapping[str, Sequence[str]], key: str) -> bool:
    """Флажок. Выключенный в теле запроса не приходит вообще — это норма HTML."""
    return bool(form.get(key))


def _number(
    form: Mapping[str, Sequence[str]],
    key: str,
    label: str,
    problems: list[str],
    previous: Any,
) -> Any:
    raw = _one(form, key).strip().replace(",", ".").replace(" ", "").replace("\u00a0", "")
    if raw == "":
        return previous
    try:
        value = float(raw)
    except ValueError:
        problems.append("{}: «{}» не число, оставил прежнее значение".format(label, raw))
        return previous
    return int(value) if value.is_integer() else value


def apply_form(
    data: Mapping[str, Any], form: Mapping[str, Sequence[str]]
) -> tuple[dict[str, Any], list[str]]:
    """Накладывает данные формы на профиль и возвращает (профиль, замечания).

    Исходный dict не меняется. Ключи, которых нет в форме, остаются как были.

    Форма понимает два вида запросов: слоты q1_text… (новая страница) и одно поле
    queries со строками через «|» (старые тесты и внешние скрипты).
    """
    result: dict[str, Any] = dict(data or {})
    problems: list[str] = []

    if "queries" in form:
        queries, query_problems = parse_queries(_one(form, "queries"))
    else:
        queries, query_problems = parse_query_slots(form)
    problems.extend(query_problems)
    result["queries"] = queries
    if not queries:
        problems.append("ни одного запроса: сбор ничего не найдёт")

    salary = dict(result.get("salary") or {})
    salary["min_net"] = _number(
        form, "salary_min_net", "Зарплата на руки", problems, salary.get("min_net", 0)
    )
    currency = _one(form, "salary_currency").strip().upper()
    salary["currency"] = currency or salary.get("currency") or "RUR"
    salary["allow_missing"] = _checked(form, "salary_allow_missing")
    result["salary"] = salary

    for key, _label in LIST_FIELDS:
        if key not in form:
            continue
        # Навыки и стоп-слова принимаются и строками, и через запятую.
        result[key] = split_items(_one(form, key))

    geo = dict(result.get("geo") or {})
    chosen_areas: list[int] = []
    for raw in form.get("geo_area") or []:
        value = (raw or "").strip()
        if not value:
            continue
        try:
            chosen_areas.append(int(value))
        except ValueError:
            problems.append("регион «{}» не число, пропустил".format(value))
    extra_areas, area_problems = parse_int_list(_one(form, "geo_areas_extra"))
    problems.extend(area_problems)
    if "geo_areas" in form:
        # Старая форма с одним текстовым полем.
        legacy_areas, legacy_problems = parse_int_list(_one(form, "geo_areas"))
        problems.extend(legacy_problems)
        chosen_areas.extend(legacy_areas)
    areas: list[int] = []
    for code in chosen_areas + extra_areas:
        if code not in areas:
            areas.append(code)
    if areas:
        geo["areas"] = areas
    elif "geo_area" in form or "geo_areas" in form or "geo_areas_extra" in form:
        problems.append("не выбран ни один регион, оставил прежние")
    geo["remote_ok"] = _checked(form, "geo_remote_ok")
    result["geo"] = geo

    chosen = [value for value in (form.get("experience_ok") or []) if value]
    known = {key for key, _ in EXPERIENCE}
    result["experience_ok"] = [value for value in chosen if value in known]
    if not result["experience_ok"]:
        problems.append("не отмечен ни один уровень опыта: фильтр по опыту не работает")

    uses_importance = any(
        ("importance_" + key) in form for key, _ in WEIGHTS
    )
    if uses_importance:
        importance = importance_of(result)
        for key, label in WEIGHTS:
            importance[key] = int(
                _number(
                    form,
                    "importance_" + key,
                    "Важность «{}»".format(label),
                    problems,
                    importance.get(key, 0),
                )
                or 0
            )
            importance[key] = max(0, min(MAX_IMPORTANCE, importance[key]))
        result["importance"] = importance
        result["weights"] = normalize_weights(importance)
        if not any(importance.values()):
            problems.append(
                "все критерии отмечены как неважные — разделил вес равно между ними"
            )
    else:
        # Старая форма с явными весами.
        weights = dict(result.get("weights") or {})
        touched = False
        for key, label in WEIGHTS:
            if ("weight_" + key) not in form:
                continue
            touched = True
            weights[key] = _number(
                form, "weight_" + key, "Вес «{}»".format(label), problems, weights.get(key, 0)
            )
        if touched:
            result["weights"] = weights
            total = sum(float(value or 0) for value in weights.values())
            if abs(total - 100.0) > 0.001:
                problems.append(
                    "сумма весов {:g}, а не 100: скоры станет не с чем сравнивать".format(total)
                )

    result["min_score"] = _number(
        form, "min_score", "Порог скора", problems, result.get("min_score", 45)
    )

    if "facts" in form:
        result["facts"] = lines_to_list(_one(form, "facts"))
        if not result["facts"]:
            problems.append("facts пуст: письма будут без цифр о тебе")

    return result, problems


def form_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """Готовые значения для отрисовки формы."""
    data = data or {}
    salary = data.get("salary") or {}
    geo = data.get("geo") or {}
    weights = data.get("weights") or {}
    areas = [code for code in (geo.get("areas") or [])]
    known = {code for code, _ in AREA_CHOICES}
    return {
        "queries": render_queries(data.get("queries") or []),
        "query_slots": query_slots(data.get("queries") or []),
        "salary_min_net": salary.get("min_net", ""),
        "salary_currency": salary.get("currency", "RUR"),
        "salary_allow_missing": bool(salary.get("allow_missing", False)),
        "skills": list_to_lines(data.get("skills") or []),
        "nice_to_have": list_to_lines(data.get("nice_to_have") or []),
        "stop_words": list_to_lines(data.get("stop_words") or []),
        "geo_areas": ", ".join(str(area) for area in areas),
        "geo_area_codes": areas,
        "geo_areas_extra": ", ".join(
            str(area) for area in areas if area not in known
        ),
        "geo_remote_ok": bool(geo.get("remote_ok", False)),
        "experience_ok": list(data.get("experience_ok") or []),
        "importance": importance_of(data),
        "weights": {key: weights.get(key, 0) for key, _ in WEIGHTS},
        "min_score": data.get("min_score", ""),
        "facts": list_to_lines(data.get("facts") or []),
    }


__all__ = (
    "AREA_CHOICES",
    "AREA_NAMES",
    "CURRENCY_CHOICES",
    "DEFAULT_PROFILE",
    "EXPERIENCE",
    "IMPORTANCE_LEVELS",
    "LIST_FIELDS",
    "MAX_IMPORTANCE",
    "MAX_SLOTS",
    "PAGE_CHOICES",
    "PERIOD_CHOICES",
    "WEIGHTS",
    "WEIGHT_HINTS",
    "apply_form",
    "area_label",
    "form_values",
    "importance_from_weights",
    "importance_of",
    "lines_to_list",
    "list_to_lines",
    "load",
    "normalize_weights",
    "parse_int_list",
    "parse_queries",
    "parse_query_slots",
    "query_slots",
    "render_queries",
    "save",
    "slot_count",
    "split_items",
)
