"""Логистическая регрессия на готовых векторах: обучение, замер, пороги.

Отдельный файл, потому что математика не привязана к данным: сейчас на ней
держится гейт отзывов (`review_gate_train.py`, разметка учителя) [CORE-024].

Без numpy и sklearn. Тысяча примеров на 1024 измерения — секунды чистого
Python, а новая зависимость ради одной свёртки не окупается: тот же довод, что
в `embeddings.py` [CORE-025]. Обучение полнобатчевое, поэтому результат
воспроизводим от прогона к прогону.

Мягкая метка (0.85 у вердикта модели-учителя) уходит в кросс-энтропию как есть:
цель — вероятность, а не класс, и модель не учится быть уверенной там, где
уверенности не было.
"""

from __future__ import annotations

import math
from typing import Sequence

# Пороги, по которым считается таблица «ложных / пропущено» и подбор границ.
GRID = tuple(round(0.05 * step, 2) for step in range(1, 20))


def sigmoid(value: float) -> float:
    if value < -30:
        return 0.0
    if value > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-value))


def score(weights: Sequence[float], bias: float, vector: Sequence[float]) -> float:
    """Вероятность «да» без проверок: для обучения, где длины совпадают."""
    return sigmoid(sum(map(float.__mul__, weights, vector)) + bias)


def predict(weights: Sequence[float], bias: float, vector: Sequence[float]) -> float:
    """То же для рантайма. Разная длина — 0.5: решать нечем, падать незачем."""
    if not weights or len(weights) != len(vector):
        return 0.5
    return score(weights, bias, vector)


def class_weight(rows: Sequence[tuple[Sequence[float], float]]) -> float:
    """Во сколько раз положительных меньше отрицательных.

    Разметка отзывов перекошена: «нет» вчетверо-впятеро больше, чем «да».
    Необученная логистическая регрессия на таком перекосе съезжает вниз целиком
    — на замере владельца все 124 положительных получили меньше 0.5, и гейт
    решал ноль процентов при формально приличном AUROC 0.815. Вес выравнивает
    классы по вкладу в градиент; на порядок пар (то есть на AUROC) он влияет
    слабо, а на калибровку — сильно.
    """
    positive = sum(1 for _, goal in rows if goal >= 0.5)
    negative = len(rows) - positive
    if not positive or not negative:
        return 1.0
    return negative / positive


def train(
    rows: Sequence[tuple[Sequence[float], float]],
    epochs: int = 40,
    rate: float = 4.0,
    decay: float = 1e-4,
    balance: bool = False,
) -> tuple[list[float], float]:
    """Веса и смещение. Полный батч, L2. `balance` выравнивает редкий класс."""
    if not rows:
        return [], 0.0
    size = len(rows[0][0])
    weights = [0.0] * size
    bias = 0.0
    heavy = class_weight(rows) if balance else 1.0
    sample_weights = [heavy if goal >= 0.5 else 1.0 for _, goal in rows]
    scale = rate / sum(sample_weights)
    for _ in range(epochs):
        grad = [0.0] * size
        grad_bias = 0.0
        for (vector, goal), weight in zip(rows, sample_weights):
            error = (score(weights, bias, vector) - goal) * weight
            if error:
                for position, value in enumerate(vector):
                    grad[position] += error * value
                grad_bias += error
        for position in range(size):
            weights[position] -= scale * grad[position] + decay * weights[position]
        bias -= scale * grad_bias
    return weights, bias


def auroc(positive: Sequence[float], negative: Sequence[float]) -> float | None:
    """Доля правильно упорядоченных пар. None — одного из классов нет."""
    if not positive or not negative:
        return None
    wins = 0.0
    for high in positive:
        for low in negative:
            wins += 1.0 if high > low else 0.5 if high == low else 0.0
    return round(wins / (len(positive) * len(negative)), 3)


def counts(scores: Sequence[float], labels: Sequence[int], limit: float) -> tuple[int, int]:
    """(ложных, пропущенных) при пороге."""
    wrong = sum(1 for value, label in zip(scores, labels) if value >= limit and not label)
    missed = sum(1 for value, label in zip(scores, labels) if value < limit and label)
    return wrong, missed


def choose(scores: Sequence[float], labels: Sequence[int]) -> tuple[float | None, float | None]:
    """(low, high): «нет» без пропусков, «да» без ложных.

    На хорошо разделимой выборке верхняя граница «нет» уезжает выше нижней
    границы «да» — тогда середины нет вовсе, и `low` прижимается к `high`:
    перевёрнутые пороги не гейт, а решето.
    """
    highs = [limit for limit in GRID if counts(scores, labels, limit)[0] == 0]
    lows = [limit for limit in GRID if counts(scores, labels, limit)[1] == 0]
    high = min(highs) if highs else None
    low = max(lows) if lows else None
    if low is not None and high is not None:
        low = min(low, high)
    return low, high


def decision(scores: Sequence[float], labels: Sequence[int]) -> tuple[float | None, dict]:
    """Порог для работы совсем без модели: лучшая F-мера на сетке.

    Гейт требует нуля ложных и потому в перекрытии классов не решает ничего.
    Решателю ноль ложных не нужен: его ответ весит один балл в счёте компании,
    и цена ошибки — та же, что у любого другого детерминированного сигнала
    [CORE-019]. Поэтому порог выбирается по балансу точности и полноты, а
    рядом возвращаются обе, чтобы решение принималось по числам.
    """
    best: tuple[float, float, dict] = (-1.0, 0.5, {})
    for limit in GRID:
        hit = sum(1 for value, label in zip(scores, labels) if value >= limit and label)
        said = sum(1 for value in scores if value >= limit)
        real = sum(1 for label in labels if label)
        if not hit:
            continue
        precision = hit / said
        recall = hit / real if real else 0.0
        f1 = 2 * precision * recall / (precision + recall)
        if f1 > best[0]:
            best = (
                f1,
                limit,
                {
                    "точность": round(precision, 3),
                    "полнота": round(recall, 3),
                    "f1": round(f1, 3),
                    "ложных": said - hit,
                    "пропущено": real - hit,
                },
            )
    return (None, {}) if best[0] < 0 else (best[1], best[2])


def yield_of(
    scores: Sequence[float], labels: Sequence[int], low: float | None, high: float | None
) -> dict:
    """Что гейт снял бы с модели и где при этом соврал.

    Главное число всей затеи: AUROC говорит, что модель различает тексты, а эта
    доля — сколько вызовов не случится. Рядом цена: сколько решено неверно.
    """
    if low is None or high is None or not labels:
        return {}
    yes = [(value, label) for value, label in zip(scores, labels) if value >= high]
    no = [(value, label) for value, label in zip(scores, labels) if value <= low]
    decided = len(yes) + len(no)
    wrong = sum(1 for _, label in yes if not label) + sum(1 for _, label in no if label)
    return {
        "решено_без_модели": decided,
        "доля": round(decided / len(labels), 3),
        "решено_да": len(yes),
        "решено_нет": len(no),
        "неверно": wrong,
    }


__all__ = (
    "GRID",
    "auroc",
    "choose",
    "class_weight",
    "decision",
    "counts",
    "predict",
    "score",
    "sigmoid",
    "train",
    "yield_of",
)
