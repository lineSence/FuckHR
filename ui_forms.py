"""Страницы с формами: поиск с подробными настройками SearXNG и модель.

Зачем настройки поиска живут рядом с выдачей, а не на общей странице настроек:
движки, язык и период подбираются только одним способом — меняешь и сразу
смотришь, что отдал инстанс.

Форма профиля переехала в ui_profile: она перестала быть формой над YAML и стала
отдельным экраном фильтра с собственными блоками и предпросмотром порога.
Здесь остались точечное сохранение фактов и короткая сводка профиля, которые
нужны другим страницам, плюс реэкспорт render_profile/save_profile для старых
импортов.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Sequence

import yaml

import contacts
import llm
import profile_form
import settings
import websearch
from ui_core import esc, number_field, table, text_field
from ui_profile import LIST_HINTS, render_profile, save_profile


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

    unmapped = gateway.unmapped_profiles() if gateway.proxy_base_url else []
    if unmapped:
        items = "".join(
            "<li>{profile}: уйдёт model={sent}, задать в {env}</li>".format(
                profile=esc(profile), sent=esc(sent), env=esc(env)
            )
            for profile, sent, env in unmapped
        )
        parts.append(
            "<div class=warn>У части профилей нет имени модели на прокси. Если "
            "такого алиаса нет в его config.yaml, запрос вернёт 400 Bad Request."
            "<ul>{}</ul></div>".format(items)
        )

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


# ———— профиль: точечные операции ————


def save_facts(profile_path: str | Path, text: str) -> tuple[str, ...]:
    """Перезаписывает только блок facts, остальное в профиле не трогает.

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
    """Короткая сводка профиля для страницы запуска."""
    path = Path(profile_path)
    if not path.exists():
        return [("файл", "{} не найден".format(path))]
    data = profile_form.load(path)
    queries = data.get("queries") or []
    salary = data.get("salary") or {}
    geo = data.get("geo") or {}
    skills = data.get("skills") or []
    query_names = []
    for item in queries:
        if isinstance(item, dict):
            query_names.append(str(item.get("text", "")))
        else:
            query_names.append(str(item))
    areas = ", ".join(
        profile_form.area_label(code) for code in (geo.get("areas") or [])
    )
    return [
        ("запросы", ", ".join(q for q in query_names if q) or "не заданы"),
        ("где ищем", areas or "не задано"),
        ("минимум на руки", str(salary.get("min_net", "не задан"))),
        ("порог скоринга", str(data.get("min_score", "не задан"))),
        ("навыки", ", ".join(str(s) for s in skills) or "не заданы"),
    ]


__all__ = (
    "LIST_HINTS",
    "profile_summary",
    "render_llm",
    "render_profile",
    "render_search",
    "save_facts",
    "save_profile",
    "search_settings_form",
    "search_updates",
)
