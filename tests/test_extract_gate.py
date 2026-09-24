"""Гейт примет условий: что он обещает.

Обещаний три. Выключенный гейт не трогает ничего [CORE-017]. Включённый снимает
вакансию, только если в тексте нет ни одной приметы полей, которые ещё
спрашивают у модели. Поле, закрытое структурным полем источника, приметой не
считается: иначе слово «офис» в тексте с заполненным графиком возвращало бы в
модель всё подряд.
"""

from __future__ import annotations

import pytest

import extract_gate


class V:
    def __init__(self, description: str, schedule: str = "", skills: str = "[]") -> None:
        self.key = "k"
        self.description = description
        self.schedule = schedule
        self.skills = skills


def test_выключенный_гейт_пропускает_всё(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATE_EXTRACT_MARKERS", raising=False)
    items = [V("ищем питониста, пишите"), V("")]
    assert extract_gate.keep(items) == items


def test_текст_без_примет_не_доходит_до_модели(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATE_EXTRACT_MARKERS", "1")
    assert not extract_gate.wanted(V("Требуется сотрудник. Звоните, всё обсудим."))


def test_адрес_и_этапы_отбора_оставляют_вакансию(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATE_EXTRACT_MARKERS", "1")
    assert extract_gate.wanted(V("Офис у метро Автово, два собеседования."))


def test_поле_закрытое_источником_приметой_не_считается() -> None:
    vacancy = V("Работа удалённо, python", schedule="Удалённая работа", skills='["Python"]')
    assert extract_gate.covered_by_source(vacancy) == {"format", "stack"}
    assert not extract_gate.found_fields(vacancy.description, {"format", "stack"})
