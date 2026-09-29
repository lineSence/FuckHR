"""План запросов к выдаче: один запрос обслуживает все профили сразу.

`profiles.collect_all` делал полный обход на профиль, и одинаковые
`(текст, регион, период)` качались заново. Кэш страниц (`hh_pages.PageCache`)
снимал только дословные повторы и только пока живёт; план убирает сам повтор:
запрос выполняется один раз, а его вакансии раздаются всем профилям, которые
его заказывали (docs/performance.md, пункт 3).

Здесь только группировка — сеть и предфильтр остаются в `collector`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger("fuckhr")


@dataclass
class Task:
    """Один запрос к выдаче и профили, которым он нужен."""

    text: str
    area: Any = None
    period: int = 7
    max_pages: int = 0
    extra: dict[str, Any] | None = None
    owners: list[Any] = field(default_factory=list)

    def kwargs(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "area": self.area,
            "period": self.period,
            "max_pages": self.max_pages,
            "extra": self.extra,
        }


def _key(text: str, area: Any, period: int, max_pages: int, extra: Any) -> str:
    """Одинаковые запросы обязаны совпасть ключом, разные — нет.

    Регион приводится к отсортированному списку: [1, 2] и [2, 1] — это один и
    тот же запрос, а json без сортировки дал бы два.
    """
    if isinstance(area, (list, tuple, set)):
        area_key: Any = sorted(str(a) for a in area)
    elif area is None:
        area_key = None
    else:
        area_key = str(area)
    return json.dumps(
        [text.strip().lower(), area_key, period, max_pages, extra or None],
        sort_keys=True,
        ensure_ascii=False,
    )


def build(bundle: Sequence[Any]) -> list[Task]:
    """Запросы всех профилей без повторов, в порядке первого появления."""
    plan: dict[str, Task] = {}
    for loaded in bundle:
        profile = loaded.profile
        for query in profile.queries:
            text = (query.get("text") or "").strip()
            if not text:
                continue
            area = query.get("area") or profile.areas or None
            period = int(query.get("period", 7))
            max_pages = int(query.get("max_pages") or 0)
            extra = query.get("extra")
            key = _key(text, area, period, max_pages, extra)
            task = plan.get(key)
            if task is None:
                task = Task(
                    text=text, area=area, period=period, max_pages=max_pages, extra=extra
                )
                plan[key] = task
            if loaded not in task.owners:
                task.owners.append(loaded)
    tasks = list(plan.values())
    total = sum(len([q for q in lo.profile.queries if q.get("text")]) for lo in bundle)
    if total > len(tasks):
        log.info(
            "план запросов: %s вместо %s — %s повторов между профилями не качаем",
            len(tasks),
            total,
            total - len(tasks),
        )
    return tasks


__all__ = ("Task", "build")
