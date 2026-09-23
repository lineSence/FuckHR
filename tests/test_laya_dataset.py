"""Разметка учителя и датасет для Laya: без сети, без весов, на базе в памяти."""

from __future__ import annotations

import json
import sqlite3

import aitext_llm
import fake_llm
import judge_labels
import laya_dataset
import laya_judge
import reviewitems

ЖИВОЙ = "Работал год бэкендом, зарплату задерживали на неделю, тимлид Олег адекватный."
РЕКЛАМА = "Лучшая компания мечты! Дружный коллектив, печеньки и бесконечный рост каждый день!"


class Учитель:
    enabled = True

    def __init__(self, answer: dict) -> None:
        self.answer = answer

    def complete(self, stage, messages):  # noqa: ANN001 — как у шлюза
        return json.dumps(self.answer, ensure_ascii=False)


def test_учитель_пишет_и_да_и_нет_но_не_да_без_цитаты():
    items = tuple(
        reviewitems.ReviewItem(url="https://x/{}".format(i), index=i, body=t)
        for i, t in enumerate((ЖИВОЙ, РЕКЛАМА, РЕКЛАМА + " ещё"))
    )
    ответ = {"items": [
        {"id": 0, "verdict": "experience", "quote": ""},
        {"id": 1, "verdict": "ad", "quote": "Лучшая компания мечты"},
        {"id": 2, "verdict": "ad", "quote": "выдумано"},
    ]}
    seen: dict[str, bool] = {}
    assert fake_llm.ad_indexes(Учитель(ответ), items, force=True, seen=seen) == {1}
    assert seen == {ЖИВОЙ: False, РЕКЛАМА: True}


def test_ai_text_пишет_явное_нет():
    текст = "Мы ценим каждого сотрудника и создаём условия для профессионального роста. " * 5
    seen: dict[str, bool] = {}
    aitext_llm.generated_indexes(
        Учитель({"items": [{"id": 0, "verdict": "human", "quote": ""}]}), {0: текст},
        force=True, seen=seen,
    )
    assert seen == {текст[: aitext_llm.MAX_CHARS]: False}


def _база() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for n in range(12):
        компания = "компания {}".format(n)
        judge_labels.record(conn, "review_fake", {ЖИВОЙ + str(n): False, РЕКЛАМА + str(n): True}, компания)
    return conn


def test_строка_в_формате_typed_decisions_с_обоими_порядками():
    row = laya_dataset.to_row("review_fake", РЕКЛАМА, 0.85, "id", "llm")
    questions, gold = json.loads(row["questions"]), json.loads(row["gold"])
    assert set(questions) == {laya_judge.KEY, laya_judge.SWAPPED}
    assert questions[laya_judge.KEY] == laya_judge.QUESTIONS["review_fake"]
    assert gold[laya_judge.KEY] == {"label": "A", "probabilities": {"A": 0.85, "B": 0.15}}
    assert gold[laya_judge.SWAPPED]["label"] == "B"
    assert gold[laya_judge.SWAPPED]["probabilities"]["B"] == 0.85
    assert json.loads(row["state"]) == {"text": РЕКЛАМА}


def test_отложенная_делится_по_компаниям():
    train, test = laya_dataset.build(_база(), ("review_fake",))
    assert train and test
    assert not {r["company"] for r in train} & {r["company"] for r in test}


def test_поправка_владельца_заменяет_учителя_и_идёт_в_отложенную(tmp_path):
    conn = _база()
    path = tmp_path / "owner.json"
    path.write_text(json.dumps([{"name": "компания 1", "stage": "review_fake",
                                 "texts": [РЕКЛАМА + "1"], "ad": []}]), encoding="utf-8")
    assert laya_dataset.import_owner(conn, path, "review_fake") == 1
    train, test = laya_dataset.build(conn, ("review_fake",))
    владелец = [r for r in test if r["source"] == "owner"]
    assert len(владелец) == 1
    assert json.loads(владелец[0]["gold"])[laya_judge.KEY]["probabilities"]["A"] == 0.0
    assert all(json.loads(r["state"])["text"] != РЕКЛАМА + "1" for r in train)


def test_отложенная_читается_как_кейсы_бенчмарка():
    _, test = laya_dataset.build(_база(), ("review_fake",))
    cases = laya_dataset.bench_cases(test)
    assert cases and all(len(c["ad"]) == 1 and len(c["texts"]) == 2 for c in cases)
