"""Каталог настроек: другие площадки вакансий.

Своя группа и свой файл: settings_fields.py упёрся в 25 КБ [CORE-024], а вопрос
здесь отдельный — откуда брать вакансии. Сами галочки живут на главной
странице, рядом с кнопкой запуска: выбор площадок — часть решения «что сейчас
собираем», а не настройка на месяцы.
"""

from __future__ import annotations

from settings_fields import FLOAT, INT, SECRET, TEXT, Field

GROUP_SOURCES = "Другие площадки"
GROUP_SOURCES_HINT = "какие агрегаторы входят в сбор и чем они платят"

SOURCE_FIELDS: tuple[Field, ...] = (
    Field(
        "SOURCE_SITES",
        "Площадки в сборе",
        GROUP_SOURCES,
        TEXT,
        "hh",
        "Коды через запятую: hh, trudvsem, superjob, zarplata, rabota. Пусто — "
        "только hh.ru, как было всегда. Удобнее ставить галочками на главной "
        "странице; здесь — чтобы видеть значение целиком.",
    ),
    Field(
        "SUPERJOB_KEY",
        "Ключ приложения SuperJob",
        GROUP_SOURCES,
        SECRET,
        "",
        "Бесплатный ключ с api.superjob.ru/register. Без него площадка отвечает "
        "403 и в сбор не идёт вовсе.",
    ),
    Field(
        "SOURCE_PAUSE",
        "Пауза между запросами, с",
        GROUP_SOURCES,
        FLOAT,
        "1",
        "Одна пауза на все площадки, кроме hh.ru: у него свои HH_PAUSE. Меньше "
        "секунды — верный способ получить блокировку вместо вакансий.",
    ),
    Field(
        "SOURCE_MAX_PAGES",
        "Страниц на запрос",
        GROUP_SOURCES,
        INT,
        "3",
        "Сколько страниц выдачи брать с каждой площадки по одному запросу "
        "профиля. Выдача отсортирована по свежести, поэтому дальше в основном "
        "старое.",
    ),
)

__all__ = ("GROUP_SOURCES", "GROUP_SOURCES_HINT", "SOURCE_FIELDS")
