"""Один прогон пайплайна MVP (шаги 1–3 из docs/mvp-windows.md).

    hh.ru (HTML поиска) -> предфильтр -> страница вакансии -> скоринг
    -> условия работы -> детектор утверждений -> SQLite -> Telegram

Источник данных — HTML страниц hh.ru: публичный API закрыт с апреля 2026
(ADR-015). Запускается из Task Scheduler через pythonw.exe (ADR-014).

Настроек в командной строке больше нет. Сколько собирать, по какому профилю,
ходить ли за описаниями и использовать ли модель — всё это живёт в .env и
правится в веб-интерфейсе (webui.py). Причина простая: запусков три — руками,
из интерфейса и из планировщика, и параметры у них должны совпадать. Флаги
--dry-run и --verbose остались: это не настройки, а режим одного запуска.

У прогона четыре обязанности, а не одна:
1. собрать и отправить карточки;
2. зафиксировать историю — и появление, и исчезновение вакансии (ADR-010);
3. сопоставить утверждения вакансии с этой историей (detector.py, ADR-009);
4. пожаловаться, если сам сломался (canary.py), а не тихо вернуть ноль.

Где здесь модель (ADR-005, ADR-017). Два этапа и оба необязательные:

- extract — вытащить условия работы из описания (формат, график, вилка),
  там где регулярки бессильны против живого языка;
- hr_filter — показать пальцем на проверяемые утверждения в тексте.

Сбор, предфильтр, скоринг, история и канарейка остаются детерминированными
[CORE-015]: с выключенной моделью и при любой её ошибке прогон доходит до конца
и выдаёт тот же список вакансий, просто беднее деталями [CORE-017].
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
import llm
import llm_tasks
import settings
from hh import Vacancy, enrich
from hh_html import BlockedError, HHHtmlClient
from score import Profile, evaluate

log = logging.getLogger("fuckhr")


def setup_logging(log_path: Path, verbose: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    ]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def collect(
    client: HHHtmlClient, profile: Profile
) -> tuple[dict[str, Vacancy], dict[str, Vacancy]]:
    """Собирает вакансии по всем запросам профиля.

    Возвращает две карты: всё увиденное и то, что прошло предфильтр. Первая нужна
    истории: «вакансия видна в выдаче» — факт о рынке, независимый от нашего интереса.

    Страница вакансии запрашивается только для того, что прошло предфильтр по
    заголовку и вилке: каждый лишний запрос приближает капчу.
    """
    seen: dict[str, Vacancy] = {}
    passed: dict[str, Vacancy] = {}
    for query in profile.queries:
        text = query.get("text")
        if not text:
            continue
        log.info("запрос: %s", text)
        for draft in client.search(
            text=text,
            area=query.get("area") or profile.areas or None,
            period=int(query.get("period", 7)),
            max_pages=int(query.get("max_pages", 3)),
            extra=query.get("extra"),
        ):
            seen.setdefault(draft.key, draft)
            rough = evaluate(draft, profile)
            if rough.rejected:
                log.debug("отброшено на предфильтре: %s (%s)", draft.title, rough.reject_reason)
                continue
            passed.setdefault(draft.key, draft)
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
        if stage in {"extract", "hr_filter"}:
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
    parser.add_argument("--verbose", action="store_true", help="лог также в консоль")
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

    gateway = build_gateway(conn, not options.use_llm)
    extracted = 0

    new_count = 0
    enriched = 0
    empty_descriptions = 0
    blocked = False
    with_details = options.details
    seen: dict[str, Vacancy] = {}
    drafts: dict[str, Vacancy] = {}

    client = HHHtmlClient(
        pause=settings.as_float(os.getenv("HH_PAUSE"), 2.0),
        cookie=os.getenv("HH_COOKIE") or None,
        proxy=os.getenv("HH_PROXY") or None,
        failure_dir=settings.get("FAILURE_DIR", "data/failures"),
    )
    try:
        seen, drafts = collect(client, profile)
        log.info("увидели: %s, прошло предфильтр: %s", len(seen), len(drafts))
        for draft in drafts.values():
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
            # Слепок пишется для всего, даже для отклонённого: история публикаций
            # нужна детектору независимо от нашего интереса (ADR-009, ADR-010).
            db.add_snapshot(conn, vacancy)
            if verdict.rejected:
                continue
            if db.upsert_vacancy(conn, vacancy, verdict.score, verdict.reasons):
                new_count += 1

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
        print(f"hh.ru заблокировал сбор: {exc}")
    finally:
        client.close()

    # Отметка «видели сегодня» нужна и для отклонённых вакансий, иначе они будут
    # считаться пропавшими сразу после первого прогона.
    if seen:
        db.touch_seen(conn, seen.keys())

    # Вторая половина истории: что исчезло из выдачи. Только после чистого прогона:
    # при капче или сломанном парсере мы бы «закрыли» всю базу разом и испортили историю.
    if not blocked and not client.fallback_pages and seen:
        db.deactivate_missing(conn, seen.keys())

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

    signals = {row["key"]: detector.load_lines(conn, row["key"]) for row in rows}

    if args.dry_run:
        for row in rows:
            print(f"{row['score']:5.1f}  {row['title']} — {row['company']}")
            print(f"        {row['url']}")
            for line in conditions.lines(conn, row["key"]):
                print(f"        {line}")
            for line in signals.get(row["key"], []):
                print(f"        {line}")
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

    filled, total = db.published_at_coverage(conn)
    flagged, assessed = detector.coverage(conn)
    log.info(
        "итого в базе: %s, слепков с датой публикации: %s из %s, "
        "отчётов детектора с флагами: %s из %s",
        db.stats(conn),
        filled,
        total,
        flagged,
        assessed,
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
    conn.close()
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
