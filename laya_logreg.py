"""Дешёвая проверка: логрегрессия на векторах bge-m3 вместо второй модели.

Зачем. Дообученная Laya — ещё один энкодер в памяти рядом с Ollama. Векторы
bge-m3 в базе уже есть (`embeddings_tasks.review_pairs` считает их для поиска
перефразированных отзывов), а на готовом векторе линейная модель учится за
секунды и в рантайме стоит одно скалярное произведение. Если она на той же
отложенной выборке не хуже Laya — Laya не нужна [CORE-025].

Метки берутся из того же датасета, что и дообучение (`laya_dataset.py`), чтобы
сравнение было на тех же текстах и той же отложенной части: делить заново
случайно нельзя, там деление по компаниям.

Без numpy и sklearn: 1024 измерения на тысячу примеров — это полминуты чистого
Python, а новая зависимость ради одной свёртки не окупается (тот же довод, что
в `embeddings.py`). Обучение полнобатчевое, поэтому результат воспроизводим.

Модель вызывается только за векторами и только через шлюз [CORE-010]. Нет
эмбеддера — скрипт честно говорит, что считать негде, и ничего не выдумывает
[CORE-017].

Цифры прогона — этот прогон на этой выборке, а не измерение пайплайна
[CORE-019].
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

import embeddings_store as store
import fake_reviews
import laya_bench
import laya_judge

STAGES = ("review_fake", "ai_text")
OUT = Path("data/bench/laya_logreg.json")
THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)

log = logging.getLogger("fuckhr")


def samples(path: Path, stage: str) -> list[tuple[str, float]]:
    """(текст, вероятность «да») из jsonl датасета Laya для одного этапа."""
    out: list[tuple[str, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("workflow") != stage:
            continue
        text = str(json.loads(row["state"]).get("text") or "")
        gold = json.loads(row["gold"])[laya_judge.KEY]
        chances = gold.get("probabilities") or {}
        if laya_judge.YES in chances:
            yes = float(chances[laya_judge.YES])
        else:
            yes = 1.0 if gold.get("label") == laya_judge.YES else 0.0
        if text.strip():
            out.append((text, yes))
    return out


def sigmoid(value: float) -> float:
    if value < -30:
        return 0.0
    if value > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-value))


def train(
    rows: Sequence[tuple[Sequence[float], float]],
    epochs: int = 40,
    rate: float = 4.0,
    decay: float = 1e-4,
) -> tuple[list[float], float]:
    """Веса и смещение. Полный батч, L2, мягкие метки учителя как есть.

    Мягкая метка (0.85) в кросс-энтропии работает без переделок: цель — не
    класс, а вероятность, и модель не учится быть уверенной там, где учитель
    уверен не был.
    """
    if not rows:
        return [], 0.0
    size = len(rows[0][0])
    weights = [0.0] * size
    bias = 0.0
    scale = rate / len(rows)
    for _ in range(epochs):
        grad = [0.0] * size
        grad_bias = 0.0
        for vector, target in rows:
            error = sigmoid(sum(map(float.__mul__, weights, vector)) + bias) - target
            if error:
                for position, value in enumerate(vector):
                    grad[position] += error * value
                grad_bias += error
        for position in range(size):
            weights[position] -= scale * grad[position] + decay * weights[position]
        bias -= scale * grad_bias
    return weights, bias


def predict(weights: Sequence[float], bias: float, vector: Sequence[float]) -> float:
    if not weights or len(weights) != len(vector):
        return 0.0
    return sigmoid(sum(map(float.__mul__, weights, vector)) + bias)


def counts(scores: Sequence[float], labels: Sequence[int], limit: float) -> tuple[int, int]:
    """(ложных, пропущенных) при пороге: те же две колонки, что в laya_bench."""
    wrong = sum(1 for score, label in zip(scores, labels) if score >= limit and not label)
    missed = sum(1 for score, label in zip(scores, labels) if score < limit and label)
    return wrong, missed


def vectors_for(
    conn: sqlite3.Connection, gateway: object | None, texts: Sequence[str]
) -> dict[str, Sequence[float]]:
    """Векторы по хэшу текста. Считает только те, которых в базе ещё нет."""
    wanted = {fake_reviews.text_hash(text): text for text in texts}
    model = store.vectorize(conn, gateway, store.KIND_REVIEW, wanted)
    if not model:
        return {}
    known = store.load(conn, store.KIND_REVIEW, model)
    return {digest: known[digest] for digest in wanted if digest in known}


def prepare(
    pairs: Sequence[tuple[str, float]], known: dict[str, Sequence[float]]
) -> list[tuple[Sequence[float], float]]:
    rows = []
    for text, yes in pairs:
        vector = known.get(fake_reviews.text_hash(text))
        if vector:
            rows.append((vector, yes))
    return rows


def evaluate(stage: str, data: Path, known: dict[str, Sequence[float]], epochs: int) -> dict:
    """Учит на train.jsonl, мерит на test.jsonl. Пустые поля — нечем мерить."""
    train_rows = prepare(samples(data / "train.jsonl", stage), known)
    test_rows = prepare(samples(data / "test.jsonl", stage), known)
    out: dict = {"этап": stage, "обучающих": len(train_rows), "отложенных": len(test_rows)}
    if not train_rows or not test_rows:
        return out
    weights, bias = train(train_rows, epochs=epochs)
    scores = [predict(weights, bias, vector) for vector, _ in test_rows]
    labels = [1 if target >= 0.5 else 0 for _, target in test_rows]
    positive = [score for score, label in zip(scores, labels) if label]
    negative = [score for score, label in zip(scores, labels) if not label]
    out["положительных"] = len(positive)
    out["auroc"] = laya_bench.auroc(positive, negative)
    out["точность"] = round(
        sum(1 for score, label in zip(scores, labels) if (score >= 0.5) == bool(label))
        / len(labels),
        3,
    )
    out["пороги"] = {
        str(limit): dict(zip(("ложных", "пропущ"), counts(scores, labels, limit)))
        for limit in THRESHOLDS
    }
    return out


def render(report: Sequence[dict]) -> str:
    lines = [
        "{:<14}{:>8}{:>8}{:>7}{:>10}".format("этап", "обуч", "отлож", "AUROC", "точность")
    ]
    lines.append("-" * 47)
    for part in report:
        auc = part.get("auroc")
        lines.append(
            "{:<14}{:>8}{:>8}{:>7}{:>10}".format(
                part["этап"],
                part["обучающих"],
                part["отложенных"],
                "—" if auc is None else "{:.3f}".format(auc),
                part.get("точность", "—"),
            )
        )
    for part in report:
        limits = part.get("пороги")
        if not limits:
            continue
        lines.append("")
        lines.append("{}: порог → ложных / пропущено из {}".format(part["этап"], part["отложенных"]))
        lines.append(
            "  " + "  ".join(
                "{}: {}/{}".format(limit, cell["ложных"], cell["пропущ"])
                for limit, cell in limits.items()
            )
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import db  # noqa: PLC0415 — тестам не нужен
    import llm  # noqa: PLC0415
    import settings  # noqa: PLC0415
    from dotenv import find_dotenv, load_dotenv  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Логрегрессия на векторах против Laya")
    parser.add_argument("--data", default="data/train/laya", help="папка датасета Laya")
    parser.add_argument("--stages", default=",".join(STAGES), help="через запятую")
    parser.add_argument("--epochs", type=int, default=40, help="проходов обучения")
    parser.add_argument("--json", action="store_true", help="печатать отчёт как JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))
    data = Path(args.data)
    if not (data / "train.jsonl").exists():
        print("датасета нет: {}\nсобери его кнопкой в разделе «Laya» или "
              "python laya_dataset.py".format(data / "train.jsonl"))
        return 2
    stages = tuple(s.strip() for s in args.stages.split(",") if s.strip())

    conn = db.connect(Path(settings.get("DB_PATH", "data/fuckhr.sqlite3")))
    try:
        gateway = llm.Gateway.from_env(conn)
        texts = [
            text
            for stage in stages
            for part in ("train.jsonl", "test.jsonl")
            for text, _ in samples(data / part, stage)
        ]
        known = vectors_for(conn, gateway, texts)
        if not known:
            print(
                "векторов нет: нужен эмбеддер (LLM_LOCAL_STAGE_MODEL_EMBEDDINGS, "
                "обычно bge-m3) — без него сравнивать нечего."
            )
            return 1
        report = [evaluate(stage, data, known, args.epochs) for stage in stages]
    finally:
        conn.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    print("\nСравнивай AUROC с eval.json дообученной Laya: выборка та же.")
    print("Цифры — этот прогон на этой выборке, не измерение пайплайна [CORE-019].")
    print("\nотчёт: {}".format(OUT))
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа
    sys.exit(main())
