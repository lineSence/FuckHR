"""Повтор прогона по кругу: сколько циклов, пауза и мягкая остановка.

Вынесено из run.py по [CORE-024]: сам прогон о цикле не знает, он умеет одно —
отработать один раз и вернуть код. Здесь только политика повтора.

Остановок две, и они разные по цене. Жёсткая — кнопка «Остановить» в
интерфейсе: процесс убивается сразу, текущий цикл теряет всё, что не успел
записать. Мягкая — файл-флаг data/stop.flag: текущий цикл доводится до конца,
карточки уходят, и только потом прогон завершается. Флаг, а не сигнал, потому
что интерфейс и прогон — разные процессы, а на Windows сигналов почти нет.

Ошибка одного цикла не обрывает остальные [CORE-017], но после неё пауза не
меньше FAIL_PAUSE: иначе сломанная сеть превращает цикл в бесконечный
молотящий круг.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("fuckhr")

STOP_PATH = Path("data/stop.flag")
# Шаг ожидания: пауза между циклами может быть длинной, а просьбу остановиться
# надо замечать быстрее, чем через полчаса.
STEP = 1.0
FAIL_PAUSE = 60.0
# Код 2 возвращает run.py, когда hh.ru закрылся капчей. Ходить туда ещё раз —
# верный способ получить бан подольше.
BLOCKED_CODE = 2


def _path(path: Path | None = None) -> Path:
    return Path(path or STOP_PATH)


def request_stop(path: Path | None = None) -> None:
    """Просит прогон остановиться после текущего цикла."""
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stop", encoding="utf-8")


def clear_stop(path: Path | None = None) -> None:
    """Убирает флаг. Вызывается в начале прогона: чужой старый флаг не должен
    останавливать сегодняшний запуск."""
    _path(path).unlink(missing_ok=True)


def stop_requested(path: Path | None = None) -> bool:
    return _path(path).exists()


def wait(seconds: float, path: Path | None = None) -> bool:
    """Пауза с проверкой флага. False — просили остановиться."""
    left = max(0.0, seconds)
    while left > 0:
        if stop_requested(path):
            return False
        step = min(STEP, left)
        time.sleep(step)
        left -= step
    return not stop_requested(path)


def run_cycles(
    once: Callable[[], int],
    cycles: int = 1,
    pause: float = 0.0,
    path: Path | None = None,
) -> int:
    """Гоняет once() по кругу. cycles=0 — пока не остановят вручную.

    Возвращает код последнего цикла: интерфейсу важно, чем всё кончилось, а не
    сколько раз до этого было хорошо.
    """
    total = max(0, cycles)
    clear_stop(path)
    code = 0
    number = 0
    while True:
        number += 1
        if total:
            log.info("цикл %s/%s", number, total)
        else:
            log.info("цикл %s, лимит не задан — до ручной остановки", number)
        failed = False
        try:
            code = once()
        except Exception:  # noqa: BLE001 — один плохой цикл не обрывает остальные
            log.exception("цикл %s упал, продолжаем со следующего", number)
            code = 1
            failed = True
        if code == BLOCKED_CODE:
            log.warning("hh.ru закрылся капчей — цикл останавливаем")
            break
        if stop_requested(path):
            log.info("остановка по просьбе владельца: сделано циклов %s", number)
            break
        if total and number >= total:
            log.info("циклы кончились: сделано %s из %s", number, total)
            break
        delay = max(pause, FAIL_PAUSE) if failed else pause
        if delay:
            log.info("пауза между циклами: %.0f с", delay)
        if not wait(delay, path):
            log.info("остановка по просьбе владельца: сделано циклов %s", number)
            break
    clear_stop(path)
    return code


__all__ = (
    "BLOCKED_CODE",
    "FAIL_PAUSE",
    "STOP_PATH",
    "clear_stop",
    "request_stop",
    "run_cycles",
    "stop_requested",
    "wait",
)
