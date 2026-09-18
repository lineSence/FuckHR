"""Разбор и сохранение profile.yaml для формы в интерфейсе (ADR-013).

Почему отдельный модуль, а не код внутри страницы. Профиль — единственный файл,
ошибка в котором тихо обессмысливает всю работу: затёрся queries — утренний прогон
вернёт пустоту, съехали веса — скоринг начнёт врать. Такой код должен быть
тестируем без браузера и без HTTP-сервера, поэтому здесь только чистые функции
над dict и словарём полей формы (тот, что отдаёт urllib.parse.parse_qs).

Справочники полей, шкала важности и пересчёт в веса вынесены в profile_fields.py
по [CORE-024] и реэкспортируются ниже: вызывающий код продолжает брать
`profile_form.WEIGHTS` и `profile_form.area_label`.

Два правила, которые важнее удобства:

1. Неизвестные ключи сохраняются. Если в profile.yaml руками добавили поле, о
   котором форма не знает, сохранение его не стирает.
2. Замечания вместо запрета. Пустой facts — повод предупредить, а не причина
   отказаться сохранять. Нечисло в числовом поле — наоборот, причина оставить
   старое значение: молча заменить min_score на ноль значит вывалить в Telegram
   всю базу.

Форма понимает два поколения полей. Новое: каждый запрос — свой слот из четырёх
полей (q1_text, q1_area, q1_period, q1_pages), регионы выбираются из списка городов,
вместо весов — важность 0–5. Старое поколение (одно поле queries со строками
через «|», явные веса) понимается тоже: так профиль правят скриптом и руками.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from profile_fields import (  # noqa: F401 — реэкспорт для старых вызовов
    AREA_CHOICES,
    AREA_NAMES,
    CURRENCY_CHOICES,
    DEFAULT_PROFILE,
    EXPERIENCE,
    IMPORTANCE_LEVELS,
    LIST_FIELDS,
    MAX_IMPORTANCE,
    MAX_SLOTS,
    MIN_SLOTS,
    PAGE_CHOICES,
    PERIOD_CHOICES,
    SPARE_SLOTS,
    WEIGHT_HINTS,
    WEIGHTS,
    area_label,
    importance_from_weights,
    importance_of,
    normalize_weights,
)


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


def _apply_queries(
    result: dict[str, Any], form: Mapping[str, Sequence[str]], problems: list[str]
) -> None:
    if "queries" in form:
        queries, query_problems = parse_queries(_one(form, "queries"))
    else:
        queries, query_problems = parse_query_slots(form)
    problems.extend(query_problems)
    result["queries"] = queries
    if not queries:
        problems.append("ни одного запроса: сбор ничего не найдёт")


def _apply_geo(
    result: dict[str, Any], form: Mapping[str, Sequence[str]], problems: list[str]
) -> None:
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


def _apply_weights(
    result: dict[str, Any], form: Mapping[str, Sequence[str]], problems: list[str]
) -> None:
    """Важность из новой формы или явные веса из старой."""
    if any(("importance_" + key) in form for key, _ in WEIGHTS):
        importance = importance_of(result)
        for key, label in WEIGHTS:
            value = int(
                _number(
                    form,
                    "importance_" + key,
                    "Важность «{}»".format(label),
                    problems,
                    importance.get(key, 0),
                )
                or 0
            )
            importance[key] = max(0, min(MAX_IMPORTANCE, value))
        result["importance"] = importance
        result["weights"] = normalize_weights(importance)
        if not any(importance.values()):
            problems.append(
                "все критерии отмечены как неважные — разделил вес равно между ними"
            )
        return

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
    if not touched:
        return
    result["weights"] = weights
    total = sum(float(value or 0) for value in weights.values())
    if abs(total - 100.0) > 0.001:
        problems.append(
            "сумма весов {:g}, а не 100: скоры станет не с чем сравнивать".format(total)
        )


def apply_form(
    data: Mapping[str, Any], form: Mapping[str, Sequence[str]]
) -> tuple[dict[str, Any], list[str]]:
    """Накладывает данные формы на профиль и возвращает (профиль, замечания).

    Исходный dict не меняется. Ключи, которых нет в форме, остаются как были.
    """
    result: dict[str, Any] = dict(data or {})
    problems: list[str] = []

    _apply_queries(result, form, problems)

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

    _apply_geo(result, form, problems)

    chosen = [value for value in (form.get("experience_ok") or []) if value]
    known = {key for key, _ in EXPERIENCE}
    result["experience_ok"] = [value for value in chosen if value in known]
    if not result["experience_ok"]:
        problems.append("не отмечен ни один уровень опыта: фильтр по опыту не работает")

    _apply_weights(result, form, problems)

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
