"""Решатель Laya: форма ответа проверяется, выключенность не ломает пайплайн."""

from __future__ import annotations

import laya_judge


class Agent:
    def __init__(self, answer: dict) -> None:
        self.answer = answer

    def predict(self, state, questions):  # noqa: ANN001 — форма ответа как у laya
        return {"answers": {next(iter(questions)): self.answer}}


def test_вероятность_берётся_из_распределения():
    agent = Agent({"choice": "B", "confidence": 0.9, "probabilities": {"A": 0.42, "B": 0.58}})
    assert laya_judge.probability("текст", "ai_text", agent=agent) == 0.42


def test_ответ_без_распределения_читается_по_уверенности():
    assert laya_judge.probability("текст", "ai_text", agent=Agent({"choice": "A", "confidence": 0.8})) == 0.8
    отрицание = laya_judge.probability("текст", "ai_text", agent=Agent({"choice": "B", "confidence": 0.8}))
    assert отрицание is not None and abs(отрицание - 0.2) < 1e-9


def test_чужая_форма_ответа_не_считается_решением():
    assert laya_judge.probability("текст", "ai_text", agent=Agent({"answer": "да"})) is None
    assert laya_judge.probability("текст", "неизвестный этап", agent=Agent({"noul": 0.9})) is None


def test_порог_отсекает_неуверенные(monkeypatch):
    monkeypatch.setenv("LAYA_THRESHOLD", "0.7")

    class ПоОчереди:
        def __init__(self) -> None:
            self.values = [0.9, 0.5]

        def predict(self, state, questions):  # noqa: ANN001
            return {"answers": {next(iter(questions)): {"noul": self.values.pop(0)}}}

    assert laya_judge.flagged(("а", "б"), "review_fake", agent=ПоОчереди()) == {0}


def test_падение_модели_не_роняет_этап():
    class Падучая:
        def predict(self, state, questions):  # noqa: ANN001
            raise RuntimeError("нет весов")

    assert laya_judge.probability("текст", "review_fake", agent=Падучая()) is None
    assert laya_judge.flagged(("а",), "review_fake", agent=Падучая()) == set()
