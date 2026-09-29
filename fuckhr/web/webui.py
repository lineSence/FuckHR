"""Локальный веб-интерфейс: единственный пульт управления программой.

Здесь четыре вещи, которые раньше жили в терминале:

- запуск: сбор, проверка модели и тесты — кнопками, с полоской и живым
  логом; тот же вывод дублируется в терминал, где запущен интерфейс;
- настройки: весь .env формой, профили поиска карточками и подробные параметры поиска;
- выдача: вакансии, условия, HR-флаги, карта, досье на компании, контакты, выдача
  поиска, маршруты модели;
- очистка: удаление накопленных данных по целям, с подтверждением там, где
  потеря необратима.

На странице запуска только то, что гоняется регулярно. Задачи, привязанные к
своим данным — шаг по цели, сбор адресов для карты — запускаются из своих
разделов, где видно, зачем они нужны. Отладочные прогоны без записи и подготовка
писем остаются в командной строке: python run.py --dry-run, python -m fuckhr.pipeline.outreach_run.

В командной строке остаётся и запуск самих программ: python run.py без флагов,
параметры они берут из .env. Так же их запускает планировщик Windows, и настройки
у них одни и те же.

Файл сознательно тонкий: здесь только сервер и маршруты. Страницы живут в
`ui_views.py`, `ui_forms.py`, `ui_resume.py`, `ui_map.py` и `ui_companies.py`, раздел профилей
целиком — в `webui_profile.py` и `ui_profiles.py`,
общие детали — в `ui_core.py`. Имена страниц проброшены сюда же, чтобы
webui.render_vacancies и подобные продолжали работать.

Границы, которые не нарушаются:

- слушает только 127.0.0.1 и отвечает только своим страницам (`ui_guard.py`):
  авторизации нет, выставлять его наружу нельзя;
- ничего не отправляет — ни писем, ни сообщений [CORE-023];
- на диск пишет только .env, profile.yaml и логи задач;
- без новых зависимостей: http.server из стандартной библиотеки справляется с одним
  пользователем.

Карта сама по себе эти границы не двигает: сервер отдаёт точки из базы, в сеть
ходит только браузер — за тайлами и Leaflet. Сбор адресов со страницы карты — это
та же фоновая задача из jobs.TASKS, что и из CLI.

Запуск:
    python webui.py                 # http://127.0.0.1:8765
    python webui.py --port 9000

В фоне (Windows) — без консольного окна, через pythonw.exe:
    .venv\\Scripts\\pythonw.exe webui.py

Точка входа внизу файла обязательна и проверяется тестом: без неё
`python webui.py` просто импортирует модуль и молча выходит с кодом 0 —
самый неприятный вид поломки: пустой вывод и нулевой статус.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from fuckhr.core import llm_profiles, settings
from fuckhr.llm import llm
from fuckhr.web import (
    jobs,
    ui_bench,
    ui_dataset,
    ui_gate,
    ui_guard,
    ui_injections,
    ui_map,
    ui_research,
    ui_run,
    ui_settings,
    ui_sources,
    ui_stages,
    ui_stats,
    ui_targets,
    webui_profile,
)
from fuckhr.web.ui_companies import (
    apply_cleanup,
    company_rows,
    render_cleanup,
    render_companies,
    render_company,
)
from fuckhr.web.ui_core import (
    DEFAULT_PORT,
    HOST,
    NAV,
    NAV_ITEMS,
    STYLE,
    area_field,
    checkbox_field,
    db_path,
    esc,
    number_field,
    open_db,
    page,
    table,
    text_field,
)
from fuckhr.web.ui_forms import (
    bench_models,
    profile_summary,
    render_llm,
    render_profile,
    render_search,
    save_facts,
    save_profile,
    search_settings_form,
    search_updates,
    start_bench,
)
from fuckhr.web.ui_resume import render_resume, save_resume
from fuckhr.web.ui_views import (
    contact_rows,
    progress_block,
    render_contacts,
    render_run,
    render_settings,
    render_vacancies,
    render_vacancy,
    vacancy_one,
    vacancy_rows,
)

log = logging.getLogger("webui")

# Адреса, которые существуют только для форм. GET сюда приходит не от ссылки,
# а от F5 или «назад», и отвечать на это «такой страницы нет» — грубо.
POST_ONLY = frozenset(
    {
        "/run", "/stop", "/loop", "/bench", "/dataset", "/llm/apply",
        "/intake/apply", "/map/geo", "/sources", "/runopts", "/cookie",
        "/gate/stages", "/gate/label", "/gate/train",
        "/area", "/deep", "/llm/stages",
    }
)


class Handler(BaseHTTPRequestHandler):
    server_version = "FuckHR-webui"
    profile_path = "profile.yaml"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.debug("%s", format % args)

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, body: str, status: int = 200) -> None:
        """Единственный не-HTML ответ: точки карты грузятся отдельно от страницы."""
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        """После POST всегда редирект: иначе F5 повторяет запуск задачи."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def _gate(self, path: str, form: dict[str, list[str]]) -> None:
        """Формы раздела «Гейт отзывов». Что делать, решает ui_gate."""
        job_id, body = ui_gate.post(path, form)
        if job_id is not None:
            self._redirect("/?job={}".format(job_id))
            return
        self._send(page("Гейт отзывов", body))

    def do_GET(self) -> None:  # noqa: N802
        if ui_guard.refuse(self):
            return
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        try:
            bare = GET_BARE.get(parsed.path)
            if bare is not None:
                bare(self, params)
                return
            conn = open_db()
            try:
                GET.get(parsed.path, get_missing)(self, conn, params)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — интерфейс не должен падать целиком
            log.exception("ошибка при обработке %s", self.path)
            self._send(page("Ошибка", "<pre>{}</pre>".format(esc(exc))), 500)

    def do_POST(self) -> None:  # noqa: N802
        if ui_guard.refuse(self):
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            form = self._form()
            handler = POST.get(parsed.path) or post_group(parsed.path)
            if handler is None:
                self._send(page("Не найдено", "<p>Такой страницы нет.</p>"), 404)
                return
            handler(self, parsed.path, form)
        except Exception as exc:  # noqa: BLE001
            log.exception("ошибка при обработке POST %s", self.path)
            self._send(page("Ошибка", "<pre>{}</pre>".format(esc(exc))), 500)


# --- Маршруты -----------------------------------------------------------------
# Страница — это строка в таблице GET/POST и функция. Обработчик GET получает
# открытое соединение с базой и параметры запроса; POST — адрес и форму.

Params = dict[str, list[str]]


def _one(params: Params, name: str, default: str = "") -> str:
    return (params.get(name) or [default])[0]


def _flat(params: Params) -> dict[str, str]:
    """Первое значение каждого параметра. Списки странице не нужны."""
    return {key: (value or [""])[0] for key, value in params.items()}


def _page_with_db(h: Handler, title: str, render: Callable[[Any], str]) -> None:
    conn = open_db()
    try:
        h._send(page(title, render(conn)))
    finally:
        conn.close()


def get_favicon(h: Handler, params: Params) -> None:
    h._send("", 404)


def get_run(h: Handler, params: Params) -> None:
    job_id = settings.as_int(_one(params, "job"), 0) or None
    # База нужна одному блоку — выбору площадок и их метрике.
    conn = open_db()
    try:
        body, refresh = render_run(job_id, conn=conn)
    finally:
        conn.close()
    h._send(page("Запуск", body, refresh, "/"))


def get_settings(h: Handler, params: Params) -> None:
    h._send(page("Настройки", render_settings()))


def get_vacancies(h: Handler, conn: Any, params: Params) -> None:
    # Параметры уходят страницей целиком: какие из них фильтры, знает
    # filters.py, а не маршрут. Неизвестные там молча игнорируются, в SQL
    # попадает только белый список.
    limit = min(settings.as_int(_one(params, "limit", "50"), 50), 500)
    body = ui_map.hint(conn) + render_vacancies(conn, 0.0, limit, "score", _flat(params))
    h._send(page("Вакансии", body))


def get_map(h: Handler, conn: Any, params: Params) -> None:
    # Карта читает те же вакансии, что и список: отдельного сбора для неё нет,
    # точки — это адреса из базы.
    h._send(page("Карта", ui_map.render_map(conn, _flat(params))))


def get_map_points(h: Handler, conn: Any, params: Params) -> None:
    # Точки отдельным ответом: страница открывается сразу, а метки приезжают
    # следом и только если Leaflet загрузился.
    h._send_json(ui_map.points_json(conn, _flat(params)))


def get_vacancy(h: Handler, conn: Any, params: Params) -> None:
    # GET ничего не запускает: сбор черновика дёргает внешний поиск и модель,
    # и обновление страницы жгло бы бюджет SEARCH_MAX_CALLS/LLM_MAX_CALLS [CORE-016].
    key = _one(params, "key")
    body = ui_map.link(conn, key) + render_vacancy(conn, key, with_draft=False)
    h._send(page("Вакансия", body))


def get_companies(h: Handler, conn: Any, params: Params) -> None:
    # Досье компаний — один раздел: канал без работодателя ничего не значит,
    # а работодатель без канала — не вход.
    body = render_companies(conn, _one(params, "csort"), _flat(params)) + render_contacts(
        conn, _one(params, "ksort")
    )
    h._send(page("Досье компаний", body))


def get_company(h: Handler, conn: Any, params: Params) -> None:
    # Звёздочка сверху: компанию из прогона можно перенести в «Цели» и дальше
    # копать её отдельно (ADR-025).
    name = _one(params, "name")
    body = (
        ui_targets.star_form(conn, name)
        + render_company(conn, name, _one(params, "jsort"), _one(params, "ksort"))
        + ui_research.render_research(conn, name)
    )
    h._send(page("Досье", body))


def get_targets(h: Handler, conn: Any, params: Params) -> None:
    h._send(page("Цели", ui_targets.render_targets(conn)))


def get_target(h: Handler, conn: Any, params: Params) -> None:
    body = ui_targets.render_target(conn, settings.as_int(_one(params, "id"), 0))
    h._send(page("Цель", body))


def get_cleanup(h: Handler, conn: Any, params: Params) -> None:
    h._send(page("Очистка", render_cleanup(conn)))


def get_profile(h: Handler, conn: Any, params: Params) -> None:
    # Без id — карточки всех профилей, с id — редактор одного.
    body = webui_profile.body(conn, h.profile_path, _one(params, "id"))
    h._send(page(webui_profile.TITLE, body))


def get_search(h: Handler, conn: Any, params: Params) -> None:
    body = render_search(conn, _one(params, "q"), _one(params, "company"))
    h._send(page("Проверка поиска", body))


def get_injections(h: Handler, conn: Any, params: Params) -> None:
    body = ui_injections.render_injections(conn, _one(params, "level"))
    h._send(page("Инъекции", body))


def get_llm(h: Handler, conn: Any, params: Params) -> None:
    # Пока идёт сравнение, страница обновляет себя сама: результат появляется
    # на месте формы, уходить в лог не нужно.
    body = (
        render_llm(conn, _one(params, "probe") == "1", _one(params, "embed") == "1")
        + ui_stages.render_stages(conn)
        + ui_dataset.render_dataset(conn)
    )
    h._send(page("Модель", body, ui_bench.refresh_seconds(), "/llm"))


def get_stats(h: Handler, conn: Any, params: Params) -> None:
    h._send(page("Статистика", ui_stats.render_stats(conn, _flat(params))))


def get_gate(h: Handler, conn: Any, params: Params) -> None:
    h._send(page("Гейт отзывов", ui_gate.render_gate(conn)))


def redirect_to(location: str) -> Callable[..., None]:
    """Старый адрес раздела, который переехал: ссылки в закладках не ломаются."""

    def handler(h: Handler, *_: Any) -> None:
        h._redirect(location)

    return handler


def get_missing(h: Handler, conn: Any, params: Params) -> None:
    if urllib.parse.urlparse(h.path).path in POST_ONLY:
        # Сюда попадают по F5 или по кнопке «назад» после POST. Главная с
        # историей задач полезнее, чем 404.
        h._redirect("/")
        return
    h._send(page("Не найдено", "<p>Такой страницы нет.</p>"), 404)


def post_run(h: Handler, path: str, form: Params) -> None:
    try:
        job = jobs.runner.start(_one(form, "task"))
    except (KeyError, RuntimeError) as exc:
        body, refresh = render_run(None, "<div class=warn>{}</div>".format(esc(exc)))
        h._send(page("Запуск", body, refresh, "/"))
        return
    h._redirect("/?job={}".format(job.id))


def post_map_geo(h: Handler, path: str, form: Params) -> None:
    # Сбор всех недостающих адресов. Из браузера не приходит ни одного
    # аргумента: команда целиком взята из jobs.TASKS [CORE-023].
    job_id, problem = ui_map.start_backfill()
    if job_id is None:
        _page_with_db(h, "Карта", lambda conn: ui_map.render_map(conn, {}, problem))
        return
    h._redirect("/?job={}".format(job_id))


def post_dataset(h: Handler, path: str, form: Params) -> None:
    # Сборка идёт минутами, поэтому уходим на страницу запуска с логом — как
    # «Адреса для карты» и «Шаг по цели».
    job_id, problem = ui_dataset.start_dataset(form)
    if job_id is None:
        h._send(page("Модель", ui_dataset.refused(problem)))
        return
    h._redirect("/?job={}".format(job_id))


def post_gate(h: Handler, path: str, form: Params) -> None:
    h._gate(path, form)


def post_bench(h: Handler, path: str, form: Params) -> None:
    note = start_bench(form)
    if note:
        _page_with_db(h, "Модель", lambda conn: render_llm(conn) + note)
        return
    h._redirect("/llm")


def post_llm_stages(h: Handler, path: str, form: Params) -> None:
    # Имена ключей сверяются с закрытым списком: из браузера приходит только
    # то, что мы сами нарисовали в таблице.
    allowed = set(llm_profiles.STAGE_MODEL_ENV.values()) | set(
        llm_profiles.LOCAL_STAGE_MODEL_ENV.values()
    )
    updates = {
        key: (values or [""])[0].strip() for key, values in form.items() if key in allowed
    }
    settings.save(updates)
    h._redirect("/llm")


def post_llm_apply(h: Handler, path: str, form: Params) -> None:
    # Из браузера приходят имя ключа и имя модели: ключ сверяется с закрытым
    # списком LLM_PROXY_MODEL_*, имя — с bench_models.
    updates = {}
    for key in form.get("apply") or []:
        if key not in ui_bench.ENV_KEYS:
            continue
        names = bench_models(_one(form, "model:" + key))
        if not names:
            continue
        # Каскадный ключ хранит порядок кандидатов, обычный — одно имя.
        updates[key] = (
            ",".join(names[: llm.MAX_CANDIDATES]) if key in ui_bench.CASCADE_KEYS else names[0]
        )
    saved = settings.save(updates) if updates else []
    if saved:
        note = "<div class=ok>Записано в .env: {}</div>".format(esc(", ".join(saved)))
    else:
        note = (
            "<div class=warn>Ничего не изменилось: либо профили не "
            "отмечены, либо там уже стоят эти модели.</div>"
        )
    _page_with_db(h, "Модель", lambda conn: note + render_llm(conn))


def post_research(h: Handler, path: str, form: Params) -> None:
    # Глубокий ресёрч по одной компании (ADR-019). Название из браузера
    # сверяется со своей базой внутри ui_research.
    conn = open_db()
    try:
        problem = ui_research.handle(conn, form)
    finally:
        conn.close()
    if problem:
        h._send(page("Досье", problem))
        return
    h._redirect("/?job={}".format(jobs.runner.last().id))


def post_loop(h: Handler, path: str, form: Params) -> None:
    ui_run.save_loop(form)
    h._redirect("/")


def post_company_setting(h: Handler, path: str, form: Params) -> None:
    # Настройка правится там, где виден её эффект: сфера отзывов — в блоке
    # разбивки, тумблер ресёрча — у его кнопки.
    company = _one(form, "company")
    if path == "/area":
        settings.save({"REVIEW_AREA": _one(form, "area").strip()})
    else:
        settings.save({"DEEP_ENABLED": "1" if form.get("enabled") else "0"})
    h._redirect("/company?name=" + urllib.parse.quote(company))


def post_runopts(h: Handler, path: str, form: Params) -> None:
    ui_run.save_options(form)
    h._redirect("/")


def post_cookie(h: Handler, path: str, form: Params) -> None:
    ui_run.save_cookie(form)
    h._redirect("/")


def post_sources(h: Handler, path: str, form: Params) -> None:
    saved = ui_sources.save(form)
    note = "<div class=ok>Площадки сохранены: {}</div>".format(
        esc(", ".join(saved) or "без изменений")
    )
    conn = open_db()
    try:
        body, refresh = render_run(None, note, conn=conn)
    finally:
        conn.close()
    h._send(page("Запуск", body, refresh, "/"))


def post_stop(h: Handler, path: str, form: Params) -> None:
    job_id = settings.as_int(_one(form, "job"), 0)
    # soft — прогон дописывает текущий цикл и выходит сам.
    ui_run.stop(job_id, bool(form.get("soft")))
    h._redirect("/?job={}".format(job_id))


def post_settings(h: Handler, path: str, form: Params) -> None:
    updates = settings.form_updates(form)
    # Галочки площадок отзывов складываются в одну настройку, поэтому
    # считаются отдельно от полей каталога.
    updates.update(ui_settings.review_sites_value(form))
    saved = settings.save(updates)
    h._send(page("Настройки", render_settings(saved)))


def post_cleanup(h: Handler, path: str, form: Params) -> None:
    # Удаление — единственное место, где результат показывается сразу, а не
    # через редирект: владелец должен видеть, сколько строк исчезло.
    def render(conn: Any) -> str:
        removed, problems = apply_cleanup(conn, form)
        return render_cleanup(conn, removed=removed, problems=problems)

    _page_with_db(h, "Очистка", render)


def post_search(h: Handler, path: str, form: Params) -> None:
    updates = search_updates(form)
    saved = settings.save(updates)
    # Параметры нужны уже на следующей странице, а не после перезапуска: поиск
    # живёт в этом же процессе, поэтому обновляем окружение.
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    _page_with_db(h, "Проверка поиска", lambda conn: render_search(conn, "", "", saved))


def post_targets(h: Handler, path: str, form: Params) -> None:
    # Раздел целей: добавление, выбор кандидата, шаги, слежение. В подпроцесс
    # уходит только id цели из своей базы (ADR-025).
    conn = open_db()
    try:
        note = ui_targets.handle(conn, path, form)
        if path == "/targets/step":
            body = ui_targets.render_target(conn, settings.as_int(_one(form, "id"), 0), note)
            h._send(page("Цель", body))
            return
        body = ui_targets.render_targets(conn, note, ui_targets.last_query(form))
        h._send(page("Цели", body))
    finally:
        conn.close()


def post_profile(h: Handler, path: str, form: Params) -> None:
    # Весь раздел профилей в одном месте: карточки, выключатель, критерии,
    # разговор и резюме правят одни и те же файлы.
    title, body, new_path = webui_profile.handle(path, form, h.profile_path)
    # Каталог профилей мог появиться прямо сейчас (первое «Добавить профиль»),
    # и RUN_PROFILE уже переписан — подхватываем без перезапуска сервера.
    Handler.profile_path = new_path
    h._send(page(title, body))


def post_vacancy(h: Handler, path: str, form: Params) -> None:
    # Черновик собирается только по явному действию, а не по открытию
    # страницы: у шага есть внешние вызовы и бюджет [CORE-016].
    key = _one(form, "key")
    _page_with_db(
        h,
        "Вакансия",
        lambda conn: ui_map.link(conn, key) + render_vacancy(conn, key, with_draft=True),
    )


def post_group(path: str) -> Callable[[Handler, str, Params], None] | None:
    """Разделы с несколькими формами: обработчик сам разбирает адрес."""
    if path.startswith("/gate/"):
        return post_gate
    if path.startswith("/targets/"):
        return post_targets
    if path in webui_profile.POST_PATHS:
        return post_profile
    return None


# Без базы: главная открывает её сама и только для одного блока.
GET_BARE: dict[str, Callable[[Handler, Params], None]] = {
    "/favicon.ico": get_favicon,
    "/": get_run,
    "/settings": get_settings,
}
GET: dict[str, Callable[[Handler, Any, Params], None]] = {
    "/vacancies": get_vacancies,
    "/map": get_map,
    "/map/points.json": get_map_points,
    "/vacancy": get_vacancy,
    "/companies": get_companies,
    "/company": get_company,
    "/targets": get_targets,
    "/target": get_target,
    "/cleanup": get_cleanup,
    "/contacts": redirect_to("/companies"),
    "/profile": get_profile,
    # Раздел один: резюме и критерии поиска — это один разговор.
    "/resume": redirect_to("/profile"),
    "/search": get_search,
    "/injections": get_injections,
    "/llm": get_llm,
    "/stats": get_stats,
    "/gate": get_gate,
}
POST: dict[str, Callable[[Handler, str, Params], None]] = {
    "/run": post_run,
    "/map/geo": post_map_geo,
    "/dataset": post_dataset,
    "/bench": post_bench,
    "/llm/stages": post_llm_stages,
    "/llm/apply": post_llm_apply,
    "/research": post_research,
    "/loop": post_loop,
    "/area": post_company_setting,
    "/deep": post_company_setting,
    "/runopts": post_runopts,
    "/cookie": post_cookie,
    "/sources": post_sources,
    "/stop": post_stop,
    "/settings": post_settings,
    "/cleanup": post_cleanup,
    "/search": post_search,
    "/vacancy": post_vacancy,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальный интерфейс FuckHR")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from fuckhr.core.logs import (
        quiet_libraries,  # noqa: PLC0415 — логи настраивает только запуск
    )

    quiet_libraries(args.verbose)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        log.warning("python-dotenv не установлен: читаю только переменные окружения")

    Handler.profile_path = settings.get("RUN_PROFILE", "profile.yaml")
    try:
        server = HTTPServer((HOST, args.port), Handler)
    except OSError as exc:
        # Самый частый случай — интерфейс уже запущен в фоне. Трасса здесь
        # бесполезна, полезна подсказка.
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)):
            log.error(
                "порт %s уже занят: интерфейс либо уже работает на http://%s:%s, "
                "либо порт занял другой процесс; возьми другой через --port",
                args.port,
                HOST,
                args.port,
            )
            return 1
        raise
    log.info("интерфейс здесь: http://%s:%s (Ctrl+C чтобы остановить)", HOST, args.port)
    log.info("база: %s · настройки: %s", db_path(), settings.ENV_PATH)
    log.info("лог задач дублируется в этот терминал")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("остановлен")
    finally:
        server.server_close()
    return 0


__all__ = (
    "DEFAULT_PORT",
    "HOST",
    "Handler",
    "NAV",
    "NAV_ITEMS",
    "POST_ONLY",
    "STYLE",
    "apply_cleanup",
    "area_field",
    "checkbox_field",
    "company_rows",
    "contact_rows",
    "db_path",
    "esc",
    "main",
    "number_field",
    "open_db",
    "page",
    "profile_summary",
    "progress_block",
    "render_cleanup",
    "render_companies",
    "render_company",
    "render_contacts",
    "render_llm",
    "render_profile",
    "render_resume",
    "render_run",
    "render_search",
    "render_settings",
    "render_vacancies",
    "render_vacancy",
    "save_facts",
    "save_profile",
    "save_resume",
    "search_settings_form",
    "search_updates",
    "table",
    "text_field",
    "vacancy_one",
    "vacancy_rows",
)


if __name__ == "__main__":
    raise SystemExit(main())
