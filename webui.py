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
`ui_views.py`, `ui_forms.py`, `ui_profile.py` и `ui_companies.py`, общие детали — в
`ui_core.py`. Имена страниц проброшены сюда же, чтобы webui.render_vacancies и
подобные продолжали работать.

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
from ui_forms import (
    profile_summary,
    render_llm,
    render_profile,
    render_search,
    save_facts,
    save_profile,
    search_settings_form,
    search_updates,
)
from ui_views import (
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

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        def one(name: str, default: str = "") -> str:
            return (params.get(name) or [default])[0]

        try:
            if parsed.path == "/favicon.ico":
                self._send("", 404)
                return
            if parsed.path == "/":
                job_id = settings.as_int(one("job"), 0) or None
                body, refresh = render_run(job_id)
                self._send(page("Запуск", body, refresh))
                return
            if parsed.path == "/settings":
                self._send(page("Настройки", render_settings()))
                return
            if parsed.path == "/profile":
                self._send(page("Профиль", render_profile(self.profile_path)))
                return

            conn = open_db()
            try:
                if parsed.path == "/vacancies":
                    min_score = settings.as_float(one("min_score", "0"), 0.0)
                    limit = min(settings.as_int(one("limit", "50"), 50), 500)
                    self._send(
                        page("Вакансии", render_vacancies(conn, min_score, limit))
                    )
                elif parsed.path == "/vacancy":
                    body = render_vacancy(
                        conn, one("key"), with_draft=one("draft") == "1"
                    )
                    self._send(page("Вакансия", body))
                elif parsed.path == "/companies":
                    self._send(page("Компании", render_companies(conn)))
                elif parsed.path == "/company":
                    self._send(page("Досье", render_company(conn, one("name"))))
                elif parsed.path == "/cleanup":
                    self._send(page("Очистка", render_cleanup(conn)))
                elif parsed.path == "/contacts":
                    self._send(page("Контакты", render_contacts(conn)))
                elif parsed.path == "/search":
                    body = render_search(conn, one("q"), one("company"))
                    self._send(page("Проверка поиска", body))
                elif parsed.path == "/llm":
                    self._send(page("Модель", render_llm(conn, one("probe") == "1")))
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
                    self._send(page("Запуск", body, refresh))
                    return
                self._redirect("/?job={}".format(job.id))
                return

            if parsed.path == "/stop":
                job_id = settings.as_int((form.get("job") or [""])[0], 0)
                jobs.runner.stop(job_id)
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
                self._send(
                    page(
                        "Профиль",
                        render_profile(self.profile_path, saved=count, problems=problems),
                    )
                )
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
    "render_run",
    "render_search",
    "render_settings",
    "render_vacancies",
    "render_vacancy",
    "save_facts",
    "save_profile",
    "search_settings_form",
    "search_updates",
    "table",
    "text_field",
    "vacancy_one",
)


if __name__ == "__main__":
    raise SystemExit(main())
