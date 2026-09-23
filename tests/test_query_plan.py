"""Одинаковый запрос двух профилей качается один раз (docs/performance.md, п. 3)."""

from __future__ import annotations

import collector
import profiles
import query_plan
import score
import settings
from hh import Vacancy


def vacancy(number: int, title: str = "Оператор 1С") -> Vacancy:
    return Vacancy(
        external_id=str(number),
        title="{} №{}".format(title, number),
        company="АКМЕ {}".format(number),
        url="https://hh.ru/vacancy/{}".format(number),
        description="1С, поддержка пользователей",
    )


class CountingClient:
    """Считает, сколько раз запрашивали выдачу и с каким текстом."""

    def __init__(self, per_query: int = 3) -> None:
        self.queries: list[str] = []
        self.per_query = per_query

    def search(self, text: str, **kwargs):
        self.queries.append(text)
        start = len(self.queries) * 100
        for number in range(start, start + self.per_query):
            yield vacancy(number)


def loaded(profile_id: str, queries: list[dict], areas: list[int] | None = None) -> object:
    return profiles.Loaded(
        id=profile_id,
        profile=score.Profile(
            queries=queries, skills=["1с"], stop_words=[], areas=areas or [113]
        ),
    )


def test_повтор_между_профилями_не_качается_дважды() -> None:
    bundle = [
        loaded("первый", [{"text": "оператор 1с"}, {"text": "аналитик"}]),
        loaded("второй", [{"text": "Оператор 1С"}, {"text": "курьер"}]),
    ]
    tasks = query_plan.build(bundle)
    assert len(tasks) == 3  # «оператор 1с» у двух профилей — одна задача
    shared = next(task for task in tasks if task.text.lower() == "оператор 1с")
    assert [lo.id for lo in shared.owners] == ["первый", "второй"]


def test_регион_списком_не_плодит_задачи() -> None:
    bundle = [
        loaded("первый", [{"text": "оператор", "area": [1, 2]}]),
        loaded("второй", [{"text": "оператор", "area": [2, 1]}]),
    ]
    assert len(query_plan.build(bundle)) == 1


def test_сбор_по_плану_раздаёт_вакансии_всем_профилям() -> None:
    client = CountingClient()
    options = settings.PrefilterOptions(enabled=False, min_score=0.0, fuzzy=88)
    bundle = [
        loaded("первый", [{"text": "оператор 1с"}]),
        loaded("второй", [{"text": "оператор 1с"}]),
    ]
    seen, passed, owners = collector.collect_plan(client, bundle, 0, options)
    assert client.queries == ["оператор 1с"]  # один поход вместо двух
    assert len(seen) == len(passed) == 3
    assert all(sorted(ids) == ["второй", "первый"] for ids in owners.values())


def test_лимит_считается_на_профиль() -> None:
    client = CountingClient(per_query=10)
    options = settings.PrefilterOptions(enabled=False, min_score=0.0, fuzzy=88)
    bundle = [
        loaded("первый", [{"text": "оператор 1с"}]),
        loaded("второй", [{"text": "оператор 1с"}]),
    ]
    _seen, passed, owners = collector.collect_plan(client, bundle, 2, options)
    assert len(passed) == 2
    for ids in owners.values():
        assert sorted(ids) == ["второй", "первый"]


def test_профиль_со_своим_запросом_получает_свой_обход() -> None:
    client = CountingClient()
    options = settings.PrefilterOptions(enabled=False, min_score=0.0, fuzzy=88)
    bundle = [
        loaded("первый", [{"text": "оператор 1с"}]),
        loaded("второй", [{"text": "курьер"}]),
    ]
    _seen, _passed, owners = collector.collect_plan(client, bundle, 0, options)
    assert client.queries == ["оператор 1с", "курьер"]
    assert {tuple(ids) for ids in owners.values()} == {("первый",), ("второй",)}
