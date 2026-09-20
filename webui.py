"""Локальный веб-интерфейс: единственный пульт управления программой.

Здесь четыре вещи, которые раньше жили в терминале:

- запуск: сбор, письма, проверка модели и тесты — кнопками, с полоской и живым
  логом; тот же вывод дублируется в терминал, где запущен интерфейс;
- настройки: весь .env формой, profile.yaml целиком и подробные параметры поиска;
- выдача: вакансии, условия, HR-флаги, досье на компании, контакты, выдача
  поиска, маршруты модели;
- очистка: удаление накопленных данных по целям, с подтверждением там, где
  потеря необратима.

В командной строке остаётся только запуск самих программ: python run.py и
python outreach.py без флагов, параметры они берут из .env. Так же их запускает
планировщик Windows, и настройки у них одни и те же.

Файл сознательно тонкий: здесь только сервер и маршруты. Страницы живут в
`ui_views.py`, `ui_forms.py`, `ui_profile.py`, `ui_resume.py` и `ui_companies.py`,
общие детали — в `ui_core.py`. Имена страниц проброшены сюда же, чтобы
webui.render_vacancies и подобные продолжали работать.

Границы, которые не нарушаются:

- слушает только 127.0.0.1: ни авторизации, ни CSRF-защиты здесь нет, и выставлять
  его наружу нельзя;
- ничего не отправляет — ни писем, ни сообщений [CORE-023];
- на диск пишет только .env, profile.yaml и логи задач;
- без новых зависимостей: http.server из стандартной библиотеки справляется с одним
  пользователем.

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

import jobs
import llm
import profile_form
import settings
from ui_companies import (
    apply_cleanup,
    company_rows,
    render_cleanup,
    render_companies,
    render_company,
)
from ui_core import (
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
import ui_bench
from ui_forms import (
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
import intake
import ui_injections
import ui_research
import ui_run
import ui_intake
from ui_resume import render_resume, save_resume
from ui_views import (
    vacancy_rows,
    contact_rows,
    progress_block,
    render_contacts,
    render_run,
    render_settings,
    render_vacancies,
    render_vacancy,
    vacancy_one,
)

log = logging.getLogger("webui")

# Адреса, которые существуют только для форм. GET сюда приходит не от ссылки,
# а от F5 или «назад», и отвечать на это «такой страницы нет» — грубо.
POST_ONLY = frozenset(
    {"/run", "/stop", "/loop", "/bench", "/llm/apply", "/intake/apply"}
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

    def _profile_page(
        self,
        conn: object,
        plan: object = None,
        note: str = "",
        saved: int | None = None,
        problems: tuple[str, ...] = (),
        resume_note: str = "",
    ) -> str:
        """Одна страница из трёх частей: разговор, критерии поиска, резюме."""
        return (
            "<h2>Разговор о поиске</h2>"
            + ui_intake.render_intake(conn, self.profile_path, plan, note)
            + "<h2>Критерии поиска</h2>"
            + render_profile(self.profile_path, saved=saved, problems=problems)
            + "<h2>Резюме</h2>"
            + render_resume(conn, self.profile_path, saved=resume_note)
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        def one(name: str, default: str = "") -> str:
            return (params.get(name) or [default])[0]

        def flat(values: dict) -> dict:
            """Первое значение каждого параметра. Списки странице не нужны."""
            return {key: (value or [""])[0] for key, value in values.items()}

        try:
            if parsed.path == "/favicon.ico":
                self._send("", 404)
                return
            if parsed.path == "/":
                job_id = settings.as_int(one("job"), 0) or None
                body, refresh = render_run(job_id)
                self._send(page("Запуск", body, refresh, "/"))
                return
            if parsed.path == "/settings":
                self._send(page("Настройки", render_settings()))
                return

            conn = open_db()
            try:
                if parsed.path == "/vacancies":
                    # Параметры уходят страницей целиком: какие из них фильтры,
                    # знает filters.py, а не маршрут. Неизвестные там молча
                    # игнорируются, в SQL попадает только белый список.
                    limit = min(settings.as_int(one("limit", "50"), 50), 500)
                    self._send(
                        page(
                            "Вакансии",
                            render_vacancies(conn, 0.0, limit, "score", flat(params)),
                        )
                    )
                elif parsed.path == "/vacancy":
                    # GET ничего не запускает: сбор черновика дёргает внешний
                    # поиск и модель, и обновление страницы жгло бы бюджет
                    # SEARCH_MAX_CALLS/LLM_MAX_CALLS [CORE-016].
                    body = render_vacancy(conn, one("key"), with_draft=False)
                    self._send(page("Вакансия", body))
                elif parsed.path == "/companies":
                    # Компании и контакты — один раздел: канал без работодателя
                    # ничего не значит, а работодатель без канала — не вход.
                    self._send(
                        page(
                            "Компании и контакты",
                            render_companies(conn, one("csort"), flat(params))
                            + render_contacts(conn, one("ksort")),
                        )
                    )
                elif parsed.path == "/company":
                    body = render_company(
                        conn, one("name"), one("jsort"), one("ksort")
                    ) + ui_research.render_research(conn, one("name"))
                    self._send(page("Досье", body))
                elif parsed.path == "/cleanup":
                    self._send(page("Очистка", render_cleanup(conn)))
                elif parsed.path == "/contacts":
                    self._redirect("/companies")
                elif parsed.path == "/profile":
                    self._send(page("Профиль и резюме", self._profile_page(conn)))
                elif parsed.path == "/resume":
                    # Раздел один: резюме и критерии поиска — это один разговор.
                    self._redirect("/profile")
                elif parsed.path == "/search":
                    body = render_search(conn, one("q"), one("company"))
                    self._send(page("Проверка поиска", body))
                elif parsed.path == "/injections":
                    self._send(
                        page(
                            "Инъекции",
                            ui_injections.render_injections(conn, one("level")),
                        )
                    )
                elif parsed.path == "/llm":
                    # Пока идёт сравнение, страница обновляет себя сама: результат
                    # появляется на месте формы, уходить в лог не нужно.
                    self._send(
                        page(
                            "Модель",
                            render_llm(conn, one("probe") == "1", one("embed") == "1"),
                            ui_bench.refresh_seconds(),
                            "/llm",
                        )
                    )
                elif parsed.path in POST_ONLY:
                    # Сюда попадают по F5 или по кнопке «назад» после POST.
                    # Главная с историей задач полезнее, чем 404.
                    self._redirect("/")
                else:
                    self._send(page("Не найдено", "<p>Такой страницы нет.</p>"), 404)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — интерфейс не должен падать целиком
            log.exception("ошибка при обработке %s", self.path)
            self._send(page("Ошибка", "<pre>{}</pre>".format(esc(exc))), 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            form = self._form()

            if parsed.path == "/run":
                task = (form.get("task") or [""])[0]
                try:
                    job = jobs.runner.start(task)
                except (KeyError, RuntimeError) as exc:
                    body, refresh = render_run(
                        None, "<div class=warn>{}</div>".format(esc(exc))
                    )
                    self._send(page("Запуск", body, refresh, "/"))
                    return
                self._redirect("/?job={}".format(job.id))
                return

            if parsed.path == "/bench":
                note = start_bench(form)
                if note:
                    conn = open_db()
                    try:
                        self._send(page("Модель", render_llm(conn) + note))
                    finally:
                        conn.close()
                    return
                self._redirect("/llm")
                return

            if parsed.path == "/llm/apply":
                # Из браузера приходят имя ключа и имя модели: ключ сверяется
                # с закрытым списком LLM_PROXY_MODEL_*, имя — с bench_models.
                updates = {}
                for key in form.get("apply") or []:
                    if key not in ui_bench.ENV_KEYS:
                        continue
                    names = bench_models((form.get("model:" + key) or [""])[0])
                    if not names:
                        continue
                    # Каскадный ключ хранит порядок кандидатов, обычный — одно имя.
                    updates[key] = (
                        ",".join(names[: llm.MAX_CANDIDATES])
                        if key in ui_bench.CASCADE_KEYS
                        else names[0]
                    )
                saved = settings.save(updates) if updates else []
                if saved:
                    note = "<div class=ok>Записано в .env: {}</div>".format(
                        esc(", ".join(saved))
                    )
                else:
                    note = (
                        "<div class=warn>Ничего не изменилось: либо профили не "
                        "отмечены, либо там уже стоят эти модели.</div>"
                    )
                conn = open_db()
                try:
                    self._send(page("Модель", note + render_llm(conn)))
                finally:
                    conn.close()
                return

            if parsed.path == "/research":
                # Глубокий ресёрч по одной компании (ADR-019). Название из
                # браузера сверяется со своей базой внутри ui_research.
                conn = open_db()
                try:
                    problem = ui_research.handle(conn, form)
                finally:
                    conn.close()
                if problem:
                    self._send(page("Досье", problem))
                    return
                self._redirect("/?job={}".format(jobs.runner.last().id))
                return

            if parsed.path == "/loop":
                ui_run.save_loop(form)
                self._redirect("/")
                return

            if parsed.path == "/stop":
                job_id = settings.as_int((form.get("job") or [""])[0], 0)
                # soft — прогон дописывает текущий цикл и выходит сам.
                ui_run.stop(job_id, bool(form.get("soft")))
                self._redirect("/?job={}".format(job_id))
                return

            if parsed.path == "/settings":
                updates = settings.form_updates(form)
                saved = settings.save(updates)
                self._send(page("Настройки", render_settings(saved)))
                return

            if parsed.path == "/cleanup":
                # Удаление единственное место, где результат показывается сразу, а не через
                # редирект: владелец должен видеть, сколько строк исчезло.
                conn = open_db()
                try:
                    removed, problems = apply_cleanup(conn, form)
                    body = render_cleanup(conn, removed=removed, problems=problems)
                    self._send(page("Очистка", body))
                finally:
                    conn.close()
                return

            if parsed.path == "/resume":
                # Как и на очистке, ответ рисуется сразу: после сохранения нужно
                # сказать, что именно модель предложила и что ждёт подтверждения;
                # редирект это сообщение теряет.
                flat = {key: values[0] for key, values in form.items() if values}
                conn = open_db()
                try:
                    saved = save_resume(conn, flat, self.profile_path)
                    body = self._profile_page(conn, resume_note=saved)
                    self._send(page("Профиль и резюме", body))
                finally:
                    conn.close()
                return

            if parsed.path == "/search":
                updates = search_updates(form)
                saved = settings.save(updates)
                # Параметры нужны уже на следующей странице, а не после перезапуска:
                # поиск живёт в этом же процессе, поэтому обновляем окружение.
                for key, value in updates.items():
                    if value:
                        os.environ[key] = value
                    else:
                        os.environ.pop(key, None)
                conn = open_db()
                try:
                    body = render_search(conn, "", "", saved)
                    self._send(page("Проверка поиска", body))
                finally:
                    conn.close()
                return

            if parsed.path == "/profile":
                count, problems = save_profile(self.profile_path, form)
                conn = open_db()
                try:
                    body = self._profile_page(
                        conn, saved=count, problems=tuple(problems)
                    )
                    self._send(page("Профиль и резюме", body))
                finally:
                    conn.close()
                return

            if parsed.path == "/intake":
                # Реплика владельца: один вызов модели, ответ показывается
                # предложением с галочками. Ничего не применяется само.
                conn = open_db()
                try:
                    if (form.get("action") or [""])[0] == "clear":
                        intake.clear(conn)
                        body = self._profile_page(
                            conn, note="<div class=ok>Разговор очищен.</div>"
                        )
                        self._send(page("Профиль и резюме", body))
                        return
                    said, dialogue = ui_intake.compose(
                        form.get("question") or [],
                        form.get("answer") or [],
                        (form.get("text") or [""])[0],
                    )
                    if not said:
                        body = self._profile_page(
                            conn,
                            note="<div class=warn>Пустое сообщение.</div>",
                        )
                        self._send(page("Профиль и резюме", body))
                        return
                    intake.log_message(conn, "owner", said)
                    gateway = (
                        llm.Gateway.from_env(conn)
                        if settings.flag("LLM_ENABLED")
                        else None
                    )
                    plan = intake.ask(
                        gateway,
                        intake.owner_words(conn),
                        profile_form.load(self.profile_path),
                        context=dialogue,
                    )
                    reply = plan.summary or (
                        "\n".join(plan.questions) if plan.questions else "Ответа нет."
                    )
                    intake.log_message(conn, "ai", reply)
                    intake.save_plan(conn, plan)
                    self._send(
                        page("Профиль и резюме", self._profile_page(conn, plan))
                    )
                finally:
                    conn.close()
                return

            if parsed.path == "/intake/apply":
                conn = open_db()
                try:
                    note = ui_intake.apply_plan(
                        conn,
                        self.profile_path,
                        (form.get("plan") or [""])[0],
                        form.get("apply") or [],
                    )
                    body = self._profile_page(
                        conn, note="<div class=ok>{}</div>".format(esc(note))
                    )
                    self._send(page("Профиль и резюме", body))
                finally:
                    conn.close()
                return

            if parsed.path == "/vacancy":
                # Черновик собирается только по явному действию, а не по открытию
                # страницы: у шага есть внешние вызовы и бюджет [CORE-016].
                key = (form.get("key") or [""])[0]
                conn = open_db()
                try:
                    body = render_vacancy(conn, key, with_draft=True)
                    self._send(page("Вакансия", body))
                finally:
                    conn.close()
                return

            self._send(page("Не найдено", "<p>Такой страницы нет.</p>"), 404)
        except Exception as exc:  # noqa: BLE001
            log.exception("ошибка при обработке POST %s", self.path)
            self._send(page("Ошибка", "<pre>{}</pre>".format(esc(exc))), 500)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальный интерфейс FuckHR")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
)


if __name__ == "__main__":
    raise SystemExit(main())
