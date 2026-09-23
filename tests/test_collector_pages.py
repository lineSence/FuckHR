"""Сбор без потолка страниц и уведомление, когда выдача кончилась."""

from __future__ import annotations

import logging

import collector
import settings
from hh import Vacancy


def vacancy(number: int) -> Vacancy:
    return Vacancy(
        external_id=str(number),
        title="Оператор 1С №{}".format(number),
        company="АКМЕ {}".format(number),
        url="https://hh.ru/vacancy/{}".format(number),
        description="1С, поддержка пользователей",
    )


class PagedClient:
    """Отдаёт страницы по 50 и уважает max_pages, как настоящий клиент."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.requested_pages = 0

    def search(self, max_pages: int = 0, **kwargs):
        page = 0
        while not max_pages or page < max_pages:
            chunk = [
                vacancy(n) for n in range(page * 50, min((page + 1) * 50, self.total))
            ]
            if not chunk:
                break
            self.requested_pages += 1
            yield from chunk
            page += 1


def profile(monkeypatch) -> object:
    import score

    return score.Profile(
        queries=[{"text": "оператор 1с"}],
        skills=["1с"],
        stop_words=[],
        areas=[113],
    )


def test_страницы_не_ограничены_потолком(monkeypatch) -> None:
    client = PagedClient(total=420)
    options = settings.PrefilterOptions(enabled=False, min_score=0.0, fuzzy=88)
    seen, passed = collector.collect(client, profile(monkeypatch), 1000, options)
    assert len(seen) == 420
    assert client.requested_pages == 9  # потолка в три страницы больше нет


def test_уведомление_когда_выдача_кончилась(monkeypatch, caplog) -> None:
    client = PagedClient(total=120)
    options = settings.PrefilterOptions(enabled=False, min_score=0.0, fuzzy=88)
    with caplog.at_level(logging.WARNING, logger="fuckhr"):
        _seen, passed = collector.collect(client, profile(monkeypatch), 1000, options)
    assert len(passed) == 120
    assert any("вакансии в выдаче кончились" in rec.message for rec in caplog.records)


def test_набрали_лимит_без_уведомления(monkeypatch, caplog) -> None:
    client = PagedClient(total=500)
    options = settings.PrefilterOptions(enabled=False, min_score=0.0, fuzzy=88)
    with caplog.at_level(logging.WARNING, logger="fuckhr"):
        _seen, passed = collector.collect(client, profile(monkeypatch), 100, options)
    assert len(passed) >= 100
    assert not any("кончились" in rec.message for rec in caplog.records)
def test_нулевой_лимит_означает_до_конца_выдачи(monkeypatch) -> None:
    # Лимит 0 читается как «без ограничения»: обход идёт, пока страницы не кончатся.
    client = PagedClient(total=137)
    options = settings.PrefilterOptions(enabled=False, min_score=0.0, fuzzy=88)
    seen, passed = collector.collect(client, profile(monkeypatch), 0, options)
    assert len(seen) == len(passed) == 137


def test_без_лимита_карточки_не_обнуляются() -> None:
    # RUN_LIMIT=0 не должен превращаться в «не отправлять ни одной карточки»:
    # в run.py на этот случай есть свой потолок.
    import run

    assert run.CARD_LIMIT > 0
