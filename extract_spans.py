"""Условия работы спанами вместо генерации: GLiNER вместо модели на этапе extract.

Зачем это вообще. Главное требование этапа — дословная цитата из текста
(`llm_tasks.extract_conditions` отбрасывает всё остальное). Генеративная модель
цитату пересказывает, и чем она меньше, тем чаще. Модель-разметчик спанов
возвращает кусок самого текста, поэтому цитата дословна по построению, а не по
результату проверки. Один прямой проход вместо авторегрессии — и заметно
быстрее [CORE-015] по духу: меньше свободы у модели, больше у кода.

Зависимость необязательная. Нет пакета `gliner`, нет весов, выключено
настройкой — функция возвращает пустоту, и вакансия идёт на обычный этап
extract, как раньше [CORE-017]. Поэтому её нет в requirements.txt: стек
проекта остаётся stdlib плюс пять библиотек, а кто хочет спаны — ставит
`pip install gliner` и включает GLINER_ENABLED=1.

Модель живёт в PyTorch и ест память рядом с Ollama, поэтому по умолчанию
выключено: на 16 ГБ ОЗУ владельца это осознанный выбор, а не фон.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Sequence

import injection
import settings
from llm_tasks import Condition

log = logging.getLogger("fuckhr")

DEFAULT_MODEL = "urchade/gliner_multi-v2.1"
MAX_CHARS = 6000
MIN_QUOTE = 6
MAX_ITEMS = 8

# Метка для модели -> поле условия. Метки по-русски: multi-модель размечает по
# смыслу подписи, и «график работы» работает лучше, чем «schedule».
LABELS: dict[str, str] = {
    "формат работы": "format",
    "офис и адрес": "office",
    "график работы": "schedule",
    "зарплата": "salary",
    "уровень и опыт": "grade",
    "технологический стек": "stack",
    "этапы отбора": "process",
}

_model: Any = None
_model_name: str = ""


def enabled() -> bool:
    return settings.flag("GLINER_ENABLED")


def model_name() -> str:
    return (os.getenv("GLINER_MODEL") or "").strip() or DEFAULT_MODEL


def threshold() -> float:
    return max(0.1, min(0.95, settings.as_float(os.getenv("GLINER_THRESHOLD"), 0.5)))


def load(name: str = "") -> Any:
    """Модель в память, один раз на процесс. Нет пакета — None, не исключение."""
    global _model, _model_name
    want = name or model_name()
    if _model is not None and _model_name == want:
        return _model
    try:
        from gliner import GLiNER  # noqa: PLC0415 — зависимость необязательная
    except ImportError:
        log.info("gliner не установлен, этап extract идёт через модель")
        return None
    try:
        _model = GLiNER.from_pretrained(want)
    except Exception as exc:  # noqa: BLE001 — веса могут не скачаться [CORE-017]
        log.warning("gliner %s не загрузился: %s", want, exc)
        return None
    _model_name = want
    log.info("gliner загружен: %s", want)
    return _model


def _value(quote: str, words: int = 6) -> str:
    """Короткое значение из цитаты: первые слова без хвостов пунктуации."""
    parts = re.split(r"\s+", quote.strip())
    return " ".join(parts[:words]).strip(" ,;:.—-")


def conditions(text: str, model: Any = None) -> tuple[Condition, ...]:
    """Условия работы спанами. Пусто — значит этап отдаётся модели.

    Текст чистится от инъекций так же, как перед вызовом модели (ADR-020):
    спан из вырезанной строки цитатой не считается.
    """
    body = (text or "").strip()
    if not body or not enabled():
        return ()
    engine = model if model is not None else load()
    if engine is None:
        return ()
    clean = injection.clean(body)[0][:MAX_CHARS]
    try:
        found = engine.predict_entities(clean, list(LABELS), threshold=threshold())
    except Exception as exc:  # noqa: BLE001 — сигнал важнее падения [CORE-017]
        log.warning("gliner не разметил текст: %s", exc)
        return ()

    out: list[Condition] = []
    seen: set[str] = set()
    for item in sorted(found, key=lambda i: -float(i.get("score", 0.0))):
        quote = str(item.get("text") or "").strip()
        field = LABELS.get(str(item.get("label") or ""), "other")
        if len(quote) < MIN_QUOTE or quote not in clean or quote in seen:
            continue
        seen.add(quote)
        out.append(Condition(field=field, value=_value(quote), quote=quote))
        if len(out) >= MAX_ITEMS:
            break
    if out:
        log.info("условий размечено спанами: %s", len(out))
    return tuple(out)


def split(texts: Sequence[tuple[str, str]]) -> tuple[dict[str, tuple[Condition, ...]], list[str]]:
    """(что разметили спанами, что осталось модели) по парам (ключ, текст)."""
    done: dict[str, tuple[Condition, ...]] = {}
    rest: list[str] = []
    for key, text in texts:
        items = conditions(text)
        if items:
            done[key] = items
        else:
            rest.append(key)
    return done, rest


__all__ = (
    "DEFAULT_MODEL",
    "LABELS",
    "conditions",
    "enabled",
    "load",
    "model_name",
    "split",
    "threshold",
)
