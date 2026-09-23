"""Сколько условий от модели уже есть в структурных полях источника.

Этап `extract` спрашивает у модели формат, график, вилку, уровень и стек. Ровно
эти четыре вещи hh.ru отдаёт полями API и они уже лежат в `vacancies`:
`schedule`, `salary_from/salary_to/currency`, `experience`, `skills`. Вызов
модели за тем, что и так пришло в JSON, — это вызов, которого не должно быть
[CORE-016], а заодно риск, что модель ошибётся там, где источник точен.

Здесь считается цена вопроса по уже собранной базе: по каждому полю условий —
сколько вакансий его получили от модели, у скольких из них то же самое есть в
источнике, и расходятся ли значения. Сеть не трогается, модель не зовётся,
ничего не пишется [CORE-019]: это замер, а не прогон.

Запуск: `python conditions_overlap.py` или `python conditions_overlap.py --show 5`.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

import conditions
import db
import settings

# Поле условий -> чем источник отвечает на тот же вопрос. Пусто означает, что
# структурного ответа нет и поле остаётся за моделью.
SOURCE_OF = {
    "format": "schedule",
    "schedule": "schedule",
    "salary": "salary_from/salary_to",
    "grade": "experience",
    "stack": "skills",
    "office": "",
    "process": "",
    "other": "",
}

# Опыт из API hh — идентификатор, условие от модели — слова. Сопоставляем по
# словам, а не по точному совпадению: «от 3 лет» и «между 3 и 6» про одно.
GRADE_WORDS = {
    "noExperience": ("без опыта", "стажёр", "стажер", "junior", "джун", "начина"),
    "between1And3": ("1 год", "2 год", "2 лет", "3 год", "3 лет", "junior", "джун", "middle", "мидл"),
    "between3And6": ("3 год", "3 лет", "4 год", "4 лет", "5 лет", "6 лет", "middle", "мидл", "senior", "сеньор"),
    "moreThan6": ("6 лет", "7 лет", "8 лет", "10 лет", "senior", "сеньор", "lead", "лид"),
}
REMOTE_WORDS = ("удал", "remote", "из дома", "дистанц")
NUMBER_RE = re.compile(r"\d[\d\s  ]*")


@dataclass
class FieldStat:
    """Одно поле условий: сколько раз пришло от модели и что было в источнике."""

    name: str
    total: int = 0
    covered: int = 0
    agreed: int = 0
    disagreed: int = 0
    examples: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def share(self) -> float:
        return 100.0 * self.covered / self.total if self.total else 0.0


def _numbers(text: str) -> set[int]:
    """Числа из текста в тысячах рублей: 200000 и «200 000» — одно и то же."""
    out = set()
    for chunk in NUMBER_RE.findall(text or ""):
        digits = re.sub(r"\D", "", chunk)
        if not digits:
            continue
        value = int(digits)
        if value >= 1000:
            value //= 1000
        out.add(value)
    return out


def _skills(row: sqlite3.Row) -> list[str]:
    try:
        return [str(item).lower() for item in json.loads(row["skills"] or "[]")]
    except (json.JSONDecodeError, TypeError):
        return []


def _covered(row: sqlite3.Row, field_name: str) -> bool:
    """Есть ли у источника структурный ответ на тот же вопрос."""
    if field_name in ("format", "schedule"):
        return bool(row["schedule"])
    if field_name == "salary":
        return bool(row["salary_from"] or row["salary_to"])
    if field_name == "grade":
        return bool(row["experience"])
    if field_name == "stack":
        return bool(_skills(row))
    return False


def _agrees(row: sqlite3.Row, field_name: str, value: str) -> bool:
    """Сходится ли значение модели с полем источника.

    Сравнение нарочно грубое: цель — понять, спорит модель с источником или
    пересказывает его, а не выставить оценку за формулировку.
    """
    low = (value or "").lower()
    if field_name == "format":
        remote_src = any(word in (row["schedule"] or "").lower() for word in REMOTE_WORDS)
        remote_model = any(word in low for word in REMOTE_WORDS)
        return remote_src == remote_model
    if field_name == "schedule":
        source = (row["schedule"] or "").lower()
        head = source.split()[0][:5] if source else ""
        return bool(head) and head in low
    if field_name == "salary":
        theirs = {
            int(item) // 1000 if int(item) >= 1000 else int(item)
            for item in (row["salary_from"], row["salary_to"])
            if item
        }
        return bool(_numbers(low) & theirs)
    if field_name == "grade":
        return any(word in low for word in GRADE_WORDS.get(row["experience"] or "", ()))
    if field_name == "stack":
        known = _skills(row)
        return any(skill and skill in low for skill in known)
    return False


# Поле `vacancies` -> как его звать в отчёте. Заполненность источника — первое,
# что надо смотреть: пустое поле выглядит в таблице как «модель не дублирует
# источник», хотя на деле источник просто не разобран.
SOURCE_FILL = (
    ("schedule", "график/формат", "schedule IS NOT NULL AND TRIM(schedule) <> ''"),
    ("salary", "вилка", "salary_from IS NOT NULL OR salary_to IS NOT NULL"),
    ("experience", "опыт", "experience IS NOT NULL AND TRIM(experience) <> ''"),
    ("employment", "занятость", "employment IS NOT NULL AND TRIM(employment) <> ''"),
    ("skills", "навыки", "skills IS NOT NULL AND skills <> '' AND skills <> '[]'"),
)


def fill(conn: sqlite3.Connection) -> list[tuple[str, int, list[float]]]:
    """Заполненность структурных полей по источникам.

    Разрез по источникам обязателен: у Труда России и Работы.ру своих полей
    почти нет, и их пустота в общей цифре выглядит как поломка разбора hh.ru.
    """
    columns = ", ".join(
        "SUM(CASE WHEN {} THEN 1 ELSE 0 END)".format(where) for _, _, where in SOURCE_FILL
    )
    rows = conn.execute(
        "SELECT source, COUNT(*), {} FROM vacancies GROUP BY source ORDER BY COUNT(*) DESC".format(
            columns
        )
    ).fetchall()
    out = []
    for row in rows:
        total = int(row[1]) or 1
        out.append(
            (str(row[0]), int(row[1]), [100.0 * int(row[index + 2] or 0) / total for index in range(len(SOURCE_FILL))])
        )
    return out


def report(conn: sqlite3.Connection) -> tuple[dict[str, FieldStat], int, int]:
    """Статистика по полям условий, число вакансий с условиями и всего."""
    stats: dict[str, FieldStat] = {}
    rows = conn.execute(
        "SELECT c.key AS key, c.field AS field, c.value AS value,"
        " v.schedule, v.salary_from, v.salary_to, v.currency, v.experience, v.skills"
        " FROM vacancy_conditions c JOIN vacancies v ON v.key = c.key"
        " ORDER BY c.id"
    ).fetchall()
    keys = set()
    for row in rows:
        keys.add(row["key"])
        stat = stats.setdefault(row["field"], FieldStat(row["field"]))
        stat.total += 1
        if not _covered(row, row["field"]):
            continue
        stat.covered += 1
        if _agrees(row, row["field"], row["value"]):
            stat.agreed += 1
        else:
            stat.disagreed += 1
            if len(stat.examples) < 20:
                source = {
                    "format": row["schedule"],
                    "schedule": row["schedule"],
                    "salary": "{}–{}".format(row["salary_from"] or "?", row["salary_to"] or "?"),
                    "grade": row["experience"],
                    "stack": ", ".join(_skills(row))[:60],
                }.get(row["field"], "")
                stat.examples.append((row["key"], row["value"][:60], str(source or "")))
    total = conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]
    return stats, len(keys), int(total)


def render(stats: dict[str, FieldStat]) -> str:
    lines = [
        "поле     | источник              | условий | есть в источнике | сошлось | спорит",
        "---------|-----------------------|---------|------------------|---------|-------",
    ]
    for name in conditions.FIELD_ORDER:
        stat = stats.get(name)
        if stat is None:
            continue
        lines.append(
            "{:<8} | {:<21} | {:>7} | {:>10} {:>4.0f}% | {:>7} | {:>6}".format(
                name,
                SOURCE_OF.get(name) or "—",
                stat.total,
                stat.covered,
                stat.share,
                stat.agreed,
                stat.disagreed,
            )
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--show", type=int, default=0, help="показать столько примеров расхождений"
    )
    args = parser.parse_args(argv)

    conn = db.connect(settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    stats, with_conditions, total = report(conn)
    if not stats:
        print("условий в базе нет — сначала прогон с включённым этапом extract")
        return 0

    rows = sum(stat.total for stat in stats.values())
    covered = sum(stat.covered for stat in stats.values())
    print("вакансий в базе: {}, из них с условиями: {}".format(total, with_conditions))
    print("условий от модели: {}, из них дублируют поле источника: {} ({:.0f}%)".format(
        rows, covered, 100.0 * covered / rows if rows else 0.0
    ))
    print()
    labels = [label for _, label, _ in SOURCE_FILL]
    print("заполненность полей источника, % вакансий:")
    print("  {:<12} {:>6} {}".format("источник", "всего", " ".join(
        "{:>12}".format(label) for label in labels
    )))
    for source, total_rows, shares in fill(conn):
        print("  {:<12} {:>6} {}".format(source, total_rows, " ".join(
            "{:>11.0f}%".format(share) for share in shares
        )))
    print()
    print(render(stats))

    if args.show:
        for stat in stats.values():
            for key, value, source in stat.examples[: args.show]:
                print("\n{} {}: модель «{}», источник «{}»".format(stat.name, key, value, source))

    print(
        "\n«Есть в источнике» — вызов модели, которого можно было не делать."
        "\n«Спорит» — там же, но модель ещё и разошлась с полем API: либо она"
        "\nошиблась, либо в тексте написано не то, что в форме hh."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
