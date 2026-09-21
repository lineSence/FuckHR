"""Сфера автора отзыва: словари, порог и агрегат по компании."""

from __future__ import annotations

import fake_company
import fake_reviews
import review_area
from reviewitems import ReviewItem


def item(index: int, text: str, rating: float | None = None, role: str = "") -> ReviewItem:
    return review_area.classify_item(
        ReviewItem(url="u", index=index, body=text, rating=rating, role=role)
    )


def verdict(index: int, label: str = "clean") -> fake_reviews.Verdict:
    return fake_reviews.Verdict(index=index, text_hash="h{}".format(index), label=label)


def test_it_review_by_text():
    area = review_area.classify("Плюсы: адекватное ревью кода, короткие спринты. Минусы: легаси")
    assert area.code == "it"
    assert area.scope == review_area.SCOPE_AREA
    assert "легаси" in area.hits


def test_role_outweighs_single_word():
    """Должность весит больше случайного слова: кладовщик не становится ИТ."""
    area = review_area.classify(
        "Минусы: недостача вешается на смену, склад холодный. Плюсы: питон изучал на досуге",
        role="кладовщик",
    )
    assert area.code == "retail"


def test_unknown_when_not_sure():
    assert review_area.classify("Хорошая компания, всем советую").code == review_area.AREA_UNKNOWN
    # Одного маркера мало: порог MIN_SCORE не взят.
    assert review_area.classify("Иногда бывают баги").code == review_area.AREA_UNKNOWN


def test_company_wide_scope():
    """Задержка зарплаты — тема про всю компанию, а не про сферу автора."""
    area = review_area.classify("Задерживают зарплату третий месяц, часть в конверте")
    assert area.scope == review_area.SCOPE_COMPANY


def test_owner_code_validates():
    assert review_area.owner_code("it") == "it"
    assert review_area.owner_code("IT ") == "it"
    assert review_area.owner_code("отдел имени меня") == ""


def test_by_area_needs_data():
    """Мало отзывов — разбивки нет: цифра по одному отзыву выглядит как факт."""
    items = [item(0, "ревью кода, спринты, легаси", 3.0)]
    verdicts = [verdict(0)]
    assert fake_company.by_area(items, verdicts, "it") is None


def test_by_area_counts_and_average():
    items = [
        item(0, "ревью кода, спринты, легаси", 2.0),
        item(1, "деплой каждый день, микросервисы, техдолг", 2.0),
        item(2, "недостача, смена, магазин, кассир", 5.0),
        item(3, "задерживают зарплату, обманывают с премией", 1.0),
        item(4, "нормально", 4.0),
    ]
    verdicts = [verdict(i) for i in range(5)]
    mark = fake_company.by_area(items, verdicts, "it")
    assert mark is not None
    assert (mark.code, mark.total, mark.avg) == ("it", 2, 2.0)
    assert (mark.wide, mark.other, mark.unknown) == (1, 1, 1)
    assert mark.label == "разработка и ИТ"


def test_by_area_skips_fake_reviews():
    """Заказной отзыв не тянет среднюю по сфере, как и общую."""
    items = [
        item(0, "ревью кода, спринты, легаси", 1.0),
        item(1, "деплой, микросервисы, техдолг", 3.0),
        item(2, "недостача, смена, кассир", 5.0),
        item(3, "нормально", 4.0),
        item(4, "тоже нормально", 4.0),
    ]
    verdicts = [verdict(0, "fake"), verdict(1), verdict(2), verdict(3), verdict(4)]
    mark = fake_company.by_area(items, verdicts, "it")
    assert mark is not None and mark.avg == 3.0


def test_evaluate_without_area_is_silent():
    items = [item(i, "ревью кода, спринты, легаси", 3.0) for i in range(5)]
    verdicts = [verdict(i) for i in range(5)]
    assert fake_company.evaluate(items, verdicts).area is None


def test_hits_are_markers_not_people():
    """В отчёт уходят маркеры, а не должность и не автор [CORE-013]."""
    area = review_area.classify("Должность: Иван Петров, программист. Плюсы: ревью, спринты")
    assert all(mark in sum(review_area.MARKERS.values(), ()) + review_area.WIDE_MARKERS
               for mark in area.hits)
