"""Метрики сравнения моделей: уровни, стабильность, порядок, отказы.

Выделено из bench.py по [CORE-024]. Здесь нет ни сети, ни моделей — только
арифметика над строками прогона.

Три решения владельца (19.09.2026), которые здесь и живут:

1. Итог этапа — взвешенное среднее по уровням сложности. Кейс третьего уровня
   весит втрое: именно он отличает умную модель от услужливой.
2. Стабильность, устойчивость к порядку и отказы — отдельные колонки, в общий
   балл они не входят. Смешивать их в одну цифру значит прятать причину.
3. Кейсов столько, сколько нужно для различения; если набор перестал
   различать модели, отчёт обязан сказать об этом вслух, а не показывать
   красивую ничью.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import bench_cases
import bench_hard

# Вес уровня: 1 «умеет вообще», 2 «не путается», 3 «не ведётся».
LEVEL_WEIGHT = {1: 1.0, 2: 2.0, 3: 3.0}
LEVEL_RU = {1: "базовые", 2: "средние", 3: "сложные"}

# Насколько баллы должны разойтись, чтобы считать, что набор различает модели.
SEPARATION = 0.05

ALL_CASES = tuple(bench_cases.CASES) + tuple(bench_hard.CASES)
CASE_BY_NAME = {case.name: case for case in ALL_CASES}


def case_of(name: str):
    return CASE_BY_NAME.get(name)


def level_of(row) -> int:
    """Уровень строки. Старый отчёт без уровней читается как базовый."""
    level = getattr(row, "level", None)
    if level:
        return int(level)
    case = case_of(row.case)
    return int(case.level) if case else 1


def weight_of(row) -> float:
    return LEVEL_WEIGHT.get(level_of(row), 1.0)


def weighted(rows: Sequence) -> dict[tuple[str, str], float]:
    """Взвешенное среднее по (этап, модель)."""
    bucket: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for row in rows:
        bucket.setdefault((row.stage, row.model), []).append(
            (row.score, weight_of(row))
        )
    out: dict[tuple[str, str], float] = {}
    for key, items in bucket.items():
        total = sum(w for _, w in items)
        out[key] = sum(s * w for s, w in items) / total if total else 0.0
    return out


def overall(rows: Sequence) -> dict[str, float]:
    """Взвешенный балл модели по всем этапам сразу."""
    bucket: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        bucket.setdefault(row.model, []).append((row.score, weight_of(row)))
    return {
        model: (sum(s * w for s, w in items) / sum(w for _, w in items))
        if items
        else 0.0
        for model, items in bucket.items()
    }


def by_level(rows: Sequence) -> dict[tuple[str, int], float]:
    """Профиль модели по уровням: где именно она сыпется."""
    bucket: dict[tuple[str, int], list[float]] = {}
    for row in rows:
        bucket.setdefault((row.model, level_of(row)), []).append(row.score)
    return {key: sum(v) / len(v) for key, v in bucket.items()}


def stability(rows: Sequence) -> dict[str, float | None]:
    """1 − средний размах баллов по одному кейсу между повторами.

    None, когда повтор был один: честнее пустая клетка, чем выдуманная
    единица [CORE-019].
    """
    bucket: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        bucket.setdefault((row.model, row.case), []).append(row.score)
    spreads: dict[str, list[float]] = {}
    for (model, _case), values in bucket.items():
        if len(values) < 2:
            continue
        spreads.setdefault(model, []).append(max(values) - min(values))
    out: dict[str, float | None] = {}
    for model in {row.model for row in rows}:
        items = spreads.get(model, [])
        out[model] = 1.0 - sum(items) / len(items) if items else None
    return out


def _twin_pairs() -> dict[str, list[str]]:
    pairs: dict[str, list[str]] = {}
    for case in ALL_CASES:
        if case.twin:
            pairs.setdefault(case.twin, []).append(case.name)
    return {twin: names for twin, names in pairs.items() if len(names) > 1}


def order_bias(rows: Sequence) -> dict[str, float | None]:
    """Доля пар «тот же вход, другой порядок», где ответ не изменился.

    Разные баллы у пары означают, что модель цепляется за позицию в списке, а
    не за смысл. None — пары в прогоне не участвовали.
    """
    means: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        means.setdefault((row.model, row.case), []).append(row.score)
    average = {key: sum(v) / len(v) for key, v in means.items()}
    out: dict[str, float | None] = {}
    for model in {row.model for row in rows}:
        hits: list[float] = []
        for names in _twin_pairs().values():
            values = [average[(model, n)] for n in names if (model, n) in average]
            if len(values) < 2:
                continue
            hits.append(1.0 if max(values) - min(values) < 1e-9 else 0.0)
        out[model] = sum(hits) / len(hits) if hits else None
    return out


def refusals(rows: Sequence) -> dict[str, float | None]:
    """Доля кейсов, где правильный ответ «не выбирать» или «спросить».

    Услужливая модель проваливает именно их: она всегда что-нибудь выбирает.
    """
    names = {case.name for case in ALL_CASES if case.refusal}
    out: dict[str, float | None] = {}
    for model in {row.model for row in rows}:
        values = [row.score for row in rows if row.model == model and row.case in names]
        out[model] = sum(values) / len(values) if values else None
    return out


def traps(rows: Sequence) -> dict[str, float | None]:
    """Доля пройденных ловушек: где успех — не поддаться тексту."""
    names = {case.name for case in ALL_CASES if case.trap}
    out: dict[str, float | None] = {}
    for model in {row.model for row in rows}:
        values = [row.score for row in rows if row.model == model and row.case in names]
        out[model] = sum(values) / len(values) if values else None
    return out


def separation(rows: Sequence) -> str:
    """Различает ли набор кейсов эти модели.

    Пустая строка — различает. Иначе готовая строка предупреждения: ничья
    означает не «модели равны», а «задачи слишком лёгкие», и молчать об этом
    нельзя, иначе выбор снова уедет в секундомер.
    """
    scores = overall(rows)
    if len(scores) < 2:
        return ""
    spread = max(scores.values()) - min(scores.values())
    if spread >= SEPARATION:
        return ""
    hard = [row for row in rows if level_of(row) >= 3]
    hint = (
        "сложных кейсов в прогоне не было"
        if not hard
        else "даже на сложных кейсах разброс {:.3f}".format(spread)
    )
    return (
        "Набор не различает эти модели: разброс итогового балла {:.3f} — {}. "
        "Выбирать по времени ответа можно, но это выбор по скорости, а не по "
        "качеству: нужны новые сложные кейсы.".format(spread, hint)
    )


def columns(rows: Sequence) -> dict[str, dict[str, float | None]]:
    """Отдельные колонки отчёта одной пачкой."""
    return {
        "стабильность": stability(rows),
        "порядок": order_bias(rows),
        "отказы": refusals(rows),
        "ловушки": traps(rows),
    }


def fmt(value: float | None) -> str:
    return "—" if value is None else "{:.2f}".format(value)


def levels_present(rows: Iterable) -> list[int]:
    return sorted({level_of(row) for row in rows})


__all__ = (
    "ALL_CASES",
    "LEVEL_RU",
    "LEVEL_WEIGHT",
    "SEPARATION",
    "by_level",
    "case_of",
    "columns",
    "fmt",
    "level_of",
    "levels_present",
    "order_bias",
    "overall",
    "refusals",
    "separation",
    "stability",
    "traps",
    "weighted",
)
