"""Датасет для дообучения Laya на этапах review_fake и ai_text.

    python laya_dataset.py                        # из разметки учителя в базе
    python laya_dataset.py --owner my.json        # плюс поправки владельца

Формат строки — как у `LocalLLaMA/typed-decisions`, на котором обучен
официальный ноутбук Laya: `state`, `questions`, `gold` строками JSON. Поэтому
ноутбук читает файл без правок в разборе (docs/laya-finetune.md).

Почему так:

- Вопросы — те же `laya_judge.QUESTIONS`, что зададут в рантайме, и каждый ещё
  раз с перевёрнутым порядком вариантов. Zero-shot чекпойнт отвечает по
  позиции; пара «прямой + перевёрнутый» учит отвечать по смыслу.
- Метки учителя мягкие (`TEACHER`), метки владельца жёсткие: модель-учитель
  ошибается, и уверенность ей копировать незачем.
- Отложенная выборка делится по компаниям, а не по отзывам: отзывы одной
  компании похожи, и случайное деление мерило бы запоминание.
  Поправки владельца идут только в отложенную: мерить надо на правде.
- Контакты маскируются (`dataset_export.scrub`) [CORE-013].

Ни одного вызова модели и ни одного запроса в сеть.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence

import judge_labels
import laya_judge
from dataset_export import scrub

ROOT = Path(__file__).resolve().parent
STAGES = ("review_fake", "ai_text")
TEACHER = 0.85            # вероятность «да» для метки учителя «да»
PER_COMPANY = 40          # чтобы одна компания не забила корпус
MIN_CHARS = 40            # короче — решать не по чему
TEST_SHARE = 5            # каждая пятая компания — в отложенную
FEW = 50                  # меньше положительных на этап — дообучение не взлетит


def target(verdict: bool, source: str) -> float:
    if source == "owner":
        return 1.0 if verdict else 0.0
    return TEACHER if verdict else 1.0 - TEACHER


def to_row(stage: str, text: str, yes: float, uid: str, source: str) -> dict:
    """Строка в формате typed-decisions: оба порядка вариантов, одинаковая правда."""
    question = laya_judge.QUESTIONS[stage]
    yes_key, no_key = laya_judge.YES, laya_judge.NO

    def gold(first: str, second: str) -> dict:
        return {
            "label": first if yes >= 0.5 else second,
            "probabilities": {first: round(yes, 4), second: round(1.0 - yes, 4)},
        }

    return {
        "id": uid,
        "workflow": stage,
        "source": source,
        "state": json.dumps({"text": scrub(text)[: laya_judge.MAX_CHARS]}, ensure_ascii=False),
        "questions": json.dumps(
            {laya_judge.KEY: question, laya_judge.SWAPPED: laya_judge.swapped(question)},
            ensure_ascii=False,
        ),
        # В перевёрнутом вопросе «да» под ключом B.
        "gold": json.dumps(
            {laya_judge.KEY: gold(yes_key, no_key), laya_judge.SWAPPED: gold(no_key, yes_key)},
            ensure_ascii=False,
        ),
    }


def is_test(company: str) -> bool:
    digest = hashlib.sha1((company or "-").encode("utf-8")).digest()
    return digest[0] % TEST_SHARE == 0


def import_owner(conn: sqlite3.Connection, path: Path, stage: str) -> int:
    """Файл в формате `laya_bench --cases` → метки владельца в базе."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    total = 0
    for item in raw:
        ad = {int(i) for i in item.get("ad", ())}
        labels = {str(text): index in ad for index, text in enumerate(item.get("texts", ()))}
        total += judge_labels.record(
            conn, str(item.get("stage") or stage), labels,
            company=str(item.get("company") or item.get("name") or ""), source="owner",
        )
    return total


def build(conn: sqlite3.Connection, stages: Sequence[str] = STAGES) -> tuple[list[dict], list[dict]]:
    """(обучающая, отложенная). Поправка владельца заменяет учителя по тому же тексту."""
    train: list[dict] = []
    test: list[dict] = []
    for stage in stages:
        best: dict[str, sqlite3.Row] = {}
        for row in judge_labels.rows(conn, stage):
            if len((row["text"] or "").strip()) < MIN_CHARS:
                continue
            if row["text_hash"] not in best or row["source"] == "owner":
                best[row["text_hash"]] = row
        per_company: Counter[str] = Counter()
        for digest, row in sorted(best.items(), key=lambda kv: (kv[1]["company"], kv[0])):
            owner = row["source"] == "owner"
            if not owner and per_company[row["company"]] >= PER_COMPANY:
                continue
            per_company[row["company"]] += 1
            item = to_row(
                stage, row["text"], target(bool(row["verdict"]), row["source"]),
                "{}:{}".format(stage, digest[:12]), row["source"],
            )
            item["company"] = row["company"]
            (test if owner or is_test(row["company"]) else train).append(item)
    return train, test


def bench_cases(rows: Iterable[dict]) -> list[dict]:
    """Отложенная выборка в формате `laya_bench --cases`: пачка на компанию и этап."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["workflow"], row["company"])].append(row)
    out = []
    for (stage, company), items in sorted(groups.items()):
        golds = [json.loads(i["gold"])[laya_judge.KEY]["label"] for i in items]
        out.append({
            "name": "{} {}".format(stage, company or "без компании"),
            "stage": stage,
            "texts": [json.loads(i["state"])["text"] for i in items],
            "ad": [n for n, label in enumerate(golds) if label == laya_judge.YES],
        })
    return out


def report_of(train: Sequence[dict], test: Sequence[dict]) -> dict:
    out: dict = {}
    for split, rows in (("train", train), ("test", test)):
        for row in rows:
            yes = json.loads(row["gold"])[laya_judge.KEY]["label"] == laya_judge.YES
            key = out.setdefault(row["workflow"], {}).setdefault(split, Counter())
            key["да" if yes else "нет"] += 1
            key[row["source"]] += 1
    return {stage: {split: dict(c) for split, c in parts.items()} for stage, parts in out.items()}


def write_jsonl(rows: Sequence[dict], path: Path) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    import db  # noqa: PLC0415 — тестам не нужен
    import settings  # noqa: PLC0415
    from dotenv import load_dotenv  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Датасет для дообучения Laya")
    parser.add_argument("--out", default="data/train/laya", help="куда сложить файлы")
    parser.add_argument("--owner", default="", help="поправки владельца в формате laya_bench --cases")
    parser.add_argument("--stages", default=",".join(STAGES), help="через запятую")
    args = parser.parse_args(argv)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if any(s not in STAGES for s in stages):
        print("этапы только: {}".format(", ".join(STAGES)))
        return 2

    load_dotenv()
    conn = db.connect(Path(settings.get("DB_PATH", "data/fuckhr.sqlite3")))
    try:
        if args.owner:
            print("меток владельца: {}".format(import_owner(conn, Path(args.owner), stages[0])))
        train, test = build(conn, stages)
    finally:
        conn.close()

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(train, out / "train.jsonl")
    write_jsonl(test, out / "test.jsonl")
    (out / "bench_cases.json").write_text(
        json.dumps(bench_cases(test), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = report_of(train, test)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("обучающих: {}, отложенных: {} → {}".format(len(train), len(test), out))
    for stage, parts in report.items():
        print("  {}: {}".format(stage, parts))
        yes = parts.get("train", {}).get("да", 0)
        if yes < FEW:
            print("  {}: положительных в обучающей {} < {} — копи разметку дальше".format(stage, yes, FEW))
    if not train and not test:
        print("Разметки нет: включи FAKE_REVIEW_LLM=1 и AI_TEXT_LLM=1 и собери досье.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
