"""Сборка черновика письма и CLI этапа contact discovery.

Система ничего не отправляет (ADR-012, [OUT-006]). Конечный артефакт —
текст, который владелец копирует в свой почтовый клиент. Ни SMTP, ни
очереди, ни учётных данных почты здесь нет и не должно быть.

Запуск:
    python outreach.py --limit 5 --dry-run
    python outreach.py --limit 3            # запишет контакты в базу и отправит карточки в Telegram
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml

import contacts
import db
import detector
import websearch

log = logging.getLogger("outreach")

MAX_LETTER_CHARS = 1200  # из справочника contact-discovery, §5
FOLLOW_UP_DAYS = 6  # [OUT-004]: один follow-up через 5–7 дней

NO_FACTS_HINT = (
    "[заполни блок facts в profile.yaml — без фактов о себе письмо отправлять не стоит]"
)


@dataclass(frozen=True)
class Draft:
    """Готовый черновик. Ничего, чего нет в данных, в нём оказаться не может."""

    subject: str
    body: str
    kind: str = "first"

    @property
    def text(self) -> str:
        return f"Тема: {self.subject}\n\n{self.body}"


def load_facts(profile_path: str | Path = "profile.yaml") -> tuple[str, ...]:
    """Факты о себе — единственный разрешённый источник самоописания [OUT-005]."""
    path = Path(profile_path)
    if not path.exists():
        return ()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    facts = data.get("facts") or []
    return tuple(str(f).strip() for f in facts if str(f).strip())


def _role_address(candidate: contacts.Candidate) -> str:
    if candidate.person:
        return candidate.person.split()[0]
    if candidate.role_rank >= 7:
        return "коллеги"
    return "здравствуйте"


def build_draft(
    row: sqlite3.Row,
    candidate: contacts.Candidate,
    facts: Sequence[str] = (),
    reason: str | None = None,
) -> Draft:
    """Собирает письмо по структуре из справочника: задача → факты → шаг → выход.

    Никакой «увлечённости миссией» и никаких достижений, которых нет в profile.yaml.
    """
    title = (row["title"] or "ваша вакансия").strip()
    company = (row["company"] or "ваша команда").strip()

    lines = [f"{_role_address(candidate)}, \u043f\u0438\u0448\u0443 \u043f\u0440\u043e \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044e \u00ab{title}\u00bb."]
    lines.append(
        "Могу закрывать задачи по этому стеку без разгона: "
        + (facts[0] if facts else NO_FACTS_HINT)
    )
    if len(facts) > 1:
        lines.append(facts[1])
    if reason:
        lines.append(f"Повод писать именно вам: {reason}")
    lines.append(
        "Если задача актуальна — готов на 20 минут разговора или пришлю код по близкой задаче."
    )
    lines.append(
        "Если найм в эту команду — не ваша зона, подскажите, кто её ведёт."
    )

    body = "\n\n".join(lines)
    if len(body) > MAX_LETTER_CHARS:
        body = body[: MAX_LETTER_CHARS - 1].rstrip() + "\u2026"
    return Draft(subject=f"{title} — напрямую, без HR-воронки", body=body)


def build_follow_up(row: sqlite3.Row, draft: Draft) -> Draft:
    """Единственный разрешённый второй контакт. Третьего не будет [OUT-004]."""
    title = (row["title"] or "вакансию").strip()
    body = (
        f"Добрый день. Писал неделю назад про «{title}» — поднимаю ветку один раз.\n\n"
        "Если задача закрыта или неактуальна — ответ не нужен, больше не потревожу."
    )
    return Draft(subject=f"Re: {draft.subject}", body=body, kind="follow_up")


def format_card(
    row: sqlite3.Row,
    discovery: contacts.Discovery,
    draft: Draft | None,
    signal_lines: Sequence[str] = (),
) -> str:
    """Карточка из справочника, §4. Плайн-текст: уходит без parse_mode."""
    parts = [
        f"Вакансия: {row['title']}, {row['company'] or 'компания не указана'}",
        f"Скоринг: {row['score']:.0f}/100",
    ]
    if signal_lines:
        parts.append("HR-флаги:\n" + "\n".join(signal_lines))
    parts.extend(contacts.format_contact_lines(discovery))
    for reason in discovery.dropped[:3]:
        parts.append(f"Отброшено: {reason}")
    if draft is not None:
        parts.append("Черновик письма (отправляешь сам):\n" + draft.text)
    parts.append(f"Ссылка: {row['url']}")
    return "\n\n".join(parts)


def company_pages(provider: websearch.SearchProvider, company: str | None) -> list[tuple[str, str]]:
    """Страницы-кандидаты из внешнего поиска. Пусто — тоже нормально."""
    if not company or not provider.enabled:
        return []
    hits = provider.search_many(websearch.contact_queries(company), limit=5)
    return [(hit.url, f"{hit.title}\n{hit.snippet}") for hit in hits]


def process_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    facts: Sequence[str],
    provider: websearch.SearchProvider,
    check_mx: bool = False,
) -> tuple[contacts.Discovery, Draft | None, str | None]:
    """Одна вакансия: проверки → кандидаты → черновик. Третий элемент — причина отказа."""
    company = row["company"]

    if contacts.is_blocked(conn, company):
        return contacts.Discovery(key=row["key"], company=company), None, "компания в блоке"
    if contacts.recently_contacted(conn, None, company):
        return contacts.Discovery(key=row["key"], company=company), None, "писали меньше трёх месяцев назад"

    text = "\n".join(str(row[field] or "") for field in ("title", "description"))
    pages = company_pages(provider, company)
    site_url = next((contacts.domain_of(url) for url, _ in pages if contacts.domain_of(url)), None)
    discovery = contacts.discover(
        key=row["key"],
        company=company,
        vacancy_text=text,
        company_pages=pages,
        site_url=site_url,
        check_mx=check_mx,
    )
    if not discovery.candidates:
        return discovery, None, "прямого контакта не нашлось"

    best = discovery.candidates[0]
    reason = None
    if best.source_url:
        reason = f"взял контакт со страницы {best.source_url}"
    return discovery, build_draft(row, best, facts, reason), None


def top_rows(conn: sqlite3.Connection, min_score: float, limit: int) -> list[sqlite3.Row]:
    """Цель — 5 хороших входов, а не 500 писем [CORE-018]."""
    return conn.execute(
        """
        SELECT * FROM vacancies
        WHERE score >= ?
        ORDER BY score DESC, last_seen_at DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Поиск нанимающего менеджера и черновик письма")
    parser.add_argument("--limit", type=int, default=5, help="сколько вакансий взять сверху")
    parser.add_argument("--min-score", type=float, default=60.0)
    parser.add_argument("--profile", default="profile.yaml")
    parser.add_argument("--dry-run", action="store_true", help="ничего не писать и не шлать")
    parser.add_argument("--check-mx", action="store_true", help="проверять MX домена (нужен dnspython)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    facts = load_facts(args.profile)
    if not facts:
        log.warning("в %s пустой блок facts — в черновике будет заглушка", args.profile)

    conn = db.connect(os.getenv("DB_PATH", "data/fuckhr.sqlite3"))
    db.init_schema(conn)
    contacts.ensure_schema(conn)
    detector.ensure_schema(conn)

    provider = websearch.SearchProvider.from_env(conn)
    if not provider.enabled:
        log.info("внешний поиск выключен: ищем только в том, что уже собрано")

    rows = top_rows(conn, args.min_score, args.limit)
    if not rows:
        log.info("нет вакансий со скором >= %s", args.min_score)
        return 0

    cards: list[str] = []
    prepared = 0
    for row in rows:
        discovery, draft, skip_reason = process_row(
            conn, row, facts, provider, check_mx=args.check_mx
        )
        if skip_reason:
            log.info("%s: пропуск — %s", row["key"], skip_reason)
            continue

        best = discovery.candidates[0]
        if not args.dry_run:
            contacts.store(conn, row["key"], row["company"], best)
        cards.append(
            format_card(row, discovery, draft, detector.load_lines(conn, row["key"]))
        )
        prepared += 1

    for card in cards:
        print(card)
        print("-" * 40)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if cards and not args.dry_run and token and chat_id:
        import bot as tg

        for card in cards:
            asyncio.run(tg.send_alert(token, chat_id, card))

    direct, total = contacts.coverage(conn)
    log.info(
        "подготовлено черновиков: %s; в логе контактов вакансий с прямым контактом %s из %s",
        prepared,
        direct,
        total,
    )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
