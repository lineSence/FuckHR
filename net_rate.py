"""Ограничитель темпа запросов к одному хосту: общий на процесс и на потоки.

Зачем. Сбор шёл строго последовательно: запрос → ответ → `sleep(pause)`.
Латентность hh.ru (0,3–1 с) и пауза складывались, хотя могли бы идти
одновременно. Бакет выдаёт разрешения с тем же средним темпом, но ждать в нём
могут несколько потоков сразу: пока один ждёт ответа, другой уже отсиживает
свою паузу. **Средний темп к источнику не растёт** — это условие, при котором
пул воркеров не повышает риск капчи [CORE-014].

Темп задаётся интервалом между запросами, а не «запросов в секунду»: настройки
проекта (`HH_PAUSE`, `HH_PAUSE_MIN`) всегда были паузами, и переводить их в
частоту значило бы спорить с тем, что владелец видит в форме.

Адаптивность прежняя: чистый ответ чуть сжимает интервал, 403/429/капча
возвращает его к верхней границе. Разница в том, что теперь это решение общее
для всех потоков хоста, а не для того, кому не повезло.
"""

from __future__ import annotations

import logging
import random
import threading
import time

log = logging.getLogger("fuckhr")

JITTER = 1.0  # дрожание сверх интервала, как в прежнем _sleep


class Bucket:
    """Разрешения на запрос к одному хосту с общим для потоков интервалом."""

    def __init__(self, interval: float, interval_min: float | None = None) -> None:
        self.interval_max = max(0.0, float(interval))
        self.interval_min = (
            self.interval_max
            if interval_min is None
            else max(0.0, min(float(interval_min), self.interval_max))
        )
        self.interval = self.interval_max
        self.jitter = JITTER
        self.taken = 0
        self._lock = threading.Lock()
        self._next = 0.0  # монотонное время, когда можно делать следующий запрос

    def take(self) -> float:
        """Ждёт своей очереди и возвращает, сколько пришлось ждать.

        Очередь считается под блокировкой, а спит поток уже без неё: иначе
        воркеры стояли бы друг за другом вместо того, чтобы ждать параллельно.
        """
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            wait = self.interval + random.uniform(0, self.jitter)
            self._next = start + wait
            self.taken += 1
            delay = start - now
        if delay > 0:
            time.sleep(delay)
        return delay

    def ease(self) -> None:
        """Ответ чистый — идём чуть быстрее, но не быстрее нижней границы."""
        with self._lock:
            self.interval = max(self.interval_min, self.interval * 0.85)

    def back_off(self) -> None:
        """Ответ подозрительный — сразу к верхней границе, без полумер."""
        with self._lock:
            self.interval = self.interval_max

    def reconfigure(self, interval: float, interval_min: float | None = None) -> None:
        """Новые границы темпа.

        Границы те же — не трогаем ничего: клиент, созданный посреди прогона
        (цели, догрузка карточек), не должен обнулять уже нащупанный темп.
        Границы другие — начинаем с верхней: новые настройки применяются
        осторожно, как после капчи.
        """
        top = max(0.0, float(interval))
        low = top if interval_min is None else max(0.0, min(float(interval_min), top))
        with self._lock:
            if (top, low) == (self.interval_max, self.interval_min):
                return
            self.interval_max = top
            self.interval_min = low
            self.interval = top

    def rate_per_minute(self) -> float:
        """Фактический потолок темпа при текущем интервале — для лога."""
        step = self.interval + self.jitter / 2
        return 60.0 / step if step > 0 else 0.0


_buckets: dict[str, Bucket] = {}
_registry_lock = threading.Lock()


def bucket(host: str, interval: float, interval_min: float | None = None) -> Bucket:
    """Бакет хоста, общий на процесс.

    Один на хост, а не на клиента: в режиме цикла соседние прогоны и цели со
    слежением создают свои клиенты, и каждый начинал бы темп с нуля.
    """
    with _registry_lock:
        found = _buckets.get(host)
        if found is None:
            found = Bucket(interval, interval_min)
            _buckets[host] = found
            log.info(
                "темп для %s: не чаще %.1f запросов в минуту",
                host,
                found.rate_per_minute(),
            )
            return found
    # Границы могли поменяться в настройках между прогонами цикла: бакет тот
    # же (очередь запросов общая), но темп берётся новый.
    found.reconfigure(interval, interval_min)
    return found


def host_of(url: str) -> str:
    """Хост из URL. Ключ бакета: темп считается на источник, а не на ссылку."""
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or url).lower()


def reset(host: str | None = None) -> None:
    """Забыть бакеты. Нужно тестам и смене настроек паузы на лету."""
    with _registry_lock:
        if host is None:
            _buckets.clear()
        else:
            _buckets.pop(host, None)


__all__ = ("Bucket", "bucket", "host_of", "reset")
