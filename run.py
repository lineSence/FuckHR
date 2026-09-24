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
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

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
import injection_store
import aitext
import aitext_rules
import market_company
import market_store
import outreach
import settings
import source_store
import sources
import targets_hh
import websearch
from collector import collect  # noqa: F401 — реэкспорт: сбор живёт в collector.py
from hh import Vacancy, enrich
from hh_html import BlockedError, HHHtmlClient
import run_bg
import run_cards
import run_loop
import embeddings_tasks
import run_details
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
    # Этапы модели и досье считаются в фоне, пока главный поток ждёт паузы
    # hh.ru на карточках вакансий (run_bg).
    # Бюджет вызовов модели один на прогон: фоновые потоки открывают свои
    # шлюзы, но считают в общий счётчик (llm_budget, ADR-022).
    budget = gateway.budget if gateway is not None else None
    stages = run_bg.Stages(db_path, options.use_llm, budget=budget)
    research_job = run_bg.Research(conn, db_path, options.use_llm, budget=budget)
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
    # Другие площадки: свой обход, свои паузы, ключ дедупа тот же. Идут в фоне,
    # пока hh.ru отсиживает свои паузы. Вакансия, найденная и здесь и на hh.ru,
    # остаётся одной записью — площадка уходит в vacancy_sources, а этапы
    # модели платятся один раз [CORE-016].
    extra_job = sources.start_external(bundle, options.limit, prefilter)
    # Пул карточек создаётся до try: его закрывает finally, и он не должен
    # оказаться неопределённым, если сбор упадёт раньше.
    details = run_details.Details(client)
    try:
        if hh_on:
            seen, drafts, owners = profiles.collect_all(
                client, bundle, options.limit, prefilter, conn=conn
            )
            log.info("увидели: %s, прошло предфильтр: %s", len(seen), len(drafts))
        else:
            owners = {}
            log.info("hh.ru выключен галочкой на главной: ни поиска, ни целей")
        extra_seen, extra_drafts, extra_owners = extra_job.result(conn, known=tuple(seen))
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
        # План карточек: пока главный поток скорит первую вакансию, пул уже
        # качает следующие. Темп к hh.ru держит бакет (net_rate), поэтому
        # потоки его не ускоряют (docs/performance.md).
        cached_details, skipped_details = run_details.plan(
            conn,
            drafts.values(),
            details,
            bundle,
            owners,
            prefilter.fuzzy,
            details_delta,
            with_details,
        )
        for position, draft in enumerate(drafts.values(), start=1):
            # Счётчик в квадратных скобках — то, по чему интерфейс рисует полоску.
            log.info("[%s/%s] %s — %s", position, total, draft.title, draft.company)
            vacancy = draft
            point = None
            # Описание из базы вместо второго похода на hh.ru: на повторном
            # прогоне именно эти запросы съедали почти всё время. Дата публикации
            # сменилась — объявление перепубликовали, описание качаем заново.
            cached = cached_details.get(draft.key)
            if cached is not None:
                text, skills, _published = cached
                vacancy = draft.model_copy(update={"description": text, "skills": skills})
                reused_details += 1
            else:
                detail = details.get(draft.key)
                if detail is not None:
                    vacancy = enrich(draft, detail)
                    enriched += 1
                    # Точка приехала с той же страницей: запишем, когда
                    # вакансия окажется в базе.
                    point = geo.point_of(detail)
                    if not vacancy.description.strip():
                        empty_descriptions += 1
                elif details.blocked and not blocked:
                    # Дальше ходить бессмысленно: сохраняем то, что уже собрали.
                    blocked = True
                    with_details = False
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
            passed = profiles.passed(bundle, matches)
            if passed and vacancy.company:
                site_url = getattr(vacancy, "site_url", None)
                if vacancy.company not in to_research:
                    to_research[vacancy.company] = site_url
                    research_job.submit(vacancy.company, site_url)
                to_contact.append(vacancy.key)

            # Этап extract. Только для вакансий, прошедших скоринг, и пулом после
            # обхода: ожидание шлюза внутри цикла сбивало ритм пауз hh.ru.
            if gateway is not None and vacancy.description.strip():
                stages.extract(vacancy)

            # Детектор запускается сразу после слепка: история уже включает
            # текущий прогон, и вывод не отстаёт от карточки на один запуск.
            if detector_opts.enabled:
                report = detector.assess(vacancy, detector.history(conn, vacancy.key))
                if detector_llm.wanted(gateway, detector_opts.use_llm_claims, passed):
                    # Этап hr_filter: модель только отмечает утверждения, вердикт у всех
                    # таких пунктов — «недостаточно данных» (detector_llm.with_llm_claims).
                    stages.claim(vacancy, report)
                else:
                    detector.store(conn, report)
    except BlockedError as exc:
        blocked = True
        log.error("%s", exc)
    finally:
        details.close()
        # getattr: у поддельных клиентов в тестах очереди нет.
        waited = getattr(client, "waited", 0.0)
        if waited:
            log.info(
                "в очереди к hh.ru простояли %.0f с при темпе не чаще %.1f запросов в минуту",
                waited,
                client.bucket.rate_per_minute(),
            )
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

    # Этапы модели: порции считались в фоне, здесь только запись в базу.
    stage_conditions, stage_claims = stages.collect()
    for key, items in stage_conditions.items():
        if conditions.store(conn, key, items):
            extracted += 1
    for report in stage_claims.values():
        detector.store(conn, report)

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

    # Досье на компании собирались в фоне, пока шёл сбор; здесь их дожидаются
    # и сохраняют. Без досье в карточке не будет самой полезной строки.
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
    researched = research_job.collect()

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
            db_path=db_path,
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
