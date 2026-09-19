"""Разбор текста отзывов: тональность, цитаты, закономерности.

Вынесено из dossier.py по [CORE-024]: там осталась сборка досье, здесь — работа
со словами. Ни сети, ни модели, ни базы: всё считается детерминированно на
словарях из dossier_rules [CORE-015].

Отрицания. Маркеры и признаки ищутся подстрокой, поэтому «рекомендую» лежит
внутри «не рекомендую», а «платят вовремя» — внутри «не платят вовремя». Перед
зачётом позитивного совпадения проверяется префикс отрицания, иначе злой отзыв
становится mixed и перестаёт красить работодателя.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from dossier_rules import (
    MAX_QUOTE_CHARS,
    NEGATION_PREFIXES,
    NEGATION_WINDOW,
    NEGATIVE_MARKERS,
    PATTERN_RULES,
    POSITIVE_MARKERS,
)

if TYPE_CHECKING:
    from dossier import Review


@dataclass(frozen=True)
class Pattern:
    """Закономерность: не один злой отзыв, а повторяющаяся жалоба."""

    code: str
    label: str
    polarity: str
    weight: int
    hits: int
    quotes: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        """Закономерность — от двух упоминаний или одного тяжёлого признака."""
        return self.hits >= 2 or self.weight >= 4


def is_negated(low: str, at: int) -> bool:
    """Стоит ли отрицание прямо перед совпадением.

    Смотрим узкое окно слева: «не платят вовремя» — отрицание, а «платят
    вовремя, не придраться» — нет.
    """
    before = low[max(0, at - NEGATION_WINDOW):at]
    return any(before.endswith(prefix) for prefix in NEGATION_PREFIXES)


def count_markers(markers: Sequence[str], low: str, *, skip_negated: bool) -> int:
    """Сколько маркеров нашлось. Под отрицанием позитивные не считаются."""
    total = 0
    for marker in markers:
        start = 0
        while True:
            at = low.find(marker, start)
            if at < 0:
                break
            start = at + len(marker)
            if skip_negated and is_negated(low, at):
                continue
            total += 1
    return total


def matched_needle(
    low: str, needles: Sequence[str], *, skip_negated: bool
) -> str | None:
    """Первый сработавший признак или None. Отрицания пропускаются."""
    for needle in needles:
        start = 0
        while True:
            at = low.find(needle, start)
            if at < 0:
                break
            start = at + len(needle)
            if skip_negated and is_negated(low, at):
                continue
            return needle
    return None


def polarity_of(text: str) -> str:
    """Грубая тональность без модели: маркеры плюс признаки закономерностей."""
    low = (text or "").lower()
    negative = count_markers(NEGATIVE_MARKERS, low, skip_negated=False)
    positive = count_markers(POSITIVE_MARKERS, low, skip_negated=True)
    for _code, _label, pol, weight, needles in PATTERN_RULES:
        # Зелёные признаки под отрицанием не считаются: «не платят вовремя» —
        # это жалоба, а не похвала.
        found = matched_needle(low, needles, skip_negated=pol == "green")
        if not found:
            continue
        if pol == "red":
            negative += 1 if weight < 4 else 2
        else:
            positive += 1
    if negative and positive:
        return "mixed"
    if negative:
        return "negative"
    if positive:
        return "positive"
    return "unknown"


def _quote(text: str, needle: str) -> str:
    """Кусок текста вокруг признака: без цитаты вывод нельзя проверить."""
    low = text.lower()
    at = low.find(needle)
    if at < 0:
        return text[:MAX_QUOTE_CHARS].strip()
    start = max(0, at - 80)
    end = min(len(text), at + len(needle) + 120)
    piece = text[start:end].strip()
    if start:
        piece = "…" + piece
    if end < len(text):
        piece = piece + "…"
    return piece[:MAX_QUOTE_CHARS]


def find_patterns(reviews: Sequence["Review"]) -> tuple[Pattern, ...]:
    """Ищет повторяющиеся сюжеты по всем отзывам сразу.

    Считаются отзывы, а не встреченные слова: один эмоциональный текст с пятью
    упоминаниями переработок — это один голос, а не закономерность.

    Зелёные признаки под отрицанием не засчитываются: иначе отзыв «не платят
    вовремя» давал бы green-флаг и подкрашивал risk_level в зелёный.
    """
    out: list[Pattern] = []
    for code, label, polarity, weight, needles in PATTERN_RULES:
        hits = 0
        quotes: list[str] = []
        for review in reviews:
            text = review.text
            low = text.lower()
            matched = matched_needle(low, needles, skip_negated=polarity == "green")
            if not matched:
                continue
            hits += 1
            if len(quotes) < 3:
                quotes.append(_quote(text, matched))
        if hits:
            out.append(
                Pattern(
                    code=code,
                    label=label,
                    polarity=polarity,
                    weight=weight,
                    hits=hits,
                    quotes=tuple(quotes),
                )
            )
    out.sort(key=lambda p: (p.polarity != "red", -p.weight * p.hits))
    return tuple(out)


def average_rating(reviews: Sequence["Review"]) -> float | None:
    values = [r.rating for r in reviews if r.rating is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


__all__ = (
    "Pattern",
    "average_rating",
    "count_markers",
    "find_patterns",
    "is_negated",
    "matched_needle",
    "polarity_of",
)
