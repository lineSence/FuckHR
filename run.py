"""Один прогон пайплайна MVP.

    hh.ru (HTML поиска) -> предфильтр -> страница вакансии -> скоринг
    -> условия работы -> детектор утверждений -> SQLite
    -> досье на компании (параллельно) -> рабочие контакты -> Telegram

Источник данных — HTML страниц hh.ru: публичный API закрыт с апреля 2026
(ADR-015). Запускается из Task Scheduler через pythonw.exe (ADR-014).

Настроек в командной строке больше нет. Сколько собирать, по какому профилю,
ходить ли за описаниями и использовать ли модель — всё это живёт в .env и
правится в веб-интерфейсе (webui.py). Флаги --dry-run и --verbose остались:
это не настройки, а режим одного запуска.

Что означает лимит (RUN_LIMIT). Он ограничивает именно сбор: как только
набралось N вакансий, прошедших предфильтр, остальные страницы и запросы не
запрашиваются.

У прогона шесть обязанностей:
1. собрать и отправить карточки;
2. зафиксировать историю — и появление, и исчезновение вакансии (ADR-010);
3. сопоставить утверждения вакансии с этой историей (detector.py, ADR-009);
4. собрать досье на компании, чьи вакансии прошли порог (research.py);
5. найти рабочие контакты по этим же вакансиям (outreach.collect_contacts);
6. пожаловаться, если сам сломался (canary.py), а не тихо вернуть ноль.

Почему досье собирается параллельно и после порога — см. research.py. Порог
здесь главный фильтр цены: досье собирается только по тем конторам, чей оффер
вообще интересен.

Контакты ищутся здесь, письма — нет. Наличие рабочего канала — такой же факт
о вакансии, как скор и HR-флаги, и он нужен в карточке сразу. А черновик письма
готовится кнопкой рядом с вакансией или запуском outreach.py: письмо — решение
человека, а не побочный эффект ночного сканирования ([OUT-006], ADR-012).

Где здесь модель (ADR-005, ADR-017). Три этапа и все необязательные: extract
(условия из описания), hr_filter (указать на проверяемые утверждения), company
(сводка по отзывам словами). Сбор, предфильтр, скоринг, история, флаги досье и
канарейка остаются детерминированными [CORE-015]: с выключенной моделью и при
любой её ошибке прогон доходит до конца, просто беднее деталями [CORE-017].
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import bot as tg
import canary
import company_score_rules
import company_score_store
import conditions
import contact_finds
import db
import detector
import detector_llm
import dossier
import llm
import injection_store
import llm_batch
import aitext
import aitext_rules
import market_company
import market_store
import llm_tasks
import outreach
import settings
import websearch
from collector import collect  # noqa: F401 — реэкспорт: сбор живёт в collector.py
from hh import Vacancy, enrich
from hh_html import BlockedError, HHHtmlClient
import run_cards
import run_loop
import embeddings_tasks
from run_setup import build_gateway, notify_if_broken, setup_logging
from research import (  # noqa: F401 — реэкспорт для старых вызовов
    MAX_RESEARCH_WORKERS,
    _research_one,
    research_companies,
    research_workers,
)
from score import Profile, evaluate

log = logging.getLogger("fuckhr")


# Потолок карточек, когда лимит сбора снят (RUN_LIMIT=0). Ноль здесь означал бы
# «не брать ничего», а вываливать в Telegram всю базу разом тоже незачем.
CARD_LIMIT = 200


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FuckHR: один прогон сбора. Настройки — в webui.py"
    )
    parser.add_argument("--dry-run", action="store_true", help="без отправки в Telegram")
    parser.add_argument("--verbose", action="store_true", help="подробный лог (DEBUG)")
    args = parser.parse_args()

    load_dotenv()
    loop = settings.loop_options()
    if not loop.enabled:
        return run_once(args)
    # Логи поднимаем до первого круга: иначе строки про цикл уходят в никуда,
    # а run_once настраивает их только у себя внутри.
    setup_logging(Path(settings.get("LOG_PATH", "data/fuckhr.log")), args.verbose)
    log.info(
        "режим цикла: %s, пауза %.0f с",
        "{} циклов".format(loop.cycles) if loop.cycles else "до ручной остановки",
        loop.pause,
    )
    return run_loop.run_cycles(lambda: run_once(args), loop.cycles, loop.pause)


def run_once(args: argparse.Namespace) -> int:
    """Один прогон целиком. Настройки перечитываются каждый цикл: владелец
    может поправить их в интерфейсе, не дожидаясь конца круга."""
    options = settings.collect_options()
    prefilter = settings.prefilter_options()
    detector_opts = settings.detector_options()
    score_opts = settings.company_score_options()
    db_path = Path(settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    setup_logging(Path(settings.get("LOG_PATH", "data/fuckhr.log")), args.verbose)
    log.info(
        "настройки: лимит %s, профиль %s, описания %s, модель %s",
        options.limit,
        options.profile,
        "да" if options.details else "нет",
        "да" if options.use_llm else "нет",
    )
    log.info(
        "предфильтр: %s, черновой порог %.0f, нечёткость %s%%",
        "включён" if prefilter.enabled else "выключен",
        prefilter.min_score,
        prefilter.fuzzy,
    )
    if detector_opts.enabled:
        detector.configure(
            detector.Limits(
                min_days=detector_opts.min_days,
                republish_alarm=detector_opts.republish_alarm,
                wide_band=detector_opts.wide_band,
            )
        )
    else:
        log.info("детектор брехни выключен в настройках: карточки пойдут без HR-флагов")

    profile = Profile.load(options.profile)

    conn = db.connect(db_path)
    db.init_schema(conn)
    detector.ensure_schema(conn)
    conditions.ensure_schema(conn)
    dossier.ensure_schema(conn)
    company_score_store.ensure_schema(conn)
    injection_store.ensure_schema(conn)

    gateway = build_gateway(conn, not options.use_llm)
    extracted = 0

    new_count = 0
    enriched = 0
    reused_details = 0
    to_extract: list[Any] = []
    to_claim: list[tuple[Any, Any]] = []
    empty_descriptions = 0
    blocked = False
    with_details = options.details
    seen: dict[str, Vacancy] = {}
    drafts: dict[str, Vacancy] = {}
    # Компании вакансий, прошедших скоринг: именно их изучаем после сбора.
    to_research: dict[str, str | None] = {}
    # Ключи тех же вакансий: по ним после досье ищутся рабочие контакты.
    to_contact: list[str] = []

    client = HHHtmlClient(
        pause=settings.as_float(os.getenv("HH_PAUSE"), 2.0),
        pause_min=settings.as_float(os.getenv("HH_PAUSE_MIN"), 0.8),
        cookie=os.getenv("HH_COOKIE") or None,
        proxy=os.getenv("HH_PROXY") or None,
        failure_dir=settings.get("FAILURE_DIR", "data/failures"),
    )
    try:
        seen, drafts = collect(client, profile, options.limit, prefilter, conn=conn)
        log.info("увидели: %s, прошло предфильтр: %s", len(seen), len(drafts))
        # Рынок пересчитывается до скоринга: вес `market` в score.py берётся
        # из свежих срезов, иначе первая вакансия прогона сравнивалась бы с
        # позавчерашней медианой.
        market_store.drop_stale(conn)
        market_store.recompute(conn)
        total = len(drafts)
        for position, draft in enumerate(drafts.values(), start=1):
            # Счётчик в квадратных скобках — то, по чему интерфейс рисует полоску.
            log.info("[%s/%s] %s — %s", position, total, draft.title, draft.company)
            vacancy = draft
            # Описание из базы вместо второго похода на hh.ru. Карточка вакансии
            # стоит паузы в пару секунд, и на повторном прогоне именно эти
            # запросы съедали почти всё время. Дата публикации сменилась —
            # значит объявление переопубликовали, описание качаем заново.
            cached = db.cached_details(conn, draft.key) if with_details else None
            if cached and (not draft.published_at or cached[2] == draft.published_at):
                text, skills, _published = cached
                vacancy = draft.model_copy(update={"description": text, "skills": skills})
                reused_details += 1
            elif with_details:
                try:
                    vacancy = enrich(draft, client.vacancy(draft.external_id))
                    enriched += 1
                    if not vacancy.description.strip():
                        empty_descriptions += 1
                except BlockedError:
                    # Дальше ходить бессмысленно: сохраняем то, что уже собрали.
                    log.error("hh.ru закрылся капчей на деталях, добирать остальное не будем")
                    blocked = True
                    with_details = False
                except Exception:  # noqa: BLE001 — вакансия могла быть уже закрыта
                    log.warning("нет деталей по %s, берём черновик", draft.external_id)
            # Спрятанная в тексте инструкция для ИИ — поступок работодателя,
            # а не техническая помеха (ADR-020). Запоминаем до скоринга: улика
            # нужна оценке компании и строке карточки.
            injection_store.check_text(
                conn, "vacancy", vacancy.key, vacancy.company or "", vacancy.description
            )
            marker = market_store.marker_for(conn, vacancy)
            # Оценка описания: только детерминированная часть. Судью здесь не
            # зовём — вызов на каждую вакансию выдачи не окупается [CORE-016].
            ai_verdict = aitext.assess(
                vacancy.description, aitext_rules.VACANCY, vacancy.published_at
            )
            verdict = evaluate(vacancy, profile, prefilter.fuzzy, market_marker=marker)
            # Слепок пишется для всего, даже для отклоныённого: история публикаций
            # нужна детектору независимо от нашего интереса (ADR-009, ADR-010).
            db.add_snapshot(conn, vacancy)
            if verdict.rejected:
                log.info("    отклонена: %s", verdict.reject_reason)
                continue
            # Оценка работодателя по умолчанию только справочная. Учёт в скоринге
            # включается настройкой и берёт уровень прошлого прогона: свежий
            # считается в конце, когда все вакансии уже в наблюдениях.
            if score_opts.in_score and vacancy.company:
                if (
                    company_score_store.level_of(conn, vacancy.company)
                    == company_score_rules.LEVEL_RED
                ):
                    verdict.score = max(0.0, verdict.score - score_opts.penalty)
                    verdict.reasons.append(
                        "оценка работодателя: красные флаги (−{:g})".format(
                            score_opts.penalty
                        )
                    )
            if db.upsert_vacancy(
                conn,
                vacancy,
                verdict.score,
                verdict.reasons,
                market_marker=marker,
                ai_verdict=ai_verdict,
            ):
                new_count += 1
            log.info("    скор %.1f", verdict.score)

            # Порог пройдён — компания идёт в очередь на изучение. Сам поиск запускается
            # после обхода hh.ru: мешать его с постраничным сбором — значит сбить паузы
            # и приблизить капчу.
            if verdict.score >= profile.min_score and vacancy.company:
                to_research.setdefault(vacancy.company, getattr(vacancy, "site_url", None))
                to_contact.append(vacancy.key)

            # Этап extract. Только для вакансий, прошедших скоринг: гонять модель
            # по отклонённым — жечь бюджет вызовов ради данных, которые никто не прочтёт.
            # Сами вызовы идут после обхода, пулом: ожидание шлюза внутри цикла
            # останавливало сбор на секунды и сбивало ритм пауз hh.ru.
            if gateway is not None and vacancy.description.strip():
                to_extract.append(vacancy)

            # Детектор запускается сразу после слепка: история уже включает
            # текущий прогон, и вывод не отстаёт от карточки на один запуск.
            if detector_opts.enabled:
                report = detector.assess(vacancy, detector.history(conn, vacancy.key))
                if gateway is not None and detector_opts.use_llm_claims:
                    # Этап hr_filter: модель только отмечает утверждения, вердикт у всех
                    # таких пунктов — «недостаточно данных» (detector_llm.with_llm_claims).
                    to_claim.append((vacancy, report))
                else:
                    detector.store(conn, report)
    except BlockedError as exc:
        blocked = True
        log.error("%s", exc)
    finally:
        client.close()

    if reused_details:
        log.info(
            "описаний взято из базы: %s, скачано с hh.ru: %s",
            reused_details,
            enriched,
        )

    # Этапы модели по всему собранному разом: сеть ждут параллельно, в базу
    # пишет главный поток.
    if to_extract:
        for key, items in llm_batch.extract_all(db_path, to_extract).items():
            if conditions.store(conn, key, items):
                extracted += 1
    if to_claim:
        enriched_reports = llm_batch.claims_all(db_path, to_claim)
        for vacancy, report in to_claim:
            detector.store(conn, enriched_reports.get(vacancy.key, report))

    # Отметка «видели сегодня» нужна и для отклонённых вакансий, иначе они будут
    # считаться пропавшими сразу после первого прогона.
    if seen:
        db.touch_seen(conn, seen.keys())

    # Вторая половина истории: что исчезло из выдачи. Только после чистого
    # прогона без лимита: при капче, сломанном парсере или оборванном по лимиту
    # сборе мы бы «закрыли» живые вакансии разом и испортили историю.
    partial = bool(options.limit) and len(drafts) >= options.limit
    if not blocked and not partial and not client.fallback_pages and seen:
        db.deactivate_missing(conn, seen.keys())
    elif partial:
        log.info("сбор оборван лимитом — пропавшие вакансии не отмечаем")

    # Векторы (ADR-021) — до досье: перефразированные отзывы ловятся уже в этом
    # же прогоне, а не со следующего. Этап выключен по умолчанию и без
    # эмбеддера просто ничего не делает [CORE-017].
    embeddings_tasks.index_vacancies(conn, gateway)

    # Досье на компании. Идёт после hh.ru и до отправки карточек: без него в карточке
    # не будет самой полезной строки — стоит ли вообще связываться с этими людьми.
    researched: dict[str, dossier.Dossier] = {}
    if to_research:
        researched = research_companies(
            conn, db_path, to_research, use_llm=options.use_llm
        )

    # Контакты ищутся здесь же, сразу после досье: наличие рабочего канала —
    # такой же факт о вакансии, как скор и HR-флаги, и он нужен в карточке до
    # всякого письма. Письма отсюда не готовятся ([OUT-006], ADR-012).
    if to_contact:
        provider = websearch.SearchProvider.from_env(conn)
        if not provider.enabled:
            log.info(
                "контакты ищем только в тексте вакансий: %s", provider.disabled_reason
            )
        placeholders = ",".join("?" for _ in to_contact)
        contact_targets = conn.execute(
            f"SELECT * FROM vacancies WHERE key IN ({placeholders})", to_contact
        ).fetchall()
        with_channel = outreach.collect_contacts(
            conn,
            contact_targets,
            provider,
            check_mx=settings.outreach_options().check_mx,
        )
        direct, scanned = contact_finds.coverage(conn)
        log.info(
            "контакты: канал нашёлся у %s из %s вакансий этого прогона "
            "(в базе с прямым каналом %s из %s)",
            with_channel,
            len(contact_targets),
            direct,
            scanned,
        )

    notify_if_broken(
        canary.RunStats(
            collected=len(seen),
            passed=len(drafts),
            blocked=blocked,
            fallback_pages=client.fallback_pages,
            enriched=enriched,
            empty_descriptions=empty_descriptions,
            failures=list(client.failures),
        ),
        dry_run=args.dry_run,
    )

    # Метки работодателей по деньгам: считаются после того, как все вакансии
    # прогона попали в наблюдения, иначе доли считались бы по половине данных.
    market_company.refresh(conn, market_store.companies(conn))

    # Общая оценка работодателя (ADR-018) считается последней: ей нужны и
    # свежие метки по деньгам, и досье, и слепки этого прогона.
    if score_opts.enabled:
        company_score_store.refresh(
            conn, sorted(set(market_store.companies(conn)) | set(to_research))
        )
        scored, red_companies, unknown = company_score_store.coverage(conn)
        log.info(
            "оценка работодателей: всего %s, красных %s, без данных %s",
            scored,
            red_companies,
            unknown,
        )

    rows = db.pending_cards(conn, profile.min_score, options.limit or CARD_LIMIT)
    log.info("новых вакансий: %s, к отправке: %s", new_count, len(rows))

    signals = run_cards.card_lines(conn, rows, score_opts.enabled)

    if args.dry_run:
        for row in rows:
            log.info("%5.1f  %s — %s", row["score"], row["title"], row["company"])
            log.info("        %s", row["url"])
            for line in conditions.lines(conn, row["key"]):
                log.info("        %s", line)
            for line in signals.get(row["key"], []):
                log.info("        %s", line)
    elif rows:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            # Раньше здесь был KeyError и прогон терял всю работу на последнем шаге.
            # Собранное уже в базе и видно в интерфейсе [CORE-017].
            log.warning(
                "Telegram не настроен: %s карточек ждут в базе, смотри их в интерфейсе",
                len(rows),
            )
        else:
            republished = {row["key"]: db.republish_count(conn, row["key"]) for row in rows}
            delivered = asyncio.run(
                tg.send_cards(token, chat_id, rows, republished, signals)
            )
            db.mark_notified(conn, delivered)
            log.info("отправлено карточек: %s", len(delivered))

    filled, total_snapshots = db.published_at_coverage(conn)
    flagged, assessed = detector.coverage(conn)
    log.info(
        "итого в базе: %s, слепков с датой публикации: %s из %s, "
        "отчётов детектора с флагами: %s из %s",
        db.stats(conn),
        filled,
        total_snapshots,
        flagged,
        assessed,
    )
    if researched:
        dossiers, red, empty = dossier.coverage(conn)
        log.info(
            "досье: собрано в этом прогоне %s, всего в базе %s, с красными флагами %s, "
            "без единого отзыва %s",
            len(researched),
            dossiers,
            red,
            empty,
        )
    if gateway is not None:
        usage = gateway.usage
        with_conditions, vacancies_total = conditions.coverage(conn)
        log.info(
            "модель: вызовов %s, из кэша %s, ошибок %s, пропущено %s; "
            "условия извлечены в этом прогоне для %s вакансий, всего в базе %s из %s",
            usage.calls,
            usage.cached,
            usage.failures,
            usage.skipped,
            extracted,
            with_conditions,
            vacancies_total,
        )
    log.info("прогон завершён")
    conn.close()
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
