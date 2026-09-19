"""Разбор на настоящей вёрстке площадок (golden-фикстуры).

Фикстуры — обрезанные страницы dreamjob.ru и pravda-sotrudnikov.ru: разметка
настоящая, тексты отзывов написаны нами, имена и ники вырезаны [CORE-012].
Проверяется не красота разбора, а то, что после редизайна тест покраснеет.

Именно этот разбор раньше не проверялся ничем, кроме синтетического HTML, и на
живых страницах ломался: один отзыв резало на три куска, даты не находились
вовсе, а восемьсот ссылок на соседние города вытесняли настоящие отзывы.
"""

from __future__ import annotations

from pathlib import Path

import reviewitems
import reviewlegit

FIXTURES = Path(__file__).parent / "fixtures"


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_dreamjob_разбирается_целиком():
    items = reviewitems.split_page(_page("dreamjob.html"), url="https://dreamjob.ru/c/1")
    assert len(items) == 2

    first = items[0]
    assert first.dated_at == "2026-01-01" and first.date_precision == "month"
    assert first.rating == 5.0
    assert first.pros.startswith("Белая зарплата")
    assert "корпоративных мероприятий" in first.cons
    # Заголовок и текст одного отзыва не должны стать двумя отзывами.
    assert items[1].dated_at == "2025-10-01" and items[1].rating == 4.5


def test_правда_сотрудников_разбирается_целиком():
    items = reviewitems.split_page(
        _page("pravda-sotrudnikov.html"), url="https://pravda-sotrudnikov.ru/c/1"
    )
    assert len(items) == 2
    assert items[0].dated_at == "2026-02-06" and items[0].date_precision == "exact"
    assert items[0].pros and items[0].cons
    # Оценки на этой площадке нет вовсе — это нормально, а не повод гадать.
    assert items[0].rating is None


def test_живые_отзывы_проходят_проверку_легитимности():
    for name in ("dreamjob.html", "pravda-sotrudnikov.html"):
        items = reviewitems.split_page(_page(name), url="https://example/1")
        kept, dropped = reviewlegit.filter_items(items)
        assert dropped == (), (name, [c.reasons for _i, c in dropped])
        assert len(kept) == len(items)


def test_страница_без_отзывов_даёт_пусто():
    assert reviewitems.split_page("<html><body><p>Отзывов пока нет</p></body></html>") == ()
