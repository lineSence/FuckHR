"""Одна контора — одно досье, как бы её ни написали в вакансии.

hh отдаёт одну и ту же компанию как «ООО «Ромашка»», «Ромашка» и «Ромашка,
ООО». До сих пор это были три разных записи в company_dossier: три прогона
поиска из потолка SEARCH_MAX_CALLS, три порции отзывов и три несогласных
светофора на соседних карточках.

Почему не просто lower(). Правовая форма стоит то слева, то справа, кавычки
бывают трёх видов, а где-то в конце висит «(группа компаний)». Сначала
чистим детерминированно [CORE-015], и только остаток сравниваем нечётко.

Порог высокий сознательно. Склеить две разные конторы хуже, чем прозевать
дубль: в первом случае чужие красные флаги прилипают к невиновному
работодателю, во втором просто тратится лишний запрос.
"""

from __future__ import annotations

import re
from typing import Iterable

from rapidfuzz import fuzz, process

# Совпадение ниже этого — разные компании.
THRESHOLD = 92

# Правовые формы и хвосты, которые ничего не говорят о том, кто это.
LEGAL_FORMS = (
    "ооо", "зао", "пао", "оао", "ао", "ип", "нко", "ано", "гк", "тоо",
    "llc", "ltd", "inc", "gmbh", "corp", "co", "plc", "sa", "bv", "ag",
)

# Приписки вроде «группа компаний»: в одной вакансии есть, в соседней нет.
NOISE = (
    "группа компаний", "группа компаний»", "холдинг", "компания",
    "group of companies", "holding",
)

QUOTES_RE = re.compile(r"[\"'\u00ab\u00bb\u201c\u201d\u201e\u2018\u2019`]")
PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
SPACES_RE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Название без кавычек, правовой формы и прочего шума."""
    low = (name or "").lower().replace("\u0451", "е")
    low = QUOTES_RE.sub(" ", low)
    for noise in NOISE:
        low = low.replace(noise, " ")
    low = PUNCT_RE.sub(" ", low)
    words = [w for w in SPACES_RE.split(low) if w and w not in LEGAL_FORMS]
    return " ".join(words)


def same(left: str, right: str, threshold: int = THRESHOLD) -> bool:
    """Один ли это работодатель."""
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return fuzz.token_sort_ratio(a, b) >= threshold


def best_match(
    name: str, known: Iterable[str], threshold: int = THRESHOLD
) -> str | None:
    """Ближайшее известное название или None, если такого работодателя ещё нет."""
    target = normalize(name)
    if not target:
        return None
    choices = [c for c in known if normalize(c)]
    if not choices:
        return None
    found = process.extractOne(
        target,
        choices,
        scorer=fuzz.token_sort_ratio,
        processor=normalize,
        score_cutoff=threshold,
    )
    return None if found is None else str(found[0])


__all__ = ("THRESHOLD", "normalize", "same", "best_match")
