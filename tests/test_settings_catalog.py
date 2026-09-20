"""Каталог настроек против кода: расхождение копится молча.

Проверка появилась после разбора страницы настроек 20.09.2026: там висели
HH_TOKEN и HH_USER_AGENT, которых не читал ни один модуль, и одновременно
отсутствовал LLM_PROXY_MODEL_LOCAL, который читает llm.Gateway. Ни один тест
этого не видел, потому что каталог полей и чтение окружения ничем не связаны.

Что здесь считается чтением: вызов вида getenv/get/flag/number с ключом-строкой
и обращение к os.environ[...]. Плюс два словаря маршрутизации моделей, где имена
переменных собираются из имён профилей и этапов, а не пишутся буквой.
"""

from __future__ import annotations

import ast
import pathlib
import re

import llm
import settings
import websearch

ENV_RE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")
READERS = {"getenv", "get", "flag", "number"}

# Ключи, которые читаются намеренно мимо страницы настроек. Список закрытый:
# новый ключ либо попадает в каталог, либо объясняется здесь.
OUTSIDE = {
    # Путь до самого .env: читается раньше, чем появляется каталог.
    "ENV_FILE",
    # Параметры инстанса SearXNG живут на странице «Поиск»: их подбирают,
    # глядя на выдачу, а не в общем списке (ui_forms.search_settings_form).
    *{key for key, *_ in websearch.SEARXNG_FIELDS},
    # Необязательные сигналы модели: выключены по умолчанию до калибровки
    # [CORE-019], включаются руками в .env.
    "AI_TEXT_LLM",
    "FAKE_REVIEW_LLM",
    # Отладка разбора отзывов: в дамп попадают чужие тексты, поэтому кнопки
    # «писать дамп» в интерфейсе нет сознательно.
    "REVIEW_DUMP_PATH",
    # Загрузка страниц отзывов: пороги подбирались один раз и живут в .env.
    "REVIEW_FETCH_ENABLED",
    "REVIEW_FETCH_PAGES",
    "REVIEW_FETCH_TIMEOUT",
    "REVIEW_FETCH_CHARS",
    "REVIEW_FETCH_PAUSE",
    "REVIEW_FETCH_CACHE_DAYS",
    # Служебный файл состояния канарейки рядом с базой.
    "ALERT_STATE_PATH",
}

ROOT = pathlib.Path(__file__).resolve().parent.parent


def env_keys_read() -> dict[str, set[str]]:
    """Ключ окружения -> модули, которые его читают."""
    found: dict[str, set[str]] = {}

    def remember(key: str, module: str) -> None:
        if ENV_RE.match(key):
            found.setdefault(key, set()).add(module)

    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                first = node.args[0]
                if name in READERS and isinstance(first, ast.Constant):
                    if isinstance(first.value, str):
                        remember(first.value, path.name)
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
                if node.value.attr == "environ" and isinstance(node.slice, ast.Constant):
                    if isinstance(node.slice.value, str):
                        remember(node.slice.value, path.name)

    # Имена собираются из профилей и этапов, в коде буквами не встречаются.
    for key in (
        *llm.PROXY_MODEL_ENV.values(),
        *llm.STAGE_MODEL_ENV.values(),
        *llm.STAGE_MODELS_ENV.values(),
    ):
        remember(key, "llm.py")
    return found


def test_каждое_поле_настроек_кто_то_читает() -> None:
    """Поле, которого не читает ни один модуль, — обещание, которого нет."""
    read = set(env_keys_read())
    dead = sorted(set(settings.FIELD_BY_KEY) - read)
    assert not dead, "поля есть в форме, но их никто не читает: {}".format(dead)


def test_каждый_читаемый_ключ_есть_в_каталоге_или_объяснён() -> None:
    """Обратная сторона: настройка, которую нельзя изменить из интерфейса."""
    read = env_keys_read()
    hidden = sorted(set(read) - set(settings.FIELD_BY_KEY) - OUTSIDE)
    assert not hidden, "код читает ключи, которых нет на странице настроек: {}".format(
        {key: sorted(read[key]) for key in hidden}
    )


def test_список_исключений_не_протух() -> None:
    """Исключение, которое больше никто не читает, — мусор в списке."""
    read = set(env_keys_read())
    stale = sorted(key for key in OUTSIDE if key not in read and key != "ENV_FILE")
    assert not stale, "в OUTSIDE остались ключи, которых нет в коде: {}".format(stale)
