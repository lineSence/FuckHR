"""Общая оценка работодателя: светофор по осям из уже собранных данных (ADR-018).

Ни одного нового источника здесь нет. Досье, метка накрутки, метка по деньгам,
слепки и детектор уже считают своё; этот модуль сводит их в один вывод, который
можно предъявить: уровень, оси, улики с числами.

Три вещи, которые делают вывод честным:
1. Своё наблюдение весит больше чужого слова, а накрутка обнуляет вес отзывов.
2. Итог — худшая ось, а не среднее: печеньки не компенсируют невыплату зарплаты.
3. Ось без данных не считается и не штрафует; меньше двух осей — «недостаточно
   данных», а не осторожное «вроде норм» [HRD-004].

Модель не участвует нигде [CORE-015], сеть не трогается: всё читается из базы.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import company_score_rules as R
import company_signals
import deepresearch_store
import detector
import dossier_rules
import dossier_store
import fake_rules
import fake_store
import injection_store
import market_store
import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Evidence:
    """Одна улика. text уже содержит числа: это готовая строка карточки [HRD-003]."""

    code: str
    axis: str
    polarity: str  # red | green
    weight: int
    trust: float
    text: str
    observed_at: str | None = None

    def value(self, now: datetime) -> float:
        return self.weight * self.trust * _freshness(self.observed_at, now)


@dataclass(frozen=True)
class CompanyScore:
    """Итог по работодателю: уровень, оси, улики, покрытие, сработавшее вето."""

    company: str
    level: str = R.LEVEL_UNKNOWN
    axes: dict[str, float] | None = None
    evidence: tuple[Evidence, ...] = ()
    covered: tuple[str, ...] = ()
    veto: str = ""

    @property
    def label(self) -> str:
        return R.LEVEL_RU.get(self.level, self.level)

    def top(self, limit: int = 3) -> tuple[Evidence, ...]:
        """Три главные улики, по одной на код: три строки про один и тот же
        салари-делей с трёх площадок — это одна новость, а не три."""
        red = [e for e in self.evidence if e.polarity == "red" and e.text]
        red.sort(key=lambda e: (e.weight * e.trust), reverse=True)
        seen: set[str] = set()
        out: list[Evidence] = []
        for item in red:
            if item.code in seen:
                continue
            seen.add(item.code)
            out.append(item)
            if len(out) >= limit:
                break
        return tuple(out)

    def lines(self) -> list[str]:
        if self.level == R.LEVEL_UNKNOWN:
            return [
                "Оценка работодателя: {} (улик {}, осей с данными {} из {})".format(
                    self.label, len(self.evidence), len(self.covered), len(R.AXES)
                )
            ]
        head = "Оценка работодателя: {} · осей с данными {} из {}".format(
            self.label, len(self.covered), len(R.AXES)
        )
        if self.veto:
            head += " · {}".format(R.VETO_RU.get(self.veto, self.veto))
        return [head] + ["— {}".format(e.text) for e in self.top()]

    def to_dict(self) -> dict[str, object]:
        return {
            "company": self.company,
            "level": self.level,
            "axes": dict(self.axes or {}),
            "evidence": [asdict(e) for e in self.evidence],
            "covered": list(self.covered),
            "veto": self.veto,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _freshness(observed_at: str | None, now: datetime) -> float:
    """Затухание улики. Без даты улика считается свежей: врать в обе стороны
    нельзя, а занижать вес собственных счётчиков не за что."""
    if not observed_at:
        return 1.0
    try:
        moment = datetime.fromisoformat(str(observed_at))
    except ValueError:
        return 1.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    months = max(0.0, (now - moment).days / 30.0)
    return math.exp(-months / R.DECAY_MONTHS)


def _saturate(total: float, cap: float) -> float:
    return cap * (1.0 - math.exp(-total / R.SATURATION))


# --- сбор улик ---


def _review_trust(row: sqlite3.Row, integrity: float) -> float:
    """Доверие к одному отзыву: площадка × метка накрутки × метка компании.

    Заказной отзыв получает ноль и в оценку не попадает вообще: его уже не
    учитывает средняя, и в уликах ему тоже не место.
    """
    site = str(row["site"] or "")
    label = str(row["label"] or fake_rules.LABEL_CLEAN)
    return (
        R.TRUST_REVIEW
        * dossier_rules.SITE_TRUST.get(site, 0.6)
        * fake_rules.LABEL_WEIGHT.get(label, 1.0)
        * integrity
    )


def _review_evidence(
    conn: sqlite3.Connection, company: str, integrity: float, fallback_date: str
) -> tuple[list[Evidence], int]:
    """Улики из отдельных отзывов: у каждой своя площадка и своя дата.

    Второе значение — сколько разобранных отзывов вообще нашлось. Ноль улик при
    найденных отзывах — это результат (все заказные), а не повод откатываться на
    агрегат досье.
    """
    rows = fake_store.load_items(conn, dossier_store.resolve_company(conn, company))
    out: list[Evidence] = []
    for row in rows:
        try:
            codes = json.loads(_cell(row, "patterns") or "[]")
        except ValueError:
            codes = []
        trust = _review_trust(row, integrity)
        if not codes or trust <= 0:
            continue
        site = str(row["site"] or "")
        dated = str(row["dated_at"] or "") if str(row["date_precision"]) != "none" else ""
        where = dossier_rules.SITE_NAMES.get(site, site or "площадка неизвестна")
        if dated:
            where += ", {}".format(dated[:10])
        for code in codes:
            axis = R.PATTERN_AXIS.get(str(code))
            meta = R.PATTERN_META.get(str(code))
            if axis is None or meta is None:
                continue
            label, polarity, weight = meta
            out.append(
                Evidence(
                    code=str(code),
                    axis=axis,
                    polarity=polarity,
                    weight=weight,
                    trust=trust,
                    text="{} — {}".format(label, where),
                    observed_at=dated or fallback_date,
                )
            )
    return out, len(rows)


def _pattern_evidence(row: sqlite3.Row, integrity: float) -> list[Evidence]:
    """Запасной путь для баз, собранных до перехода на review_items: агрегат
    досье без площадки и без даты отдельного отзыва."""
    trust = R.TRUST_REVIEW * integrity
    updated = str(row["updated_at"] or "")
    out: list[Evidence] = []
    try:
        patterns = json.loads(row["patterns"] or "[]")
    except ValueError:
        patterns = []
    for item in patterns:
        code = str(item.get("code") or "")
        axis = R.PATTERN_AXIS.get(code)
        meta = R.PATTERN_META.get(code)
        if axis is None or meta is None or not item.get("hits"):
            continue
        label, polarity, weight = meta
        # Улика на каждое упоминание, до пяти: так старый агрегат считается тем
        # же способом, что и отзывы по отдельности, включая порог вето.
        hits = max(1, min(int(item["hits"]), 5))
        for number in range(hits):
            out.append(
                Evidence(
                    code=code,
                    axis=axis,
                    polarity=polarity,
                    weight=weight,
                    trust=trust,
                    text=(
                        "{}: упоминаний в отзывах {}".format(label, int(item["hits"]))
                        if number == 0
                        else ""
                    ),
                    observed_at=updated,
                )
            )
    return out


def _dossier_evidence(conn: sqlite3.Connection, company: str) -> list[Evidence]:
    """Отзывы: закономерности отдельных отзывов и сама метка накрутки."""
    row = dossier_store.load(conn, company)
    if row is None:
        return []
    level = str(row["fake_level"] or fake_rules.MARK_NONE)
    integrity = R.INTEGRITY_TRUST.get(level, 1.0)
    updated = str(row["updated_at"] or "")
    out, reviews = _review_evidence(conn, company, integrity, updated)
    if not reviews:
        out = _pattern_evidence(row, integrity)
    if level != fake_rules.MARK_NONE:
        try:
            signs = json.loads(row["fake_signs"] or "[]")
        except ValueError:
            signs = []
        text = "; ".join(str(s.get("text") or "") for s in signs)
        out.append(
            Evidence(
                code=R.FAKE_CODE,
                axis=R.TRUTH,
                polarity="red",
                weight=R.FAKE_WEIGHT if level == fake_rules.MARK_FAKE else 2,
                trust=R.TRUST_OWN,
                text="{}: {}".format(fake_rules.MARK_RU.get(level, level), text),
                observed_at=updated,
            )
        )
        # Признаки накрутки участвуют в закономерностях по своим кодам.
        for sign in signs:
            code = str(sign.get("code") or "")
            if code:
                out.append(
                    Evidence(
                        code=code,
                        axis=R.TRUTH,
                        polarity="red",
                        weight=0,
                        trust=R.TRUST_OWN,
                        text="",
                        observed_at=updated,
                    )
                )
    return out


def _market_evidence(conn: sqlite3.Connection, company: str) -> list[Evidence]:
    row = market_store.load_company(conn, company)
    if row is None:
        return []
    try:
        signs = json.loads(row["signs"] or "[]")
    except ValueError:
        signs = []
    out: list[Evidence] = []
    for sign in signs:
        code = str(sign.get("code") or "")
        axis = R.MARKET_AXIS.get(code)
        if axis is None:
            continue
        out.append(
            Evidence(
                code=code,
                axis=axis,
                polarity="red",
                weight=R.MARKET_WEIGHT.get(code, 2),
                trust=R.TRUST_OWN,
                text=str(sign.get("text") or code),
            )
        )
    return out


def _signals_evidence(signals: company_signals.CompanySignals) -> list[Evidence]:
    if not signals.enough:
        return []
    out: list[Evidence] = []
    counts = {
        "republished": (
            signals.republished,
            {"count": signals.republished, "total": signals.tracked},
        ),
        "long_running": (
            signals.long_running,
            {"count": signals.long_running, "days": company_signals.LONG_RUNNING_DAYS},
        ),
        "salary_moved": (
            signals.salary_moved,
            {"count": signals.salary_moved},
        ),
    }
    if signals.vacancies and signals.no_salary * 2 >= signals.vacancies:
        counts["no_salary"] = (
            signals.no_salary,
            {"count": signals.no_salary, "total": signals.vacancies},
        )
    for code, (hits, fields) in counts.items():
        if not hits:
            continue
        axis, weight, template = R.OWN_SIGNALS[code]
        out.append(
            Evidence(
                code=code,
                axis=axis,
                polarity="red",
                weight=weight,
                trust=R.TRUST_OWN,
                text=template.format(**fields),
            )
        )
    return out


def _vacancy_evidence(conn: sqlite3.Connection, company: str) -> list[Evidence]:
    """Детектор и метка ИИ-текста по вакансиям этого же работодателя."""
    rows = company_signals.vacancy_rows(conn, company)
    if not rows:
        return []
    # Оценка считается и на базе, где детектор ни разу не запускался:
    # отсутствие таблицы — это «нет улик», а не падение прогона [CORE-017].
    detector.ensure_schema(conn)
    flagged: dict[str, int] = {}
    ai_texts = 0
    for row in rows:
        if str(_cell(row, "ai_label")) in ("suspect", "likely"):
            ai_texts += 1
        payload = detector.load(conn, row["key"])
        if not payload:
            continue
        for finding in payload.get("findings", []):
            if finding.get("verdict") != detector.NOT_SUPPORTED:
                continue
            kind = str(finding.get("kind") or "")
            if kind in R.DETECTOR_AXIS:
                flagged[kind] = flagged.get(kind, 0) + 1
    out: list[Evidence] = []
    for kind, hits in flagged.items():
        axis, weight, text = R.DETECTOR_AXIS[kind]
        out.append(
            Evidence(
                code=kind,
                axis=axis,
                polarity="red",
                weight=weight,
                trust=R.TRUST_OWN,
                text="{} (вакансий: {})".format(text, hits),
            )
        )
    if ai_texts:
        axis, weight, template = R.OWN_SIGNALS["ai_text"]
        out.append(
            Evidence(
                code="ai_text",
                axis=axis,
                polarity="red",
                weight=weight,
                trust=R.TRUST_OWN,
                text=template.format(count=ai_texts),
            )
        )
    return out


def _injection_evidence(conn: sqlite3.Connection, company: str) -> list[Evidence]:
    """Спрятанные инструкции для ИИ в текстах этой компании (ADR-020).

    Вес 3 — «подозрение», а не приговор: инъекцию мог вставить агрегатор или
    автор отзыва, а не сам работодатель. Доверие 1.0: это наше наблюдение в
    тексте, а не чужие слова.
    """
    return [
        Evidence(
            code=code,
            axis=axis,
            polarity=polarity,
            weight=weight,
            trust=trust,
            text=text,
            observed_at=observed_at or None,
        )
        for code, axis, polarity, weight, trust, text, observed_at in (
            injection_store.evidence(conn, company)
        )
    ]


def _deep_evidence(conn: sqlite3.Connection, company: str) -> list[Evidence]:
    """Находки глубокого ресёрча (ADR-019).

    По умолчанию не участвуют в оценке: суд и реестр — сильная улика, но
    привязка страницы к конторе делается по названию, и ошибка здесь красит
    невиновного. Включается настройкой DEEP_IN_SCORE, как и COMPANY_SCORE_IN_SCORE.
    """
    if not settings.flag("DEEP_IN_SCORE"):
        return []
    try:
        rows = deepresearch_store.evidence(conn, company)
    except sqlite3.Error:  # старая база без таблиц ресёрча [CORE-017]
        return []
    return [
        Evidence(
            code=code,
            axis=axis,
            polarity=polarity,
            weight=weight,
            trust=trust,
            text=text,
            observed_at=observed_at or None,
        )
        for code, axis, polarity, weight, trust, text, observed_at in rows
    ]


def _cell(row: sqlite3.Row, name: str) -> object:
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _combo_evidence(found: list[Evidence]) -> list[Evidence]:
    codes = {e.code for e in found}
    out: list[Evidence] = []
    for needed, axis, weight, text in R.COMBOS:
        if codes.issuperset(needed):
            out.append(
                Evidence(
                    code="+".join(needed),
                    axis=axis,
                    polarity="red",
                    weight=weight,
                    trust=R.TRUST_OWN,
                    text=text,
                )
            )
    return out


# --- сведение ---


def _axis_value(items: list[Evidence], now: datetime) -> float:
    minus = sum(e.value(now) for e in items if e.polarity == "red")
    plus = sum(e.value(now) for e in items if e.polarity == "green")
    return round(
        _saturate(minus, R.AXIS_CAP) - min(R.PLUS_CAP, _saturate(plus, R.AXIS_CAP)), 1
    )


def _axis_level(value: float) -> str:
    if value >= R.AXIS_RED:
        return R.LEVEL_RED
    if value >= R.AXIS_YELLOW:
        return R.LEVEL_YELLOW
    return R.LEVEL_GREEN


def _covered(items: list[Evidence]) -> bool:
    """Ось покрыта, если есть улика с высоким доверием или две любые."""
    weighty = [e for e in items if e.weight > 0]
    if len(weighty) >= 2:
        return True
    return any(e.trust >= R.COVER_TRUST for e in weighty)


def _veto(evidence: list[Evidence]) -> tuple[str, str]:
    """(код вето, уровень-потолок). Пустое значение — вето не сработало.

    Задержки зарплаты закрывают вопрос, но одного чужого слова для этого мало:
    нужны два независимых отзыва либо наше собственное наблюдение.
    """
    delays = [e for e in evidence if e.code == R.VETO_RED and e.trust > 0]
    own = any(e.trust >= R.TRUST_OWN for e in delays)
    if own or len(delays) >= R.VETO_RED_HITS:
        return R.VETO_RED, R.LEVEL_RED
    for item in evidence:
        if item.code in R.VETO_YELLOW and item.weight > 0:
            return item.code, R.LEVEL_YELLOW
    return "", ""


def _worst(levels: list[str]) -> str:
    return max(levels, key=R.LEVEL_ORDER.index) if levels else R.LEVEL_UNKNOWN


def evaluate(conn: sqlite3.Connection, company: str) -> CompanyScore:
    """Считает уровень работодателя по всему, что уже лежит в базе."""
    company = (company or "").strip()
    if not company:
        return CompanyScore(company="")
    now = _now()
    signals = company_signals.collect(conn, company)
    evidence = (
        _dossier_evidence(conn, company)
        + _market_evidence(conn, company)
        + _signals_evidence(signals)
        + _vacancy_evidence(conn, company)
        + _deep_evidence(conn, company)
        + _injection_evidence(conn, company)
    )
    evidence += _combo_evidence(evidence)
    scoring = [e for e in evidence if e.weight > 0]

    axes: dict[str, float] = {}
    covered: list[str] = []
    for axis in R.AXES:
        items = [e for e in scoring if e.axis == axis]
        if not items:
            continue
        axes[axis] = _axis_value(items, now)
        if _covered(items):
            covered.append(axis)

    level = R.LEVEL_UNKNOWN
    if len(covered) >= R.MIN_AXES_COVERED:
        level = _worst([_axis_level(axes[axis]) for axis in covered])

    veto_code, ceiling = _veto(scoring)
    if ceiling == R.LEVEL_RED:
        level = R.LEVEL_RED
    elif ceiling == R.LEVEL_YELLOW and level in (R.LEVEL_GREEN, R.LEVEL_UNKNOWN):
        level = R.LEVEL_YELLOW

    return CompanyScore(
        company=company,
        level=level,
        axes=axes,
        evidence=tuple(scoring),
        covered=tuple(covered),
        veto=veto_code,
    )


def lines(conn: sqlite3.Connection, company: str) -> list[str]:
    return evaluate(conn, company).lines()


__all__ = ("CompanyScore", "Evidence", "evaluate", "lines")
