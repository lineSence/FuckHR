"""Метка компании: накручивает отзывы или нет.

Считается по агрегатам, а не по одному отзыву: один шаблонный текст — это
неудачный автор, а треть таких текстов, всплеск в одно окно и расхождение
площадок — это уже поведение компании.

Метка живёт рядом со светофором риска и всегда раскрывается: какие признаки
сработали, сколько отзывов затронуто. Молчаливое «плохая компания» недопустимо,
поэтому каждый признак несёт свою формулировку и числа.

Правила симметричны. Заказным бывает и разгромный отзыв от конкурента, поэтому
ничто здесь не смотрит на знак оценки: чистится и хвала, и брань.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Sequence

import fake_rules as R
import review_area
from fake_reviews import Verdict, shingles, similarity
from reviewitems import ReviewItem


# Разбивка по сферам имеет смысл только на некоторой выборке: «по вашей сфере
# 1.0 из 5 по одному отзыву» выглядит как факт, а является шумом.
AREA_MIN_TOTAL = 5
AREA_MIN_ITEMS = 2


@dataclass(frozen=True)
class AreaMark:
    """Отзывы своей сферы отдельно от остальных.

    `avg` считается только по своей сфере и с тем же весом метки, что общая
    средняя: заказной отзыв не тянет ни в одну сторону. Отзывы про компанию
    целиком (задержки зарплаты, сокращения) сюда не попадают — они в `wide` и
    важны независимо от сферы.
    """

    code: str = ""
    total: int = 0
    avg: float | None = None
    wide: int = 0
    other: int = 0
    unknown: int = 0

    @property
    def label(self) -> str:
        return review_area.label(self.code)


@dataclass(frozen=True)
class Sign:
    code: str
    text: str


@dataclass(frozen=True)
class CompanyMark:
    """Итог по компании: уровень метки, признаки и обе средние оценки."""

    level: str = R.MARK_NONE
    signs: tuple[Sign, ...] = ()
    total: int = 0
    fake: int = 0
    suspect: int = 0
    avg_all: float | None = None
    avg_clean: float | None = None
    area: AreaMark | None = None

    @property
    def label(self) -> str:
        return R.MARK_RU.get(self.level, self.level)

    @property
    def flagged(self) -> bool:
        return self.level == R.MARK_FAKE

    @property
    def clean_weight(self) -> float:
        """Сколько «целых» отзывов осталось после чистки."""
        return self.total - self.fake - self.suspect * 0.5


def weighted_average(
    items: Sequence[ReviewItem], verdicts: Sequence[Verdict]
) -> float | None:
    """Средняя оценка с учётом веса метки: заказные — ноль, сомнительные — половина."""
    weights = {v.index: v.weight for v in verdicts}
    total = 0.0
    weight = 0.0
    for item in items:
        if item.rating is None:
            continue
        w = weights.get(item.index, 1.0)
        total += item.rating * w
        weight += w
    return round(total / weight, 2) if weight else None


def plain_average(items: Sequence[ReviewItem]) -> float | None:
    values = [item.rating for item in items if item.rating is not None]
    return round(sum(values) / len(values), 2) if values else None


def by_area(
    items: Sequence[ReviewItem], verdicts: Sequence[Verdict], area: str
) -> AreaMark | None:
    """Своя сфера отдельной цифрой. Мало данных — None, ничего не показываем."""
    code = review_area.owner_code(area)
    if not code or len(items) < AREA_MIN_TOTAL:
        return None
    mine: list[ReviewItem] = []
    wide = other = unknown = 0
    for item in items:
        item_area = str(getattr(item, "area", review_area.AREA_UNKNOWN))
        if str(getattr(item, "area_scope", review_area.SCOPE_AREA)) == review_area.SCOPE_COMPANY:
            wide += 1
        elif item_area == code:
            mine.append(item)
        elif item_area == review_area.AREA_UNKNOWN:
            unknown += 1
        else:
            other += 1
    if len(mine) < AREA_MIN_ITEMS:
        return None
    return AreaMark(
        code=code,
        total=len(mine),
        avg=weighted_average(mine, verdicts),
        wide=wide,
        other=other,
        unknown=unknown,
    )


def _share_sign(verdicts: Sequence[Verdict]) -> Sign | None:
    total = len(verdicts)
    fake = sum(1 for v in verdicts if v.label == R.LABEL_FAKE)
    if total < R.SHARE_MIN_REVIEWS or not fake:
        return None
    share = fake / total
    if share <= R.FAKE_SHARE:
        return None
    return Sign(
        "fake_share",
        "похожи на заказные {} отзыва из {} ({:.0%})".format(fake, total, share),
    )


def _group_sign(items: Sequence[ReviewItem]) -> Sign | None:
    """Группа похожих отзывов, написанных в одно окно."""
    dated = [i for i in items if i.dated_by_day and i.dated_at]
    if len(dated) < R.GROUP_MIN:
        return None
    prints = {i.index: shingles(i.text) for i in dated}
    for anchor in dated:
        start = date.fromisoformat(anchor.dated_at or "")
        group = [
            other
            for other in dated
            if 0 <= (date.fromisoformat(other.dated_at or "") - start).days < R.BURST_DAYS
            and similarity(prints[anchor.index], prints[other.index]) >= R.DUP_RATIO
        ]
        if len(group) >= R.GROUP_MIN:
            return Sign(
                "group",
                "{} похожих отзыва написаны за {} дней с {}".format(
                    len(group), R.BURST_DAYS, start.isoformat()
                ),
            )
    return None


def _bimodal_sign(items: Sequence[ReviewItem], verdicts: Sequence[Verdict]) -> Sign | None:
    rated = [i for i in items if i.rating is not None]
    if len(rated) < R.BIMODAL_MIN_REVIEWS:
        return None
    labels = {v.index: v.label for v in verdicts}
    tops = [i for i in rated if i.rating and i.rating >= R.TOP_RATING]
    lows = [i for i in rated if i.rating and i.rating <= R.LOW_RATING]
    share = (len(tops) + len(lows)) / len(rated)
    if share <= R.BIMODAL_SHARE or not tops:
        return None
    dirty = sum(1 for i in tops if labels.get(i.index, R.LABEL_CLEAN) != R.LABEL_CLEAN)
    if dirty < len(tops) * 0.8:
        return None
    return Sign(
        "bimodal",
        "{:.0%} оценок — крайние, и почти все пятёрки помечены".format(share),
    )


def _site_sign(items: Sequence[ReviewItem], verdicts: Sequence[Verdict]) -> Sign | None:
    """Расхождение площадок: где выше средняя, там и подозрительные отзывы."""
    by_site: dict[str, list[float]] = defaultdict(list)
    for item in items:
        if item.rating is not None and item.site:
            by_site[item.site].append(item.rating)
    if len(by_site) < 2:
        return None
    means = {site: sum(v) / len(v) for site, v in by_site.items()}
    best = max(means, key=lambda s: means[s])
    others = [m for site, m in means.items() if site != best]
    if means[best] - (sum(others) / len(others)) < R.SITE_GAP:
        return None
    labels = {v.index: v.label for v in verdicts}
    dirty = Counter(
        item.site
        for item in items
        if labels.get(item.index, R.LABEL_CLEAN) != R.LABEL_CLEAN
    )
    if not dirty or dirty.most_common(1)[0][0] != best:
        return None
    return Sign(
        "site_gap",
        "на {} средняя выше остальных на {:.1f} балла, там же подозрительные отзывы".format(
            best, means[best] - (sum(others) / len(others))
        ),
    )


def _cross_sign(verdicts: Sequence[Verdict]) -> Sign | None:
    hits = sum(1 for v in verdicts if "dup_other_company" in v.signals)
    if not hits:
        return None
    return Sign(
        "cross_dup",
        "{} отзыв(а) дословно совпадают с отзывами о других компаниях".format(hits),
    )


def evaluate(
    items: Sequence[ReviewItem], verdicts: Sequence[Verdict], area: str = ""
) -> CompanyMark:
    """Признаки накрутки и уровень метки. Один признак — подозрение, два — метка."""
    items = tuple(items)
    verdicts = tuple(verdicts)
    if not items:
        return CompanyMark()

    signs = [
        sign
        for sign in (
            _share_sign(verdicts),
            _group_sign(items),
            _bimodal_sign(items, verdicts),
            _site_sign(items, verdicts),
            _cross_sign(verdicts),
        )
        if sign is not None
    ]
    level = R.MARK_NONE
    if len(signs) == 1:
        level = R.MARK_SUSPECT
    elif len(signs) >= 2:
        level = R.MARK_FAKE

    return CompanyMark(
        level=level,
        signs=tuple(signs),
        total=len(items),
        fake=sum(1 for v in verdicts if v.label == R.LABEL_FAKE),
        suspect=sum(1 for v in verdicts if v.label == R.LABEL_SUSPECT),
        avg_all=plain_average(items),
        avg_clean=weighted_average(items, verdicts),
        area=by_area(items, verdicts, area),
    )


__all__ = (
    "AreaMark",
    "CompanyMark",
    "Sign",
    "by_area",
    "evaluate",
    "plain_average",
    "weighted_average",
)
