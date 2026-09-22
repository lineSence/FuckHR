"""Датасет для файнтюна из своей базы: те же промпты, эталоны из своих данных.

    python dataset_export.py --out data/train

Зачем. Синтетика покрывает ловушки, но не распределение: живые вакансии
длиннее, грязнее и написаны иначе. В базе уже лежат ответы, которые пайплайн
принял и сохранил, — их и берём эталонами, без единого вызова модели.

Что откуда:

- extract — `vacancy_conditions` (поле, значение, дословная цитата);
- hr_filter — `vacancy_signals`, пункты kind=llm_claim;
- dossier — `company_dossier.summary`, где summary_by = «модель», плюс тексты
  отзывов из `company_reviews`.

Чего здесь нет и почему:

- contacts, draft, intake, резюме — персональные этапы. Файл уезжает в
  облако на обучение, а имена, письма и резюме владельца туда не уходят
  [CORE-012], [CORE-013], [CORE-021].
- review_fake и ai_text — в `review_items` хранится только выдержка в 240
  символов, а вердикт выносился по полному тексту. Пара «обрезанный текст →
  чужой вердикт» учит плохому, поэтому этапа тут нет.

Эталон всё равно проверяется: он возвращается в ту же функцию пайплайна, и
если та его отбрасывает (цитата перестала быть дословной после чистки), пример
не попадает в файл [CORE-015].
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

from dotenv import load_dotenv

import dataset_core
import db
import detector
import settings
from contacts_rules import EMAIL_RE, PHONE_RE

log = logging.getLogger("dataset")

ROOT = Path(__file__).resolve().parent
STAGES: tuple[str, ...] = ("extract", "hr_filter", "dossier")
DEFAULT_CAP = 400          # примеров на этап
PER_COMPANY = 8            # чтобы один работодатель не забил корпус
MIN_CHARS = 200            # совсем короткие описания этапу нечего разбирать
REPORT_NAME = "report.json"

# Маски вместо контактов. Текст остаётся связным, а почта живого человека
# в облако не уезжает [CORE-021].
MASKS = ((EMAIL_RE, "почта@example.test"), (PHONE_RE, "+7 000 000-00-00"))


def scrub(text: str) -> str:
    """Убирает из текста контакты. Применяется и к источнику, и к эталону.

    Обязательно к обоим: иначе замаскированная цитата перестанет быть
    дословной и пример отбракуется на проверке.
    """
    out = text or ""
    for pattern, mask in MASKS:
        out = pattern.sub(mask, out)
    return out


# —— выборки из базы ——


def extract_items(conn: sqlite3.Connection, cap: int) -> Iterable[tuple[dict, dict]]:
    """Вакансия и её условия: (payload, gold) для этапа extract."""
    rows = conn.execute(
        """
        SELECT v.key, v.company, v.description
        FROM vacancies v
        WHERE EXISTS (SELECT 1 FROM vacancy_conditions c WHERE c.key = v.key)
          AND LENGTH(v.description) >= ?
        ORDER BY v.last_seen_at DESC
        """,
        (MIN_CHARS,),
    ).fetchall()
    for row in _capped(rows, cap):
        conditions = conn.execute(
            "SELECT field, value, quote FROM vacancy_conditions WHERE key = ? ORDER BY id",
            (row["key"],),
        ).fetchall()
        gold = {
            "conditions": [
                {
                    "field": item["field"],
                    "value": scrub(item["value"]),
                    "quote": scrub(item["quote"]),
                }
                for item in conditions
            ]
        }
        if gold["conditions"]:
            yield {"description": scrub(row["description"])}, gold


def hr_filter_items(conn: sqlite3.Connection, cap: int) -> Iterable[tuple[dict, dict]]:
    """Утверждения, которые модель нашла в тексте вакансии, из отчёта детектора."""
    rows = conn.execute(
        """
        SELECT v.key, v.company, v.title, v.description, v.skills, g.payload
        FROM vacancies v
        JOIN vacancy_signals g ON g.key = v.key
        ORDER BY v.last_seen_at DESC
        """
    ).fetchall()
    picked = 0
    for row in rows:
        if picked >= cap:
            return
        claims = _claims_of(row["payload"])
        if not claims:
            continue
        picked += 1
        yield {"text": scrub(detector.vacancy_text(row))}, {"claims": claims}


def _claims_of(payload: str) -> list[dict[str, str]]:
    """Пункты kind=llm_claim обратно в формат ответа модели."""
    try:
        data = json.loads(payload or "{}")
    except ValueError:
        return []
    out = []
    for finding in data.get("findings", []):
        if finding.get("kind") != "llm_claim":
            continue
        quote = str(finding.get("claimed") or "").strip()
        found = str(finding.get("found") or "")
        label = found.partition("«")[2].rpartition("»")[0] or "утверждение из текста"
        if quote:
            out.append({"label": scrub(label), "quote": scrub(quote)})
    return out


def dossier_items(conn: sqlite3.Connection, cap: int) -> Iterable[tuple[dict, str]]:
    """Сводка по отзывам, которую в своё время написала модель.

    Берутся только компании, у которых после сводки не появилось новых
    отзывов: иначе в промпт уйдут тексты, которых модель тогда не видела, и
    пример научит её выдумывать.
    """
    rows = conn.execute(
        """
        SELECT d.company, d.summary, d.updated_at
        FROM company_dossier d
        WHERE d.summary_by = 'модель' AND d.summary <> ''
          AND NOT EXISTS (
              SELECT 1 FROM company_reviews r
              WHERE r.company = d.company AND r.created_at > d.updated_at
          )
        ORDER BY d.updated_at DESC
        LIMIT ?
        """,
        (cap,),
    ).fetchall()
    for row in rows:
        reviews = conn.execute(
            """
            SELECT url, site, title, snippet, body FROM company_reviews
            WHERE company = ? ORDER BY id
            """,
            (row["company"],),
        ).fetchall()
        payload = {
            "company": row["company"],
            "reviews": [
                {
                    "url": review["url"],
                    "site": review["site"] or "",
                    "title": scrub(review["title"] or ""),
                    "snippet": scrub(review["snippet"] or ""),
                    "body": scrub(review["body"] or ""),
                }
                for review in reviews
            ],
        }
        if payload["reviews"]:
            yield payload, scrub(row["summary"])


def _capped(rows: Sequence[sqlite3.Row], cap: int) -> list[sqlite3.Row]:
    """Не больше PER_COMPANY строк на работодателя и не больше cap всего.

    У активного работодателя вакансии часто под копирку: без потолка корпус
    выучит именно его, а не язык объявлений.
    """
    seen: dict[str, int] = {}
    out = []
    for row in rows:
        company = (row["company"] or "").strip().lower()
        if seen.get(company, 0) >= PER_COMPANY:
            continue
        seen[company] = seen.get(company, 0) + 1
        out.append(row)
        if len(out) >= cap:
            break
    return out


SOURCES = {
    "extract": extract_items,
    "hr_filter": hr_filter_items,
    "dossier": dossier_items,
}


# —— сборка ——


def build(
    conn: sqlite3.Connection, stages: Sequence[str] = STAGES, cap: int = DEFAULT_CAP
) -> tuple[list[dict], dict[str, int]]:
    """Примеры и счётчик причин отказа."""
    rows: list[dict] = []
    rejected: dict[str, int] = {}
    seen: set[str] = set()
    for number, stage in enumerate(stages, start=1):
        source = SOURCES.get(stage)
        if source is None:
            continue
        taken = 0
        for payload, gold in source(conn, cap):
            row, why = dataset_core.example(stage, payload, gold)
            if row is None:
                rejected[why] = rejected.get(why, 0) + 1
                continue
            key = dataset_core.request_key(row)
            if key in seen:
                rejected["{}: дубль запроса".format(stage)] = (
                    rejected.get("{}: дубль запроса".format(stage), 0) + 1
                )
                continue
            seen.add(key)
            rows.append(row)
            taken += 1
        print("[{}/{}] {}: примеров {}".format(number, len(stages), stage, taken))
    return rows, rejected


def report_of(rows: Sequence[dict], rejected: dict[str, int], sizes: dict) -> dict:
    by_stage: dict[str, int] = {}
    for row in rows:
        by_stage[row["stage"]] = by_stage.get(row["stage"], 0) + 1
    return {
        "finished_at": time.time(),
        "total": len(rows),
        "by_stage": by_stage,
        "files": sizes,
        "rejected": rejected,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Датасет для файнтюна из своей базы. Сеть и модель не трогаются."
    )
    parser.add_argument("--out", default="data/train", help="куда сложить jsonl")
    parser.add_argument(
        "--cap", type=int, default=DEFAULT_CAP, help="потолок примеров на этап"
    )
    parser.add_argument(
        "--stages", default="", help="через запятую: {}".format(", ".join(STAGES))
    )
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    wanted = [s.strip() for s in args.stages.split(",") if s.strip()] or list(STAGES)
    unknown = [s for s in wanted if s not in SOURCES]
    if unknown:
        print("неизвестные этапы: {}. Есть: {}".format(", ".join(unknown), ", ".join(STAGES)))
        return 2

    conn = db.connect(Path(settings.get("DB_PATH", "data/fuckhr.sqlite3")))
    try:
        rows, rejected = build(conn, wanted, max(1, args.cap))
    finally:
        conn.close()

    out_dir = ROOT / args.out
    sizes = dataset_core.write(rows, out_dir)
    report = report_of(rows, rejected, sizes)
    (out_dir / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("примеров: {}".format(len(rows)))
    for stage, count in sorted(report["by_stage"].items()):
        print("  {:<12}{}".format(stage, count))
    print("файлы: {}".format(", ".join("{} — {}".format(k, v) for k, v in sizes.items())))
    if rejected:
        print("отбраковано: {}".format(sum(rejected.values())))
        for why, count in sorted(rejected.items()):
            print("  {} × {}".format(count, why))
    if not rows:
        print("Ни одного примера: похоже, в базе ещё нет условий и отчётов детектора.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
