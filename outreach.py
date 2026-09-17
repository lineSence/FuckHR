"""Сборка черновика письма и CLI этапа contact discovery.

Система ничего не отправляет (ADR-012, [OUT-006]). Конечный артефакт —
текст, который владелец копирует в свой почтовый клиент. Ни SMTP, ни
очереди, ни учётных данных почты здесь нет и не должно быть.

Про режим --allow-generic. Замер на живой базе: 159 вакансий с полными
описаниями, адрес почты нашёлся в одной (и та — ИП, где работодатель и есть
человек). Это не дефект парсера: hh.ru вырезает контакты из текста, потому что
живῶёт с отклика через себя. Значит этап, у которого на входе только описание
вакансии, обречён отдавать ноль — и молча выбрасывать хорошие вакансии.

Поэтому есть два режима. По умолчанию — только прямой контакт, как требует
[OUT-001]: нашли нанимающего менеджера — пишем ему. С --allow-generic вакансия
без контакта не пропадает: собирается то же письмо, но как сопроводительное к
отклику, и в карточке прямо сказано, что прямого контакта нет. Хуже, чем письмо
руководителю разработки, лучше, чем отклик в пустоту без повода и фактов.

Где здесь модель (ADR-017). Три необязательных шага поверх детерминированного
результата, и каждый умеет откатываться:

- company  — справка о компании из выдачи поиска → повод написать;
- contacts — выбор адресата из уже найденных кандидатов;
- draft    — правка языка письма без добавления фактов и чисел.

Два последних видят ФИО и адреса живых людей, поэтому маршрут им выбирает
llm.Gateway по правилам [CORE-012], а не этот модуль. С --no-llm и без
настроенного шлюза этап работает ровно так, как работал до моделей.

Запуск:
    python outreach.py --limit 5 --dry-run
    python outreach.py --limit 5 --dry-run --allow-generic
    python outreach.py --limit 5 --dry-run --no-llm
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
from typing import Any, Sequence

import yaml

import conditions
import contacts
import db
import detector
import llm
import llm_tasks
import websearch

log = logging.getLogger("outreach")

MAX_LETTER_CHARS = 1200  # из справочника contact-discovery, §5
FOLLOW_UP_DAYS = 6  # [OUT-004]: один follow-up через 5–7 дней

NO_FACTS_HINT = (
    "[заполни блок facts в profile.yaml — без фактов о себе письмо отправлять не стоит]"
)

# Канал «отклик на площадке»: не контакт человека, а признанное отсутствие контакта.
APPLY_CHANNEL = "apply"
NO_CONTACT_NOTE = (
    "Прямого контакта нет — отклик через площадку, письмо ниже как сопроводительное"
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
    """Факты о себе — единственный разрешённый источник самоописания [OUT-005].

    Пустой пункт списка YAML разбирает в None, а str(None) даёт непустую строку
    "None": если её не отбросить до приведения к строке, в письмо уезжает факт,
    которого владелец не писал. Поэтому None отбрасывается отдельно [CORE-019].
    """
    path = Path(profile_path)
    if not path.exists():
        return ()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("facts") or []
    facts: list[str] = []
    for item in raw:
        if item is None or isinstance(item, bool):
            continue
        text = str(item).strip()
        if text:
            facts.append(text)
    return tuple(facts)


def apply_candidate(row: sqlite3.Row) -> contacts.Candidate:
    """Заглушка вместо контакта: ссылка на вакансию и явная пометка в notes.

    Нужна, чтобы отсутствие контакта было записано в лог контактов как факт, а не
    исчезало из истории. role_rank остаётся худшим: такая «находка» никогда не
    должна опережать живого человека при ранжировании [OUT-001].
    """
    return contacts.Candidate(
        channel_kind=APPLY_CHANNEL,
        channel_value=row["url"] or "",
        role="отклик на площадке",
        role_rank=99,
        source_url=row["url"] or None,
        confidence="low",
        notes="прямого контакта не нашлось",
    )


def _role_address(candidate: contacts.Candidate) -> str:
    if candidate.person:
        return candidate.person.split()[0]
    if candidate.channel_kind == APPLY_CHANNEL:
        return "Здравствуйте"
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
    generic = candidate.channel_kind == APPLY_CHANNEL

    lines = [f"{_role_address(candidate)}, пишу про вакансию «{title}»."]
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
    if generic:
        # Просить переадресации у отклика бессмысленно: его читает не тот, кто наймёт.
        lines.append(
            "Если удобнее обсудить голосом — напишите, в какое время созвониться."
        )
    else:
        lines.append("Если найм в эту команду — не ваша зона, подскажите, кто её ведёт.")

    body = "\n\n".join(lines)
    if len(body) > MAX_LETTER_CHARS:
        body = body[: MAX_LETTER_CHARS - 1].rstrip() + "…"
    subject = (
        f"{title} — сопроводительное" if generic else f"{title} — напрямую, без HR-воронки"
    )
    return Draft(subject=subject, body=body)


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
    condition_lines: Sequence[str] = (),
) -> str:
    """Карточка из справочника, §4. Плайн-текст: уходит без parse_mode."""
    parts = [
        f"Вакансия: {row['title']}, {row['company'] or 'компания не указана'}",
        f"Скоринг: {row['score']:.0f}/100",
    ]
    if condition_lines:
        parts.append("Условия:\n" + "\n".join(condition_lines))
    if signal_lines:
        parts.append("HR-флаги:\n" + "\n".join(signal_lines))

    generic = any(c.channel_kind == APPLY_CHANNEL for c in discovery.candidates)
    if generic:
        parts.append(NO_CONTACT_NOTE)
    else:
        parts.extend(contacts.format_contact_lines(discovery))
    for reason in discovery.dropped[:3]:
        parts.append(f"Отброшено: {reason}")
    if draft is not None:
        label = "Сопроводительное" if generic else "Черновик письма"
        parts.append(f"{label} (отправляешь сам):\n" + draft.text)
    parts.append(f"Ссылка: {row['url']}")
    return "\n\n".join(parts)


def company_hits(
    provider: websearch.SearchProvider, company: str | None
) -> list[websearch.Hit]:
    """Сырая выдача по компании. Пусто — тоже нормально.

    Хиты нужны целиком дважды: поиску контактов (текст страницы) и справке
    о компании (титул и сниппет), поэтому поиск делается один раз.
    """
    if not company or not provider.enabled:
        return []
    return list(provider.search_many(websearch.contact_queries(company), limit=5))


def company_pages(
    provider: websearch.SearchProvider, company: str | None
) -> list[tuple[str, str]]:
    """Страницы-кандидаты в виде, который ждёт contacts.discover."""
    return pages_from_hits(company_hits(provider, company))


def pages_from_hits(hits: Sequence[websearch.Hit]) -> list[tuple[str, str]]:
    return [(hit.url, f"{hit.title}\n{hit.snippet}") for hit in hits]


def _brief_reason(brief: Any | None) -> str | None:
    """Первая строка справки как повод писать.

    В письмо идёт ровно одна строка, а не вся справка: пересказ сайта
    компании её сотруднику — шум, а не повод.
    """
    lines = tuple(getattr(brief, "lines", ()) or ()) if brief is not None else ()
    return lines[0] if lines else None


def process_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    facts: Sequence[str],
    provider: websearch.SearchProvider,
    check_mx: bool = False,
    allow_generic: bool = False,
    gateway: Any | None = None,
) -> tuple[contacts.Discovery, Draft | None, str | None]:
    """Одна вакансия: проверки → кандидаты → черновик. Третий элемент — причина отказа.

    Блок компании и правило «не чаще раза в три месяца» действуют в обоих режимах:
    отсутствие контакта не повод обходить [OUT-007] и [OUT-009].

    gateway необязателен. Без него функция ведёт себя ровно так, как до
    появления моделей: первый кандидат по ранжированию и шаблонное письмо.
    """
    company = row["company"]

    if contacts.is_blocked(conn, company):
        return contacts.Discovery(key=row["key"], company=company), None, "компания в блоке"
    if contacts.recently_contacted(conn, None, company):
        return contacts.Discovery(key=row["key"], company=company), None, "писали меньше трёх месяцев назад"

    text = "\n".join(str(row[field] or "") for field in ("title", "description"))
    hits = company_hits(provider, company)
    pages = pages_from_hits(hits)
    site_url = next((contacts.domain_of(url) for url, _ in pages if contacts.domain_of(url)), None)
    discovery = contacts.discover(
        key=row["key"],
        company=company,
        vacancy_text=text,
        company_pages=pages,
        site_url=site_url,
        check_mx=check_mx,
    )

    # Этап company. Справка собирается только из уже полученных сниппетов:
    # модель в сеть не ходит и свои знания о компании не вспоминает.
    brief = None
    if gateway is not None and hits:
        brief = llm_tasks.company_brief(gateway, company or "", hits)

    if not discovery.candidates:
        if not allow_generic:
            return discovery, None, "прямого контакта не нашлось"
        fallback = apply_candidate(row)
        discovery = contacts.Discovery(
            key=discovery.key,
            company=discovery.company,
            candidates=(fallback,),
            dropped=discovery.dropped,
        )
        draft = build_draft(row, fallback, facts, _brief_reason(brief))
        if gateway is not None:
            draft = llm_tasks.polish_draft(gateway, draft, facts)
        return discovery, draft, None

    # Этап contacts. Модель выбирает номер из списка, поэтому нового человека
    # придумать не может. Выбранный ставится первым: именно первого пишет в базу
    # main() и его же показывает карточка.
    best = discovery.candidates[0]
    if gateway is not None and len(discovery.candidates) > 1:
        chosen = llm_tasks.pick_contact(
            gateway, discovery.candidates, role_hint=str(row["title"] or "")
        )
        if chosen is not None and chosen is not best:
            rest = tuple(c for c in discovery.candidates if c is not chosen)
            discovery = contacts.Discovery(
                key=discovery.key,
                company=discovery.company,
                candidates=(chosen,) + rest,
                dropped=discovery.dropped,
            )
            best = chosen

    reason = _brief_reason(brief)
    if reason is None and best.source_url:
        reason = f"взял контакт со страницы {best.source_url}"

    # Этап draft. polish_draft сам откатывается к шаблону, если модель дописала
    # числа или вернула огрызок, так что проверять результат здесь не нужно.
    draft = build_draft(row, best, facts, reason)
    if gateway is not None:
        draft = llm_tasks.polish_draft(gateway, draft, facts)
    return discovery, draft, None


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
    parser.add_argument(
        "--allow-generic",
        action="store_true",
        help="не выбрасывать вакансию без прямого контакта: дать сопроводительное к отклику",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="без вызовов модели: шаблонное письмо и первый кандидат по ранжированию",
    )
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
    conditions.ensure_schema(conn)

    gateway: llm.Gateway | None = None
    if args.no_llm:
        log.info("модель отключена флагом --no-llm")
    else:
        candidate = llm.Gateway.from_env(conn)
        if candidate.enabled:
            gateway = candidate
            for stage, profile, route, model in gateway.describe_routes():
                if stage in {"company", "contacts", "draft"}:
                    log.info(
                        "этап %s: профиль %s, маршрут %s, модель %s",
                        stage,
                        profile,
                        route,
                        model,
                    )
        else:
            log.info("модель не настроена (%s), идём без неё", candidate.disabled_reason)

    provider = websearch.SearchProvider.from_env(conn)
    if not provider.enabled:
        log.info(
            "внешний поиск выключен (%s): ищем только в том, что уже собрано",
            provider.disabled_reason,
        )

    rows = top_rows(conn, args.min_score, args.limit)
    if not rows:
        log.info("нет вакансий со скором >= %s", args.min_score)
        return 0

    cards: list[str] = []
    prepared = 0
    generic_cards = 0
    for row in rows:
        discovery, draft, skip_reason = process_row(
            conn,
            row,
            facts,
            provider,
            check_mx=args.check_mx,
            allow_generic=args.allow_generic,
            gateway=gateway,
        )
        if skip_reason:
            log.info("%s: пропуск — %s", row["key"], skip_reason)
            continue

        best = discovery.candidates[0]
        if best.channel_kind == APPLY_CHANNEL:
            generic_cards += 1
        if not args.dry_run:
            contacts.store(conn, row["key"], row["company"], best)
        cards.append(
            format_card(
                row,
                discovery,
                draft,
                detector.load_lines(conn, row["key"]),
                conditions.lines(conn, row["key"]),
            )
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
        "подготовлено: %s (из них без прямого контакта %s); "
        "в логе контактов вакансий с прямым контактом %s из %s",
        prepared,
        generic_cards,
        direct,
        total,
    )
    if gateway is not None:
        usage = gateway.usage
        log.info(
            "модель: вызовов %s, из кэша %s, ошибок %s, пропущено %s",
            usage.calls,
            usage.cached,
            usage.failures,
            usage.skipped,
        )
    if not args.allow_generic and prepared == 0:
        log.info(
            "прямых контактов в тексте вакансий почти не бывает: "
            "настрой внешний поиск (SEARCH_BASE_URL для своего SearXNG "
            "или SEARCH_API_KEY для tavily/brave) либо запусти с --allow-generic"
        )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
