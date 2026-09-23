"""Обучение линейного гейта на накопленной разметке. Ни одной генерации.

Данные — таблица `judge_labels`: полный текст, который видел учитель, и его
вердикт, плюс поправки владельца (источник `owner`). Признаки — векторы bge-m3
из общего кэша `embeddings`. Результат — веса в `gate_models`, по строке на
этап и модель векторов.

Почему деление по компаниям, а не случайное. Отзывы одной компании похожи;
случайное деление мерило бы, как хорошо модель запомнила конкретного
работодателя. Компании с `sha1(имя) % 5 == 0` целиком уходят в отложенную
часть, поправки владельца — тоже: мерить надо на правде.

Почему без numpy и sklearn. Тысяча примеров на 1024 измерения — это секунды
чистого Python, а новая зависимость ради одной свёртки не окупается (тот же
довод, что в `embeddings.py`) [CORE-025]. Обучение полнобатчевое, поэтому
результат воспроизводим.

Мягкая метка учителя (0.85) уходит в кросс-энтропию как есть: цель — не класс,
а вероятность, и модель не учится быть уверенной там, где учитель уверен не был.

Пороги подбираются по отложенной части и пишутся рядом с весами: `high` — самый
низкий порог без ложных срабатываний, `low` — самый высокий порог, ниже
которого не осталось ни одного положительного. Цифры прогона — этот прогон на
этой разметке, не измерение пайплайна [CORE-019].

Текст никуда не выгружается: он идёт только в локальный эмбеддер, а в базе
остаются веса, по которым текст не восстановить [CORE-012], [CORE-013].
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import judge_labels
import review_gate
import review_gate_store

STAGES = review_gate.STAGES
TEACHER = 0.85            # вероятность «да» для метки учителя «да»
PER_COMPANY = 40          # чтобы одна компания не забила корпус
MIN_CHARS = 40            # короче — решать не по чему
TEST_SHARE = 5            # каждая пятая компания — в отложенную
FEW = 50                  # меньше положительных на этап — мерить нечего
GRID = tuple(round(0.05 * step, 2) for step in range(1, 20))
OUT = Path("data/bench/review_gate.json")

log = logging.getLogger("fuckhr")


@dataclass(frozen=True)
class Sample:
    text: str
    yes: float
    company: str
    owner: bool


def target(verdict: bool, source: str) -> float:
    if source == "owner":
        return 1.0 if verdict else 0.0
    return TEACHER if verdict else 1.0 - TEACHER


def is_test(company: str) -> bool:
    digest = hashlib.sha1((company or "-").encode("utf-8")).digest()
    return digest[0] % TEST_SHARE == 0


def dataset(conn: sqlite3.Connection, stage: str) -> tuple[list[Sample], list[Sample]]:
    """(обучающая, отложенная). Поправка владельца заменяет учителя по тексту."""
    best: dict[str, sqlite3.Row] = {}
    for row in judge_labels.rows(conn, stage):
        if len((row["text"] or "").strip()) < MIN_CHARS:
            continue
        if row["text_hash"] not in best or row["source"] == "owner":
            best[row["text_hash"]] = row
    train: list[Sample] = []
    test: list[Sample] = []
    per_company: dict[str, int] = {}
    for digest, row in sorted(best.items(), key=lambda kv: (kv[1]["company"], kv[0])):
        owner = row["source"] == "owner"
        company = row["company"] or ""
        if not owner and per_company.get(company, 0) >= PER_COMPANY:
            continue
        per_company[company] = per_company.get(company, 0) + 1
        sample = Sample(
            text=row["text"],
            yes=target(bool(row["verdict"]), row["source"]),
            company=company,
            owner=owner,
        )
        (test if owner or is_test(company) else train).append(sample)
    return train, test


def import_owner(conn: sqlite3.Connection, path: Path, stage: str) -> int:
    """Файл `[{"stage": ..., "texts": [...], "ad": [0]}]` → поправки владельца."""
    total = 0
    for item in json.loads(path.read_text(encoding="utf-8")):
        ad = {int(index) for index in item.get("ad", ())}
        labels = {
            str(text): index in ad for index, text in enumerate(item.get("texts", ()))
        }
        total += judge_labels.record(
            conn,
            str(item.get("stage") or stage),
            labels,
            company=str(item.get("company") or item.get("name") or ""),
            source="owner",
        )
    return total


def train(
    rows: Sequence[tuple[Sequence[float], float]],
    epochs: int = 40,
    rate: float = 4.0,
    decay: float = 1e-4,
) -> tuple[list[float], float]:
    """Веса и смещение. Полный батч, L2, мягкие метки как есть."""
    if not rows:
        return [], 0.0
    size = len(rows[0][0])
    weights = [0.0] * size
    bias = 0.0
    scale = rate / len(rows)
    for _ in range(epochs):
        grad = [0.0] * size
        grad_bias = 0.0
        for vector, goal in rows:
            guess = review_gate.sigmoid(sum(map(float.__mul__, weights, vector)) + bias)
            error = guess - goal
            if error:
                for position, value in enumerate(vector):
                    grad[position] += error * value
                grad_bias += error
        for position in range(size):
            weights[position] -= scale * grad[position] + decay * weights[position]
        bias -= scale * grad_bias
    return weights, bias


def choose(scores: Sequence[float], labels: Sequence[int]) -> tuple[float | None, float | None]:
    """(low, high) по отложенной части: «нет» без пропусков, «да» без ложных.

    На хорошо разделимой выборке верхняя граница «нет» уезжает выше нижней
    границы «да» — тогда середины нет вовсе, и `low` прижимается к `high`:
    перевёрнутые пороги не гейт, а решето (`review_gate.GateOptions.usable`).
    """
    highs = [limit for limit in GRID if review_gate.counts(scores, labels, limit)[0] == 0]
    lows = [limit for limit in GRID if review_gate.counts(scores, labels, limit)[1] == 0]
    high = min(highs) if highs else None
    low = max(lows) if lows else None
    if low is not None and high is not None:
        low = min(low, high)
    return low, high


def prepare(
    samples: Sequence[Sample], known: dict[str, Sequence[float]]
) -> list[tuple[Sequence[float], float]]:
    from fake_reviews import text_hash  # noqa: PLC0415 — только за хэшем

    rows = []
    for sample in samples:
        vector = known.get(text_hash(sample.text))
        if vector:
            rows.append((vector, sample.yes))
    return rows


def evaluate(
    conn: sqlite3.Connection,
    gateway: object | None,
    stage: str,
    model: str,
    epochs: int,
    save: bool = True,
) -> dict:
    """Учит на обучающей части, мерит на отложенной, пишет веса."""
    train_set, test_set = dataset(conn, stage)
    out: dict = {"этап": stage, "модель": model}
    texts = {sample.text for sample in list(train_set) + list(test_set)}
    from fake_reviews import text_hash  # noqa: PLC0415

    _, known = review_gate.vectors_for(
        conn, gateway, {text_hash(text): text for text in texts}
    )
    train_rows = prepare(train_set, known)
    test_rows = prepare(test_set, known)
    out["обучающих"] = len(train_rows)
    out["отложенных"] = len(test_rows)
    if not train_rows:
        out["итог"] = "нет обучающих примеров с векторами"
        return out
    weights, bias = train(train_rows, epochs=epochs)
    if not test_rows:
        out["итог"] = "отложенная часть пуста: мерить нечем, веса не записаны"
        return out
    scores = [review_gate.predict(weights, bias, vector) for vector, _ in test_rows]
    labels = [1 if goal >= 0.5 else 0 for _, goal in test_rows]
    positives = sum(labels)
    low, high = choose(scores, labels)
    out.update(
        {
            "положительных": positives,
            "auroc": review_gate.auroc(
                [s for s, label in zip(scores, labels) if label],
                [s for s, label in zip(scores, labels) if not label],
            ),
            "точность": round(
                sum(1 for s, label in zip(scores, labels) if (s >= 0.5) == bool(label))
                / len(labels),
                3,
            ),
            "low": low,
            "high": high,
            "пороги": {
                str(limit): dict(
                    zip(("ложных", "пропущ"), review_gate.counts(scores, labels, limit))
                )
                for limit in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
            },
        }
    )
    if positives < FEW:
        out["итог"] = "положительных меньше {}: замер ни о чём не говорит".format(FEW)
    if save:
        review_gate_store.save(
            conn, stage, model, weights, bias,
            rows=len(train_rows), positives=positives,
            auroc=out["auroc"], accuracy=out["точность"], low=low, high=high,
        )
    return out


def render(report: Sequence[dict]) -> str:
    head = "{:<14}{:>7}{:>7}{:>6}{:>7}{:>9}{:>12}".format(
        "этап", "обуч", "отлож", "полож", "AUROC", "точность", "порог да/нет"
    )
    lines = [head, "-" * len(head)]
    for part in report:
        auc = part.get("auroc")
        lines.append(
            "{:<14}{:>7}{:>7}{:>6}{:>7}{:>9}{:>12}".format(
                part["этап"],
                part.get("обучающих", 0),
                part.get("отложенных", 0),
                part.get("положительных", 0),
                "—" if auc is None else "{:.3f}".format(auc),
                part.get("точность", "—"),
                "{} / {}".format(part.get("high", "—"), part.get("low", "—")),
            )
        )
    for part in report:
        if part.get("итог"):
            lines.append("")
            lines.append("{}: {}".format(part["этап"], part["итог"]))
        limits = part.get("пороги")
        if not limits:
            continue
        lines.append("")
        lines.append(
            "{}: порог → ложных / пропущено из {}".format(part["этап"], part["отложенных"])
        )
        lines.append(
            "  "
            + "  ".join(
                "{}: {}/{}".format(limit, cell["ложных"], cell["пропущ"])
                for limit, cell in limits.items()
            )
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import db  # noqa: PLC0415 — тестам не нужен
    import llm  # noqa: PLC0415
    import llm_embed  # noqa: PLC0415
    import settings  # noqa: PLC0415
    from dotenv import find_dotenv, load_dotenv  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Обучение гейта отзывов")
    parser.add_argument("--stages", default=",".join(STAGES), help="через запятую")
    parser.add_argument("--epochs", type=int, default=40, help="проходов обучения")
    parser.add_argument("--owner", default="", help="файл с поправками владельца")
    parser.add_argument("--dry-run", action="store_true", help="померить, но не писать веса")
    parser.add_argument("--json", action="store_true", help="печатать отчёт как JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))
    stages = tuple(stage.strip() for stage in args.stages.split(",") if stage.strip())
    unknown = [stage for stage in stages if stage not in STAGES]
    if unknown:
        print("неизвестные этапы: {}".format(", ".join(unknown)))
        return 2

    conn = db.connect(Path(settings.get("DB_PATH", "data/fuckhr.sqlite3")))
    try:
        if args.owner:
            path = Path(args.owner)
            if not path.exists():
                print("файла с поправками нет: {}".format(path))
                return 2
            print("поправок владельца записано: {}".format(
                import_owner(conn, path, stages[0])
            ))
        gateway = llm.Gateway.from_env(conn)
        model = llm_embed.model_name(gateway)
        if not model:
            print(
                "эмбеддера нет: задай LLM_LOCAL_STAGE_MODEL_EMBEDDINGS (обычно bge-m3).\n"
                "Без векторов гейт обучать не на чем."
            )
            return 1
        report = [
            evaluate(conn, gateway, stage, model, args.epochs, save=not args.dry_run)
            for stage in stages
        ]
    finally:
        conn.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    print("\nЦифры — этот прогон на этой разметке, не измерение пайплайна [CORE-019].")
    if args.dry_run:
        print("Веса не записаны: --dry-run.")
    print("\nотчёт: {}".format(OUT))
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа
    sys.exit(main())
