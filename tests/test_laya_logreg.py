"""Логрегрессия на векторах: разбор датасета, обучение, колонки отчёта."""

from __future__ import annotations

import json
from pathlib import Path

import laya_dataset
import laya_logreg


def write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )


def row(stage: str, text: str, yes: float, source: str = "llm") -> dict:
    item = laya_dataset.to_row(stage, text, yes, "{}:{}".format(stage, text[:8]), source)
    item["company"] = "ООО Ромашка"
    return item


def test_samples_reads_text_and_soft_label(tmp_path: Path) -> None:
    write(
        tmp_path / "train.jsonl",
        [
            row("review_fake", "лучший работодатель в городе, всё нравится", 0.85),
            row("review_fake", "зарплата вовремя, но переработки каждую неделю", 0.15),
            row("ai_text", "текст не про этот этап", 0.85),
        ],
    )
    pairs = laya_logreg.samples(tmp_path / "train.jsonl", "review_fake")
    assert [round(yes, 2) for _, yes in pairs] == [0.85, 0.15]
    assert "лучший работодатель" in pairs[0][0]


def test_samples_keeps_owner_hard_label(tmp_path: Path) -> None:
    write(tmp_path / "test.jsonl", [row("ai_text", "гладкий обобщённый абзац", 1.0, "owner")])
    assert laya_logreg.samples(tmp_path / "test.jsonl", "ai_text") == [
        ("гладкий обобщённый абзац", 1.0)
    ]


def test_train_separates_linearly_separable() -> None:
    rows = [([1.0, 0.0], 1.0)] * 20 + [([0.0, 1.0], 0.0)] * 20
    weights, bias = laya_logreg.train(rows, epochs=200)
    assert laya_logreg.predict(weights, bias, [1.0, 0.0]) > 0.7
    assert laya_logreg.predict(weights, bias, [0.0, 1.0]) < 0.3


def test_train_on_empty_rows_returns_nothing() -> None:
    assert laya_logreg.train([]) == ([], 0.0)
    assert laya_logreg.predict([], 0.0, [1.0]) == 0.0


def test_predict_ignores_wrong_dimension() -> None:
    assert laya_logreg.predict([1.0, 1.0], 0.0, [1.0]) == 0.0


def test_counts_splits_false_and_missed() -> None:
    scores = [0.9, 0.8, 0.2, 0.1]
    labels = [1, 0, 1, 0]
    assert laya_logreg.counts(scores, labels, 0.5) == (1, 1)
    assert laya_logreg.counts(scores, labels, 0.95) == (0, 2)


def test_evaluate_reports_auroc_and_thresholds(tmp_path: Path) -> None:
    ads = ["реклама номер {}".format(n) for n in range(8)]
    live = ["живой отзыв номер {}".format(n) for n in range(8)]
    write(
        tmp_path / "train.jsonl",
        [row("review_fake", text, 0.85) for text in ads]
        + [row("review_fake", text, 0.15) for text in live],
    )
    write(
        tmp_path / "test.jsonl",
        [row("review_fake", text + " ещё", 0.85) for text in ads]
        + [row("review_fake", text + " ещё", 0.15) for text in live],
    )
    import fake_reviews

    known = {}
    for text in ads:
        for variant in (text, text + " ещё"):
            known[fake_reviews.text_hash(variant)] = [1.0, 0.0]
    for text in live:
        for variant in (text, text + " ещё"):
            known[fake_reviews.text_hash(variant)] = [0.0, 1.0]

    part = laya_logreg.evaluate("review_fake", tmp_path, known, epochs=120)
    assert part["обучающих"] == 16 and part["отложенных"] == 16
    assert part["auroc"] == 1.0
    assert part["пороги"]["0.5"] == {"ложных": 0, "пропущ": 0}
    assert "review_fake" in laya_logreg.render([part])


def test_evaluate_without_vectors_says_nothing_to_measure(tmp_path: Path) -> None:
    write(tmp_path / "train.jsonl", [row("review_fake", "текст без вектора", 0.85)])
    write(tmp_path / "test.jsonl", [])
    part = laya_logreg.evaluate("review_fake", tmp_path, {}, epochs=5)
    assert part["обучающих"] == 0 and "auroc" not in part
    assert "—" in laya_logreg.render([part])
