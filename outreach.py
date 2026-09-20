"""Сборка черновика письма и запуск этапа contact discovery.

Система ничего не отправляет (ADR-012, [OUT-006]). Конечный артефакт —
текст, который владелец копирует в свой почтовый клиент.

Настроек в командной строке больше нет: сколько вакансий брать, с какого
скора, пускать ли вакансии без прямого контакта и использовать ли модель —
всё это живёт в .env и правится в веб-интерфейсе (webui.py). Остались два
режимных флага: --dry-run и --verbose.

Про режим OUTREACH_ALLOW_GENERIC. Замер на живой базе: 159 вакансий с полными
описаниями, адрес почты нашёлся в одной (и та — ИП). Это не дефект парсера:
hh.ru вырезает контакты из текста, потому что живёт с отклика через себя.
Поэтому есть два режима. Строгий — только прямой контакт [OUT-001]. В мягком
вакансия без контакта не пропадает: то же письмо, но как сопроводительное к
отклику, и в карточке прямо сказано, что прямого контакта нет.

Факты о себе берутся из резюме (B-01) и только из подтверждённых блоков: то,
что предложила модель и владелец ещё не видел, в письмо попасть не должно
[OUT-005]. Если резюме пустое — берётся блок facts из profile.yaml, как раньше.

Где здесь модель (ADR-017). Три необязательных шага, каждый умеет откатываться:

- company  — справка о компании из выдачи поиска → повод написать;
- contacts — выбор адресата из уже найденных кандидатов;
- draft    — правка языка письма без добавления фактов и чисел.

Два последних видят ФИО и адреса живых людей, поэтому маршрут им выбирает
llm.Gateway по правилам [CORE-012]. С выключенной моделью этап работает ровно
так, как работал до моделей.

Запуск:
    python outreach.py --dry-run
    python outreach.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

import conditions
import contact_finds
import contacts
import db
import detector
import dossier_store
import llm
import llm_tasks
import resume
import settings
import websearch
from dossier_rules import RISK_RED
# Текст письма и карточки живут в outreach_draft.py [CORE-024]; имена
# реэкспортируются, чтобы вызовы и тесты не переписывались.
from outreach_draft import (  # noqa: F401
    APPLY_CHANNEL,
    MAX_LETTER_CHARS,
    NO_CONTACT_NOTE,
    NO_FACTS_HINT,
    Draft,
    apply_candidate,
    build_draft,
    build_follow_up,
    format_card,
)

log = logging.getLogger("outreach")

FOLLOW_UP_DAYS = 6  # [OUT-004]: один follow-up через 5–7 дней



def load_facts(profile_path: str | Path = "profile.yaml") -> tuple[str, ...]:
    """Факты о себе из profile.yaml — фолбэк, если резюме пустое [OUT-005].

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


def collect_facts(
    conn: sqlite3.Connection, profile_path: str | Path = "profile.yaml"
) -> tuple[str, ...]:
    """Факты для письма: сначала резюме, потом profile.yaml.

    Резюме ведётся в интерфейсе и обновляется чаще, чем блок facts в YAML,
    поэтому приоритет у него. Смешивать два источника нельзя: получится письмо,
    где один факт свежий, а второй — из прошлого года, и они друг другу
    противоречат. Ошибка базы не должна лишать этап фактов совсем.
    """
    try:
        from_resume = resume.facts(conn)
    except sqlite3.Error as exc:
        log.warning("резюме не прочиталось (%s), беру факты из %s", exc, profile_path)
        from_resume = ()
    if from_resume:
        log.info("факты из резюме: %s шт.", len(from_resume))
        return tuple(from_resume)
    return load_facts(profile_path)


def follow_up_cards(
    conn: sqlite3.Connection, facts: Sequence[str], dry_run: bool = False
) -> list[tuple[str, int | None]]:
    """Карточки follow-up для писем, отправленных больше FOLLOW_UP_DAYS назад.

    Берутся только контакты со статусом sent_manually: факт отправки ставит
    владелец кнопкой [OUT-006], напоминать о неотправленном письме незачем.
    """
    out: list[tuple[str, int | None]] = []
    for contact in contacts.due_follow_ups(conn, FOLLOW_UP_DAYS):
        row = conn.execute(
            "SELECT * FROM vacancies WHERE key = ?", (contact["key"],)
        ).fetchone()
        if row is None:
            continue
        candidate = contacts.Candidate(
            channel_kind=contact["channel_kind"],
            channel_value=contact["channel_value"],
            person=contact["person"],
            role=contact["role"],
            role_rank=int(contact["role_rank"] or 99),
            source_url=contact["source_url"],
        )
        follow = build_follow_up(row, build_draft(row, candidate, facts))
        out.append(
            (
                "Follow-up (второй и последний) по вакансии "
                f"{row['title']}, {contact['company'] or 'компания не указана'}\n\n"
                f"Канал: {candidate.channel_kind} {candidate.channel_value}\n\n"
                f"{follow.text}\n\nСсылка: {row['url']}",
                None,
            )
        )
        if not dry_run:
            contacts.mark_follow_up(conn, int(contact["id"]))
    return out


def company_hits(
    provider: websearch.SearchProvider, company: str | None, title: str | None = None
) -> list[websearch.Hit]:
    """Сырая выдача по компании. Пусто — тоже нормально.

    Хиты нужны целиком дважды: поиску контактов (текст страницы) и справке
    о компании (титул и сниппет), поэтому поиск делается один раз.
    """
    if not company or not provider.enabled:
        return []
    roles = contacts.lead_roles(title)
    return list(provider.search_many(websearch.contact_queries(company, roles), limit=5))


def pages_from_hits(hits: Sequence[websearch.Hit]) -> list[tuple[str, str]]:
    return [(hit.url, f"{hit.title}\n{hit.snippet}") for hit in hits]


def find_contacts(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    provider: websearch.SearchProvider,
    check_mx: bool = False,
    hits: Sequence[websearch.Hit] | None = None,
) -> tuple[contacts.Discovery, list[websearch.Hit]]:
    """Ищет рабочие каналы по одной вакансии. Внешний поиск — один раз.

    Хиты возвращаются наружу: справка о компании собирается из них же, чтобы
    не платить за второй поиск. Готовые хиты можно передать: у трёх вакансий
    одной компании страницы «Команда» и «Контакты» одни и те же.
    """
    company = row["company"]
    text = "\n".join(str(row[field] or "") for field in ("title", "description"))
    hits = list(hits) if hits is not None else company_hits(provider, company, row["title"])
    pages = pages_from_hits(hits)
    site_url = next((contacts.domain_of(url) for url, _ in pages if contacts.domain_of(url)), None)
    discovery = contacts.discover(
        key=row["key"],
        company=company,
        vacancy_text=text,
        company_pages=pages,
        site_url=site_url,
        check_mx=check_mx,
        vacancy_url=row["url"],
    )
    return discovery, list(hits)


def collect_contacts(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    provider: websearch.SearchProvider,
    check_mx: bool = False,
) -> int:
    """Этап discovery в общем прогоне: найти каналы и сложить их в базу.

    Письма здесь не готовятся: письмо — решение владельца ([OUT-006]), а
    наличие контакта — такой же факт о вакансии, как скор или HR-флаги, и
    собирать его отдельным запуском незачем.
    """
    if not rows:
        return 0
    found = 0
    # Внешний поиск — один раз на компанию, а не на вакансию: страницы команды и
    # контактов у трёх вакансий одного работодателя одни и те же, а роль берётся
    # по самой интересной из них. Раньше это были три набора запросов и три
    # промаха кэша.
    by_company: dict[str, list[websearch.Hit]] = {}
    for row in rows:
        name = (row["company"] or "").strip()
        if name and name not in by_company:
            best = max(
                (r for r in rows if (r["company"] or "").strip() == name),
                key=lambda r: r["score"] if "score" in r.keys() and r["score"] is not None else 0,
            )
            by_company[name] = company_hits(provider, name, best["title"])

    for position, row in enumerate(rows, start=1):
        reason = precondition(conn, row)
        if reason:
            log.debug("%s: контакты не ищем — %s", row["key"], reason)
            continue
        shared = by_company.get((row["company"] or "").strip())
        discovery, _ = find_contacts(conn, row, provider, check_mx=check_mx, hits=shared)
        contact_finds.save(conn, discovery)
        if discovery.candidates:
            found += 1
        # Счётчик в квадратных скобках — по нему интерфейс рисует полоску.
        log.info(
            "[%s/%s] контакты: %s — %s",
            position,
            len(rows),
            row["company"] or "компания не указана",
            f"каналов {len(discovery.candidates)}" if discovery.candidates else "ничего",
        )
    return found


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

    # Контакты ищет общий сбор; здесь берём готовое и не платим за поиск
    # второй раз. Пусто — значит этап по вакансии ещё не отрабатывал.
    hits: list[websearch.Hit] = []
    discovery = contact_finds.load(conn, row["key"], company)
    if discovery is None:
        discovery, hits = find_contacts(conn, row, provider, check_mx=check_mx)
        contact_finds.save(conn, discovery)

    # «Другой контакт» в Telegram обязан приводить к другому человеку: без
    # этого фильтра берётся всё тот же candidates[0], и кнопка выглядит
    # сломанной (B-10, [OUT-006]).
    rejected = contacts.rejected_channels(conn, row["key"])
    if rejected and discovery.candidates:
        left = tuple(c for c in discovery.candidates if c.channel_value not in rejected)
        if not left:
            return discovery, None, "все найденные каналы владелец отклонил"
        discovery = contacts.Discovery(
            key=discovery.key,
            company=discovery.company,
            candidates=left,
            dropped=discovery.dropped,
        )

    # Этап company. Справка собирается только из уже полученных сниппетов:
    # модель в сеть не ходит и свои знания о компании не вспоминает.
    brief = None
    if gateway is not None and not hits:
        hits = company_hits(provider, company, row["title"])
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


def precondition(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Причина, по которой письмо готовить рано, или None [OUT-002].

    Досье и вердикт детектора — предусловие этапа, а не украшение карточки:
    стучаться напрямую в компанию, помеченную как токсичная, бессмысленно.
    """
    company = row["company"]
    if not company:
        return "компания не указана: досье строить не по чему"
    card = dossier_store.load(conn, company)
    if card is None:
        return "нет досье на компанию"
    if card["risk"] == RISK_RED:
        return "досье красное: в такую компанию напрямую не пишем"
    if settings.detector_options().enabled and detector.load(conn, row["key"]) is None:
        return "детектор HR-брехни по вакансии ещё не прогонялся"
    return None


def top_rows(conn: sqlite3.Connection, min_score: float, limit: int) -> list[sqlite3.Row]:
    """Цель — 5 хороших входов, а не 500 писем [CORE-018].

    Скор — только первый фильтр. Вакансия без досье или с красным досье в
    выборку не попадает [OUT-002], поэтому строк берётся с запасом.
    """
    rows = conn.execute(
        """
        SELECT * FROM vacancies
        WHERE score >= ?
        ORDER BY score DESC, last_seen_at DESC
        LIMIT ?
        """,
        (min_score, max(limit * 5, limit)),
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in rows:
        reason = precondition(conn, row)
        if reason:
            log.info("%s: пропуск — %s", row["key"], reason)
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_gateway(conn: sqlite3.Connection, disabled: bool) -> llm.Gateway | None:
    """Шлюз или None. None — штатный режим, а не авария [CORE-017]."""
    if disabled:
        log.info("модель выключена в настройках (LLM_ENABLED)")
        return None
    candidate = llm.Gateway.from_env(conn)
    if not candidate.enabled:
        log.info("модель не настроена (%s), идём без неё", candidate.disabled_reason)
        return None
    for stage, profile, route, model, source in candidate.describe_routes():
        if stage in {"company", "contacts", "draft"}:
            log.info(
                "этап %s: профиль %s, маршрут %s, модель %s (имя из: %s)",
                stage, profile, route, model, source,
            )
    return candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Поиск нанимающего менеджера и черновик письма. Настройки — в webui.py"
    )
    parser.add_argument("--dry-run", action="store_true", help="ничего не писать и не шлать")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    options = settings.outreach_options()
    log.info(
        "настройки: лимит %s, порог %s, профиль %s, без прямого контакта %s, MX %s, модель %s",
        options.limit,
        options.min_score,
        options.profile,
        "да" if options.allow_generic else "нет",
        "да" if options.check_mx else "нет",
        "да" if options.use_llm else "нет",
    )

    conn = db.connect(settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    db.init_schema(conn)
    contacts.ensure_schema(conn)
    detector.ensure_schema(conn)
    conditions.ensure_schema(conn)
    resume.ensure_schema(conn)

    # Факты читаются после открытия базы: главный их источник теперь резюме.
    facts = collect_facts(conn, options.profile)
    if not facts:
        log.warning(
            "ни подтверждённых блоков резюме, ни фактов в %s — в черновике будет "
            "заглушка; заполните резюме на /resume",
            options.profile,
        )

    gateway = build_gateway(conn, not options.use_llm)

    provider = websearch.SearchProvider.from_env(conn)
    if not provider.enabled:
        log.info(
            "внешний поиск выключен (%s): ищем только в том, что уже собрано",
            provider.disabled_reason,
        )

    rows = top_rows(conn, options.min_score, options.limit)
    if not rows:
        log.info("нет вакансий со скором >= %s", options.min_score)
        return 0

    cards: list[tuple[str, int | None]] = []
    prepared = 0
    generic_cards = 0
    for row in rows:
        discovery, draft, skip_reason = process_row(
            conn,
            row,
            facts,
            provider,
            check_mx=options.check_mx,
            allow_generic=options.allow_generic,
            gateway=gateway,
        )
        if skip_reason:
            log.info("%s: пропуск — %s", row["key"], skip_reason)
            continue

        best = discovery.candidates[0]
        if best.channel_kind == APPLY_CHANNEL:
            generic_cards += 1
        contact_id = None
        if not args.dry_run:
            contact_id = contacts.store(conn, row["key"], row["company"], best)
        cards.append(
            (
                format_card(
                    row,
                    discovery,
                    draft,
                    detector.load_lines(conn, row["key"]),
                    conditions.lines(conn, row["key"]),
                ),
                contact_id,
            )
        )
        prepared += 1

    cards.extend(follow_up_cards(conn, facts, args.dry_run))

    for card, _ in cards:
        print(card)
        print("-" * 40)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if cards and not args.dry_run and token and chat_id:
        import bot as tg

        for card, contact_id in cards:
            # Кнопки статуса [OUT-006]: без них контакт навсегда остаётся в drafted.
            asyncio.run(tg.send_contact_card(token, chat_id, card, contact_id))

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
    if not options.allow_generic and prepared == 0:
        log.info(
            "прямых контактов в тексте вакансий почти не бывает: "
            "включи внешний поиск и режим «сопроводительное к отклику» в настройках"
        )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
