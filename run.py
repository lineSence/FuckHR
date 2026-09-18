"""Один прогон пайплайна MVP.

    hh.ru (HTML поиска) -> предфильтр -> страница вакансии -> скоринг
    -> условия работы -> детектор утверждений -> SQLite
    -> досье на компании (параллельно) -> Telegram

Источник данных — HTML страниц hh.ru: публичный API закрыт с апреля 2026
(ADR-015). Запускается из Task Scheduler через pythonw.exe (ADR-014).

Настроек в командной строке больше нет. Сколько собирать, по какому профилю,
ходить ли за описаниями и использовать ли модель — всё это живёт в .env и
правится в веб-интерфейсе (webui.py). Флаги --dry-run и --verbose остались:
это не настройки, а режим одного запуска.

Что означает лимит (RUN_LIMIT). Он ограничивает именно сбор: как только
набралось N вакансий, прошедших предфильтр, остальные страницы и запросы не
запрашиваются.

У прогона пять обязанностей:
1. собрать и отправить карточки;
2. зафиксировать историю — и появление, и исчезновение вакансии (ADR-010);
3. сопоставить утверждения вакансии с этой историей (detector.py, ADR-009);
4. собрать досье на компании, чьи вакансии прошли порог (research.py);
5. пожаловаться, если сам сломался (canary.py), а не тихо вернуть ноль.

Почему досье собирается параллельно и после порога — см. research.py. Порог
здесь главный фильтр цены: досье собирается только по тем конторам, чей оффер
вообще интересен.

Писем здесь нет и не будет. Контакты и черновики готовит outreach.py по
явному запросу владельца: письмо — решение человека, а не побочный эффект
ночного сканирования ([OUT-006], ADR-012).

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
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

import bot as tg
import canary
import conditions
import db
import detector
import detector_llm
import dossier
import llm
import llm_tasks
import settings
from hh import Vacancy, enrich
from hh_html import BlockedError, HHHtmlClient
from research import (  # noqa: F401 — реэкспорт для старых вызовов
    MAX_RESEARCH_WORKERS,
    _research_one,
    research_companies,
    research_workers,
)
from score import Profile, evaluate

log = logging.getLogger("fuckhr")


def setup_logging(log_path: Path, verbose: bool) -> None:
    """Лог всегда идёт и в файл, и в stdout.

    Строки в stdout — единственный источник обратной связи для интерфейса и
    планировщика: он читает их и по счётчикам вида «[3/30]» рисует полоску.
    Раньше stdout появлялся только при --verbose, и запуск из браузера выглядел
    как зависание. Теперь --verbose меняет только подробность (DEBUG).

    Потоки досье пишут в тот же лог: имя потока в формате нужно, иначе
    переплетённые строки нескольких компаний невозможно различить.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    file_handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s")
    )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[file_handler, stream],
        force=True,
    )


def collect(
    client: HHHtmlClient, profile: Profile, limit: int = 0
) -> tuple[dict[str, Vacancy], dict[str, Vacancy]]:
    """Собирает вакансии по запросам профиля, но не больше limit штук.

    Возвращает две карты: всё увиденное и то, что прошло предфильтр. Первая нужна
    истории: «вакансия видна в выдаче» — факт о рынке, независимый от нашего интереса.

    limit останавливает обход сразу как только набралось нужное число: генератор
    поиска бросается недочитанным, и остальные страницы не запрашиваются. Каждая
    незапрошенная страница — это сэкономленные две-три секунды паузы и шаг от капчи.
    """
    seen: dict[str, Vacancy] = {}
    passed: dict[str, Vacancy] = {}
    queries = [q for q in profile.queries if q.get("text")]
    for index, query in enumerate(queries, start=1):
        if limit and len(passed) >= limit:
            log.info("лимит %s набран, остальные запросы не трогаем", limit)
            break
        text = query["text"]
        log.info("[%s/%s] запрос: %s", index, len(queries), text)
        pages = client.search(
            text=text,
            area=query.get("area") or profile.areas or None,
            period=int(query.get("period", 7)),
            max_pages=int(query.get("max_pages", 3)),
            extra=query.get("extra"),
        )
        try:
            for draft in pages:
                seen.setdefault(draft.key, draft)
                rough = evaluate(draft, profile)
                if rough.rejected:
                    log.debug(
                        "отброшено на предфильтре: %s (%s)",
                        draft.title,
                        rough.reject_reason,
                    )
                    continue
                passed.setdefault(draft.key, draft)
                if limit and len(passed) >= limit:
                    log.info(
                        "собрали %s вакансий при лимите %s, больше страниц не запрашиваем",
                        len(passed),
                        limit,
                    )
                    break
        finally:
            # Генератор закрываем явно: иначе он доживает до сборки мусора и не
            # очевидно когда отпустит соединение.
            pages.close()
    return seen, passed


def build_gateway(conn, disabled: bool) -> llm.Gateway | None:
    """Шлюз или None. None — штатный режим, а не авария.

    Кэш живёт в той же базе, что и вакансии: повторный прогон по тем же
    описаниям не должен стоить ни одного вызова [LLM-006].
    """
    if disabled:
        log.info("модель выключена в настройках (LLM_ENABLED)")
        return None
    gateway = llm.Gateway.from_env(conn)
    if not gateway.enabled:
        log.info("модель не настроена (%s), идём без неё", gateway.disabled_reason)
        return None
    for stage, profile, route, model in gateway.describe_routes():
        if stage in {"extract", "hr_filter", "company"}:
            log.info("этап %s: профиль %s, маршрут %s, модель %s", stage, profile, route, model)
    return gateway


def notify_if_broken(stats: canary.RunStats, dry_run: bool) -> list[canary.Alert]:
    """Считает поводы для тревоги и пишет в Telegram не чаще раза в сутки."""
    alerts = canary.check(stats)
    for alert in alerts:
        log.warning("канарейка [%s]: %s", alert.kind, alert.text.replace("\n", " "))
    if not alerts or dry_run:
        return alerts

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("канарейке некуда писать: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return alerts

    state_path = Path(os.getenv("ALERT_STATE_PATH", "data/alerts.json"))
    state = canary.load_state(state_path)
    due = canary.filter_due(
        state, alerts, cooldown_hours=float(os.getenv("ALERT_COOLDOWN_HOURS", "24"))
    )
    if not due:
        log.info("о этих сбоях уже писали недавно, молчим")
        return alerts
    if asyncio.run(tg.send_alert(token, chat_id, canary.format_message(due))):
        canary.save_state(state_path, state)
    return alerts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FuckHR: один прогон сбора. Настройки — в webui.py"
    )
    parser.add_argument("--dry-run", action="store_true", help="без отправки в Telegram")
    parser.add_argument("--verbose", action="store_true", help="подробный лог (DEBUG)")
    args = parser.parse_args()

    load_dotenv()
    options = settings.collect_options()
    db_path = Path(settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    setup_logging(Path(settings.get("LOG_PATH", "data/fuckhr.log")), args.verbose)
    log.info(
        "настройки: лимит %s, профиль %s, описания %s, модель %s",
        options.limit,
        options.profile,
        "да" if options.details else "нет",
        "да" if options.use_llm else "нет",
    )

    profile = Profile.load(options.profile)

    conn = db.connect(db_path)
    db.init_schema(conn)
    detector.ensure_schema(conn)
    conditions.ensure_schema(conn)
    dossier.ensure_schema(conn)

    gateway = build_gateway(conn, not options.use_llm)
    extracted = 0

    new_count = 0
    enriched = 0
    empty_descriptions = 0
    blocked = False
    with_details = options.details
    seen: dict[str, Vacancy] = {}
    drafts: dict[str, Vacancy] = {}
    # Компании вакансий, прошедших скоринг: именно их изучаем после сбора.
    to_research: dict[str, str | None] = {}

    client = HHHtmlClient(
        pause=settings.as_float(os.getenv("HH_PAUSE"), 2.0),
        cookie=os.getenv("HH_COOKIE") or None,
        proxy=os.getenv("HH_PROXY") or None,
        failure_dir=settings.get("FAILURE_DIR", "data/failures"),
    )
    try:
        seen, drafts = collect(client, profile, options.limit)
        log.info("увидели: %s, прошло предфильтр: %s", len(seen), len(drafts))
        total = len(drafts)
        for position, draft in enumerate(drafts.values(), start=1):
            # Счётчик в квадратных скобках — то, по чему интерфейс рисует полоску.
            log.info("[%s/%s] %s — %s", position, total, draft.title, draft.company)
            vacancy = draft
            if with_details:
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
            verdict = evaluate(vacancy, profile)
            # Слепок пишется для всего, даже для отклоныённого: история публикаций
            # нужна детектору независимо от нашего интереса (ADR-009, ADR-010).
            db.add_snapshot(conn, vacancy)
            if verdict.rejected:
                log.info("    отклонена: %s", verdict.reject_reason)
                continue
            if db.upsert_vacancy(conn, vacancy, verdict.score, verdict.reasons):
                new_count += 1
            log.info("    скор %.1f", verdict.score)

            # Порог пройдён — компания идёт в очередь на изучение. Сам поиск запускается
            # после обхода hh.ru: мешать его с постраничным сбором — значит сбить паузы
            # и приблизить капчу.
            if verdict.score >= profile.min_score and vacancy.company:
                to_research.setdefault(vacancy.company, getattr(vacancy, "site_url", None))

            # Этап extract. Только для вакансий, прошедших скоринг: гонять модель
            # по отклонённым — жечь бюджет вызовов ради данных, которые никто не прочтёт.
            if gateway is not None and vacancy.description.strip():
                items = llm_tasks.extract_conditions(gateway, vacancy.description)
                if conditions.store(conn, vacancy.key, items):
                    extracted += 1

            # Детектор запускается сразу после слепка: история уже включает
            # текущий прогон, и вывод не отстаёт от карточки на один запуск.
            report = detector.assess(vacancy, detector.history(conn, vacancy.key))
            if gateway is not None:
                # Этап hr_filter: модель только отмечает утверждения, вердикт у всех
                # таких пунктов — «недостаточно данных» (detector_llm.with_llm_claims).
                report = detector_llm.with_llm_claims(report, vacancy, gateway)
            detector.store(conn, report)
    except BlockedError as exc:
        blocked = True
        log.error("%s", exc)
    finally:
        client.close()

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

    # Досье на компании. Идёт после hh.ru и до отправки карточек: без него в карточке
    # не будет самой полезной строки — стоит ли вообще связываться с этими людьми.
    researched: dict[str, dossier.Dossier] = {}
    if to_research:
        researched = research_companies(
            conn, db_path, to_research, use_llm=options.use_llm
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

    rows = db.pending_cards(conn, profile.min_score, options.limit)
    log.info("новых вакансий: %s, к отправке: %s", new_count, len(rows))

    # Строки под карточкой: сначала работодатель, потом утверждения вакансии.
    # Порядок не косметика: красные флаги компании отменяют смысл читать дальше.
    signals: dict[str, list[str]] = {}
    for row in rows:
        lines: list[str] = []
        saved = dossier.load(conn, row["company"]) if row["company"] else None
        if saved is not None:
            lines += dossier.row_to_lines(saved)
        lines += detector.load_lines(conn, row["key"])
        signals[row["key"]] = lines

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
