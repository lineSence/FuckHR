"""Разбор и сохранение profile.yaml для формы в интерфейсе (ADR-013).

Почему отдельный модуль, а не код внутри webui.py. Профиль — единственный файл,
ошибка в котором тихо обессмысливает всю работу: затёрся queries — утренний прогон
вернёт пустоту, съехали веса — скоринг начнёт врать. Такой код должен быть
тестируем без браузера и без HTTP-сервера, поэтому здесь только чистые функции
над dict и словарём полей формы (тот, что отдаёт urllib.parse.parse_qs).

Два правила, которые важнее удобства:

1. Неизвестные ключи сохраняются. Если в profile.yaml руками добавили поле, о
   котором форма не знает, сохранение его не стирает.
2. Замечания вместо запрета. Неверная сумма весов или пустой facts — повод
   предупредить, а не причина отказаться сохранять. Нечисло в числовом поле —
   наоборот, причина оставить старое значение: молча заменить min_score на ноль
   значит вывалить в Telegram всю базу.

Запросы редактируются одной строкой на запрос:

    python разработчик | 113 | 7 | 3
    текст             | регион | дней | страниц

Текстовая строка вместо четырёх полей ввода на каждый запрос — сознательное решение:
добавить и удалить строку проще, чем жать «добавить запрос» без JavaScript.
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

# Веса скоринга. Порядок важен только для вида формы.
WEIGHTS: tuple[tuple[str, str], ...] = (
    ("skills", "навыки"),
    ("salary", "зарплата"),
    ("nice_to_have", "желательные"),
    ("remote", "удалёнка"),
    ("experience", "опыт"),
)

# Списки, которые редактируются построчно: ключ и подпись.
LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("skills", "Обязательные навыки"),
    ("nice_to_have", "Желательные навыки"),
    ("stop_words", "Стоп-слова"),
)

DEFAULT_PROFILE: dict[str, Any] = {
    "queries": [],
    "salary": {"min_net": 0, "currency": "RUR", "allow_missing": True},
    "skills": [],
    "nice_to_have": [],
    "stop_words": [],
    "geo": {"areas": [113], "remote_ok": True},
    "experience_ok": [],
    "weights": {key: 0 for key, _ in WEIGHTS},
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


def lines_to_list(text: str) -> list[str]:
    """Построчный список без пустых строк и без повторов, порядок сохраняется."""
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


def render_queries(queries: Sequence[Mapping[str, Any]]) -> str:
    """Запросы в текст для textarea."""
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

    Пропущенные поля остаются незаполненными — сбор подставит свои значения
    по умолчанию. Выдумывать их здесь значило бы спрятать от пользователя, с какими
    параметрами пойдёт поиск.
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
    raw = _one(form, key).strip().replace(",", ".")
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
    """
    result: dict[str, Any] = dict(data or {})
    problems: list[str] = []

    queries, query_problems = parse_queries(_one(form, "queries"))
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
        result[key] = lines_to_list(_one(form, key))

    geo = dict(result.get("geo") or {})
    areas, area_problems = parse_int_list(_one(form, "geo_areas"))
    problems.extend(area_problems)
    if areas:
        geo["areas"] = areas
    geo["remote_ok"] = _checked(form, "geo_remote_ok")
    result["geo"] = geo

    chosen = [value for value in (form.get("experience_ok") or []) if value]
    known = {key for key, _ in EXPERIENCE}
    result["experience_ok"] = [value for value in chosen if value in known]
    if not result["experience_ok"]:
        problems.append("не отмечен ни один уровень опыта: фильтр по опыту не работает")

    weights = dict(result.get("weights") or {})
    for key, label in WEIGHTS:
        weights[key] = _number(
            form, "weight_" + key, "Вес «{}»".format(label), problems, weights.get(key, 0)
        )
    result["weights"] = weights
    total = sum(float(value or 0) for value in weights.values())
    if abs(total - 100.0) > 0.001:
        problems.append(
            "сумма весов {:g}, а не 100: скоры станет не с чем сравнивать".format(total)
        )

    result["min_score"] = _number(
        form, "min_score", "Порог скора", problems, result.get("min_score", 45)
    )

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
    return {
        "queries": render_queries(data.get("queries") or []),
        "salary_min_net": salary.get("min_net", ""),
        "salary_currency": salary.get("currency", "RUR"),
        "salary_allow_missing": bool(salary.get("allow_missing", False)),
        "skills": list_to_lines(data.get("skills") or []),
        "nice_to_have": list_to_lines(data.get("nice_to_have") or []),
        "stop_words": list_to_lines(data.get("stop_words") or []),
        "geo_areas": ", ".join(str(area) for area in (geo.get("areas") or [])),
        "geo_remote_ok": bool(geo.get("remote_ok", False)),
        "experience_ok": list(data.get("experience_ok") or []),
        "weights": {key: weights.get(key, 0) for key, _ in WEIGHTS},
        "min_score": data.get("min_score", ""),
        "facts": list_to_lines(data.get("facts") or []),
    }


__all__ = (
    "DEFAULT_PROFILE",
    "EXPERIENCE",
    "LIST_FIELDS",
    "WEIGHTS",
    "apply_form",
    "form_values",
    "lines_to_list",
    "list_to_lines",
    "load",
    "parse_int_list",
    "parse_queries",
    "render_queries",
    "save",
)
