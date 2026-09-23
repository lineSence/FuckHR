"""Необязательный сигнал модели: читается ли текст как сгенерированный.

Включается вручную (`AI_TEXT_LLM=1`) и по умолчанию выключен: пока
детерминированная часть не откалибрована, ответ модели только зашумляет счёт
[CORE-019]. Вес сигнала — 1.0 из 6.0, сам по себе он ничего не помечает.

О маршруте. Этап `ai_text` в PERSONAL_STAGES не внесён и может ходить на
прокси — решение владельца от 19.09.2026. Для вакансий это очевидно: описание
вакансии публично. Для отзывов это отступление от [CORE-012]: соседний этап
`review_fake` остаётся local-only именно потому, что в отзывах встречаются
имена сотрудников. Отступление осознанное и описано в docs/ai-text.md; чтобы
вернуть прежнюю границу, достаточно поменять профиль этапа в llm_profiles.

Модель отвечает одним словом на текст плюс дословной цитатой. Цитата
проверяется подстрокой: без неё вывод нечем проверить, и он выбрасывается.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Mapping

import aitext_rules as R

import injection

log = logging.getLogger(__name__)

STAGE = "ai_text"
MAX_ITEMS = 12
MAX_CHARS = 1500

PROMPT = (
    "Ниже тексты. Для каждого скажи, читается он как написанный языковой "
    "моделью или как написанный человеком.\n"
    "Правила:\n"
    "- quote копируй из текста дословно, без пересказа;\n"
    "- не оценивай компанию, вакансию и правдивость написанного;\n"
    "- сомневаешься — отвечай human.\n"
    'JSON: {"items": [{"id": 0, "verdict": "generated|human", "quote": "точная цитата"}]}'
)


def enabled() -> bool:
    return (os.getenv("AI_TEXT_LLM") or "0").strip().lower() in ("1", "true", "yes", "on")


def _normalize(text: str) -> str:
    return " ".join(R.WORD_RE.findall((text or "").lower()))


def _parse(raw: str) -> list[dict[str, Any]]:
    match = re.search(r"\{.*\}", raw or "", flags=re.DOTALL)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        log.warning("модель вернула не JSON, сигнал ai_text пропускаю")
        return []
    items = payload.get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def generated_indexes(
    gateway: Any,
    texts: Mapping[int, str],
    force: bool = False,
    seen: dict[str, bool] | None = None,
) -> set[int]:
    """Индексы текстов, которые модель прочитала как сгенерированные.

    `force` обходит выключатель и нужен бенчмарку. `seen` получает разметку
    учителя (judge_labels.py). Никогда не бросает исключение: детектор важнее
    одного сигнала [CORE-017].
    """
    if not (force or enabled()) or gateway is None or not getattr(gateway, "enabled", False):
        return set()
    chosen = {
        index: (text or "")[:MAX_CHARS]
        for index, text in list(texts.items())[:MAX_ITEMS]
        if len(text or "") >= R.MIN_CHARS and not injection.scan(text or "").red
    }
    if not chosen:
        return set()

    body = "\n\n".join("[{}] {}".format(index, text) for index, text in chosen.items())
    try:
        raw = gateway.complete(STAGE, [{"role": "user", "content": PROMPT + "\n\n" + body}])
    except Exception as exc:  # noqa: BLE001 — [CORE-017]
        log.warning("сигнал ai_text не получен: %s", exc)
        return set()

    normalized = {index: _normalize(text) for index, text in chosen.items()}
    out: set[int] = set()
    for answer in _parse(raw or ""):
        verdict = str(answer.get("verdict") or "").strip().lower()
        try:
            index = int(answer.get("id"))
        except (TypeError, ValueError):
            continue
        if verdict == "human" and seen is not None and index in chosen:
            seen[chosen[index]] = False
        if verdict != "generated":
            continue
        quote = _normalize(str(answer.get("quote") or ""))
        if quote and quote in normalized.get(index, ""):
            out.add(index)
            if seen is not None:
                seen[chosen[index]] = True
        else:
            log.info("цитата модели не найдена в тексте %s, сигнал отброшен", index)
    return out


__all__ = ("STAGE", "enabled", "generated_indexes")
