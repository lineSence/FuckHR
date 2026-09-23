"""Легитимность отзыва: это отзыв сотрудника или мусор со страницы.

Три вопроса решаются порознь, и здесь — первый из них:

    легитимность  — это вообще отзыв о работе в этой компании?  (здесь)
    накрутка      — отзыв настоящий, но заказной?               (fake_reviews)
    генерация     — как он написан?                             (aitext)

Порядок обязателен. Обрывок меню — это гарантированные «нет проверяемых
деталей» и «длина как у соседей», то есть накрутка на ровном месте. Поэтому
мусор отсеивается до всех остальных сигналов.

Вёрстку площадок по-прежнему не разбираем: правила говорят о смысле текста и
переживают редизайн, селекторы — нет [CORE-014].

Шаблонные строки сайта не ведутся словарём. Строка, встреченная на страницах
трёх разных компаний одной площадки, и есть шаблон — это считает
reviewlegit_store по факту, и оно не устаревает после редизайна.

Отброшенное не исчезает молча: причина и счётчик доходят до интерфейса, иначе
поломку разбора видно только по внезапно опустевшему досье [CORE-017].
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Sequence

from rapidfuzz import fuzz

import company_key
import reviewlegit_rules as R


@dataclass(frozen=True)
class Check:
    """Итог проверки одного текста."""

    ok: bool = True
    score: float = 0.0
    reasons: tuple[str, ...] = ()

    @property
    def why(self) -> str:
        parts = [
            R.MINUS[code][1] for code in self.reasons if code in R.MINUS
        ]
        return "; ".join(parts)


def line_hash(text: str) -> str:
    """Хэш строки для поиска шаблонов площадки. Регистр и пробелы не в счёт."""
    return hashlib.sha256(" ".join((text or "").lower().split()).encode()).hexdigest()[:32]


def company_on_page(text: str, company: str) -> bool:
    """Есть ли на странице название нашей компании.

    Страница без него — чужая: поиск часто приводит на подборку «отзывы о
    работодателях города», где каждый отзыв про своего работодателя.
    """
    name = company_key.normalize(company)
    if len(name) < R.COMPANY_MIN_CHARS:
        return True  # слишком короткое название, проверка дала бы шум
    haystack = company_key.normalize(text or "")
    if not haystack:
        return False
    if name in haystack:
        return True
    return fuzz.partial_ratio(name, haystack) >= R.COMPANY_MATCH


def _navigation(text: str) -> bool:
    separators = sum(text.count(mark) for mark in R.SEPARATORS)
    sentences = sum(text.count(mark) for mark in ".!?")
    return separators > R.MAX_SEPARATORS and sentences < R.MIN_SENTENCE_MARKS


def signals_of(item: object, boilerplate: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """Признаки «за» и «против» для одного отзыва."""
    text = str(getattr(item, "text", "") or "")
    low = text.lower()
    plus: list[str] = []
    minus: list[str] = []

    if any(word in low for word in R.WORK_WORDS):
        plus.append("work_words")
    else:
        minus.append("no_work_context")
    if any(word in low for word in R.FIRST_PERSON):
        plus.append("first_person")
    if getattr(item, "pros", "") and getattr(item, "cons", ""):
        plus.append("pros_cons")
    if getattr(item, "rating", None) is not None:
        plus.append("has_rating")
    if getattr(item, "dated_at", None):
        plus.append("has_date")

    if any(word in low for word in R.CUSTOMER_WORDS) and "work_words" not in plus:
        minus.append("customer")
    if any(word in low for word in R.REPLY_WORDS):
        minus.append("employer_reply")
    if any(word in low for word in R.READER_WORDS):
        minus.append("reader_address")
    if R.PHONE_RE.search(text) or R.PRICE_RE.search(text) or R.URL_RE.search(text):
        minus.append("promo")
    if _navigation(text):
        minus.append("navigation")
    if line_hash(text) in set(boilerplate):
        minus.append("boilerplate")
    return plus, minus


def assess(item: object, boilerplate: Iterable[str] = ()) -> Check:
    """Проверка одного отзыва. Жёсткая причина отбрасывает сразу."""
    plus, minus = signals_of(item, boilerplate)
    score = round(
        sum(R.PLUS[code][0] for code in plus) - sum(R.MINUS[code][0] for code in minus), 2
    )
    hard = [code for code in minus if code in R.HARD]
    ok = not hard and score >= R.ACCEPT_AT
    return Check(ok=ok, score=score, reasons=tuple(minus))


def filter_items(
    items: Sequence[object], boilerplate: Iterable[str] = ()
) -> tuple[tuple[object, ...], tuple[tuple[object, Check], ...]]:
    """Делит отзывы на принятые и отброшенные. Номера остаются сквозными."""
    marks = frozenset(boilerplate)
    kept: list[object] = []
    dropped: list[tuple[object, Check]] = []
    for item in items:
        check = assess(item, marks)
        if check.ok:
            kept.append(item)
        else:
            dropped.append((item, check))
    return tuple(kept), tuple(dropped)


__all__ = ("Check", "assess", "company_on_page", "filter_items", "line_hash", "signals_of")
