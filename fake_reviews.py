"""Детекция накрученных отзывов на уровне одного отзыва.

Что здесь есть: нормализация текста, шинглы, хэш, сигналы 1–11 из проектного
документа и итоговый `fake_score`. Чего здесь нет: сети, модели и решений о
компании — агрегаты живут в `fake_company.py`, необязательный сигнал модели в
`fake_llm.py`.

Вердикт выносят правила, не модель [CORE-015]. Ответ модели приходит сюда одним
булевым сигналом минимального веса и сам по себе не может пометить отзыв
[CORE-019]. Без модели всё считается полностью [CORE-017].

Доказать заказной отзыв нельзя, поэтому наружу уходит не «фейк», а метка
«похоже на заказной» вместе со списком сработавших сигналов: вывод без
перечисления сигналов невозможно перепроверить.

Данные авторов не нужны: сравниваются тексты и хэши [CORE-012], [CORE-013].
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Sequence

import aitext
import aitext_rules as AI
import fake_rules as R
from dossier_rules import PATTERN_RULES, POSITIVE_MARKERS, SITE_TRUST
from reviewitems import ReviewItem

log = logging.getLogger("fuckhr")


@dataclass(frozen=True)
class Verdict:
    """Оценка одного отзыва. `signals` — коды из fake_rules.SIGNALS."""

    index: int
    url: str = ""
    text_hash: str = ""
    score: float = 0.0
    signals: tuple[str, ...] = ()
    label: str = R.LABEL_CLEAN

    @property
    def weight(self) -> float:
        """Вес отзыва в средней оценке: заказные не учитываются вовсе."""
        return R.LABEL_WEIGHT.get(self.label, 1.0)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(R.SIGNALS[code][1] for code in self.signals if code in R.SIGNALS)


def normalize(text: str) -> str:
    """Текст без регистра, пунктуации и лишних пробелов — основа сравнений."""
    return " ".join(R.WORD_RE.findall((text or "").lower()))


def text_hash(text: str) -> str:
    """Хэш нормализованного текста. Им ловятся фабрики отзывов между компаниями."""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


def shingles(text: str, size: int = R.SHINGLE_WORDS) -> frozenset[str]:
    words = normalize(text).split()
    if len(words) < size:
        return frozenset([" ".join(words)]) if words else frozenset()
    return frozenset(" ".join(words[i:i + size]) for i in range(len(words) - size + 1))


def similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """Доля общих шинглов от меньшего текста: короткая вставка тоже видна."""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def label_of(score: float) -> str:
    if score >= R.FAKE_AT:
        return R.LABEL_FAKE
    if score >= R.SUSPECT_AT:
        return R.LABEL_SUSPECT
    return R.LABEL_CLEAN


def _has_red_text(low: str) -> bool:
    return any(
        needle in low
        for _code, _label, polarity, _weight, needles in PATTERN_RULES
        if polarity == "red"
        for needle in needles
    )


def _has_praise(low: str) -> bool:
    if any(marker in low for marker in POSITIVE_MARKERS):
        return True
    return any(
        needle in low
        for _code, _label, polarity, _weight, needles in PATTERN_RULES
        if polarity == "green"
        for needle in needles
    )


_AD_RE = tuple(
    re.compile(r"(?<![а-яёa-z]){}".format(re.escape(tail)), re.IGNORECASE)
    for tail in R.AD_TAILS
)


def _ad_tail(low: str) -> bool:
    """Рекламный хвост ищется по границе слова: «реакция» — не «акция»."""
    return any(pattern.search(low) for pattern in _AD_RE)


def _thin_crowd(signals: dict[int, list[str]], total: int) -> None:
    """Гасит корпусные сигналы, которые есть у большинства выборки.

    У компании с сотнями отзывов «ровная длина» и «похож на соседа» находятся
    случайно, и вместе с любым третьим сигналом выносили отзыв в «заказной».
    Сигнал, который стоит у всех, ничего не различает [CORE-019].
    """
    if total < R.CROWD_MIN_ITEMS:
        return
    for code in R.CROWD_SIGNALS:
        hits = [index for index, codes in signals.items() if code in codes]
        if len(hits) / total <= R.CROWD_SHARE:
            continue
        log.info(
            "сигнал «%s» есть у %s из %s отзывов — не улика, не считаем",
            R.SIGNALS[code][1],
            len(hits),
            total,
        )
        for index in hits:
            signals[index].remove(code)


def _bursts(items: Sequence[ReviewItem]) -> set[int]:
    """Индексы отзывов, попавших в окно всплеска.

    Считаются только отзывы с известным днём: по «март 2026» окно в неделю не
    строится, и приписывать такому отзыву всплеск нечестно.
    """
    dated = [
        (date.fromisoformat(item.dated_at), item.index)
        for item in items
        if item.dated_by_day and item.dated_at
    ]
    dated.sort()
    hot: set[int] = set()
    for at, _index in dated:
        window = [idx for day, idx in dated if 0 <= (day - at).days < R.BURST_DAYS]
        if len(window) >= R.BURST_MIN:
            hot.update(window)
    return hot


def _uniform(items: Sequence[ReviewItem]) -> set[int]:
    """Индексы отзывов, у которых длина подозрительно ровная.

    Группа — это соседние по длине отзывы: у живых людей длина гуляет вдвое,
    у пачки, написанной по шаблону, — на проценты.
    """
    # Короткие тексты совпадают по длине случайно, поэтому в группу не идут.
    sized = sorted(
        ((len(item.text), item.index) for item in items if len(item.text) >= R.SHORT_CHARS),
        reverse=True,
    )
    flat: set[int] = set()
    for start in range(len(sized)):
        group = [sized[start]]
        for length, index in sized[start + 1:]:
            if group[0][0] and abs(length - group[0][0]) / group[0][0] <= R.UNIFORM_SPREAD:
                group.append((length, index))
        if len(group) >= R.UNIFORM_MIN:
            flat.update(index for _length, index in group)
    return flat


def _item_signals(
    item: ReviewItem,
    low: str,
    *,
    single_site: bool,
    trust: Mapping[str, float],
    ai_llm: bool = False,
) -> list[str]:
    signals: list[str] = []
    text = item.text

    if not R.NUMBER_RE.search(text) and not any(w in low for w in R.SPECIFIC_WORDS):
        signals.append("no_specifics")
    if len(text) <= R.CLICHE_CHARS:
        if sum(1 for phrase in R.CLICHES if phrase in low) >= R.CLICHE_MIN:
            signals.append("cliches")
    if item.cons and normalize(item.cons) in {normalize(x) for x in R.EMPTY_CONS}:
        signals.append("empty_cons")
    if item.rating is not None and item.rating >= R.TOP_RATING and len(text) < R.SHORT_CHARS:
        signals.append("short_top")
    if item.rating is not None:
        if item.rating >= R.TOP_RATING and _has_red_text(low):
            signals.append("rating_mismatch")
        elif item.rating <= R.LOW_RATING and not _has_red_text(low):
            # Похвала в отдельном поле «плюсы» при низкой оценке — это формат
            # площадки, а не противоречие: «Не били палкой, ДМС» с оценкой 1.0
            # написал живой человек. Смотрим только на свободный текст.
            free = " ".join(part for part in (item.cons, item.body) if part).lower()
            if _has_praise(free if item.pros else low):
                signals.append("rating_mismatch")
    if _ad_tail(low):
        signals.append("ad_tail")
    if any(phrase in low for phrase in R.IMPERSONAL):
        signals.append("impersonal")
    if single_site and trust.get(item.site, 1.0) < R.LOW_TRUST:
        signals.append("low_trust_site")
    # Генерация — отдельное явление от копипасты: шинглы её не ловят. Короткие
    # и старые отзывы aitext не оценивает и сигнала не даёт.
    if aitext.assess(text, AI.REVIEW, item.dated_at, llm=ai_llm).flagged:
        signals.append("ai_text")
    return signals


def score_items(
    items: Sequence[ReviewItem],
    *,
    known_hashes: Mapping[str, str] | None = None,
    llm_ads: Iterable[int] = (),
    ai_texts: Iterable[int] = (),
    near_pairs: Iterable[tuple[int, int]] = (),
    trust: Mapping[str, float] | None = None,
) -> tuple[Verdict, ...]:
    """Считает fake_score каждому отзыву компании.

    `known_hashes` — хэши отзывов о *других* компаниях: дословное совпадение с
    ними и есть след фабрики отзывов.

    `near_pairs` — пары `item.index`, признанные пересказом друг друга по
    векторам (ADR-021). Считаются снаружи по той же причине, что и `llm_ads`:
    здесь нет ни сети, ни модели, только правила [CORE-015].
    """
    items = tuple(items)
    if not items:
        return ()
    trust = trust or SITE_TRUST
    known_hashes = known_hashes or {}
    ads = set(llm_ads)
    paraphrased = {index for pair in near_pairs for index in pair}
    generated = set(ai_texts)
    single_site = len({item.site for item in items if item.site}) <= 1
    hot = _bursts(items)
    flat = _uniform(items)
    prints = [shingles(item.text) for item in items]

    collected: dict[int, list[str]] = {}
    digests: dict[int, str] = {}
    for position, item in enumerate(items):
        low = item.text.lower()
        signals = _item_signals(
            item,
            low,
            single_site=single_site,
            trust=trust,
            ai_llm=item.index in generated,
        )
        digest = text_hash(item.text)
        if digest in known_hashes:
            signals.append("dup_other_company")
        elif any(
            similarity(prints[position], prints[other]) >= R.DUP_RATIO
            for other in range(len(items))
            if other != position
        ):
            signals.append("dup_same_company")
        if item.index in hot:
            signals.append("burst")
        if item.index in flat:
            signals.append("uniform_length")
        if item.index in ads:
            signals.append("llm_ad")
        # Перефраз не добавляется поверх дословного дубля: это одно наблюдение,
        # а не два, и вместе они выносили отзыв в «заказной» на ровном месте.
        if item.index in paraphrased and "dup_same_company" not in signals:
            signals.append("paraphrase")

        collected[position] = signals
        digests[position] = digest

    _thin_crowd(collected, len(items))

    verdicts: list[Verdict] = []
    for position, item in enumerate(items):
        signals = collected[position]
        total = sum(R.SIGNALS[code][0] for code in signals)
        score = round(min(1.0, total / R.SCORE_CAP), 2)
        label = label_of(score)
        if label == R.LABEL_FAKE and not (set(signals) & R.TEXT_SIGNALS):
            # Улик про сам текст нет — говорить «заказной» не на чем.
            # Отзыв остаётся сомнительным: половина веса вместо нуля.
            label = R.LABEL_SUSPECT
            score = min(score, R.FAKE_AT)
        verdicts.append(
            Verdict(
                index=item.index,
                url=item.url,
                text_hash=digests[position],
                score=score,
                signals=tuple(signals),
                label=label,
            )
        )
    return tuple(verdicts)


__all__ = (
    "Verdict",
    "label_of",
    "normalize",
    "score_items",
    "shingles",
    "similarity",
    "text_hash",
)
