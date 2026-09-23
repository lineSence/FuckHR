"""Что отсекут ворота на карточки при разной дельте (docs/performance.md, п. 4).

Код ворот готов с 20.09.2026, но `PREFILTER_DETAILS_DELTA=0` его выключает, и
менять умолчание вслепую нельзя: пропущенная карточка — это вакансия без
описания, без условий и без HR-флагов. Здесь считается цена по уже собранной
базе: сколько карточек не качалось бы при дельте 5, 10, 15 и что именно
потерял бы детектор.

Сеть не трогается: берётся то, что в базе. Черновой скор считается так же, как
на выдаче, — по вакансии без описания [CORE-015].

Запуск: `python gate_report.py` или `python gate_report.py --deltas 5,10`.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from typing import Sequence

import conditions
import db
import detector
import hh_pages
import profiles
import rebuild
import settings
import sources

log = logging.getLogger("fuckhr")

DEFAULT_DELTAS = (5.0, 10.0, 15.0)


@dataclass
class Row:
    delta: float
    total: int
    skipped: int
    lost_conditions: int
    lost_claims: int

    @property
    def share(self) -> float:
        return 100.0 * self.skipped / self.total if self.total else 0.0


def _has_conditions(conn: sqlite3.Connection, key: str) -> bool:
    return bool(conditions.load(conn, key))


def report(
    conn: sqlite3.Connection,
    bundle: Sequence[object],
    deltas: Sequence[float] = DEFAULT_DELTAS,
) -> list[Row]:
    """По строке на дельту: сколько карточек не качалось бы и что потерялось."""
    conditions.ensure_schema(conn)
    detector.ensure_schema(conn)
    fuzzy = settings.prefilter_options().fuzzy
    rows = conn.execute(
        "SELECT * FROM vacancies WHERE source = ? ORDER BY last_seen_at DESC",
        (sources.SOURCE_HH,),
    ).fetchall()
    out: list[Row] = []
    for delta in deltas:
        skipped = lost_conditions = lost_claims = 0
        for row in rows:
            vacancy = rebuild.vacancy_of(row)
            # Ворота работают до загрузки описания: считаем по черновику.
            draft = vacancy.model_copy(update={"description": "", "skills": []})
            if hh_pages.worth_details(draft, bundle, None, fuzzy, delta):
                continue
            skipped += 1
            if _has_conditions(conn, row["key"]):
                lost_conditions += 1
            if detector.load(conn, row["key"]) is not None:
                lost_claims += 1
        out.append(Row(delta, len(rows), skipped, lost_conditions, lost_claims))
    return out


def render(rows: Sequence[Row]) -> str:
    lines = [
        "дельта | не качаем | доля | теряем условий | теряем отчётов детектора",
        "-------|-----------|------|----------------|-------------------------",
    ]
    for row in rows:
        lines.append(
            "{:>6.0f} | {:>9} | {:>3.0f}% | {:>14} | {:>24}".format(
                row.delta, row.skipped, row.share, row.lost_conditions, row.lost_claims
            )
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--deltas", default="5,10,15", help="список дельт через запятую"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    deltas = [float(part) for part in args.deltas.split(",") if part.strip()]
    conn = db.connect(settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    bundle = profiles.load_all(settings.get("RUN_PROFILE", "profile.yaml"))
    rows = report(conn, bundle, deltas)
    if not rows or not rows[0].total:
        print("в базе нет вакансий с hh.ru — считать нечего")
        return 0
    print("вакансий с hh.ru в базе: {}".format(rows[0].total))
    print(render(rows))
    print(
        "\nСтавить PREFILTER_DETAILS_DELTA имеет смысл там, где «не качаем» велико, "
        "а потери условий и отчётов малы: это и есть цена решения."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
