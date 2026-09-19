"""Детектор сгенерированного текста в вакансиях и отзывах.

Считает Python по словарям и статистике, без модели [CORE-015]. Ответ модели —
необязательное слагаемое веса 1.0 (aitext_llm.py), и без него всё работает
[CORE-017].

Чего этот детектор не делает. Он не выносит вердикт «написано нейросетью»:
детекторы ИИ-текста ненадёжны, а наши тексты — худший для них случай (короткие,
канцелярские, шаблонные, на русском). Поэтому формулировки в интерфейсе говорят
о свойствах текста («шаблонный», «проверять нечего»), а не о его происхождении,
и один сигнал никогда не помечает текст сам по себе.

Два предохранителя от ложных срабатываний. Короче MIN_CHARS текст не
оценивается вовсе: на трёх строчках ровная пунктуация — не признак, а
случайность. Известная дата раньше HUMAN_BEFORE тоже снимает оценку: тогда
генерация была редкостью. Неизвестная дата предохранителем не считается —
«даты нет» не значит «текст старый».
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev

import aitext_rules as R


@dataclass(frozen=True)
class Verdict:
    """Оценка одного текста. `signals` — коды из aitext_rules.SIGNALS."""

    score: float = 0.0
    label: str = R.LABEL_SKIP
    signals: tuple[str, ...] = ()
    chars: int = 0

    @property
    def label_ru(self) -> str:
        return R.LABEL_RU.get(self.label, self.label)

    @property
    def flagged(self) -> bool:
        """Есть ли о чём говорить владельцу."""
        return self.label in (R.LABEL_SUSPECT, R.LABEL_LIKELY)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(R.SIGNALS[code][1] for code in self.signals if code in R.SIGNALS)

    def line(self) -> str:
        """Строка карточки. Метка без причин бесполезна и потому не выводится."""
        if not self.flagged:
            return ""
        return "Текст: {} — {}".format(self.label_ru, "; ".join(self.reasons))


def label_of(score: float) -> str:
    if score >= R.LIKELY_AT:
        return R.LABEL_LIKELY
    if score >= R.SUSPECT_AT:
        return R.LABEL_SUSPECT
    return R.LABEL_HUMAN


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in R.SENTENCE_RE.findall(text) if len(s.strip()) > 1]


def _bullets(text: str) -> list[str]:
    return [b.strip() for b in R.BULLET_RE.findall(text) if b.strip()]


def _form(word: str) -> str:
    """Грубая форма первого слова пункта: существительное, инфинитив, прочее.

    Морфологии в проекте нет и не нужно: важно не определить часть речи, а
    заметить, что все пункты списка скроены по одному шаблону.
    """
    low = word.lower()
    if low.endswith(("ние", "ция", "ость", "ика", "ка", "ство")):
        return "сущ"
    if low.endswith(("ать", "ить", "еть", "ыть", "ти")):
        return "инф"
    if low.endswith(("ем", "им", "ете", "ите", "ешь")):
        return "глаг"
    return "прочее"


def _spread(values: list[int]) -> float:
    """Коэффициент вариации. Ноль — все значения одинаковы."""
    average = mean(values)
    return pstdev(values) / average if average else 0.0


def _uniform_bullets(text: str) -> bool:
    items = _bullets(text)
    if len(items) < R.MIN_BULLETS:
        return False
    if _spread([len(item) for item in items]) > R.BULLET_SPREAD:
        return False
    forms = [_form(R.WORD_RE.findall(item)[0]) for item in items if R.WORD_RE.findall(item)]
    if not forms:
        return False
    top = max(set(forms), key=forms.count)
    return forms.count(top) / len(forms) >= R.BULLET_FORM_SHARE


def _too_clean(text: str, low: str) -> bool:
    """Живой отзыв почти всегда оставляет след: сленг, эмодзи, капс, многоточие."""
    if any(mark in low for mark in R.HUMAN_MARKS):
        return False
    return not (
        R.EMOJI_RE.search(text) or R.ELLIPSIS_RE.search(text) or R.CAPS_RE.search(text)
    )


def signals_of(text: str, kind: str = R.VACANCY, llm: bool = False) -> list[str]:
    """Коды сработавших признаков. Порядок — по убыванию веса сигнала."""
    low = text.lower()
    per_1000 = len(text) / 1000 or 1.0
    found: list[str] = []

    if len(R.SPECIFIC_RE.findall(text)) / per_1000 < R.SPECIFIC_PER_1000:
        found.append("no_specifics")

    markers = sum(1 for phrase in R.MARKERS if phrase in low)
    markers += len(R.PAIR_RE.findall(text))
    if markers / per_1000 >= R.MARKER_PER_1000:
        found.append("markers")

    if _uniform_bullets(text):
        found.append("uniform_bullets")

    sentences = _sentences(text)
    if len(sentences) >= R.MIN_SENTENCES:
        if _spread([len(s) for s in sentences]) < R.EVEN_SPREAD:
            found.append("even_sentences")
        openings = [
            R.WORD_RE.findall(s)[0].lower() for s in sentences if R.WORD_RE.findall(s)
        ]
        if openings:
            top = max(set(openings), key=openings.count)
            if openings.count(top) >= R.SAME_OPENING_MIN:
                found.append("same_openings")

    if len(R.TRIAD_RE.findall(text)) >= R.TRIAD_MIN:
        found.append("triads")

    # Отсутствие человеческих следов — признак только в отзыве: вакансия и
    # должна быть написана ровно, это официальный текст.
    if kind == R.REVIEW and _too_clean(text, low):
        found.append("too_clean")

    if len(R.DASH_RE.findall(text)) / per_1000 >= R.DASH_PER_1000:
        found.append("dashes")

    if llm:
        found.append("llm_generated")
    return found


def assess(
    text: str,
    kind: str = R.VACANCY,
    published_at: str | None = None,
    llm: bool = False,
) -> Verdict:
    """Оценка одного текста. Слишком короткий или старый — без оценки."""
    text = (text or "").strip()
    if len(text) < R.MIN_CHARS:
        return Verdict(label=R.LABEL_SKIP, chars=len(text))
    if published_at and str(published_at)[:10] < R.HUMAN_BEFORE:
        return Verdict(label=R.LABEL_SKIP, chars=len(text))

    found = signals_of(text, kind, llm)
    total = sum(R.SIGNALS[code][0] for code in found)
    score = round(min(1.0, total / R.SCORE_CAP), 2)
    return Verdict(score=score, label=label_of(score), signals=tuple(found), chars=len(text))


def row_line(row: object) -> str:
    """Строка карточки из сохранённой вакансии, без пересчёта текста."""
    label = str(row["ai_label"] or "")  # type: ignore[index]
    if label not in (R.LABEL_SUSPECT, R.LABEL_LIKELY):
        return ""
    reasons = str(row["ai_signals"] or "")  # type: ignore[index]
    text = "Текст: {}".format(R.LABEL_RU.get(label, label))
    if reasons:
        text += " — {}".format(reasons)
    return text


__all__ = ("Verdict", "assess", "label_of", "row_line", "signals_of")
