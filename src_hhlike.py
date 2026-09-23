"""Zarplata.ru: тот же движок, что у hh.ru, только другой домен.

Проверено запросом: страница поиска `zarplata.ru/search/vacancy` отдаёт
состояние в `<template id="HH-Lux-InitialState">`, а её API отвечает в формате
hh.ru. Значит писать второй парсер не нужно — берём готовый разбор состояния из
`hh_html` и меняем только адрес и подпись источника `[CORE-025]`.

Отдельный клиент не заводится: у `hh_html.HHHtmlClient` уже есть паузы,
браузерные заголовки, дампы сбоев и распознавание капчи. Здесь только адреса,
поэтому файл маленький и будет таким же для любой другой площадки на этом
движке.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from hh import Vacancy

log = logging.getLogger("fuckhr")

CODE = "zarplata"
LABEL = "Zarplata.ru"
HOST = "https://zarplata.ru"
SEARCH_URL = HOST + "/search/vacancy"
VACANCY_PREFIX = HOST + "/vacancy/"


def search(
    text: str,
    area: Any = None,
    period: int = 7,
    limit: int = 0,
    fetcher: Any = None,
    max_pages: int = 2,
) -> Iterator[Vacancy]:
    """Поиск через разбор состояния страницы, как у hh.ru.

    Коды регионов у площадки те же, что у hh.ru: движок один, справочник общий.
    """
    import hh_html

    found = 0
    for page in range(max_pages):
        params: dict[str, Any] = {"text": text, "page": page, "items_on_page": 50}
        if area:
            params["area"] = area if not isinstance(area, (list, tuple)) else list(area)
        if fetcher is not None:
            body = fetcher.text(SEARCH_URL, params=params)
        else:
            import src_common

            with src_common.client() as own:
                body = own.text(SEARCH_URL, params=params)
        if not body:
            return
        try:
            state = hh_html.extract_state(body)
        except Exception as exc:  # noqa: BLE001 — чужая страница [CORE-017]
            log.warning("%s: состояние страницы не разобралось: %s", CODE, exc)
            return
        nodes = hh_html.find_vacancy_nodes(state)
        if not nodes:
            return
        for node in nodes:
            try:
                vacancy = hh_html.node_to_vacancy(node)
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: карточка не разобралась: %s", CODE, exc)
                continue
            if vacancy is None or not vacancy.title:
                continue
            url = vacancy.url.replace("https://hh.ru", HOST)
            yield vacancy.model_copy(
                update={"source": CODE, "url": url or VACANCY_PREFIX + vacancy.external_id}
            )
            found += 1
            if limit and found >= limit:
                return


__all__ = ("CODE", "LABEL", "HOST", "search")
