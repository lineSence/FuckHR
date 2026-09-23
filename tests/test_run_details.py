"""Карточки вакансий качаются пулом, но темп держит бакет hh.ru."""

from __future__ import annotations

import threading
import time

import net_rate
import run_details
from hh_html import BlockedError


class FakeClient:
    """Клиент, который честно ждёт очереди в бакете, как настоящий."""

    def __init__(self, bucket: net_rate.Bucket, blocked_on: str | None = None) -> None:
        self.bucket = bucket
        self.blocked_on = blocked_on
        self.threads: set[str] = set()
        self.asked: list[str] = []
        self._lock = threading.Lock()

    def vacancy(self, vacancy_id: str) -> dict[str, object]:
        self.bucket.take()
        with self._lock:
            self.asked.append(vacancy_id)
            self.threads.add(threading.current_thread().name)
        if self.blocked_on == vacancy_id:
            raise BlockedError("капча")
        return {"description": "описание {}".format(vacancy_id), "key_skills": []}


def _bucket(interval: float = 0.0) -> net_rate.Bucket:
    bucket = net_rate.Bucket(interval, interval)
    bucket.jitter = 0.0
    return bucket


def test_карточки_идут_параллельно_но_не_быстрее_бакета() -> None:
    bucket = _bucket(0.05)
    client = FakeClient(bucket)
    details = run_details.Details(client, workers=4)
    start = time.monotonic()
    for number in range(8):
        details.submit("hh:{}".format(number), str(number))
    got = [details.get("hh:{}".format(n)) for n in range(8)]
    spent = time.monotonic() - start
    details.close()

    assert all(item is not None for item in got)
    assert details.fetched == 8
    assert len(client.threads) > 1  # запросы действительно разошлись по потокам
    assert spent >= 7 * 0.05  # но темп к hh.ru прежний


def test_капча_останавливает_остальные_заказы() -> None:
    client = FakeClient(_bucket(), blocked_on="0")
    details = run_details.Details(client, workers=1)
    for number in range(5):
        details.submit("hh:{}".format(number), str(number))
    got = [details.get("hh:{}".format(n)) for n in range(5)]
    details.close()

    assert details.blocked is True
    assert got[0] is None
    assert len(client.asked) == 1  # после капчи в сеть больше не ходим


def test_потолок_потоков_не_превышается(monkeypatch) -> None:
    monkeypatch.setenv("HH_DETAIL_WORKERS", "99")
    assert run_details.detail_workers() == run_details.MAX_DETAIL_WORKERS
    monkeypatch.setenv("HH_DETAIL_WORKERS", "0")
    assert run_details.detail_workers() == 1
