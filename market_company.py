"""Метка работодателя по деньгам: платит ниже, выше рынка или прячет вилку.

Считается по вакансиям компании за окно, а не по одной: единственная дешёвая
вакансия — это вакансия, а не политика найма. Порог тот же по смыслу, что
MIN_TRACKED в company_signals.

Метка всегда раскрывается: сколько вакансий, какая медиана отклонения, какие
срезы. «Платит выше рынка» — не похвала: столь же часто это вилка-приманка
«до», которую на офере не подтверждают, поэтому формулировка нейтральная.

На уровень риска в досье метка не влияет сознательно. Досье пересобирается раз
в месяц, а рынок пересчитывается каждый прогон; смешав их, мы получили бы
светофор, который врёт ровно между пересчётами. Метка живёт рядом с досье
своей строкой и своим весом в скоринге вакансии.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from statistics import median

import market
import market_rules as R
import market_store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sign:
    code: str
    text: str


@dataclass(frozen=True)
class CompanyMarket:
    """Итог по работодателю: уровень метки, признаки, числа."""

    company: str = ""
    level: str = R.MARK_NONE
    signs: tuple[Sign, ...] = ()
    vacancies: int = 0          # вакансий с известной точкой и известным рынком
    deviation: float | None = None  # медиана отклонений
    hidden: int = 0             # вакансий без вилки

    @property
    def label(self) -> str:
        return R.MARK_RU.get(self.level, self.level)

    @property
    def flagged(self) -> bool:
        return self.level == R.MARK_SET

    @property
    def weight(self) -> int:
        """Вес жёлтого флага: берётся по самому тяжёлому признаку."""
        return max((R.SIGN_WEIGHT.get(s.code, 0) for s in self.signs), default=0)

    def lines(self) -> list[str]:
        if self.level == R.MARK_NONE:
            return []
        return ["— {}: {}".format(self.label, "; ".join(s.text for s in self.signs))]


def evaluate(conn: sqlite3.Connection, company: str) -> CompanyMarket:
    """Признаки и уровень метки. Один признак — наблюдение, два — метка."""
    company = (company or "").strip()
    if not company:
        return CompanyMarket()
    rows = market_store.company_rows(conn, company)
    if not rows:
        return CompanyMarket(company=company)

    deviations: list[float] = []
    labels: list[str] = []
    for row in rows:
        if row["point"] is None or not row["role"]:
            continue
        stats = market_store.stats_for(
            conn, str(row["role"]), str(row["grade"]), str(row["geo"]), str(row["format"])
        )
        marker = market.marker(int(row["point"]), stats)
        if marker.label in (R.UNKNOWN, R.NO_SALARY) or marker.deviation is None:
            continue
        labels.append(marker.label)
        deviations.append(marker.deviation)

    hidden = sum(1 for row in rows if row["point"] is None)
    signs: list[Sign] = []
    middle = round(median(deviations), 3) if deviations else None

    if len(labels) >= R.MIN_COMPANY_VACANCIES and middle is not None:
        below = labels.count(R.BELOW) / len(labels)
        above = labels.count(R.ABOVE) / len(labels)
        if below >= R.SHARE and middle <= -R.DEVIATION:
            signs.append(
                Sign(
                    "below",
                    "{} из {} вакансий ниже рынка, медиана отклонения {:.0%}".format(
                        labels.count(R.BELOW), len(labels), middle
                    ),
                )
            )
        elif above >= R.SHARE and middle >= R.DEVIATION:
            signs.append(
                Sign(
                    "above",
                    "{} из {} вакансий выше рынка, медиана отклонения +{:.0%}; "
                    "вилку «до» на офере подтверждают не всегда".format(
                        labels.count(R.ABOVE), len(labels), middle
                    ),
                )
            )

    if len(rows) >= R.MIN_COMPANY_VACANCIES and hidden / len(rows) >= R.HIDDEN_SHARE:
        signs.append(
            Sign(
                "hidden",
                "вилки нет у {} из {} вакансий".format(hidden, len(rows)),
            )
        )

    level = R.MARK_NONE
    if len(signs) == 1:
        level = R.MARK_WATCH
    elif len(signs) >= 2:
        level = R.MARK_SET

    return CompanyMarket(
        company=company,
        level=level,
        signs=tuple(signs),
        vacancies=len(labels),
        deviation=middle,
        hidden=hidden,
    )


def row_lines(row: object) -> list[str]:
    """Строки карточки из сохранённой метки. Метка без признаков не строка."""
    level = str(row["level"] or R.MARK_NONE)  # type: ignore[index]
    if level == R.MARK_NONE:
        return []
    try:
        signs = json.loads(row["signs"] or "[]")  # type: ignore[index]
    except ValueError:
        signs = []
    if not signs:
        return []
    return [
        "— {}: {}".format(
            R.MARK_RU.get(level, level),
            "; ".join(str(sign.get("text") or "") for sign in signs),
        )
    ]


def refresh(conn: sqlite3.Connection, companies: list[str]) -> int:
    """Пересчитывает метки списка компаний и сохраняет их."""
    done = 0
    for company in dict.fromkeys(c for c in companies if c):
        mark = evaluate(conn, company)
        market_store.save_company(conn, company, mark)
        done += 1
    log.info("метки по деньгам пересчитаны: компаний %s", done)
    return done


__all__ = ("CompanyMarket", "Sign", "evaluate", "refresh")
