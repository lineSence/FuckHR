"""Проверяемые факты о работодателе из истории публикаций (этап 2).

Досье компании до сих пор собиралось из отзывов — то есть из чужих слов.
Здесь берутся наши собственные наблюдения: сколько вакансий компании мы
видели, сколько из них перевыкладывались, сколько висят месяцами, у скольких
двигалась вилка, у скольких её нет вовсе. Это данные, которые можно
предъявить, не ссылаясь ни на модель, ни на анонимный отзыв [CORE-019].

Названия сводятся через company_key: «ООО «Ромашка»» и «Ромашка» — один
работодатель, иначе статистика разъедется по трём строкам.

Порог MIN_TRACKED существует ради [HRD-004]: по одной вакансии с историей в
два дня вывода о работодателе не делается, возвращается «недостаточно
данных», а не осторожная формулировка о плохом.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

import company_key
import detector

log = logging.getLogger(__name__)

# Меньше двух вакансий с историей — это наблюдение за вакансией, а не за компанией.
MIN_TRACKED = 2
# Столько дней в выдаче подряд считаем признаком вечной вакансии.
LONG_RUNNING_DAYS = 90
# Порог перепубликаций берём у детектора, чтобы карточка и досье не спорили.
REPUBLISH_ALARM = detector.REPUBLISH_ALARM


@dataclass(frozen=True)
class CompanySignals:
    """Счётчики по работодателю. Только числа, никаких оценок."""

    company: str
    vacancies: int = 0
    tracked: int = 0
    republished: int = 0
    long_running: int = 0
    salary_moved: int = 0
    no_salary: int = 0
    days_tracked: int = 0

    @property
    def enough(self) -> bool:
        return self.tracked >= MIN_TRACKED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def vacancy_rows(conn: sqlite3.Connection, company: str) -> list[sqlite3.Row]:
    """Вакансии компании с учётом разных написаний названия.

    Фильтр по company_key делается в Python, а не в SQL: сравнение нечёткое,
    и LIKE тут дал бы и ложные склейки, и пропуски.
    """
    if not (company or "").strip():
        return []
    rows = conn.execute(
        """
        SELECT key, company, salary_from, salary_to
        FROM vacancies
        WHERE company IS NOT NULL AND company <> ''
        """
    ).fetchall()
    return [r for r in rows if company_key.same(r["company"], company)]


def collect(conn: sqlite3.Connection, company: str, months: int = 8) -> CompanySignals:
    """Считает факты по работодателю. Пустая история — нулевые счётчики."""
    rows = vacancy_rows(conn, company)
    if not rows:
        return CompanySignals(company=company)

    tracked = republished = long_running = salary_moved = no_salary = 0
    days_tracked = 0
    for row in rows:
        if not row["salary_from"] and not row["salary_to"]:
            no_salary += 1
        hist = detector.history(conn, row["key"], months)
        if hist.snapshots == 0:
            continue
        days_tracked = max(days_tracked, hist.days_tracked)
        if hist.days_tracked >= detector.MIN_DAYS_TRACKED:
            tracked += 1
        if hist.republished >= REPUBLISH_ALARM:
            republished += 1
        if hist.active and hist.days_tracked >= LONG_RUNNING_DAYS:
            long_running += 1
        if hist.salary_changes:
            salary_moved += 1

    return CompanySignals(
        company=company,
        vacancies=len(rows),
        tracked=tracked,
        republished=republished,
        long_running=long_running,
        salary_moved=salary_moved,
        no_salary=no_salary,
        days_tracked=days_tracked,
    )


def facts(signals: CompanySignals) -> list[str]:
    """Строки для досье: каждая — либо число, либо ничего.

    Нулевые счётчики не печатаются: «перепубликаций 0» выглядит как вывод,
    хотя это чаще всего означает короткую историю.
    """
    if not signals.enough:
        return [
            f"истории публикаций мало: вакансий в базе {signals.vacancies}, "
            f"с историей от {detector.MIN_DAYS_TRACKED} дн. — {signals.tracked}"
        ]
    out = [
        f"вакансий в базе: {signals.vacancies}, наблюдаем до {signals.days_tracked} дн."
    ]
    if signals.republished:
        out.append(
            f"публиковались заново {REPUBLISH_ALARM}+ раз: "
            f"{signals.republished} из {signals.tracked}"
        )
    if signals.long_running:
        out.append(
            f"висят в выдаче {LONG_RUNNING_DAYS}+ дн.: {signals.long_running}"
        )
    if signals.salary_moved:
        out.append(f"вилка менялась между слепками: {signals.salary_moved}")
    if signals.no_salary:
        out.append(f"без вилки вообще: {signals.no_salary} из {signals.vacancies}")
    return out


def lines(conn: sqlite3.Connection, company: str, months: int = 8) -> list[str]:
    """Готовые строки по названию компании — основной вход для UI и досье."""
    return facts(collect(conn, company, months))


__all__ = (
    "MIN_TRACKED",
    "LONG_RUNNING_DAYS",
    "REPUBLISH_ALARM",
    "CompanySignals",
    "vacancy_rows",
    "collect",
    "facts",
    "lines",
)
