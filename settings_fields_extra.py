"""Сборка дополнительных каталогов настроек в один.

settings_fields.py упёрся в 25 КБ [CORE-024], поэтому новые группы полей живут
отдельными файлами. Чтобы основной каталог не рос на три строки за каждую
группу, все они подмешиваются здесь, а он импортирует один список и один
словарь подписей.
"""

from __future__ import annotations

from settings_fields_gates import GATE_FIELDS, GROUP_GATES, GROUP_GATES_HINT
from settings_fields_reviews import AREA_FIELDS, GROUP_AREA, GROUP_AREA_HINT
from settings_fields_sources import GROUP_SOURCES, GROUP_SOURCES_HINT, SOURCE_FIELDS

EXTRA_FIELDS = GATE_FIELDS + AREA_FIELDS + SOURCE_FIELDS

EXTRA_HINTS: dict[str, str] = {
    GROUP_GATES: GROUP_GATES_HINT,
    GROUP_AREA: GROUP_AREA_HINT,
    GROUP_SOURCES: GROUP_SOURCES_HINT,
}

__all__ = ("EXTRA_FIELDS", "EXTRA_HINTS")
