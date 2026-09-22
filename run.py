"""Один прогон пайплайна MVP.

    hh.ru (HTML поиска) -> предфильтр -> страница вакансии -> скоринг
    -> условия работы -> детектор утверждений -> SQLite
    -> досье на компании (параллельно) -> рабочие контакты -> Telegram

Источник данных — HTML страниц hh.ru: публичный API закрыт с апреля 2026
(ADR-015). Запускается из Task Scheduler через pythonw.exe (ADR-014).

Настроек в командной строке нет: сколько собирать, по какому профилю, ходить ли
за описаниями и звать ли модель — всё живёт в .env и правится в веб-интерфейсе
(webui.py). Флаги --dry-run и --verbose остались: это режим запуска, а не
настройки.

RUN_LIMIT ограничивает именно сбор: как только набралось N вакансий, прошедших
предфильтр, остальные страницы и запросы не запрашиваются.

У прогона шесть обязанностей:
1. собрать и отправить карточки;
2. зафиксировать историю — и появление, и исчезновение вакансии (ADR-010);
3. сопоставить утверждения вакансии с этой историей (detector.py, ADR-009);
4. собрать досье на компании, чьи вакансии прошли порог (research.py);
5. найти рабочие контакты по этим же вакансиям (outreach.collect_contacts);
6. пожаловаться, если сам сломался (canary.py), а не тихо вернуть ноль.

Порог здесь главный фильтр цены: досье собирается только по тем конторам, чей
оффер вообще интересен. Почему параллельно и почему после порога — research.py.

Контакты ищутся здесь, письма — нет. Наличие рабочего канала — такой же факт о
вакансии, как скор и HR-флаги, и он нужен в карточке сразу. А черновик письма
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
import geo
import hh_pages
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
import source_store
import sources
import targets_hh
import websearch
from collector import collect  # noqa: F401 — реэкспорт: сбор живёт в collector.py
from hh import Vacancy, enrich
from hh_html import BlockedError, HHHtmlClient
import run_cards
import run_loop
import embeddings_tasks
import extract_spans
import stage_gates
from run_setup import build_gateway, notify_if_broken, setup_logging
from research import (  # noqa: F401 — реэкспорт для старых вызовов
    MAX_RESEARCH_WORKERS,
    _research_one,
    research_companies,
    research_workers,
)
import profiles
from score import Profile, Verdict, evaluate  # noqa: F401 — реэкспорт для старых вызовов

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

    # Профилей может быть несколько: каталог с YAML или один файл (ADR-023).
    bundle = profiles.load_all(options.profile)

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
    addressed = 0
    # Сколько карточек не стали качать: даже идеальное описание не вытянуло бы
    # вакансию до порога профиля (PREFILTER_DETAILS_DELTA, B-15).
    skipped_details = 0
    details_delta = hh_pages.details_delta()
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
        cache=hh_pages.search_cache(),
    )
    # Галочка hh.ru на главной — это разрешение туда ходить, а не украшение:
    # снятая означает «не трогай hh вообще», включая цели со слежением.
    hh_on = sources.hh_enabled()
    try:
        if hh_on:
            seen, drafts, owners = profiles.collect_all(
                client, bundle, options.limit, prefilter, conn=conn
            )
            log.info("увидели: %s, прошло предфильтр: %s", len(seen), len(drafts))
        else:
            owners = {}
            log.info("hh.ru выключен галочкой на главной: ни поиска, ни целей")
        # Другие площадки: свой обход, свои паузы, ключ дедупа тот же. Вакансия,
        # найденная и здесь и на hh.ru, остаётся одной записью — площадка уходит
        # в vacancy_sources, а этапы модели платятся один раз [CORE-016].
        extra_seen, extra_drafts, extra_owners = sources.collect_external(
            bundle, options.limit, prefilter, conn=conn, known=tuple(seen)
        )
        if extra_seen:
            for key, draft in extra_seen.items():
                seen.setdefault(key, draft)
            for key, draft in extra_drafts.items():
                drafts.setdefault(key, draft)
            for key, ids in extra_owners.items():
                owners.setdefault(key, []).extend(ids)
            log.info(
                "другие площадки: увидели %s, добавили в прогон %s",
                len(extra_seen),
                len(extra_drafts),
            )
        # Цели со слежением (ADR-025): отдельный обход по employer_id, не чаще
        # раза в сутки на цель. Компания выбрана владельцем, поэтому её
        # вакансии сохраняются целиком, без предфильтра и порога.
        if hh_on:
            for company, fresh in targets_hh.sweep(conn, client):
                log.info("цель %s: новых вакансий %s", company, fresh)
        # Рынок пересчитывается до скоринга: вес `market` в score.py берётся
        # из свежих срезов, иначе первая вакансия прогона сравнивалась бы с
        # позавчерашней медианой.
        market_store.drop_stale(conn)
        market_store.recompute(conn)
        total = len(drafts)
        geo.ensure_schema(conn)  # колонки адреса: один раз, не в цикле
        source_store.ensure_schema(conn)
        for position, draft in enumerate(drafts.values(), start=1):
            # Счётчик в квадратных скобках — то, по чему интерфейс рисует полоску.
            log.info("[%s/%s] %s — %s", position, total, draft.title, draft.company)
            vacancy = draft
            point = None
            # Описание из базы вместо второго похода на hh.ru: на повторном
            # прогоне именно эти запросы съедали почти всё время. Дата публикации
            # сменилась — объявление перепубликовали, описание качаем заново.
            cached = db.cached_details(conn, draft.key) if with_details else None
            if cached and (not draft.published_at or cached[2] == draft.published_at):
                text, skills, _published = cached
                vacancy = draft.model_copy(update={"description": text, "skills": skills})
                reused_details += 1
            elif with_details and hh_pages.worth_details(
                draft, bundle, owners.get(draft.key), prefilter.fuzzy, details_delta
            ):
                try:
                    detail = client.vacancy(draft.external_id)
                    vacancy = enrich(draft, detail)
                    enriched += 1
                    # Точка приехала с той же страницей: запишем, когда
                    # вакансия окажется в базе.
                    point = geo.point_of(detail)
                    if not vacancy.description.strip():
                        empty_descriptions += 1
                except BlockedError:
                    # Дальше ходить бессмысленно: сохраняем то, что уже собрали.
                    log.error("hh.ru закрылся капчей на деталях, добирать остальное не будем")
                    blocked = True
                    with_details = False
                except Exception:  # noqa: BLE001 — вакансия могла быть уже закрыта
                    log.warning("нет деталей по %s, берём черновик", draft.external_id)
            elif with_details and draft.source == sources.SOURCE_HH:
                skipped_details += 1
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
            matches = profiles.score_all(
                vacancy, bundle, owners.get(vacancy.key), prefilter.fuzzy, marker
            )
            chosen = profiles.best(matches)
            # Ни один свой профиль не пропустил: вакансия отклонена целиком.
            verdict = (
                chosen[1]
                if chosen is not None
                else Verdict(0.0, [], rejected=True, reject_reason="не подошла ни одному профилю")
            )
            # Слепок пишется для всего, даже для отклонённого: история публикаций
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
            # Без этого карта живёт только кнопкой дозаполнения.
            if point is not None and geo.save(conn, vacancy.key, point):
                addressed += 1
            # Где именно видели вакансию: главная ссылка в vacancies одна, а
            # площадок может быть несколько.
            source_store.remember(
                conn, vacancy.key, vacancy.source, vacancy.external_id, vacancy.url
            )
            # Балл каждого профиля живёт на связи: у профилей разные критерии.
            db.save_matches(
                conn,
                vacancy.key,
                [(pid, v.score, v.reasons) for pid, v in matches],
            )
            log.info("    скор %.1f", verdict.score)

            # Порог пройдён — компания идёт в очередь на изучение. Сам поиск идёт
            # после обхода hh.ru: мешать его с постраничным сбором — значит сбить
            # паузы и приблизить капчу.
            if profiles.passed(bundle, matches) and vacancy.company:
                to_research.setdefault(vacancy.company, getattr(vacancy, "site_url", None))
                to_contact.append(vacancy.key)

            # Этап extract. Только для вакансий, прошедших скоринг, и пулом после
            # обхода: ожидание шлюза внутри цикла сбивало ритм пауз hh.ru.
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
    if addressed:
        log.info("адресов с точкой на карте сохранено: %s", addressed)
    if skipped_details:
        log.info(
            "карточек не качали: %s (до порога не хватало больше %.0f баллов)",
            skipped_details,
            details_delta,
        )

    # Этапы модели по всему собранному разом: сеть ждут параллельно, в базу
    # пишет главный поток.
    if to_extract:
        # Сначала разметка спанами, если она включена: цитата оттуда дословна
        # по построению, и на эти вакансии модель не тратится вовсе.
        by_spans = {
            vacancy.key: items
            for vacancy in to_extract
            for items in (extract_spans.conditions(vacancy.description),)
            if items
        }
        for key, items in by_spans.items():
            if conditions.store(conn, key, items):
                extracted += 1
        rest = stage_gates.keep_for_stage(
            conn,
            gateway,
            "extract",
            [v for v in to_extract if v.key not in by_spans],
        )
        for key, items in llm_batch.extract_all(db_path, rest).items():
            if conditions.store(conn, key, items):
                extracted += 1
    if to_claim:
        kept = {
            vacancy.key
            for vacancy in stage_gates.keep_for_stage(
                conn, gateway, "hr_filter", [pair[0] for pair in to_claim]
            )
        }
        # Отчёт детектора сохраняется всегда: гейт экономит вызов модели, а не
        # выкидывает детерминированные находки [CORE-015].
        for vacancy, report in to_claim:
            if vacancy.key not in kept:
                detector.store(conn, report)
        to_claim = [pair for pair in to_claim if pair[0].key in kept]
        enriched_reports = llm_batch.claims_all(db_path, to_claim)
        for vacancy, report in to_claim:
            detector.store(conn, enriched_reports.get(vacancy.key, report))

    # Отметка «видели сегодня» нужна и для отклонённых вакансий, иначе они будут
    # считаться пропавшими сразу после первого прогона.
    if seen:
        db.touch_seen(conn, seen.keys())

    # Вторая половина истории: что исчезло из выдачи. Только после чистого
    # прогона без лимита — иначе «закроем» живые вакансии разом.
    partial = bool(options.limit) and len(drafts) >= options.limit
    if not blocked and not partial and not client.fallback_pages and seen:
        db.deactivate_missing(conn, seen.keys())
    elif partial:
        log.info("сбор оборван лимитом — пропавшие вакансии не отмечаем")

    # Векторы (ADR-021) — до досье: перефразированные отзывы ловятся уже в этом
    # же прогоне. Этап выключен по умолчанию и без эмбеддера ничего не делает
    # [CORE-017].
    embeddings_tasks.index_vacancies(conn, gateway)

    # Досье на компании. Идёт после hh.ru и до отправки карточек: без него в
    # карточке не будет самой полезной строки.
    researched: dict[str, dossier.Dossier] = {}
    # Строка про два порога: в базу попадает всё, что прошло предфильтр, а
    # досье и контакты — только то, что прошло порог профиля. Без этой строки
    # «вакансий 19, досье 5» выглядит как потеря данных.
    log.info("по площадкам — %s", sources.queue_note(seen, drafts))
    log.info(
        "в очередь на досье: компаний %s из %s вакансий прогона (порог профиля %.0f)",
        len(to_research),
        len(drafts),
        profiles.min_threshold(bundle),
    )
    if to_research:
        researched = research_companies(
            conn, db_path, to_research, use_llm=options.use_llm
        )

    # Контакты — сразу после досье: канал нужен в карточке до всякого письма.
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

    # Ниже самого низкого порога карточка не нужна ни одному профилю.
    rows = db.pending_cards(
        conn, profiles.min_threshold(bundle), options.limit or CARD_LIMIT
    )
    log.info("новых вакансий: %s, к отправке: %s", new_count, len(rows))

    signals = run_cards.card_lines(conn, rows, score_opts.enabled)

    run_cards.deliver(conn, rows, signals, dry_run=args.dry_run)

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
