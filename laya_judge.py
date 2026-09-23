"""Решатель Laya: этапы-классификаторы без генерации текста.

Зачем. Два этапа пайплайна не пишут текст, а выносят решение по тексту:
`review_fake` («это заказной отзыв?») и `ai_text` («это сгенерировано?»).
Генеративная модель отвечает на них абзацем, который потом приходится парсить
и проверять, а Laya — энкодер с типизированными вопросами: один прямой проход,
на выходе калиброванная вероятность, парсить нечего [CORE-015].

Чем это не замена GLiNER. GLiNER размечает спаны и потому даёт дословную
цитату, а Laya цитат не возвращает вовсе. Этап `extract` остаётся за GLiNER
и генеративной моделью, здесь только решения «да/нет».

Зависимость необязательная и по умолчанию выключена: нет пакета `laya`, нет
весов, выключено настройкой — функции возвращают пустоту, и сигнал считается
как раньше [CORE-017]. В requirements.txt пакета нет сознательно: это ещё одна
модель в PyTorch рядом с Ollama, ставит её тот, кто её мерил (docs/laya.md).

Вопрос задаётся двумя нейтральными вариантами (`choice`), а не типом `noul`:
у `noul` метки `false:`/`true:` перетягивают ответ на себя (issue #156 в laya),
и авторы сами советуют этот обход.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

import settings

DEFAULT_MODEL = "convaiinnovations/laya-multilingual"
MAX_CHARS = 4000
KEY = "verdict"
YES, NO = "A", "B"

QUESTIONS: dict[str, dict[str, Any]] = {
    "review_fake": {
        "type": "choice",
        "instructions": (
            "Это рекламный или заказной отзыв о работодателе, без личного опыта "
            "и проверяемых деталей?"
        ),
        "criteria": {
            YES: "да, текст рекламный или заказной: клише, никаких проверяемых деталей",
            NO: "нет, это личный опыт: конкретные детали, сроки, имена систем, числа",
        },
    },
    "ai_text": {
        "type": "choice",
        "instructions": "Этот текст написан генеративной моделью, а не человеком?",
        "criteria": {
            YES: "да, генеративный канцелярит: гладко, обобщённо, без конкретики",
            NO: "нет, писал человек: числа, названия, сбивчивый порядок мыслей",
        },
    },
}

log = logging.getLogger("fuckhr")

_agent: Any = None
_agent_name: str = ""
_failed: bool = False


def enabled() -> bool:
    return settings.flag("LAYA_ENABLED")


def model_name() -> str:
    return (os.getenv("LAYA_MODEL") or "").strip() or DEFAULT_MODEL


def threshold() -> float:
    return max(0.0, min(1.0, settings.as_float(os.getenv("LAYA_THRESHOLD"), 0.5)))


def reset() -> None:
    """Забыть загруженную модель. Нужно тестам и смене имени в настройках."""
    global _agent, _agent_name, _failed
    _agent, _agent_name, _failed = None, "", False


def load(name: str | None = None) -> Any | None:
    """Модель или None, если её нет. Второй раз не грузится и не ругается."""
    global _agent, _agent_name, _failed
    wanted = (name or model_name()).strip()
    if _agent is not None and _agent_name == wanted:
        return _agent
    if _failed and _agent_name == wanted:
        return None
    try:
        import laya  # noqa: PLC0415 — необязательная зависимость
    except ImportError:
        _agent_name, _failed = wanted, True
        log.info("Laya не установлена (pip install laya), решатель пропущен")
        return None
    try:
        _agent = laya.load(wanted)
    except Exception as exc:  # noqa: BLE001 — [CORE-017]
        _agent, _agent_name, _failed = None, wanted, True
        log.warning("Laya %s не загрузилась: %s", wanted, exc)
        return None
    _agent_name, _failed = wanted, False
    return _agent


def _yes_probability(answer: Any) -> float | None:
    """Вероятность «да» из ответа Laya. Форма ответа проверяется, а не верится."""
    if not isinstance(answer, dict):
        return None
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict) and YES in probabilities:
        try:
            return float(probabilities[YES])
        except (TypeError, ValueError):
            return None
    if "noul" in answer:
        try:
            return float(answer["noul"])
        except (TypeError, ValueError):
            return None
    choice = str(answer.get("choice") or "")
    try:
        confidence = float(answer.get("confidence"))
    except (TypeError, ValueError):
        return None
    if choice == YES:
        return confidence
    if choice == NO:
        return 1.0 - confidence
    return None


def probability(text: str, stage: str, agent: Any | None = None) -> float | None:
    """Вероятность «да» по одному тексту. None — спросить некого или не вышло."""
    question = QUESTIONS.get(stage)
    if question is None or not (text or "").strip():
        return None
    model = agent if agent is not None else load()
    if model is None:
        return None
    try:
        result = model.predict({"text": text[:MAX_CHARS]}, {KEY: question})
    except Exception as exc:  # noqa: BLE001 — [CORE-017]
        log.warning("Laya не ответила на этапе %s: %s", stage, exc)
        return None
    answers = (result or {}).get("answers") if isinstance(result, dict) else None
    return _yes_probability((answers or {}).get(KEY))


def probabilities(
    texts: Sequence[str], stage: str, agent: Any | None = None
) -> list[float | None]:
    """Вероятности по списку текстов в том же порядке."""
    model = agent if agent is not None else load()
    if model is None:
        return [None] * len(texts)
    return [probability(text, stage, agent=model) for text in texts]


def flagged(
    texts: Sequence[str],
    stage: str,
    agent: Any | None = None,
    limit: float | None = None,
) -> set[int]:
    """Индексы текстов, по которым решатель сказал «да» увереннее порога."""
    bar = threshold() if limit is None else limit
    return {
        index
        for index, value in enumerate(probabilities(texts, stage, agent=agent))
        if value is not None and value >= bar
    }


__all__ = (
    "DEFAULT_MODEL",
    "KEY",
    "QUESTIONS",
    "enabled",
    "flagged",
    "load",
    "model_name",
    "probabilities",
    "probability",
    "reset",
    "threshold",
)
