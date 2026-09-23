"""Бюджет вызовов модели общий на прогон, а не на вакансию (ADR-022)."""

from __future__ import annotations

import threading

import llm
from llm_budget import Budget


def _gateway(budget: Budget, answers: list[str] | None = None) -> llm.Gateway:
    def transport(profile, messages, temperature):  # noqa: ANN001 — сигнатура шлюза
        return "ok"

    return llm.Gateway(base_url="http://local", transport=transport, budget=budget)


def test_потолок_считается_на_все_шлюзы_прогона():
    budget = Budget(max_calls=3)
    messages = [{"role": "user", "content": "привет"}]
    answers = []
    for _ in range(5):
        # Каждая вакансия открывает свой шлюз, как в llm_batch и run_bg.
        gateway = _gateway(budget)
        answers.append(gateway.complete("extract", messages))
    assert answers.count("ok") == 3
    assert answers.count(None) == 2
    assert budget.usage.calls == 3
    assert budget.usage.skipped == 2


def test_взятие_вызова_атомарно():
    budget = Budget(max_calls=50)
    taken = []

    def worker() -> None:
        for _ in range(100):
            taken.append(budget.take())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert taken.count(True) == 50
    assert budget.usage.calls == 50


def test_выбывшая_модель_не_воскресает_в_другом_шлюзе():
    budget = Budget(max_calls=10)
    first = _gateway(budget)
    first._dropped.add("proxy", "gpt-x", "429")
    second = _gateway(budget)
    assert ("proxy", "gpt-x") in second._dropped
    assert second._dropped.reason("proxy", "gpt-x") == "429"
