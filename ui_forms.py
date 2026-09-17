"""Страницы с формами: поиск с подробными настройками SearXNG, модель, профиль.

Зачем настройки поиска живут рядом с выдачей, а не на общей странице настроек:
движки, язык и период подбираются только одним способом — меняешь и сразу
смотришь, что отдал инстанс.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Sequence

import yaml

import contacts
import llm
import outreach
import profile_form
import settings
import websearch
from ui_core import (
    area_field,
    checkbox_field,
    esc,
    number_field,
    table,
    text_field,
)


# ———— поиск ————


def search_settings_form(saved: Sequence[str] = ()) -> str:
    """Подробные параметры инстанса: метаданные полей берутся из websearch."""
    values = settings.load()
    parts = []
    if saved:
        parts.append("<div class=ok>Сохранено: {}</div>".format(esc(", ".join(saved))))

    parts.append('<form method=post action="/search">')
    parts.append("<div class=cols>")
    for key, label, kind, default, hint in websearch.SEARXNG_FIELDS:
        current = values.get(key, "")
        if current == "":
            current = os.getenv(key, "")
        full_hint = "{} · {}".format(key, hint)
        if kind == "int":
            parts.append(number_field(key, label, current or default, full_hint))
        else:
            parts.append(text_field(key, label, current, full_hint, default))
    parts.append("</div>")
    parts.append("<p><button>Сохранить параметры поиска</button></p></form>")
    parts.append(
        "<p class=muted>Пустое текстовое поле стирает значение и возвращает поведение "
        "инстанса по умолчанию. Все эти параметры входят в ключ кэша: после изменения "
        "тот же запрос выполнится заново, а не вернётся из кэша.</p>"
    )
    return "".join(parts)


def search_updates(form: dict[str, list[str]]) -> dict[str, str]:
    """Только известные ключи: из браузера в .env не попадает ничего сверх списка."""
    updates: dict[str, str] = {}
    for key, _label, _kind, _default, _hint in websearch.SEARXNG_FIELDS:
        if key not in form:
            continue
        updates[key] = (form.get(key) or [""])[0].strip()
    return updates


def render_search(
    conn: sqlite3.Connection,
    query: str,
    company: str,
    saved: Sequence[str] = (),
) -> str:
    """Живая проверка выдачи: видно, что именно отдаёт поиск до ранжирования."""
    provider = websearch.SearchProvider.from_env(conn)
    state = "готов" if provider.enabled else esc(provider.disabled_reason)

    head = (
        '<form method=get action="/search">'
        'Запрос <input type=text name=q value="{query}" style="width:60%"> '
        "<button>Искать</button></form>"
        '<form method=get action="/search">'
        'Или запросы по компании <input type=text name=company value="{company}" '
        'style="width:40%"> <button>Показать и выполнить</button></form>'
        "<p class=muted>Состояние: {state}</p>"
    ).format(query=esc(query), company=esc(company), state=state)

    rows = [[esc(name), esc(value)] for name, value in provider.describe()]
    current = "<h2>С какими параметрами идёт запрос</h2>" + table(
        ["Параметр", "Значение"], rows
    )
    form = "<h2>Настройки поиска</h2>" + search_settings_form(saved)

    if not provider.enabled:
        return (
            head
            + "<div class=warn>Внешний поиск выключен, искать негде: не задан "
            "SEARCH_BASE_URL. Заполни адрес инстанса ниже — через SSH-туннель это "
            "http://127.0.0.1:8888.</div>"
            + form
            + current
        )

    queries = [query] if query else []
    if company:
        queries = list(websearch.contact_queries(company))
    if not queries:
        return (
            head
            + "<p class=muted>Введи запрос или название компании.</p>"
            + form
            + current
        )

    parts = [head, "<h2>Запросы</h2><pre>{}</pre>".format(esc("\n".join(queries)))]
    hits = provider.search_many(queries, limit=5)
    usage = provider.usage
    parts.append(
        (
            "<p class=muted>Вызовов: {calls} · из кэша: {cached} · "
            "ошибок: {failures} · пропущено: {skipped}</p>"
        ).format(
            calls=usage.calls,
            cached=usage.cached,
            failures=usage.failures,
            skipped=usage.skipped,
        )
    )

    if not hits:
        parts.append(
            "<div class=warn>Пустая выдача. Проверь, жив ли туннель, отвечает ли "
            "инстанс форматом json и не сузили ли выборку движки или период.</div>"
        )
        return "".join(parts) + form + current

    body = []
    for hit in hits:
        link = '<a href="{}" target=_blank rel=noreferrer>{}</a>'.format(
            esc(hit.url), esc(hit.title or hit.url)
        )
        body.append(
            [link, esc(contacts.domain_of(hit.url) or ""), esc(hit.snippet[:300])]
        )
    parts.append(table(["Страница", "Домен", "Сниппет"], body))
    return "".join(parts) + form + current


# ———— модель ————


def render_llm(conn: sqlite3.Connection, probe: bool = False) -> str:
    """Какой этап на какую модель уходит — без запуска пайплайна."""
    if not settings.flag("LLM_ENABLED"):
        return (
            '<div class=warn>Модель выключена в <a href="/settings">настройках</a>. '
            "Пайплайн работает без неё целиком, просто грубее.</div>"
        )

    gateway = llm.Gateway.from_env(conn)
    parts = []
    if not gateway.enabled:
        parts.append(
            "<div class=danger>Шлюз не готов: {}</div>".format(
                esc(gateway.disabled_reason)
            )
        )

    rows = []
    for stage, profile, route, model in gateway.describe_routes():
        marker = ""
        if stage in llm.PERSONAL_STAGES and route == llm.ROUTE_PROXY:
            marker = ' <span class=pill>персональные данные уходят наружу</span>'
        rows.append(
            [esc(stage), esc(profile), esc(route) + marker, esc(model or "не задана")]
        )
    parts.append(table(["Этап", "Профиль", "Маршрут", "Модель"], rows))

    if settings.flag("LLM_PERSONAL_VIA_PROXY"):
        parts.append(
            "<div class=danger>Включён режим, при котором ФИО и адреса живых людей "
            "уходят на внешний прокси. Если прокси ходит наружу, а не в твою Ollama, "
            "это утечка чужих персональных данных [CORE-012].</div>"
        )

    parts.append(
        '<p><a href="/llm?probe=1">Спросить список моделей у прокси</a> '
        "<span class=muted>(один запрос к /v1/models)</span></p>"
    )

    if probe:
        try:
            names = gateway.models()
            if names:
                items = "".join("<li>{}</li>".format(esc(name)) for name in names)
                parts.append("<h2>Модели на прокси</h2><ul>{}</ul>".format(items))
            else:
                parts.append(
                    "<div class=warn>Прокси не отдал список моделей. Чаще всего это "
                    "отсутствующий ключ: без него LiteLLM отвечает 401.</div>"
                )
        except Exception as exc:  # noqa: BLE001 — сеть может лежать, это не повод падать
            parts.append("<div class=danger>{}</div>".format(esc(exc)))

    parts.append(
        "<p class=muted>Живой вызов на выдуманном тексте — задача «Проверка модели» "
        'на <a href="/">странице запуска</a>.</p>'
    )
    return "".join(parts)


# ———— профиль ————


def save_facts(profile_path: str | Path, text: str) -> tuple[str, ...]:
    """Перезаписывает только блок facts, остальное в профиле не трогает.

    Точечное сохранение фактов; полная форма профиля живёт в profile_form.
    Пустые строки отбрасываются здесь же: именно они разбирались в None и уезжали
    в письмо как факт о себе.
    """
    path = Path(profile_path)
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    facts = [line.strip() for line in text.splitlines() if line.strip()]
    data["facts"] = facts
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return tuple(facts)


def profile_summary(profile_path: str | Path) -> list[tuple[str, str]]:
    """Короткая сводка профиля."""
    path = Path(profile_path)
    if not path.exists():
        return [("файл", "{} не найден".format(path))]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    queries = data.get("queries") or []
    salary = data.get("salary") or {}
    skills = data.get("skills") or []
    query_names = []
    for item in queries:
        if isinstance(item, dict):
            query_names.append(str(item.get("text", "")))
        else:
            query_names.append(str(item))
    return [
        ("запросы", ", ".join(q for q in query_names if q) or "не заданы"),
        ("минимум на руки", str(salary.get("min_net", "не задан"))),
        ("порог скоринга", str(data.get("min_score", "не задан"))),
        ("навыки", ", ".join(str(s) for s in skills) or "не заданы"),
    ]


LIST_HINTS = {
    "skills": "По одному на строку. Дают основную часть скора.",
    "nice_to_have": "Добавляют баллы, но не обязательны.",
    "stop_words": "Вакансия с таким словом отбрасывается до скоринга.",
}


def render_profile(
    profile_path: str,
    saved: int | None = None,
    problems: Sequence[str] = (),
) -> str:
    """Полный редактор profile.yaml прямо в интерфейсе.

    Неизвестные ключи файла сохраняются: ручные правки в YAML не теряются.
    """
    data = profile_form.load(profile_path)
    values = profile_form.form_values(data)

    parts: list[str] = []
    if saved is not None:
        parts.append(
            "<div class=ok>Сохранено в {}. Фактов: {}</div>".format(
                esc(profile_path), saved
            )
        )
    for problem in problems:
        parts.append("<div class=warn>{}</div>".format(esc(problem)))

    if not values["facts"] and saved is None:
        parts.append(
            "<div class=warn>Блок facts пуст. Без него каждое письмо собирается с "
            "заглушкой вместо повода писать.</div>"
        )

    parts.append('<form method=post action="/profile">')

    parts.append("<h2>Запросы к hh.ru</h2>")
    parts.append(
        area_field(
            "queries",
            "Один запрос на строку",
            values["queries"],
            "Формат: текст | регион | дней | страниц. Например: python разработчик | 113 | 7 | 3. "
            "Регионы hh.ru: 1 — Москва, 2 — Петербург, 113 — вся Россия. Пропущенные поля "
            "заменяются значениями сбора по умолчанию.",
        )
    )

    parts.append("<h2>Зарплата</h2><div class=cols>")
    parts.append(
        number_field(
            "salary_min_net",
            "Минимум на руки",
            values["salary_min_net"],
            "Гросс пересчитывается с вычетом 13%.",
        )
    )
    parts.append(
        text_field(
            "salary_currency",
            "Валюта",
            values["salary_currency"],
            "Обычно RUR.",
            "RUR",
        )
    )
    parts.append("</div>")
    parts.append(
        checkbox_field(
            "salary_allow_missing",
            "Пропускать дальше вакансии без указанной зарплаты",
            values["salary_allow_missing"],
            "Выключить — и большая часть рынка отсеется сразу: зарплату часто не пишут.",
        )
    )

    parts.append("<h2>Навыки и стоп-слова</h2>")
    for key, label in profile_form.LIST_FIELDS:
        parts.append(area_field(key, label, values[key], LIST_HINTS.get(key, "")))

    parts.append("<h2>География и опыт</h2>")
    parts.append(
        text_field(
            "geo_areas",
            "Регионы",
            values["geo_areas"],
            "Числа через запятую. 113 — вся Россия.",
            "113",
        )
    )
    parts.append(
        checkbox_field("geo_remote_ok", "Удалёнка подходит", values["geo_remote_ok"])
    )
    checks = []
    for key, label in profile_form.EXPERIENCE:
        checks.append(
            (
                '<label><input type=checkbox name=experience_ok value="{key}"{checked}> '
                "{label}</label>"
            ).format(
                key=esc(key),
                checked=" checked" if key in values["experience_ok"] else "",
                label=esc(label),
            )
        )
    parts.append(
        (
            "<div class=field><label>Подходящий опыт</label>"
            "<div class=checks>{checks}</div>"
            "<div class=hint>Обозначения hh.ru. Снять всё — значит выключить фильтр "
            "по опыту.</div></div>"
        ).format(checks="".join(checks))
    )

    parts.append("<h2>Веса скоринга</h2>")
    parts.append(
        "<p class=muted>Сумма должна быть 100: иначе порог скора не с чем сравнивать.</p>"
    )
    parts.append("<div class=cols>")
    for key, label in profile_form.WEIGHTS:
        parts.append(number_field("weight_" + key, label, values["weights"].get(key, 0)))
    parts.append("</div>")
    parts.append(
        number_field(
            "min_score",
            "Порог скора для карточки",
            values["min_score"],
            "Вакансии ниже порога попадают в базу, но не идут в Telegram.",
        )
    )

    parts.append("<h2>Факты о себе</h2>")
    parts.append(
        area_field(
            "facts",
            "По одному на строку, с цифрами",
            values["facts"],
            "Только эти строки попадают в письмо: ничего кроме них система о вас не напишет.",
        )
    )

    parts.append("<p><button>Сохранить профиль</button></p></form>")
    parts.append(
        "<p class=muted>Файл: {}. Новые значения подхватываются со следующего запуска "
        "задачи.</p>".format(esc(profile_path))
    )
    return "".join(parts)


def save_profile(
    profile_path: str, form: dict[str, list[str]]
) -> tuple[int, list[str]]:
    """Принимает форму целиком и возвращает (сколько фактов, замечания)."""
    data = profile_form.load(profile_path)
    updated, problems = profile_form.apply_form(data, form)
    profile_form.save(profile_path, updated)
    return len(updated.get("facts") or []), problems


__all__ = (
    "profile_summary",
    "render_llm",
    "render_profile",
    "render_search",
    "save_facts",
    "save_profile",
    "search_settings_form",
    "search_updates",
)
