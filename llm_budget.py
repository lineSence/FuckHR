"""Бюджет вызовов модели на прогон: один счётчик на все потоки.

Шлюз создаётся заново на каждую вакансию (`llm_batch`, `run_bg`: своё
соединение sqlite на поток), а счётчик вызовов и список выбывших моделей жили
внутри шлюза. Из-за этого `LLM_MAX_CALLS` ограничивал одну вакансию, а модель,
ответившая 429 или 404, воскресала на следующей — против ADR-022.

Теперь счётчик и выбывшие живут здесь, один объект на прогон, и раздаются
шлюзам снаружи. Взятие вызова атомарно: иначе четыре потока проходят проверку
потолка одновременно и вместе перебирают бюджет.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from llm_cascade import Dropped


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    failures: int = 0
    skipped: int = 0
    degraded: int = 0
    # Промахи кэша по причине: тот же вопрос другой моделью — и новый вопрос.
    miss_model: int = 0
    miss_new: int = 0


class Budget:
    """Потолок вызовов, счётчики и выбывшие кандидаты — общие на прогон."""

    def __init__(self, max_calls: int = 300) -> None:
        self.max_calls = int(max_calls)
        self.usage = Usage()
        self.dropped = Dropped()
        self._lock = threading.Lock()

    def take(self) -> bool:
        """Занять один вызов. False — бюджет исчерпан, звонить нельзя."""
        with self._lock:
            if self.usage.calls >= self.max_calls:
                return False
            self.usage.calls += 1
            return True

    @property
    def spent(self) -> bool:
        with self._lock:
            return self.usage.calls >= self.max_calls

    def note(self, field: str) -> None:
        """+1 к счётчику: `cached`, `failures`, `skipped`, `degraded`,
        `miss_model` или `miss_new`."""
        with self._lock:
            setattr(self.usage, field, getattr(self.usage, field) + 1)


__all__ = ("Budget", "Usage")
