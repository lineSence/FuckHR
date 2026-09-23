"""Векторы: упаковка, косинус, поиск похожего. Ни сети, ни базы.

Отдельный файл без зависимостей, потому что это арифметика, а не пайплайн:
вызов модели живёт в `llm_embed.py`, хранение — в `embeddings_store.py`
[CORE-024].

Почему без numpy. Замер 20.09.2026: 5000 векторов по 1024 измерения, чистый
Python — 0.43 с на один поиск (`sum(map(float.__mul__, ...))` по `array('f')`).
База держит тысячи вакансий, не миллионы, поэтому зависимость ради одной
свёртки не окупается [CORE-025]. Когда перестанет хватать — `sqlite-vec`
(ADR-008), а не самописный индекс.

Векторы хранятся уже нормированными, поэтому косинус — это скалярное
произведение. Нормировка на записи, а не на чтении: читаем мы чаще.
"""

from __future__ import annotations

import array
import math
from typing import Iterable, Sequence

# float32: 1024 измерения = 4 КБ на вектор. float64 удвоил бы базу без выигрыша
# в качестве — модели и так отдают float32.
TYPECODE = "f"


def normalize(values: Sequence[float]) -> array.array:
    """Вектор единичной длины. Нулевой остаётся нулевым, а не делится на ноль."""
    vector = array.array(TYPECODE, (float(v) for v in values))
    length = math.sqrt(sum(v * v for v in vector))
    if length:
        for position in range(len(vector)):
            vector[position] = vector[position] / length
    return vector


def pack(values: Sequence[float]) -> bytes:
    """Нормированный вектор в BLOB для SQLite."""
    return normalize(values).tobytes()


def unpack(blob: bytes) -> array.array:
    vector = array.array(TYPECODE)
    vector.frombytes(blob)
    return vector


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Косинус нормированных векторов. Разная длина — не ошибка, а ноль.

    Разная длина означает разные модели: сравнивать такие векторы нельзя, и
    падать здесь тоже нельзя — база могла остаться от прежней модели [LLM-011].
    """
    if len(left) != len(right) or not len(left):
        return 0.0
    return float(sum(map(float.__mul__, left, right)))


def near_pairs(
    vectors: Sequence[Sequence[float]], threshold: float
) -> tuple[tuple[int, int], ...]:
    """Пары индексов, похожих выше порога. Перебор: пар здесь десятки."""
    out: list[tuple[int, int]] = []
    for left in range(len(vectors)):
        for right in range(left + 1, len(vectors)):
            if cosine(vectors[left], vectors[right]) >= threshold:
                out.append((left, right))
    return tuple(out)


def top_similar(
    query: Sequence[float],
    rows: Iterable[tuple[str, Sequence[float]]],
    limit: int = 5,
    threshold: float = 0.0,
) -> list[tuple[str, float]]:
    """Ближайшие к запросу ключи: [(ключ, близость)], по убыванию."""
    scored = []
    for key, vector in rows:
        value = cosine(query, vector)
        if value >= threshold:
            scored.append((key, round(value, 4)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


__all__ = (
    "TYPECODE",
    "cosine",
    "near_pairs",
    "normalize",
    "pack",
    "top_similar",
    "unpack",
)
