"""Режим цикла: что проверяется.

Цикл — это место, где ошибка стоит дорого в буквальном смысле: лишний круг по
hh.ru приближает капчу, а невыполненная остановка гоняет сбор всю ночь. Поэтому
проверяются ровно четыре обещания: число кругов, мягкая остановка, остановка на
капче и то, что упавший круг не обрывает остальные [CORE-017].

Пауза везде нулевая: тест не должен ждать.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import run_loop
import settings
import ui_run


def test_циклы_повторяются_заданное_число_раз(tmp_path: Path) -> None:
    calls: list[int] = []
    code = run_loop.run_cycles(
        lambda: calls.append(1) or 0, cycles=3, pause=0, path=tmp_path / "stop.flag"
    )
    assert len(calls) == 3
    assert code == 0


def test_без_лимита_цикл_идёт_до_просьбы_остановиться(tmp_path: Path) -> None:
    flag = tmp_path / "stop.flag"
    calls: list[int] = []

    def once() -> int:
        calls.append(1)
        if len(calls) == 2:
            run_loop.request_stop(flag)
        return 0

    run_loop.run_cycles(once, cycles=0, pause=0, path=flag)
    assert len(calls) == 2
    # Флаг снимается за собой: иначе следующий запуск остановился бы на первом круге.
    assert not run_loop.stop_requested(flag)


def test_капча_обрывает_цикл(tmp_path: Path) -> None:
    calls: list[int] = []
    code = run_loop.run_cycles(
        lambda: calls.append(1) or run_loop.BLOCKED_CODE,
        cycles=5,
        pause=0,
        path=tmp_path / "stop.flag",
    )
    assert len(calls) == 1
    assert code == run_loop.BLOCKED_CODE


def test_упавший_цикл_не_обрывает_остальные(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # После падения пауза принудительно длинная: в тесте её укорачиваем.
    monkeypatch.setattr(run_loop, "FAIL_PAUSE", 0.0)
    calls: list[int] = []

    def once() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сеть отвалилась")
        return 0

    code = run_loop.run_cycles(once, cycles=2, pause=0, path=tmp_path / "stop.flag")
    assert len(calls) == 2
    assert code == 0


def test_настройки_цикла_читаются_из_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_LOOP_ENABLED", "1")
    monkeypatch.setenv("RUN_LOOP_CYCLES", "-3")
    monkeypatch.setenv("RUN_LOOP_PAUSE", "мусор")
    options = settings.loop_options()
    assert options.enabled
    # Отрицательное число циклов читается как «без ограничения», мусор в паузе —
    # как значение по умолчанию: падать из-за опечатки в форме незачем.
    assert options.cycles == 0
    assert options.pause == 300.0


def test_галочка_цикла_есть_на_странице_запуска(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_LOOP_ENABLED", "1")
    monkeypatch.setenv("RUN_LOOP_CYCLES", "4")
    html = ui_run.loop_form()
    assert 'action="/loop"' in html
    assert "checked" in html
    assert 'value="4"' in html


def test_форма_цикла_сохраняет_ключи_env(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[str, str] = {}
    monkeypatch.setattr(
        settings, "save", lambda updates: saved.update(updates) or list(updates)
    )
    ui_run.save_loop({"cycles": ["5"], "pause": ["10"]})
    assert saved == {
        "RUN_LOOP_ENABLED": "0",
        "RUN_LOOP_CYCLES": "5",
        "RUN_LOOP_PAUSE": "10",
    }
    ui_run.save_loop({"enabled": ["1"], "cycles": ["0"], "pause": ["0"]})
    assert saved["RUN_LOOP_ENABLED"] == "1"


def test_мягкая_остановка_не_убивает_процесс(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_loop, "STOP_PATH", tmp_path / "stop.flag")
    killed: list[int] = []
    monkeypatch.setattr("jobs.runner.stop", lambda job_id: killed.append(job_id))
    ui_run.stop(7, soft=True)
    assert killed == []
    assert run_loop.stop_requested(tmp_path / "stop.flag")
    ui_run.stop(7, soft=False)
    assert killed == [7]
