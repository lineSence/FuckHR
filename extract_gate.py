"""Гейт перед этапом extract: не звать модель за тем, чего в тексте нет.

Этап `extract` спрашивает у модели пять полей (`conditions.MODEL_FIELDS`):
формат, офис, стек, процесс отбора и прочее. График, вилку и уровень у неё
спрашивать перестали — их присылает источник (docs/performance.md). Оставшиеся
поля тоже не всегда есть в тексте: описание «ищем питониста, пишите» не
содержит ни адреса, ни этапов отбора, ни ДМС, и вызов по нему заведомо вернёт
пустоту [CORE-016].

Решение детерминированное [CORE-015]: в тексте ищутся слова-приметы каждого
поля. Нет ни одной приметы по полям, которые источник не закрыл, — вакансия до
модели не доходит. Поле считается закрытым источником по тем же правилам, что
в `conditions_overlap.py`: есть `schedule` — закрыт формат, есть `skills` —
закрыт стек.

Гейт выключен по умолчанию (`GATE_EXTRACT_MARKERS=1` включает) и деградирует в
«пропустить дальше» [CORE-017]. Цена известна заранее: `python extract_gate.py`
считает по уже собранной базе, сколько вакансий гейт снял бы и сколько
сохранённых условий при этом потерялось бы. Это замер, а не прогон [CORE-019]:
сеть и модель не трогаются.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from typing import Any, Iterable, Sequence

import conditions
import db
import settings

log = logging.getLogger("fuckhr")

# Поле условий -> слова, по которым видно, что в тексте про него что-то есть.
# Список нарочно широкий: лишний вызов дешевле потерянного условия.
MARKERS: dict[str, tuple[str, ...]] = {
    "format": (
        "удал", "remote", "гибрид", "офис", "дистанц", "из дома", "на месте", "разъезд",
    ),
    "office": (
        "офис", "адрес", "метро", "бизнес-центр", "бц ", "коворкинг", "улиц", "ул.",
        "проспект", "этаж", "территори", "площадк", "цех", "склад",
    ),
    "stack": (
        "стек", "технолог", "фреймворк", "framework", "python", "java", "sql", "linux",
        "docker", "1с", "1c", "excel", "битрикс", "api", "git", "php", "c#", "javascript",
    ),
    "process": (
        "собеседован", "интервью", "этап", "тестово", "отбор", "скрининг", "оффер",
        "знакомств", "анкет", "резюме", "испытательн",
    ),
    "other": (
        "дмс", "соцпакет", "соц. пакет", "отпуск", "премия", "бонус", "обучен",
        "компенсац", "релокац", "оборудован", "ноутбук", "питани", "спортзал",
        "страхов", "корпоратив", "наставник", "оформлен", "тк рф", "переработ",
    ),
}


def enabled() -> bool:
    return settings.flag("GATE_EXTRACT_MARKERS")


def covered_by_source(vacancy: Any) -> set[str]:
    """Какие поля источник уже закрыл структурно — их приметы не считаются."""
    out: set[str] = set()
    if str(getattr(vacancy, "schedule", "") or "").strip():
        out.add("format")
    skills = getattr(vacancy, "skills", None)
    if isinstance(skills, str):
        try:
            skills = json.loads(skills or "[]")
        except json.JSONDecodeError:
            skills = []
    if skills:
        out.add("stack")
    return out


def found_fields(text: str, skip: Iterable[str] = ()) -> set[str]:
    """Поля, чьи приметы встретились в тексте. `skip` — закрытые источником."""
    low = (text or "").lower()
    ignored = set(skip)
    return {
        field
        for field, words in MARKERS.items()
        if field not in ignored and any(word in low for word in words)
    }


def wanted(vacancy: Any) -> bool:
    """Звать ли модель по этой вакансии. Выключенный гейт всегда говорит «да»."""
    text = str(getattr(vacancy, "description", "") or "")
    if not enabled():
        return True
    if not text.strip():
        return False
    fields = found_fields(text, covered_by_source(vacancy))
    if fields:
        return True
    log.info(
        "этап extract пропущен для %s: примет условий в тексте нет",
        getattr(vacancy, "key", "?"),
    )
    return False


def keep(vacancies: Sequence[Any]) -> list[Any]:
    """Отбор порции. Молча ничего не теряет: каждый отказ пишется в лог."""
    if not enabled():
        return list(vacancies)
    kept = [item for item in vacancies if wanted(item)]
    if len(kept) != len(vacancies):
        log.info("гейт extract: осталось %s из %s", len(kept), len(vacancies))
    return kept


class _Row:
    """Строка базы в том же виде, в каком гейт видит вакансию."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.key = row["key"]
        self.description = row["description"] or ""
        self.schedule = row["schedule"]
        self.skills = row["skills"]


def measure(conn: sqlite3.Connection) -> dict[str, Any]:
    """Цена гейта по уже собранной базе: кого снял бы и что потерял бы.

    Потери считаются дважды. По всем полям — как в базе, включая `salary` и
    `schedule` прошлых прогонов. И по `conditions.MODEL_FIELDS` — только то,
    что у модели спрашивают сегодня: график и вилку присылает источник, и их
    потеря гейту не в укор.
    """
    rows = conn.execute(
        "SELECT key, description, schedule, skills FROM vacancies"
        " WHERE description IS NOT NULL AND TRIM(description) <> ''"
    ).fetchall()
    stored: dict[str, list[tuple[str, str, str]]] = {}
    for row in conn.execute(
        "SELECT key, field, value, quote FROM vacancy_conditions"
    ).fetchall():
        stored.setdefault(row["key"], []).append(
            (row["field"], row["value"], row["quote"])
        )
    dropped: list[str] = []
    lost: dict[str, int] = {}
    lost_now: dict[str, int] = {}
    hurt = 0
    samples: list[tuple[str, str, str, str]] = []
    for raw in rows:
        item = _Row(raw)
        if found_fields(item.description, covered_by_source(item)):
            continue
        dropped.append(item.key)
        actual = False
        for field_name, value, quote in stored.get(item.key, ()):
            lost[field_name] = lost.get(field_name, 0) + 1
            if field_name in conditions.MODEL_FIELDS:
                lost_now[field_name] = lost_now.get(field_name, 0) + 1
                actual = True
                if len(samples) < 40:
                    samples.append((item.key, field_name, value, quote))
        hurt += 1 if actual else 0
    return {
        "total": len(rows),
        "dropped": len(dropped),
        "with_conditions": sum(1 for key in dropped if stored.get(key)),
        "hurt": hurt,
        "lost": lost,
        "lost_now": lost_now,
        "samples": samples,
        "examples": dropped[:5],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Цена гейта extract по базе")
    parser.add_argument("--db", default=settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    parser.add_argument(
        "--show", type=int, default=0, help="показать N потерянных условий с цитатами"
    )
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        data = measure(conn)
    finally:
        conn.close()
    total = data["total"] or 1
    print("вакансий с описанием: {}".format(data["total"]))
    print(
        "гейт снял бы: {} ({:.0f}%), из них с сохранёнными условиями: {}".format(
            data["dropped"], 100.0 * data["dropped"] / total, data["with_conditions"]
        )
    )
    print(
        "из них потеряли бы условие по полям, которые ещё спрашивают у модели: {}".format(
            data["hurt"]
        )
    )
    if data["lost_now"]:
        print("потерялось бы условий (поля модели):")
        for field_name, count in sorted(data["lost_now"].items(), key=lambda p: -p[1]):
            print("  {:<8} {}".format(field_name, count))
    else:
        print("ни одного условия по полям модели гейт не потерял бы")
    stale = {
        name: count
        for name, count in data["lost"].items()
        if name not in conditions.MODEL_FIELDS
    }
    if stale:
        print(
            "за компанию ушли бы поля прошлых прогонов (их теперь даёт источник): {}".format(
                ", ".join("{} {}".format(name, count) for name, count in sorted(stale.items()))
            )
        )
    for key, field_name, value, quote in data["samples"][: max(0, args.show)]:
        print("  {} {}: {} | {}".format(key, field_name, value[:50], quote[:90]))
    if data["examples"]:
        print("примеры снятых: {}".format(", ".join(data["examples"])))
    return 0


__all__ = (
    "MARKERS",
    "covered_by_source",
    "enabled",
    "found_fields",
    "keep",
    "measure",
    "wanted",
)


if __name__ == "__main__":
    raise SystemExit(main())
