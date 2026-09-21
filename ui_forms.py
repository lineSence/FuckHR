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
import re
import sqlite3
import time
from pathlib import Path
from typing import Sequence

import yaml

import bench  # noqa: F401 — STAGES нужен вызывающим
import ui_bench
from ui_bench import render_bench_form  # реэкспорт: имя осталось прежним
import contacts
import embeddings_store
import jobs
import llm
import llm_embed
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


# Имена моделей у LiteLLM выглядят как openai/gpt-4o-mini, qwen2.5:7b, auto:fast.
BENCH_NAME_RE = re.compile(r"[A-Za-z0-9._:/@-]{1,80}")


# ———— модель ————


def render_llm(
    conn: sqlite3.Connection, probe: bool = False, embed: bool = False
) -> str:
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
    for stage, profile, route, model, source in gateway.describe_routes():
        marker = ""
        if stage in llm.PERSONAL_STAGES and route == llm.ROUTE_PROXY:
            marker = ' <span class=pill>персональные данные уходят наружу</span>'
        rows.append(
            [
                esc(stage),
                esc(profile),
                esc(route) + marker,
                esc(model or "не задана"),
                esc(source),
            ]
        )
    parts.append(
        table(["Этап", "Профиль", "Маршрут", "Модель", "Имя из"], rows)
    )
    parts.append(
        "<p class=muted>«Имя из» — откуда взято имя модели: каскад этапа, имя этапа, "
        "имя профиля. «По умолчанию» у локального маршрута значит, что в запрос "
        "уходит название профиля: шлюзу с одной моделью этого хватает, а Ollama с "
        "несколькими ответит «model not found» — задай LLM_LOCAL_MODEL_* или "
        "LLM_LOCAL_STAGE_MODEL_*.</p>"
    )

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
        '<p><a href="/llm?probe=1">Спросить список моделей</a> '
        "<span class=muted>(по одному запросу /v1/models на каждый заданный адрес: "
        "прокси и локальный)</span></p>"
    )

    known: list[str] = []
    if probe:
        block, known = models_probe(gateway)
        parts.append(block)

    parts.append(embeddings_block(conn, gateway, embed))

    parts.append(
        "<p class=muted>Живой вызов на выдуманном тексте — задача «Проверка модели» "
        'на <a href="/">странице запуска</a>.</p>'
    )
    parts.append(render_bench_form(known=known))
    return "".join(parts)


def models_probe(gateway: llm.Gateway) -> tuple[str, list[str]]:
    """Списки моделей с обоих адресов и имена с прокси для формы сравнения.

    Локальный список нужен не меньше проксёвого: имена вроде `qwen3:8b`
    раньше смотрели только через `ollama list` в терминале, а теперь их есть
    куда вписать — LLM_LOCAL_MODEL_* и LLM_LOCAL_STAGE_MODEL_*.

    В форму сравнения чекбоксами попадают только имена с прокси: бенч ходит
    через него, и локальное имя там ответило бы 400.
    """
    parts: list[str] = []
    known: list[str] = []
    addresses = (
        (
            llm.ROUTE_PROXY,
            "Прокси",
            gateway.proxy_base_url,
            "Чаще всего это отсутствующий ключ: без него LiteLLM отвечает 401.",
            "они ниже чекбоксами в форме сравнения",
        ),
        (
            llm.ROUTE_LOCAL,
            "Локальный адрес",
            gateway.base_url,
            "Проверь, поднят ли сервер и есть ли в LLM_BASE_URL суффикс /v1.",
            "эти имена ставятся в LLM_LOCAL_MODEL_* и LLM_LOCAL_STAGE_MODEL_*",
        ),
    )
    for route, title, base, why, note in addresses:
        if not base:
            continue
        try:
            names = gateway.models(route)
        except Exception as exc:  # noqa: BLE001 — сеть может лежать, это не повод падать
            parts.append(
                "<div class=danger>{}: {}</div>".format(esc(title), esc(exc))
            )
            continue
        if not names:
            parts.append(
                "<div class=warn>{title} ({base}) не отдал список моделей. {why}</div>".format(
                    title=esc(title), base=esc(base), why=esc(why)
                )
            )
            continue
        if route == llm.ROUTE_PROXY:
            known = names
        parts.append(
            "<div class=ok><b>{title}</b> знает {count} моделей — {note}: {names}</div>".format(
                title=esc(title),
                count=len(names),
                note=esc(note),
                names=esc(", ".join(names[:40])),
            )
        )
    return "".join(parts), known


def embeddings_block(
    conn: sqlite3.Connection, gateway: llm.Gateway, probe: bool = False
) -> str:
    """Эмбеддер отдельным блоком: у него своя модель и свои грабли.

    Проверка «Модель» живым вызовом сюда не доходит: check_llm.py дёргает чат,
    а `/v1/embeddings` — другая ручка и часто другая модель. Отсюда и отдельная
    кнопка: один вектор на коротком тексте отвечает сразу на все вопросы —
    отвечает ли адрес, знает ли сервер это имя модели и какая у неё размерность.
    """
    route = gateway.route_for(llm_embed.STAGE)
    model = llm_embed.model_name(gateway)
    enabled = settings.embeddings_options().enabled
    rows = [
        ["Считать векторы", "да" if enabled else "нет (EMBEDDINGS_ENABLED)"],
        ["Маршрут", esc(route.name) if route else "нет маршрута"],
        ["Адрес", esc(route.base_url) if route else "—"],
        ["Модель", esc(model or "не задана (LLM_LOCAL_STAGE_MODEL_EMBEDDINGS)")],
    ]
    for kind, name, count in embeddings_store.counts(conn):
        rows.append(
            ["Векторов в базе: {}".format(esc(kind)), "{} · {}".format(count, esc(name))]
        )
    parts = ["<h2>Векторы текстов</h2>", table(["Что", "Значение"], rows)]

    if route is not None and route.name == llm.ROUTE_PROXY:
        parts.append(
            "<div class=warn>Этап предпочитает локальный адрес, но его нет, "
            "поэтому векторы уйдут на прокси. Имя модели должно совпадать с "
            "алиасом из его config.yaml, иначе будет 400.</div>"
        )
    if not model:
        parts.append(
            "<div class=warn>Имя модели не задано — этап пропускается. Задай "
            "LLM_LOCAL_STAGE_MODEL_EMBEDDINGS в .env или поле «Этап embeddings» в "
            '<a href="/settings">настройках</a>; значение берётся из '
            "<code>ollama list</code> или из списка моделей выше.</div>"
        )

    parts.append(
        '<p><a href="/llm?embed=1">Проверить эмбеддер живым вызовом</a> '
        "<span class=muted>(один короткий текст)</span></p>"
    )
    if probe:
        started = time.monotonic()
        vectors = llm_embed.embed(gateway, ["проверка связи"])
        spent = time.monotonic() - started
        if vectors:
            parts.append(
                "<div class=ok>{model}: вектор из {dim} чисел за {sec:.1f} с. "
                "Размерность запоминается с первым вектором и менять модель "
                "после этого нельзя [LLM-011].</div>".format(
                    model=esc(model), dim=len(vectors[0]), sec=spent
                )
            )
        else:
            parts.append(
                "<div class=danger>Вектор не получен. Причина одной строкой — "
                "в логе интерфейса: там видно и код ответа, и текст ошибки "
                "сервера.</div>"
            )
    return "".join(parts)


def start_bench(form: dict) -> str:
    """Запускает сравнение моделей из формы страницы «Модель».

    Пустая строка — задача пошла. Иначе это готовый кусок страницы с
    объяснением: единственная задача с аргументами из браузера, и объяснять
    отказ надо на месте, а не кодом ответа.
    """
    # Имена приходят и чекбоксами, и строкой: склеиваем и чистим. В argv[0]
    # из браузера не попадает ничего — команда берётся из jobs.TASKS.
    models = bench_models(",".join(form.get("models") or []))
    stages = [s for s in (form.get("stage") or []) if s in bench.STAGES]
    repeat = max(1, min(5, settings.as_int((form.get("repeat") or ["1"])[0], 1)))
    if not models:
        note = (
            "<div class=warn>Не разобрал ни одного имени модели. "
            "Пиши их через запятую, как в config.yaml прокси.</div>"
        )
        return render_bench_form(note)
    extra = ["--models", ",".join(models), "--repeat", str(repeat)]
    if stages:
        extra += ["--stages", ",".join(stages)]
    try:
        jobs.runner.start("bench", extra)
    except (KeyError, RuntimeError) as exc:
        return "<div class=warn>{}</div>".format(esc(exc))
    return ""


def bench_models(raw: str) -> list[str]:
    """Имена моделей из формы. Всё подозрительное молча выбрасывается.

    Это единственное место, где строка из браузера идёт в командную строку,
    поэтому список символов закрытый: буквы, цифры и то, что встречается в
    именах моделей LiteLLM.
    """
    out = []
    for chunk in (raw or "").replace(";", ",").split(","):
        name = chunk.strip()
        if name and BENCH_NAME_RE.fullmatch(name) and name not in out:
            out.append(name)
    return out[:6]


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
    "bench_models",
    "embeddings_block",
    "models_probe",
    "start_bench",
    "render_bench_form",
    "render_llm",
    "render_profile",
    "render_search",
    "save_facts",
    "save_profile",
    "search_settings_form",
    "search_updates",
)
