"""Тип поля настройки, виды значений и имена групп.

Нижний уровень каталога: отсюда берут Field и settings_fields.py, и
тематические каталоги settings_fields_*.py, поэтому здесь нет импортов проекта.
"""

from __future__ import annotations

from dataclasses import dataclass

TEXT = "text"
SECRET = "secret"
INT = "int"
FLOAT = "float"
BOOL = "bool"

TRUE_VALUES = {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Field:
    """Одна настройка: ключ в .env плюс всё, что нужно для формы."""

    key: str
    label: str
    group: str
    kind: str = TEXT
    default: str = ""
    help: str = ""

    @property
    def is_secret(self) -> bool:
        return self.kind == SECRET


GROUP_RUN = "Запуск"
GROUP_PREFILTER = "Предфильтр"
GROUP_DETECTOR = "Детектор брехни"
GROUP_SCORE = "Оценка работодателя"
GROUP_DEEP = "Глубокий ресёрч"
GROUP_SOURCE = "Источник вакансий"
GROUP_TELEGRAM = "Telegram"
GROUP_LLM = "Модель"
GROUP_LLM_STAGES = "Модель по этапам"
GROUP_EMBED = "Векторы и похожесть"
GROUP_SEARCH = "Внешний поиск"
GROUP_PATHS = "Файлы и логи"
