"""Необязательный сигнал модели: читается ли отзыв как рекламный текст.

Этап включается вручную (`FAKE_REVIEW_LLM=1`) и по умолчанию выключен: пока
детерминированная часть не откалибрована на размеченной выборке, ответ модели
только зашумляет счёт [CORE-019]. Выключённый этап ничего не меняет — вся
детекция считается без него [CORE-017].

Роль модели предельно узкая: «рекламный / личный опыт» плюс дословная цитата,
на которой основан вывод. Цитата проверяется подстрокой в исходном тексте, и
без неё ответ выбрасывается. Вес сигнала минимальный, сам по себе он не может
пометить отзыв [CORE-015].

Этап `review_fake` объявлен в STAGE_PROFILES как local-only и внесён в
PERSONAL_STAGES: в отзывах встречаются имена сотрудников [CORE-012].
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Sequence

from fake_reviews import normalize

log = logging.getLogger(__name__)

STAGE = "review_fake"
MAX_ITEMS = 12
MAX_CHARS = 1200

PROMPT = (
    "Ниже отзывы о работодателе. Для каждого скажи, читается он как рекламный "
    "текст или как личный опыт работы.\n"
    "Правила:\n"
    "- quote копируй из отзыва дословно, без пересказа;\n"
    "- не оценивай компанию и не делай выводов о правдивости;\n"
    "- сомневаешься — отвечай experience.\n"
    'JSON: {"items": [{"id": 0, "verdict": "ad|experience", "quote": "точная цитата"}]}'
)


def enabled() -> bool:
    return (os.getenv("FAKE_REVIEW_LLM") or "0").strip().lower() in ("1", "true", "yes", "on")


def _parse(raw: str) -> list[dict[str, Any]]:
    match = re.search(r"\{.*\}", raw or "", flags=re.DOTALL)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        log.warning("модель вернула не JSON, сигнал review_fake пропускаю")
        return []
    items = payload.get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def ad_indexes(gateway: Any, items: Sequence[Any], force: bool = False) -> set[int]:
    """Индексы отзывов, которые модель прочитала как рекламные.

    `force` обходит выключатель и нужен только бенчмарку: там этап гоняется
    осознанно, чтобы сравнить модели между собой.

    Никогда не бросает исключение: досье важнее одного сигнала веса 0.5.
    """
    if not (force or enabled()) or gateway is None or not getattr(gateway, "enabled", False):
        return set()
    chosen = list(items)[:MAX_ITEMS]
    if not chosen:
        return set()

    body = "\n\n".join(
        "[{}] {}".format(item.index, (item.text or "")[:MAX_CHARS]) for item in chosen
    )
    try:
        raw = gateway.complete(STAGE, [{"role": "user", "content": PROMPT + "\n\n" + body}])
    except Exception as exc:  # noqa: BLE001 — [CORE-017]
        log.warning("сигнал review_fake не получен: %s", exc)
        return set()

    texts = {item.index: normalize(item.text) for item in chosen}
    out: set[int] = set()
    for answer in _parse(raw or ""):
        if str(answer.get("verdict") or "").strip().lower() != "ad":
            continue
        try:
            index = int(answer.get("id"))
        except (TypeError, ValueError):
            continue
        quote = normalize(str(answer.get("quote") or ""))
        # Без дословной цитаты вывод нечем проверить, поэтому он не считается.
        if quote and quote in texts.get(index, ""):
            out.add(index)
        else:
            log.info("цитата модели не найдена в отзыве %s, сигнал отброшен", index)
    return out


__all__ = ("STAGE", "ad_indexes", "enabled")
