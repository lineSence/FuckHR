"""Какие настройки считаются установочными.

Отдельный файл, потому что settings_fields.py давно у потолка [CORE-024], а
вопрос здесь самостоятельный: пути, потолки и темп запросов ставят один раз
и больше не трогают, поэтому на странице настроек они собраны в одну группу
в конце, а не разбросаны по смысловым (docs/ui-map.md).
"""

from __future__ import annotations

GROUP_INSTALL = "Установка"
INSTALL_HINT = "пути, потолки и темп запросов: ставится один раз"

KEYS = frozenset(
    {"DB_PATH", "LOG_PATH", "FAILURE_DIR", "SEARCH_MAX_CALLS", "ALERT_COOLDOWN_HOURS"}
)
SUFFIXES = ("_WORKERS", "_PAUSE", "_PAUSE_MIN", "_TIMEOUT")
PREFIXES = ("REVIEW_FETCH_",)
# Пауза цикла спрашивается на главной вместе с самим циклом: там это часть
# решения «гонять по кругу», а не установочный параметр.
KEEP = frozenset({"RUN_LOOP_PAUSE"})


def is_install(key: str) -> bool:
    """Установочная ли настройка. Только по имени ключа: правило видно глазами."""
    if key in KEEP:
        return False
    return key in KEYS or key.endswith(SUFFIXES) or key.startswith(PREFIXES)


__all__ = ("GROUP_INSTALL", "INSTALL_HINT", "KEEP", "KEYS", "is_install")
