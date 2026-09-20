"""Пересчёт уже собранной базы: применить сегодняшние правила ко вчерашним данным.

Зачем это нужно. Пайплайн считает всё один раз — в момент сбора. Поэтому
вакансия, пойманная месяц назад, до сих пор живёт с тем скором, который дал
тогдашний профиль, без ai-метки (её тогда не было), без проверки на инъекции
(детектор появился позже) и без вектора. Поменял вес навыка или порог — новые
вакансии считаются по-новому, старые по-старому, и список перестаёт быть
сравнимым сам с собой. Это худший вид ошибки: цифры есть, выглядят настоящими
и врут.

Что здесь есть и чего нет. Только то, что считается из своей базы и своими
правилами [CORE-015]: сеть не трогается вообще, hh.ru не опрашивается, модель
зовётся ровно в одном шаге — эмбеддинги, и тот пропускается, если эмбеддера
нет [CORE-017]. Добрать описания или отзывы отсюда нельзя: это сбор, его дело
run.py.

Что не переписывается ни при каких шагах: `first_seen_at`, `last_seen_at`,
`notified_at`, `feedback`. История встреч и то, что владелец уже видел карточку,
— это факты, а не расчёт.

Запуск: `python rebuild.py` (все шаги) или `python rebuild.py --steps score,vectors`.
В интерфейсе — кнопка «Пересчёт базы» на странице запуска.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

import aitext
import aitext_rules
import company_score_rules
import company_score_store
import db
import detector
import embeddings_store
import embeddings_tasks
import injection_store
import market_store
import settings
from hh import Vacancy
from run_setup import build_gateway, setup_logging
from score import Profile, evaluate

log = logging.getLogger("fuckhr")

# Сколько строк обрабатывается за один SELECT. База личная и маленькая, но
# описание вакансии — это килобайты, и грузить всё разом незачем.
CHUNK = 200


def vacancy_of(row: sqlite3.Row) -> Vacancy:
    """Строка базы обратно в модель. Ключ берётся из базы, а не считается.

    Пересчитанный ключ совпал бы почти всегда, но «почти» здесь мало: если
    правило нормализации названия когда-нибудь изменится, запись уедет в
    новую строку, а старая останется с прежним скором навсегда.
    """
    try:
        skills = json.loads(row["skills"] or "[]")
    except (TypeError, ValueError):
        skills = []
    gross = row["gross"]
    return Vacancy(
        source=str(row["source"] or "hh.ru"),
        external_id=str(row["external_id"] or ""),
        url=str(row["url"] or ""),
        title=str(row["title"] or ""),
        company=row["company"],
        company_id=row["company_id"],
        area=row["area"],
        salary_from=row["salary_from"],
        salary_to=row["salary_to"],
        currency=row["currency"],
        gross=None if gross is None else bool(gross),
        schedule=row["schedule"],
        experience=row["experience"],
        employment=row["employment"],
        skills=[str(item) for item in skills],
        description=str(row["description"] or ""),
        published_at=row["published_at"],
    )


def _rows(conn: sqlite3.Connection, where: str = "") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM vacancies {} ORDER BY last_seen_at DESC".format(where)
    ).fetchall()


# ———— шаги ————


def market(conn: sqlite3.Connection) -> int:
    """Медианы по рынку заново. Первым шагом: скоринг опирается на них."""
    return market_store.recompute(conn)


def score(conn: sqlite3.Connection, profile: Profile) -> int:
    """Скор, причины, метка рынка и ai-метка по сегодняшним правилам.

    Пишется узким UPDATE, а не `db.upsert_vacancy`: тот обновляет `last_seen_at`
    и сделал бы вид, что вакансию сегодня видели на сайте.
    """
    prefilter = settings.prefilter_options()
    score_opts = settings.company_score_options()
    rows = _rows(conn)
    changed = 0
    for index, row in enumerate(rows, 1):
        vacancy = vacancy_of(row)
        marker = market_store.marker_for(conn, vacancy)
        verdict = evaluate(vacancy, profile, prefilter.fuzzy, market_marker=marker)
        if score_opts.in_score and vacancy.company:
            level = company_score_store.level_of(conn, vacancy.company)
            if level == company_score_rules.LEVEL_RED:
                verdict.score = max(0.0, verdict.score - score_opts.penalty)
                verdict.reasons.append(
                    "оценка работодателя: красные флаги (−{:g})".format(
                        score_opts.penalty
                    )
                )
        ai_verdict = aitext.assess(
            vacancy.description, aitext_rules.VACANCY, vacancy.published_at
        )
        conn.execute(
            "UPDATE vacancies SET score = ?, score_reasons = ?, market_label = ?,"
            " market_median = ?, market_delta = ?, market_level = ?, ai_label = ?,"
            " ai_score = ?, ai_signals = ? WHERE key = ?",
            (
                verdict.score,
                json.dumps(list(verdict.reasons), ensure_ascii=False),
                getattr(marker, "label", None),
                getattr(getattr(marker, "stats", None), "median", None),
                getattr(marker, "deviation", None),
                getattr(getattr(marker, "stats", None), "level", None),
                getattr(ai_verdict, "label", None),
                getattr(ai_verdict, "score", None),
                "; ".join(getattr(ai_verdict, "reasons", ()) or ()) or None,
                str(row["key"]),
            ),
        )
        if abs(float(row["score"] or 0.0) - verdict.score) >= 0.05:
            changed += 1
        if index % CHUNK == 0:
            conn.commit()
            log.info("  скоринг [%s/%s]", index, len(rows))
    conn.commit()
    log.info("скоринг: пересчитано %s, скор изменился у %s", len(rows), changed)
    return changed


def injections(conn: sqlite3.Connection) -> int:
    """Детектор инъекций по всем описаниям. Находки видны на странице «Инъекции».

    Гоняется по всей базе, а не по непроверенным: «проверено и чисто» нигде не
    записано, а заводить для этого колонку дороже, чем прогнать регулярки
    ещё раз — это секунды на личной базе.
    """
    injection_store.ensure_schema(conn)
    rows = _rows(conn, "WHERE description IS NOT NULL AND description <> ''")
    dirty = 0
    for index, row in enumerate(rows, 1):
        report = injection_store.check_text(
            conn,
            "vacancy",
            str(row["key"]),
            str(row["company"] or ""),
            str(row["description"] or ""),
        )
        if report.dirty:
            dirty += 1
        if index % CHUNK == 0:
            log.info("  инъекции [%s/%s]", index, len(rows))
    log.info("инъекции: проверено %s, с находками %s", len(rows), dirty)
    return dirty


def claims(conn: sqlite3.Connection) -> int:
    """Утверждения вакансии против накопленной истории (ADR-009).

    Смысл пересчёта: история растёт после каждого прогона, и вакансия,
    выглядевшая честной в первый день, через три перепубликации перестаёт.
    Модель здесь не участвует — только детерминированная часть.
    """
    detector.ensure_schema(conn)
    rows = _rows(conn)
    flagged = 0
    for index, row in enumerate(rows, 1):
        vacancy = vacancy_of(row)
        report = detector.assess(vacancy, detector.history(conn, str(row["key"])))
        detector.store(conn, report)
        if getattr(report, "flags", None):
            flagged += 1
        if index % CHUNK == 0:
            log.info("  утверждения [%s/%s]", index, len(rows))
    log.info("утверждения: пересчитано %s, с флагами %s", len(rows), flagged)
    return flagged


def companies(conn: sqlite3.Connection) -> int:
    """Оценка работодателей по всем накопленным уликам."""
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT company FROM vacancies"
            " WHERE company IS NOT NULL AND company <> ''"
        ).fetchall()
    ]
    return company_score_store.refresh(conn, names)


def vectors(conn: sqlite3.Connection, gateway: object | None) -> int:
    """Векторы для всей базы, а не для 500 свежих, как в прогоне.

    Единственный шаг, который ходит к модели. Без эмбеддера — ноль и строка
    в логе, остальные шаги от этого не страдают [CORE-017].
    """
    total = conn.execute(
        "SELECT COUNT(*) FROM vacancies WHERE description IS NOT NULL"
        " AND description <> ''"
    ).fetchone()[0]
    model = embeddings_tasks.index_vacancies(conn, gateway, limit=int(total or 0))
    if not model:
        log.info("векторы: пропущены (эмбеддер выключен или недоступен)")
        return 0
    counted = sum(
        count
        for kind, name, count in embeddings_store.counts(conn)
        if kind == embeddings_store.KIND_VACANCY and name == model
    )
    log.info("векторы: модель %s, всего векторов вакансий %s", model, counted)
    return counted


STEPS: dict[str, tuple[str, str]] = {
    "market": ("Медианы по рынку", "пересчитать статистику зарплат"),
    "injections": ("Инъекции", "проверить все описания детектором"),
    "claims": ("Утверждения", "сверить обещания вакансий с историей"),
    "score": ("Скоринг", "пересчитать скор по текущему профилю"),
    "companies": ("Работодатели", "пересчитать оценку компаний по уликам"),
    "vectors": ("Векторы", "досчитать эмбеддинги по всей базе"),
}

# Порядок не алфавитный и важен: рынок нужен скорингу, улики — оценке компаний,
# а оценка компаний — скорингу, если владелец включил её учёт. Полный круг
# сходится за один проход, потому что скоринг берёт уровень прошлого расчёта:
# так же, как в прогоне.
ORDER = ("market", "injections", "claims", "companies", "score", "vectors")


def run(conn: sqlite3.Connection, steps: list[str], gateway: object = None) -> dict:
    """Выполняет шаги в правильном порядке. Возвращает {шаг: число}."""
    profile = None
    out: dict[str, int] = {}
    for name in ORDER:
        if name not in steps:
            continue
        log.info("— %s: %s", STEPS[name][0], STEPS[name][1])
        if name == "score":
            if profile is None:
                profile = Profile.load(settings.collect_options().profile)
            out[name] = score(conn, profile)
        elif name == "market":
            out[name] = market(conn)
        elif name == "injections":
            out[name] = injections(conn)
        elif name == "claims":
            out[name] = claims(conn)
        elif name == "companies":
            out[name] = companies(conn)
        elif name == "vectors":
            out[name] = vectors(conn, gateway)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Пересчёт уже собранной базы по текущим правилам. Сеть не трогается."
    )
    parser.add_argument(
        "--steps",
        default="",
        help="через запятую: {}. По умолчанию все".format(", ".join(ORDER)),
    )
    parser.add_argument("--verbose", action="store_true", help="подробный лог (DEBUG)")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(Path(settings.get("LOG_PATH", "data/fuckhr.log")), args.verbose)
    wanted = [name.strip() for name in args.steps.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in STEPS]
    if unknown:
        log.error("неизвестные шаги: %s. Есть: %s", ", ".join(unknown), ", ".join(ORDER))
        return 2
    steps = wanted or list(ORDER)

    conn = db.connect(Path(settings.get("DB_PATH", "data/fuckhr.sqlite3")))
    try:
        db.init_schema(conn)
        total = conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]
        log.info("вакансий в базе: %s, шагов: %s", total, len(steps))
        gateway = build_gateway(conn, disabled=False) if "vectors" in steps else None
        result = run(conn, steps, gateway)
    finally:
        conn.close()

    log.info("готово: %s", ", ".join("{}={}".format(k, v) for k, v in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
