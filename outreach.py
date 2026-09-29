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

import logging
import sqlite3
from pathlib import Path
from typing import Any, Sequence

import yaml

import contact_finds
import contacts
import detector
import dossier_store
import llm_tasks
import outreach_scan
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


def _skip_reason(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Повод не искать контакты по вакансии (или None, если искать стоит)."""
    reason = precondition(conn, row)
    if reason:
        log.debug("%s: контакты не ищем — %s", row["key"], reason)
    return reason


def collect_contacts(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    provider: websearch.SearchProvider,
    check_mx: bool = False,
    db_path: str | Path | None = None,
) -> int:
    """Этап discovery в общем прогоне: найти каналы и сложить их в базу.

    Письма здесь не готовятся: письмо — решение владельца ([OUT-006]), а
    наличие контакта — такой же факт о вакансии, как скор или HR-флаги, и
    собирать его отдельным запуском незачем.

    Внешний поиск — один раз на компанию, а не на вакансию: страницы команды и
    контактов у трёх вакансий одного работодателя одни и те же. Сами компании
    расходятся по потокам (`outreach_scan`), в базу пишет главный поток.
    """
    if not rows:
        return 0
    todo = [row for row in rows if not _skip_reason(conn, row)]
    if not todo:
        return 0
    by_company: dict[str, list[sqlite3.Row]] = {}
    for row in todo:
        by_company.setdefault((row["company"] or "").strip(), []).append(row)

    if db_path is None:
        # Без пути к базе потоки завести нечем: провайдер и его кэш привязаны
        # к соединению вызывающего, а делить соединение между потоками нельзя.
        found_by_key = outreach_scan.discover_here(conn, by_company, provider, check_mx)
    else:
        found_by_key = outreach_scan.discover_all(db_path, by_company, check_mx=check_mx)

    found = 0
    for row in todo:
        discovery = found_by_key.get(row["key"])
        if discovery is None:
            continue
        contact_finds.save(conn, discovery)
        if discovery.candidates:
            found += 1
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
        discovery, hits = outreach_scan.find_contacts(conn, row, provider, check_mx=check_mx)
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
        hits = outreach_scan.company_hits(provider, company, row["title"])
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
            draft = llm_tasks.polish_draft(gateway, draft, facts, limit=MAX_LETTER_CHARS)
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
        draft = llm_tasks.polish_draft(gateway, draft, facts, limit=MAX_LETTER_CHARS)
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
