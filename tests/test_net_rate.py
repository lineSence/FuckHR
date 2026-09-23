"""Темп к источнику держит общий бакет, а не число потоков (docs/performance.md)."""

from __future__ import annotations

import threading
import time

import net_rate


def _bucket(interval: float) -> net_rate.Bucket:
    bucket = net_rate.Bucket(interval, interval)
    bucket.jitter = 0.0  # в тесте дрожание только мешает считать
    return bucket


def test_темп_не_растёт_от_числа_потоков() -> None:
    bucket = _bucket(0.05)
    start = time.monotonic()

    def worker() -> None:
        for _ in range(5):
            bucket.take()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    spent = time.monotonic() - start
    assert bucket.taken == 20
    # 20 разрешений по 0,05 с — не быстрее 0,95 с, сколько бы потоков ни ждало.
    assert spent >= 19 * 0.05


def test_очередь_общая_на_хост() -> None:
    net_rate.reset()
    first = net_rate.bucket("example.org", 2.0, 0.8)
    second = net_rate.bucket("example.org", 2.0, 0.8)
    assert first is second
    net_rate.reset()


def test_границы_меняются_только_при_смене_настроек() -> None:
    net_rate.reset()
    bucket = net_rate.bucket("example.org", 2.0, 0.8)
    bucket.ease()
    nudged = bucket.interval
    assert nudged < 2.0
    # Те же настройки: клиент, созданный посреди прогона, темп не сбрасывает.
    net_rate.bucket("example.org", 2.0, 0.8)
    assert bucket.interval == nudged
    # Другие настройки: начинаем с верхней границы, как после капчи.
    net_rate.bucket("example.org", 3.0, 1.0)
    assert bucket.interval == 3.0
    net_rate.reset()


def test_капча_возвращает_к_верхней_границе() -> None:
    bucket = _bucket(2.0)
    bucket.interval_min = 0.8
    for _ in range(10):
        bucket.ease()
    assert bucket.interval == 0.8
    bucket.back_off()
    assert bucket.interval == 2.0
