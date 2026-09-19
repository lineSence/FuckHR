"""Рыночная зарплата: наблюдение, срез, медиана, метка вакансии.

Что здесь считается и почему без модели. Медиана, перцентили и отклонение —
арифметика по собственным наблюдениям, и подмешивать сюда модель значило бы
испортить единственное проверяемое число в системе [CORE-015], [CORE-019].

Важная граница: это не рынок труда, а медиана нашей выборки с hh.ru за
окно. Поэтому наружу вместе с меткой всегда уходят срез, уровень каскада, окно
и число наблюдений: «ниже рынка» без этих чисел проверить нельзя.

Наблюдения снимаются в collector.py со всей выдачи до предфильтра: иначе
рынком считалось бы то, что уже прошло порог владельца, и метки «ниже рынка»
не существовало бы в принципе. Страница выдачи к этому моменту уже скачана,
новых запросов к hh.ru это не добавляет [CORE-016].

Открытые вилки. «от 200 000» берётся как нижняя граница — это занижает срез,
но честно. «до 200 000» в статистику не идёт вовсе: это чаще приманка, чем
зарплата, и такие вилки завышали бы медиану. Метка самой вакансии при этом
считается, просто её точка помечена как граница.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

import market_rules as R

CLOSED = "closed"
FROM_ONLY = "from"
TO_ONLY = "to"


@dataclass(frozen=True)
class Observation:
    """Одна зарплатная точка из выдачи. Сырые числа хранятся как есть."""

    key: str
    company: str = ""
    role: str = ""
    grade: str = ""
    geo: str = R.OTHER
    format: str = R.ONSITE
    low: int | None = None
    high: int | None = None
    point: int | None = None      # net, по NDFL
    range_kind: str = CLOSED
    published_at: str = ""

    @property
    def usable(self) -> bool:
        """Идёт ли точка в статистику. «до X» не идёт — см. модуль."""
        return self.point is not None and self.role != "" and self.range_kind != TO_ONLY


@dataclass(frozen=True)
class Stats:
    """Посчитанный срез. `level` — насколько пришлось огрубить ключ."""

    bucket: str
    level: int = 0
    count: int = 0
    median: float = 0.0
    p25: float = 0.0
    p75: float = 0.0

    @property
    def level_label(self) -> str:
        return R.LEVEL_RU[min(self.level, len(R.LEVEL_RU) - 1)]

    @property
    def enough(self) -> bool:
        return self.count >= R.MIN_OBSERVATIONS


@dataclass(frozen=True)
class Marker:
    """Метка вакансии вместе с числами, по которым она получена."""

    label: str = R.UNKNOWN
    point: int | None = None
    deviation: float | None = None
    stats: Stats | None = None

    @property
    def label_ru(self) -> str:
        return R.LABEL_RU.get(self.label, self.label)

    @property
    def points_share(self) -> float:
        return R.LABEL_POINTS.get(self.label, 0.7)

    def line(self) -> str:
        """Строка для карточки. Без чисел метку показывать нельзя [CORE-019]."""
        if self.label in (R.UNKNOWN, R.NO_SALARY) or self.stats is None:
            return "Рынок: {}".format(self.label_ru)
        return (
            "Рынок: {label} — {point} против медианы {median} (p25 {p25}, p75 {p75}), "
            "{bucket}, {count} вакансий за {days} дней"
        ).format(
            label=self.label_ru,
            point=_money(self.point),
            median=_money(self.stats.median),
            p25=_money(self.stats.p25),
            p75=_money(self.stats.p75),
            bucket=bucket_label(self.stats.bucket),
            count=self.stats.count,
            days=R.WINDOW_DAYS,
        )


def _money(value: float | int | None) -> str:
    if value is None:
        return "—"
    return "{:,.0f}".format(float(value)).replace(",", " ")


def net(value: int | None, gross: Any) -> int | None:
    """gross → net по единой ставке НДФЛ. Огрубление описано в market_rules."""
    if value is None:
        return None
    return int(value * R.NET_RATE) if gross else int(value)


def role_of(title: str) -> str:
    low = " {} ".format((title or "").lower().replace("ё", "е"))
    for family, words in R.ROLE_FAMILIES:
        if any(word in low for word in words):
            return family
    return ""


def grade_of(title: str, experience: str = "") -> str:
    low = (title or "").lower()
    for grade, words in R.GRADE_WORDS:
        if any(word in low for word in words):
            return grade
    return R.EXPERIENCE_GRADE.get(experience or "", "")


def geo_of(area: str) -> str:
    low = (area or "").lower()
    for code, cities in R.GEO_CITIES:
        if any(city in low for city in cities):
            return code
    return R.OTHER


def format_of(schedule: str, title: str = "") -> str:
    low = "{} {}".format(schedule or "", title or "").lower().replace("ё", "е")
    return R.REMOTE if ("удален" in low or "remote" in low) else R.ONSITE


def observe(vacancy: Any, today: date | None = None) -> Observation | None:
    """Вакансия из выдачи → зарплатная точка. Не-RUB отбрасывается."""
    currency = (getattr(vacancy, "currency", "") or "").upper()
    if currency and currency not in R.RUB:
        return None
    low_raw = getattr(vacancy, "salary_from", None)
    high_raw = getattr(vacancy, "salary_to", None)
    gross = getattr(vacancy, "gross", None)
    low = net(low_raw, gross)
    high = net(high_raw, gross)

    if low is not None and high is not None:
        kind, point = CLOSED, int((low + high) / 2)
    elif low is not None:
        kind, point = FROM_ONLY, low
    elif high is not None:
        kind, point = TO_ONLY, high
    else:
        kind, point = CLOSED, None

    title = getattr(vacancy, "title", "") or ""
    published = str(getattr(vacancy, "published_at", "") or "")[:10]
    return Observation(
        key=str(getattr(vacancy, "key", "") or ""),
        company=str(getattr(vacancy, "company", "") or ""),
        role=role_of(title),
        grade=grade_of(title, str(getattr(vacancy, "experience", "") or "")),
        geo=geo_of(str(getattr(vacancy, "area", "") or "")),
        format=format_of(str(getattr(vacancy, "schedule", "") or ""), title),
        low=low,
        high=high,
        point=point,
        range_kind=kind,
        published_at=published or (today or date.today()).isoformat(),
    )


def bucket_key(role: str, grade: str, geo: str, fmt: str, level: int = 0) -> str:
    """Ключ среза для уровня каскада. Снятые измерения остаются пустыми."""
    fields = {"role": role, "grade": grade, "geo": geo, "format": fmt}
    kept = R.CASCADE[min(level, len(R.CASCADE) - 1)]
    return "|".join(fields[name] if name in kept else "" for name in ("role", "grade", "geo", "format"))


def bucket_label(bucket: str) -> str:
    """Человеческая подпись среза: «python/middle/Москва/удалёнка»."""
    role, grade, geo, fmt = (bucket.split("|") + ["", "", "", ""])[:4]
    parts = [
        R.ROLE_RU.get(role, role or "роль не определена"),
        R.GRADE_RU.get(grade, grade) if grade else "",
        R.GEO_RU.get(geo, geo) if geo else "",
        R.FORMAT_RU.get(fmt, fmt) if fmt else "",
    ]
    return "/".join(part for part in parts if part)


def percentile(values: Sequence[float], share: float) -> float:
    """Перцентиль с линейной интерполяцией. Пустой ряд — ноль."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * share
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def summarize(bucket: str, level: int, values: Sequence[float]) -> Stats:
    return Stats(
        bucket=bucket,
        level=level,
        count=len(values),
        median=round(percentile(values, 0.5), 2),
        p25=round(percentile(values, 0.25), 2),
        p75=round(percentile(values, 0.75), 2),
    )


def cap_by_company(rows: Sequence[tuple[str, float]]) -> list[float]:
    """Потолок доли одной компании в срезе.

    Без него работодатель с сорока клонами одной вакансии и есть рынок.
    Лишние точки отбрасываются, а не усредняются: усреднение оставило бы
    той же компании полный голос, только тише.
    """
    limit = max(1, int(len(rows) * R.COMPANY_CAP))
    used: dict[str, int] = {}
    out: list[float] = []
    for company, value in rows:
        name = company or "?"
        if used.get(name, 0) >= limit:
            continue
        used[name] = used.get(name, 0) + 1
        out.append(value)
    return out


def marker(point: int | None, stats: Stats | None) -> Marker:
    """Метка вакансии по перцентилям среза."""
    if point is None:
        return Marker(label=R.NO_SALARY, point=None, stats=stats)
    if stats is None or not stats.enough:
        return Marker(label=R.UNKNOWN, point=point, stats=stats if stats else None)
    deviation = round((point - stats.median) / stats.median, 3) if stats.median else None
    if point < stats.p25:
        label = R.BELOW
    elif point > stats.p75:
        label = R.ABOVE
    else:
        label = R.IN_MARKET
    return Marker(label=label, point=point, deviation=deviation, stats=stats)


def row_line(row: Any) -> str:
    """Строка карточки из сохранённой вакансии, без похода за срезом."""
    label = str(row["market_label"] or "")
    if not label:
        return ""
    text = "Рынок: {}".format(R.LABEL_RU.get(label, label))
    median = row["market_median"]
    delta = row["market_delta"]
    if median is not None:
        text += " — медиана {}".format(_money(median))
    if delta is not None:
        text += " ({:+.0%})".format(float(delta))
    level = row["market_level"]
    if level is not None:
        text += ", срез: {}".format(R.LEVEL_RU[min(int(level), len(R.LEVEL_RU) - 1)])
    return text


def within_window(published_at: str, today: date | None = None) -> bool:
    today = today or date.today()
    try:
        day = datetime.fromisoformat(published_at[:10]).date()
    except ValueError:
        return False
    return 0 <= (today - day).days <= R.WINDOW_DAYS


__all__ = (
    "Marker",
    "Observation",
    "Stats",
    "bucket_key",
    "bucket_label",
    "cap_by_company",
    "format_of",
    "geo_of",
    "grade_of",
    "marker",
    "net",
    "observe",
    "percentile",
    "role_of",
    "row_line",
    "summarize",
    "within_window",
)
