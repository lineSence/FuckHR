"""Сбор вакансий с выдачи и предфильтр.

Выделено из run.py по [CORE-024]; `run.collect` продолжает работать через
реэкспорт. Здесь заканчивается всё, что зависит от площадки: дальше прогон
работает с уже собранными объектами.
"""

from __future__ import annotations

import logging

import settings
from hh import Vacancy
from hh_html import HHHtmlClient
from score import Profile, evaluate

log = logging.getLogger("fuckhr")


def collect(
    client: HHHtmlClient,
    profile: Profile,
    limit: int = 0,
    prefilter: settings.PrefilterOptions | None = None,
) -> tuple[dict[str, Vacancy], dict[str, Vacancy]]:
    """Собирает вакансии по запросам профиля, но не больше limit штук.

    Возвращает две карты: всё увиденное и то, что прошло предфильтр. Первая нужна
    истории: «вакансия видна в выдаче» — факт о рынке, независимый от нашего интереса.

    limit останавливает обход сразу как только набралось нужное число: генератор
    поиска бросается недочитанным, и остальные страницы не запрашиваются. Каждая
    незапрошенная страница — это сэкономленные две-три секунды паузы и шаг от капчи.

    Предфильтр настраивается: его можно выключить целиком или поднять порог
    чернового скора. Скор на выдаче занижен — описания ещё нет, поэтому по
    умолчанию порог нулевой и отсев идёт только по стоп-словам и вилке.
    """
    prefilter = prefilter or settings.prefilter_options()
    seen: dict[str, Vacancy] = {}
    passed: dict[str, Vacancy] = {}
    queries = [q for q in profile.queries if q.get("text")]
    for index, query in enumerate(queries, start=1):
        if limit and len(passed) >= limit:
            log.info("лимит %s набран, остальные запросы не трогаем", limit)
            break
        text = query["text"]
        log.info("[%s/%s] запрос: %s", index, len(queries), text)
        pages = client.search(
            text=text,
            area=query.get("area") or profile.areas or None,
            period=int(query.get("period", 7)),
            max_pages=int(query.get("max_pages", 3)),
            extra=query.get("extra"),
        )
        try:
            for draft in pages:
                seen.setdefault(draft.key, draft)
                rough = evaluate(draft, profile, prefilter.fuzzy)
                if prefilter.enabled and rough.rejected:
                    log.debug(
                        "отброшено на предфильтре: %s (%s)",
                        draft.title,
                        rough.reject_reason,
                    )
                    continue
                if prefilter.enabled and rough.score < prefilter.min_score:
                    log.debug(
                        "отброшено на предфильтре: %s (черновой скор %.1f < %.1f)",
                        draft.title,
                        rough.score,
                        prefilter.min_score,
                    )
                    continue
                passed.setdefault(draft.key, draft)
                if limit and len(passed) >= limit:
                    log.info(
                        "собрали %s вакансий при лимите %s, больше страниц не запрашиваем",
                        len(passed),
                        limit,
                    )
                    break
        finally:
            # Генератор закрываем явно: иначе он доживает до сборки мусора и не
            # очевидно когда отпустит соединение.
            pages.close()
    return seen, passed
