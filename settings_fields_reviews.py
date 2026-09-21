"""Каталог настроек: разбивка отзывов по сферам.

Отдельный файл по той же причине, что и settings_fields_gates.py:
settings_fields.py упёрся в 25 КБ [CORE-024]. Поля подмешиваются в общий
каталог снизу, поэтому на странице настроек ничем не отличаются от остальных.
"""

from __future__ import annotations

from settings_fields import TEXT, Field

import review_area

GROUP_AREA = "Отзывы по сферам"
GROUP_AREA_HINT = "чьими глазами написан отзыв"

AREA_FIELDS: tuple[Field, ...] = (
    Field(
        "REVIEW_AREA",
        "Твоя сфера",
        GROUP_AREA,
        TEXT,
        "",
        "Код сферы: {}. Заполнено — в досье появляется вторая оценка, по отзывам "
        "твоей стороны компании: в крупной сети кассиры и разработчики описывают "
        "разные вселенные. Пусто — разбивки нет, всё как раньше. Сфера отзыва "
        "определяется словарями, не моделью; неуверенные остаются без метки.".format(
            ", ".join(review_area.codes())
        ),
    ),
)

__all__ = ("AREA_FIELDS", "GROUP_AREA", "GROUP_AREA_HINT")
