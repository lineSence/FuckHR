"""Обучение гейта пустоты на результатах самого пайплайна. Ни одной генерации.

Разметка здесь ничья и бесплатная: пайплайн уже знает, чем этап кончился на
каждой вакансии, где отрабатывал (`stage_gates.outcomes`). «Да» — этап
что-то дал, «нет» — отработал и вернул пусто. Признаки — векторы вакансий,
которые считаются на каждом прогоне (`embeddings_tasks.index_vacancies`).

Зачем это вместо гейта по соседям. Тому нужно k вакансий ближе 0.85 и у всех
пусто; на живой базе такая теснота — редкость, и гейт почти не срабатывает.
Регрессия смотрит на всю выборку сразу и даёт вероятность, к которой можно
подобрать порог по замеру [CORE-019].

Работает только сторона «нет»: пропустить этап можно, а вынести за него
вердикт нельзя — `hr_filter` обязан вернуть дословную цитату из текста.
Поэтому в замере важна одна колонка — «пропущ»: это вакансии, на которых этап
что-то нашёл бы, а гейт их снял. При пороге `low` их ноль по построению.

Деление отложенной части по работодателям: вакансии одной компании написаны
одним HR и похожи, случайное деление мерило бы запоминание.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

import embeddings_store as store
import linear_model
import review_gate_store
import stage_gates

STAGES = ("hr_filter", "extract")
TEST_SHARE = 5
FEW = 50
OUT = Path("data/bench/stage_gate.json")

log = logging.getLogger("fuckhr")


def is_test(company: str) -> bool:
    digest = hashlib.sha1((company or "-").encode("utf-8")).digest()
    return digest[0] % TEST_SHARE == 0


def companies(conn: sqlite3.Connection) -> dict[str, str]:
    """{ключ вакансии: работодатель}. Без компании вакансия делится сама по ключу."""
    try:
        rows = conn.execute("SELECT key, company FROM vacancies").fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]): str(row[1] or "") for row in rows}


def dataset(
    conn: sqlite3.Connection, stage: str, model: str
) -> tuple[list[tuple[Sequence[float], float]], list[tuple[Sequence[float], float]], int]:
    """(обучающая, отложенная, сколько ключей без вектора)."""
    labels = stage_gates.outcomes(conn, stage)
    vectors = store.load(conn, store.KIND_VACANCY, model)
    owners = companies(conn)
    train: list[tuple[Sequence[float], float]] = []
    test: list[tuple[Sequence[float], float]] = []
    missing = 0
    for key, productive in sorted(labels.items()):
        vector = vectors.get(key)
        if not vector:
            missing += 1
            continue
        row = (vector, 1.0 if productive else 0.0)
        (test if is_test(owners.get(key, key)) else train).append(row)
    return train, test, missing


def evaluate(
    conn: sqlite3.Connection, stage: str, model: str, epochs: int, save: bool = True
) -> dict:
    """Учит, мерит на отложенной части и пишет веса рядом с гейтом отзывов."""
    train_rows, test_rows, missing = dataset(conn, stage, model)
    out: dict = {
        "этап": stage,
        "модель": model,
        "обучающих": len(train_rows),
        "отложенных": len(test_rows),
        "без_вектора": missing,
    }
    if not train_rows:
        out["итог"] = "нет вакансий, где этап отрабатывал и есть вектор"
        return out
    weights, bias = linear_model.train(train_rows, epochs=epochs)
    if not test_rows:
        out["итог"] = "отложенная часть пуста: мерить нечем, веса не записаны"
        return out
    scores = [linear_model.predict(weights, bias, vector) for vector, _ in test_rows]
    labels = [1 if goal >= 0.5 else 0 for _, goal in test_rows]
    positives = sum(labels)
    low, high = linear_model.choose(scores, labels)
    out.update(
        {
            "продуктивных": positives,
            "auroc": linear_model.auroc(
                [s for s, label in zip(scores, labels) if label],
                [s for s, label in zip(scores, labels) if not label],
            ),
            "low": low,
            "экономия": skipped(scores, labels, low),
            "пороги": {
                str(limit): dict(
                    zip(("ложных", "пропущ"), linear_model.counts(scores, labels, limit))
                )
                for limit in (0.1, 0.2, 0.3, 0.4, 0.5)
            },
        }
    )
    if positives < FEW or positives == len(labels):
        out["итог"] = (
            "продуктивных {} из {}: замер ни о чём не говорит, нужен прогон "
            "с включённым этапом".format(positives, len(labels))
        )
    if save:
        review_gate_store.save(
            conn, stage, model, weights, bias,
            rows=len(train_rows), positives=positives,
            auroc=out["auroc"], low=low, high=high,
        )
    return out


def skipped(scores: Sequence[float], labels: Sequence[int], low: float | None) -> dict:
    """Сколько вакансий гейт снял бы с этапа и сколько из них зря."""
    if low is None or not labels:
        return {}
    dropped = [label for value, label in zip(scores, labels) if value <= low]
    return {
        "снято": len(dropped),
        "доля": round(len(dropped) / len(labels), 3),
        "потеряно_продуктивных": sum(dropped),
    }


def render(report: Sequence[dict]) -> str:
    head = "{:<12}{:>7}{:>7}{:>7}{:>7}{:>8}".format(
        "этап", "обуч", "отлож", "продукт", "AUROC", "порог"
    )
    lines = [head, "-" * len(head)]
    for part in report:
        auc = part.get("auroc")
        lines.append(
            "{:<12}{:>7}{:>7}{:>7}{:>7}{:>8}".format(
                part["этап"],
                part.get("обучающих", 0),
                part.get("отложенных", 0),
                part.get("продуктивных", 0),
                "—" if auc is None else "{:.3f}".format(auc),
                part.get("low") if part.get("low") is not None else "—",
            )
        )
    for part in report:
        экономия = part.get("экономия")
        if экономия:
            lines.append("")
            lines.append(
                "{}: гейт снял бы {} из {} ({:.0%}), продуктивных потеряно {}".format(
                    part["этап"],
                    экономия["снято"],
                    part.get("отложенных", 0),
                    экономия["доля"],
                    экономия["потеряно_продуктивных"],
                )
            )
        if part.get("без_вектора"):
            lines.append(
                "{}: без вектора {} вакансий — посчитаются на следующем прогоне".format(
                    part["этап"], part["без_вектора"]
                )
            )
        if part.get("итог"):
            lines.append("{}: {}".format(part["этап"], part["итог"]))
        limits = part.get("пороги")
        if not limits:
            continue
        lines.append(
            "{}: порог → ложных / потеряно из {}".format(part["этап"], part["отложенных"])
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

    parser = argparse.ArgumentParser(description="Обучение гейта пустоты этапов")
    parser.add_argument("--stages", default="hr_filter", help="через запятую")
    parser.add_argument("--epochs", type=int, default=40, help="проходов обучения")
    parser.add_argument("--dry-run", action="store_true", help="померить, но не писать веса")
    parser.add_argument("--json", action="store_true", help="печатать отчёт как JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(find_dotenv(usecwd=True))
    stages = tuple(stage.strip() for stage in args.stages.split(",") if stage.strip())
    unknown = [stage for stage in stages if stage not in STAGES]
    if unknown:
        print("неизвестные этапы: {} (знаем {})".format(
            ", ".join(unknown), ", ".join(STAGES)
        ))
        return 2

    conn = db.connect(Path(settings.get("DB_PATH", "data/fuckhr.sqlite3")))
    try:
        gateway = llm.Gateway.from_env(conn)
        model = llm_embed.model_name(gateway)
        if not model:
            print(
                "эмбеддера нет: задай LLM_LOCAL_STAGE_MODEL_EMBEDDINGS (обычно bge-m3).\n"
                "Гейт работает на векторах вакансий, без них обучать не на чем."
            )
            return 1
        report = [
            evaluate(conn, stage, model, args.epochs, save=not args.dry_run)
            for stage in stages
        ]
    finally:
        conn.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    print("\nВключается настройкой «Обученный гейт пустоты» (GATE_LEARNED).")
    print("Цифры — этот прогон на этой базе, не измерение пайплайна [CORE-019].")
    if args.dry_run:
        print("Веса не записаны: --dry-run.")
    print("\nотчёт: {}".format(OUT))
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа
    sys.exit(main())
