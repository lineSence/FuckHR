"""Диагностический прогон: что он обещает.

Три обещания. Выключенная диагностика не пишет ничего и ничего не стоит
[CORE-017]. Включённая кладёт события в JSONL и умеет свернуть их в сводку.
И главное — в файл не попадают секреты: его смысл в том, чтобы отдать целиком
[CORE-012].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import diag


def test_выключенная_диагностика_молчит(tmp_path: Path) -> None:
    diag.event("вакансия", ключ="k")
    assert not diag.enabled()
    assert not list(tmp_path.glob("*.jsonl"))


def test_события_пишутся_и_сворачиваются_в_сводку(tmp_path: Path) -> None:
    diag.start("тест", directory=tmp_path)
    diag.event("запрос", площадка="hh.ru", текст="Ревизор", найдено=12)
    diag.event("вакансия", ключ="1", балл=61.0, отклонена=False)
    diag.event("вакансия", ключ="2", отклонена=True, почему="стоп-слово вахта в названии")
    diag.event("модель", этап="extract", исход="из кэша")
    path = diag.finish(вакансий_увидели=2)
    assert path is not None

    data = diag.summary(path)
    assert data["по видам"]["вакансия"] == 2
    assert data["отказы"] == {"стоп-слово вахта в названии": 1}
    assert data["баллы"]["максимум"] == 61.0
    assert data["модель"]["extract"] == {"из кэша": 1}
    assert data["итог"]["вакансий_увидели"] == 2
    assert not diag.enabled()


def test_секреты_в_файл_не_попадают(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-очень-секретно")
    monkeypatch.setenv("HH_COOKIE", "хвост сессии")
    monkeypatch.setenv("RUN_LIMIT", "30")
    diag.start("тест", directory=tmp_path)
    path = diag.finish()
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "очень-секретно" not in text
    assert "хвост сессии" not in text
    head = json.loads(text.splitlines()[0])
    assert head["env"]["LLM_API_KEY"] == "задано"
    assert head["env"]["RUN_LIMIT"] == "30"


def test_потолок_событий_не_даёт_файлу_расти(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diag, "MAX_EVENTS", 3)
    recorder = diag.start("тест", directory=tmp_path)
    for index in range(10):
        diag.event("вакансия", ключ=str(index))
    assert recorder.count == 3
    path = diag.finish()
    assert path is not None and path.read_text(encoding="utf-8").count("\n") == 3


def test_ошибки_кандидатов_видны_и_без_ключей(tmp_path: Path) -> None:
    """Неудачная попытка — это потраченный вызов, и он обязан быть в файле."""
    diag.start("тест", directory=tmp_path)
    diag.event(
        "модель",
        этап="dossier",
        исход="ошибка",
        маршрут="proxy",
        модель="groq/qwen",
        кандидат=1,
        попытка=2,
        статус=429,
        ошибка=diag.mask("429 from https://api.groq.com/v1?api_key=sk-abcdef123456"),
    )
    diag.event("модель", этап="dossier", исход="ответ", маршрут="proxy", модель="groq/qwen")
    path = diag.finish()
    assert path is not None

    body = path.read_text(encoding="utf-8")
    assert "sk-abcdef123456" not in body
    data = diag.summary(path)
    assert data["модель"]["dossier"] == {"ошибка": 1, "ответ": 1}
    assert list(data["сбои модели"])[0].startswith("proxy groq/qwen: 429")
