"""Сравнение Laya с текущей моделью: оценка считается без сети и без весов."""

from __future__ import annotations

import json

import laya_bench
import laya_judge


class FakeAgent:
    """Модель-заглушка: вероятность берётся из подготовленной карты."""

    def __init__(self, answers: dict[str, float]) -> None:
        self.answers = answers
        self.calls = 0

    def predict(self, state, questions):  # noqa: ANN001 — форма ответа как у laya
        self.calls += 1
        key = next(iter(questions))
        value = self.answers.get(state["text"][:20], 0.0)
        return {"answers": {key: {"choice": "A" if value >= 0.5 else "B",
                                  "confidence": value if value >= 0.5 else 1 - value}}}


def test_кейсы_проекта_разворачиваются_в_пачки():
    items = laya_bench.batches()
    assert items, "кейсы review_fake и ai_text должны найтись"
    assert {batch.stage for batch in items} == {"review_fake", "ai_text"}
    ловушки = [batch for batch in items if not batch.ad]
    assert ловушки, "нужны кейсы, где правильный ответ — молчание"


def test_ложное_срабатывание_дороже_пропуска():
    assert laya_bench.case_score({1}, frozenset({1})) == 1.0
    assert laya_bench.case_score(set(), frozenset({1})) == 0.5
    assert laya_bench.case_score({0, 1}, frozenset({1})) == 0.0


def test_итог_считает_ошибки_по_сторонам():
    items = (
        laya_bench.Batch("точный", "review_fake", ("а", "б"), frozenset({1})),
        laya_bench.Batch("ловушка", "review_fake", ("в",), frozenset()),
    )
    точная = laya_bench.measure("точная", lambda batch: set(batch.ad), items, "review_fake")
    шумная = laya_bench.measure("шумная", lambda batch: {0}, items, "review_fake")
    assert точная.score == 1.0 and точная.false_alarms == 0
    assert шумная.score == 0.0 and шумная.false_alarms == 2
    assert "точнее точная" in laya_bench.verdict([точная, шумная], "review_fake")


def test_упавшая_сторона_не_роняет_прогон():
    def падает(batch):
        raise RuntimeError("нет модели")

    items = (laya_bench.Batch("кейс", "ai_text", ("текст",), frozenset({0})),)
    row = laya_bench.measure("падучая", падает, items, "ai_text")
    assert row.cases == 1 and row.score == 0.5 and row.misses == 1


def test_сторона_laya_зовёт_модель_на_каждый_текст(monkeypatch):
    monkeypatch.delenv("LAYA_THRESHOLD", raising=False)
    agent = FakeAgent({"реклама": 0.9, "живой отзыв": 0.1})
    batch = laya_bench.Batch("кейс", "review_fake", ("реклама", "живой отзыв"), frozenset({0}))
    assert laya_bench.laya_side(agent)(batch) == {0}
    assert agent.calls == 2


def test_свои_кейсы_читаются_из_файла(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps([{"name": "мои отзывы", "texts": ["а", "б"], "ad": [1], "level": 3}]),
        encoding="utf-8",
    )
    items = laya_bench.batches_from_file(path, "review_fake")
    assert items[0].ad == frozenset({1}) and items[0].level == 3


def test_отчёт_пишется_рядом_с_отчётом_бенчмарка(tmp_path):
    row = laya_bench.Outcome("laya", "ai_text", 2, 0.75, 1, 0, 1.0, 0.03)
    path = laya_bench.save_report([row], tmp_path / "laya.json")
    assert json.loads(path.read_text(encoding="utf-8"))[0]["side"] == "laya"


def test_без_пакета_laya_решатель_молчит(monkeypatch):
    laya_judge.reset()
    monkeypatch.setitem(__import__("sys").modules, "laya", None)
    assert laya_judge.probability("текст", "ai_text") is None
    laya_judge.reset()


def test_перебор_порогов_зовёт_модель_один_раз():
    agent = FakeAgent({"реклама": 0.95, "живой отзыв": 0.6})
    items = (
        laya_bench.Batch(
            "кейс", "review_fake", ("реклама", "живой отзыв"), frozenset({0})
        ),
    )
    rows = laya_bench.sweep(agent, items, (0.5, 0.9), ("review_fake",))
    assert agent.calls == 2, "вероятности считаются один раз на все пороги"
    по_порогу = {row.side: row for row in rows}
    assert по_порогу["laya@0.5"].false_alarms == 1
    assert по_порогу["laya@0.9"].score == 1.0
