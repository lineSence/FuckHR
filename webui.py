"""Локальный веб-интерфейс: настройка и разбор выдачи.

Зачем он есть. Диагностика через однострочники в PowerShell работает, но плохо
масштабируется: посмотреть сотню вакансий, сравнить скоринг, проверить, что
отдаёт поиск по конкретной компании, и тут же поправить facts — в терминале это
десяток команд, в браузере — четыре страницы.

Границы, которые не нарушаются:

- слушает только 127.0.0.1 и не умеет слушать иначе: ни авторизации, ни CSRF-защиты
  здесь нет, и выставлять его наружу нельзя;
- ничего не отправляет — ни писем, ни сообщений [CORE-023];
- не собирает вакансии: сбор остаётся за run.py, интерфейс только смотрит;
- единственная запись на диск — блок facts в profile.yaml; остальные ключи профиля
  сохраняются как есть;
- без новых зависимостей: http.server из стандартной библиотеки справляется с одним
  пользователем, а Flask и FastAPI тянут за собой стек, который потом надо обновлять.

Запуск:
    python webui.py                 # http://127.0.0.1:8765
    python webui.py --port 9000
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Sequence

import yaml

import contacts
import db
import detector
import outreach
import websearch

log = logging.getLogger("webui")

# Адрес зашит намеренно: интерфейс без авторизации не должен слушать сеть.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

STYLE = """
body { font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0 auto;
       max-width: 1000px; padding: 24px; color: #1d1d1f; }
a { color: #0b62d6; }
nav { display: flex; gap: 16px; margin-bottom: 24px; padding-bottom: 12px;
      border-bottom: 1px solid #e3e3e6; }
h1 { font-size: 22px; margin: 0 0 16px; }
h2 { font-size: 17px; margin: 24px 0 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #ececef;
         vertical-align: top; }
th { font-weight: 600; font-size: 13px; color: #6b6b70; }
.score { font-variant-numeric: tabular-nums; font-weight: 600; }
.muted { color: #6b6b70; }
.warn { background: #fff6e5; border: 1px solid #f0d9a8; padding: 10px 12px;
        border-radius: 6px; margin: 12px 0; }
pre { background: #f6f6f8; padding: 12px; border-radius: 6px; white-space: pre-wrap;
      word-break: break-word; }
textarea { width: 100%; min-height: 160px; font: 14px/1.5 ui-monospace, Consolas, monospace;
           padding: 10px; border: 1px solid #d2d2d7; border-radius: 6px; }
input[type=text], input[type=number] { padding: 7px 9px; border: 1px solid #d2d2d7;
           border-radius: 6px; font-size: 14px; }
button { padding: 8px 14px; border: 0; border-radius: 6px; background: #0b62d6;
         color: #fff; font-size: 14px; cursor: pointer; }
.pill { display: inline-block; padding: 1px 7px; border-radius: 99px; font-size: 12px;
        background: #eef1f5; margin-right: 6px; }
"""

NAV = (
    '<nav><a href="/">Вакансии</a><a href="/contacts">Контакты</a>'
    '<a href="/search">Проверка поиска</a><a href="/profile">Профиль</a></nav>'
)


def esc(value: object) -> str:
    """Всё, что пришло из базы или из поиска, попадает в HTML только через это.

    В описаниях вакансий и сниппетах выдачи регулярно приезжает сырой HTML.
    """
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} — FuckHR</title><style>{STYLE}</style></head><body>"
        f"{NAV}<h1>{esc(title)}</h1>{body}</body></html>"
    )


def db_path() -> str:
    return os.getenv("DB_PATH", "data/fuckhr.sqlite3")


def open_db() -> sqlite3.Connection:
    """Новое соединение на запрос: sqlite3 не любит передачи между потоками."""
    conn = db.connect(db_path())
    db.init_schema(conn)
    contacts.ensure_schema(conn)
    detector.ensure_schema(conn)
    return conn


def vacancy_rows(
    conn: sqlite3.Connection, min_score: float = 0.0, limit: int = 50
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT key, title, company, score, url, published_at, last_seen_at, notified_at
        FROM vacancies
        WHERE score >= ?
        ORDER BY score DESC, last_seen_at DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()


def vacancy_one(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vacancies WHERE key = ?", (key,)).fetchone()


def contact_rows(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT key, company, person, role, role_rank, channel_kind, channel_value,
               confidence, guessed, status, created_at, notes
        FROM contacts
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def save_facts(profile_path: str | Path, text: str) -> tuple[str, ...]:
    """Перезаписывает только блок facts, остальное в профиле не трогает.

    Пустые строки отбрасываются здесь же, чтобы в файл не попали пустые пункты
    списка: именно они разбирались в None и уезжали в письмо как факт о себе.
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
    """Короткая сводка профиля для просмотра — без редактирования."""
    path = Path(profile_path)
    if not path.exists():
        return [("файл", f"{path} не найден")]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    queries = data.get("queries") or []
    salary = data.get("salary") or {}
    return [
        ("запросы", ", ".join(str(q.get("text", q)) for q in queries) or "не заданы"),
        ("минимум на руки", str(salary.get("min_net", "не задан"))),
        ("порог скоринга", str(data.get("min_score", "не задан"))),
        ("навыки", ", ".join(str(s) for s in (data.get("skills") or [])) or "не заданы"),
    ]


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Ячейки приходят уже готовым HTML: экранирует вызывающая сторона."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def render_vacancies(conn: sqlite3.Connection, min_score: float, limit: int) -> str:
    rows = vacancy_rows(conn, min_score, limit)
    stats = db.stats(conn)
    direct, total = contacts.coverage(conn)

    form = (
        '<form method=get action="/">'
        f'Скоринг от <input type=number step=1 name=min_score value="{esc(int(min_score))}"> '
        f'показать <input type=number step=10 name=limit value="{esc(limit)}"> '
        "<button>Применить</button></form>"
    )
    summary = (
        f'<p class=muted>В базе: {esc(stats.get("vacancies", "?"))} вакансий. '
        f"В логе контактов: {esc(direct)} с прямым контактом из {esc(total)}. "
        f"Найдено по фильтру: {len(rows)}.</p>"
    )
    if not rows:
        return (
            form
            + summary
            + '<div class=warn>Нет вакансий под фильтр. Если база пуста — сначала сбор: '
            "<code>python run.py --limit 30</code></div>"
        )

    body = []
    for row in rows:
        body.append(
            [
                f'<span class=score>{esc(f"{row[\'score\'] or 0:.0f}")}</span>',
                f'<a href="/vacancy?key={urllib.parse.quote(row["key"])}">{esc(row["title"])}</a>',
                esc(row["company"]),
                esc((row["published_at"] or "")[:10]),
                "✓" if row["notified_at"] else "",
            ]
        )
    return (
        form
        + summary
        + table(["Скор", "Вакансия", "Компания", "Опубликована", "В TG"], body)
    )


def render_vacancy(conn: sqlite3.Connection, key: str, with_draft: bool) -> str:
    row = vacancy_one(conn, key)
    if row is None:
        return "<p>Вакансия не найдена.</p>"

    parts = [
        f'<p><b>{esc(row["company"])}</b> · скоринг '
        f'<span class=score>{esc(f"{row[\'score\'] or 0:.0f}")}</span> · '
        f'<a href="{esc(row["url"])}" target=_blank rel=noreferrer>открыть на hh.ru</a></p>'
    ]
    if row["score_reasons"]:
        parts.append(f"<h2>Почему такой скор</h2><pre>{esc(row['score_reasons'])}</pre>")

    signal_lines = detector.load_lines(conn, key)
    if signal_lines:
        parts.append("<h2>HR-флаги</h2><pre>" + esc("\n".join(signal_lines)) + "</pre>")

    if with_draft:
        provider = websearch.SearchProvider.from_env(conn)
        facts = outreach.load_facts()
        discovery, draft, skip_reason = outreach.process_row(
            conn, row, facts, provider, allow_generic=True
        )
        if skip_reason:
            parts.append(f"<h2>Черновик</h2><div class=warn>Пропуск: {esc(skip_reason)}</div>")
        else:
            card = outreach.format_card(row, discovery, draft, signal_lines)
            parts.append(f"<h2>Карточка и черновик</h2><pre>{esc(card)}</pre>")
            if not facts:
                parts.append(
                    '<div class=warn>Блок facts пуст — в письме заглушка вместо повода писать. '
                    '<a href="/profile">Заполнить</a></div>'
                )
    else:
        parts.append(
            f'<p><a href="/vacancy?key={urllib.parse.quote(key)}&draft=1">'
            "Собрать черновик и найти контакт</a> "
            "<span class=muted>(может дёрнуть внешний поиск, ничего не отправляет)</span></p>"
        )

    parts.append(f"<h2>Описание</h2><pre>{esc(row['description'])}</pre>")
    return "".join(parts)


def render_contacts(conn: sqlite3.Connection) -> str:
    rows = contact_rows(conn)
    direct, total = contacts.coverage(conn)
    if not rows:
        return (
            f"<p class=muted>Прямых контактов {esc(direct)} из {esc(total)}.</p>"
            "<div class=warn>Лог контактов пуст. Он заполняется при запуске "
            "<code>python outreach.py</code> без --dry-run.</div>"
        )
    body = [
        [
            esc(r["company"]),
            esc(r["person"] or "—"),
            esc(r["role"] or "—"),
            f'<span class=pill>{esc(r["channel_kind"])}</span>{esc(r["channel_value"])}',
            esc(r["confidence"]) + (" · угадан" if r["guessed"] else ""),
            esc(r["status"]),
            esc((r["created_at"] or "")[:10]),
        ]
        for r in rows
    ]
    return (
        f"<p class=muted>Прямых контактов {esc(direct)} из {esc(total)}.</p>"
        + table(
            ["Компания", "Человек", "Роль", "Канал", "Уверенность", "Статус", "Записан"],
            body,
        )
    )


def render_search(conn: sqlite3.Connection, query: str, company: str) -> str:
    """Живая проверка выдачи: видно, что именно отдаёт поиск до ранжирования."""
    provider = websearch.SearchProvider.from_env(conn)
    head = (
        '<form method=get action="/search">'
        f'Запрос <input type=text size=42 name=q value="{esc(query)}"> '
        "<button>Искать</button></form>"
        '<form method=get action="/search">'
        f'Или запросы по компании <input type=text size=28 name=company value="{esc(company)}"> '
        "<button>Показать и выполнить</button></form>"
        f'<p class=muted>Провайдер: {esc(provider.provider)} · адрес: '
        f'{esc(provider.base_url or "не задан")} · '
        f'{"готов" if provider.enabled else esc(provider.disabled_reason)}</p>'
    )
    if not provider.enabled:
        return head + (
            "<div class=warn>Внешний поиск выключен, искать негде. "
            "Для своего SearXNG задай SEARCH_BASE_URL в .env.</div>"
        )

    queries = [query] if query else []
    if company:
        queries = list(websearch.contact_queries(company))
    if not queries:
        return head + "<p class=muted>Введи запрос или название компании.</p>"

    parts = [head, "<h2>Запросы</h2><pre>" + esc("\n".join(queries)) + "</pre>"]
    hits = provider.search_many(queries, limit=5)
    usage = provider.usage
    parts.append(
        f"<p class=muted>Вызовов: {usage.calls} · из кэша: {usage.cached} · "
        f"ошибок: {usage.failures} · пропущено: {usage.skipped}</p>"
    )
    if not hits:
        parts.append(
            "<div class=warn>Пустая выдача. Проверь, жив ли туннель и отвечает ли "
            "инстанс форматом json.</div>"
        )
        return "".join(parts)

    body = [
        [
            f'<a href="{esc(h.url)}" target=_blank rel=noreferrer>{esc(h.title or h.url)}</a>',
            esc(contacts.domain_of(h.url) or ""),
            esc(h.snippet[:300]),
        ]
        for h in hits
    ]
    parts.append(table(["Страница", "Домен", "Сниппет"], body))
    return "".join(parts)


def render_profile(profile_path: str, saved: int | None = None) -> str:
    facts = outreach.load_facts(profile_path)
    rows = [[esc(name), esc(value)] for name, value in profile_summary(profile_path)]
    note = ""
    if saved is not None:
        note = f"<div class=warn>Сохранено фактов: {esc(saved)}</div>"
    elif not facts:
        note = (
            "<div class=warn>Блок facts пуст. Без него каждое письмо собирается с заглушкой "
            "вместо повода писать.</div>"
        )
    return (
        note
        + "<h2>Факты о себе</h2>"
        + "<p class=muted>По одному на строку, с цифрами. Только эти строки попадают в письмо: "
        + "ничего кроме них система о вас не напишет.</p>"
        + '<form method=post action="/profile">'
        + f"<textarea name=facts>{esc(chr(10).join(facts))}</textarea>"
        + "<p><button>Сохранить</button></p></form>"
        + "<h2>Остальное в профиле</h2>"
        + "<p class=muted>Чтение; правытся в profile.yaml.</p>"
        + table(["Параметр", "Значение"], rows)
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
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        one = lambda name, default="": (params.get(name) or [default])[0]

        try:
            if parsed.path == "/profile":
                self._send(page("Профиль", render_profile(self.profile_path)))
                return
            if parsed.path == "/favicon.ico":
                self._send("", 404)
                return

            conn = open_db()
            try:
                if parsed.path == "/":
                    min_score = float(one("min_score", "0") or 0)
                    limit = min(int(one("limit", "50") or 50), 500)
                    body = render_vacancies(conn, min_score, limit)
                    self._send(page("Вакансии", body))
                elif parsed.path == "/vacancy":
                    key = one("key")
                    body = render_vacancy(conn, key, with_draft=one("draft") == "1")
                    self._send(page("Вакансия", body))
                elif parsed.path == "/contacts":
                    self._send(page("Контакты", render_contacts(conn)))
                elif parsed.path == "/search":
                    body = render_search(conn, one("q"), one("company"))
                    self._send(page("Проверка поиска", body))
                else:
                    self._send(page("Не найдено", "<p>Такой страницы нет.</p>"), 404)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — интерфейс не должен падать целиком
            log.exception("ошибка при обработке %s", self.path)
            self._send(page("Ошибка", f"<pre>{esc(exc)}</pre>"), 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/profile":
            self._send(page("Не найдено", "<p>Такой страницы нет.</p>"), 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            text = (urllib.parse.parse_qs(raw).get("facts") or [""])[0]
            facts = save_facts(self.profile_path, text)
            self._send(page("Профиль", render_profile(self.profile_path, saved=len(facts))))
        except Exception as exc:  # noqa: BLE001
            log.exception("не смог сохранить факты")
            self._send(page("Ошибка", f"<pre>{esc(exc)}</pre>"), 500)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальный интерфейс FuckHR")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--profile", default="profile.yaml")
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

    Handler.profile_path = args.profile
    server = HTTPServer((HOST, args.port), Handler)
    log.info("интерфейс здесь: http://%s:%s (Ctrl+C чтобы остановить)", HOST, args.port)
    log.info("база: %s", db_path())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("остановлен")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
