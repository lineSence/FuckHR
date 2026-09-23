"""Сравнение решателя Laya с текущей моделью на этапах-классификаторах.

    python laya_bench.py                                  # только Laya
    python laya_bench.py --model qwen3:8b --route local   # Laya против модели
    python laya_bench.py --stages review_fake --cases my.json

Зачем отдельно от bench.py. Там сравниваются генеративные модели между собой
на всех этапах; здесь вопрос другой — стоит ли вообще заводить в проект вторую
модель ради двух этапов. Поэтому меряются те же кейсы, что в `bench_cases` и
`bench_hard`, но по правилу этих этапов: ложное срабатывание дороже пропуска
[CORE-019], и время на текст считается наравне с точностью.

Оценка детерминированная и та же, что в пайплайне: набор индексов «это реклама
/ это сгенерировано» сравнивается с ожиданием кейса. Laya в базу ничего не
пишет и ни на что не влияет, пока владелец не включит `LAYA_ENABLED`.

Сеть: Laya скачивает веса при первом запуске, сторона модели ходит к шлюзу.
Офлайн-проверки самой оценки — tests/test_laya_bench.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import bench_cases
import bench_hard
import laya_judge

log = logging.getLogger("laya_bench")

STAGES = ("review_fake", "ai_text")
PAYLOAD_KEY = {"review_fake": "reviews", "ai_text": "texts"}
REPORT_PATH = Path(__file__).resolve().parent / "data" / "bench" / "laya.json"

# Вес уровня тот же, что у bench_metrics: кейс третьего уровня весит втрое.
LEVEL_WEIGHT = {1: 1.0, 2: 2.0, 3: 3.0}


@dataclass(frozen=True)
class Batch:
    """Один кейс: тексты одной пачки и индексы, которые надо пометить."""

    name: str
    stage: str
    texts: tuple[str, ...]
    ad: frozenset[int]
    level: int = 1


@dataclass(frozen=True)
class Outcome:
    """Итог одной стороны сравнения на одном этапе."""

    side: str
    stage: str
    cases: int
    score: float
    false_alarms: int
    misses: int
    seconds: float
    per_text: float


def batches(stages: Sequence[str] = STAGES) -> tuple[Batch, ...]:
    """Кейсы проекта, развёрнутые в пачки текстов с ожиданием."""
    out: list[Batch] = []
    for case in tuple(bench_cases.CASES) + tuple(bench_hard.CASES):
        if case.stage not in stages:
            continue
        texts = tuple(case.payload.get(PAYLOAD_KEY[case.stage], ()))
        if not texts:
            continue
        out.append(
            Batch(
                name=case.name,
                stage=case.stage,
                texts=texts,
                ad=frozenset(case.expect.get("ad", ())),
                level=getattr(case, "level", 1),
            )
        )
    return tuple(out)


def batches_from_file(path: str | Path, stage: str) -> tuple[Batch, ...]:
    """Свои примеры: [{"name": "...", "texts": [...], "ad": [0]}, ...].

    Нужны, чтобы померить на своих отзывах, а не только на выдуманных кейсах:
    решение «заводить ли модель» принимается по своим данным.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Batch] = []
    for number, item in enumerate(raw, start=1):
        texts = tuple(str(text) for text in item.get("texts", ()))
        if not texts:
            continue
        out.append(
            Batch(
                name=str(item.get("name") or "свой кейс {}".format(number)),
                stage=str(item.get("stage") or stage),
                texts=texts,
                ad=frozenset(int(index) for index in item.get("ad", ())),
                level=int(item.get("level", 1)),
            )
        )
    return tuple(out)


def case_score(got: set[int], want: frozenset[int]) -> float:
    """Балл кейса. Лишний индекс обнуляет, пропущенный — половинит.

    То же правило, что у `bench_cases._check_review_fake`: на этих этапах
    ложное срабатывание дороже молчания, и две ошибки не равны.
    """
    if got - want:
        return 0.0
    return 1.0 if got == want else 0.5


def measure(
    side: str,
    predict: Callable[[Batch], set[int]],
    items: Sequence[Batch],
    stage: str,
) -> Outcome:
    """Прогон одной стороны по кейсам этапа. Падение кейса — ноль, не срыв."""
    chosen = [batch for batch in items if batch.stage == stage]
    weight = total = 0.0
    false_alarms = misses = texts = 0
    started = time.monotonic()
    for batch in chosen:
        try:
            got = set(predict(batch))
        except Exception as exc:  # noqa: BLE001 — [CORE-017]
            log.warning("%s: кейс %s не отработал: %s", side, batch.name, exc)
            got = set()
        false_alarms += len(got - batch.ad)
        misses += len(batch.ad - got)
        texts += len(batch.texts)
        level = LEVEL_WEIGHT.get(batch.level, 1.0)
        weight += level
        total += level * case_score(got, batch.ad)
    seconds = time.monotonic() - started
    return Outcome(
        side=side,
        stage=stage,
        cases=len(chosen),
        score=round(total / weight, 3) if weight else 0.0,
        false_alarms=false_alarms,
        misses=misses,
        seconds=round(seconds, 2),
        per_text=round(seconds / texts, 3) if texts else 0.0,
    )


def compare(
    sides: dict[str, Callable[[Batch], set[int]]],
    items: Sequence[Batch],
    stages: Sequence[str] = STAGES,
) -> list[Outcome]:
    """Все стороны на всех этапах. Порядок сторон сохраняется."""
    return [
        measure(side, predict, items, stage)
        for stage in stages
        for side, predict in sides.items()
        if any(batch.stage == stage for batch in items)
    ]


def verdict(rows: Sequence[Outcome], stage: str) -> str:
    """Вывод по этапу словами, а не цифрой: считает владелец, пишет код."""
    here = [row for row in rows if row.stage == stage]
    if len(here) < 2:
        return "сравнивать не с чем: сторона одна"
    best = max(here, key=lambda row: row.score)
    other = min(here, key=lambda row: row.score)
    if abs(best.score - other.score) < 0.05:
        fast = min(here, key=lambda row: row.per_text)
        return "ничья по точности, быстрее {} ({} с на текст)".format(fast.side, fast.per_text)
    return "точнее {} ({} против {})".format(best.side, best.score, other.score)


def render(rows: Sequence[Outcome]) -> str:
    """Таблица для терминала."""
    if not rows:
        return "нечего сравнивать: кейсов нет"
    head = "{:<22} {:<12} {:>6} {:>6} {:>8} {:>8} {:>9}".format(
        "сторона", "этап", "кейсов", "балл", "ложных", "пропущ", "с/текст"
    )
    lines = [head, "-" * len(head)]
    for row in rows:
        lines.append(
            "{:<22} {:<12} {:>6} {:>6} {:>8} {:>8} {:>9}".format(
                row.side[:22],
                row.stage[:12],
                row.cases,
                row.score,
                row.false_alarms,
                row.misses,
                row.per_text,
            )
        )
    lines.append("")
    for stage in sorted({row.stage for row in rows}):
        lines.append("{}: {}".format(stage, verdict(rows, stage)))
    lines.append("")
    lines.append(
        "Балл: ложное срабатывание обнуляет кейс, пропуск половинит. "
        "Цифры — этот прогон на этих кейсах, не измерение пайплайна [CORE-019]."
    )
    return "\n".join(lines)


def save_report(rows: Sequence[Outcome], path: str | Path = REPORT_PATH) -> Path:
    """Отчёт рядом с отчётом bench.py: чтобы было что сравнить через месяц."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def laya_side(agent: Any, limit: float | None = None) -> Callable[[Batch], set[int]]:
    """Сторона Laya: один вопрос на текст, ответ — вероятность «да»."""

    def predict(batch: Batch) -> set[int]:
        return laya_judge.flagged(batch.texts, batch.stage, agent=agent, limit=limit)

    return predict


def probe(agent: Any, items: Sequence[Batch]) -> dict[str, list[tuple[float | None, float | None]]]:
    """Вероятности при обоих порядках вариантов: один вызов на текст на все пороги."""
    return {
        batch.name: [laya_judge.pair(text, batch.stage, agent=agent) for text in batch.texts]
        for batch in items
    }


def side_from_probe(
    cache: dict[str, list[tuple[float | None, float | None]]], limit: float
) -> Callable[[Batch], set[int]]:
    """Сторона по уже посчитанным вероятностям прямого порядка и порогу."""

    def predict(batch: Batch) -> set[int]:
        values = cache.get(batch.name, [])
        return {
            index
            for index, (value, _) in enumerate(values)
            if value is not None and value >= limit
        }

    return predict


def auroc(positive: Sequence[float], negative: Sequence[float]) -> float | None:
    """Доля пар «рекламный выше живого». 0.5 — сигнала нет, порог его не создаст."""
    if not positive or not negative:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positive for n in negative)
    return round(wins / (len(positive) * len(negative)), 3)


def order_check(
    cache: dict[str, list[tuple[float | None, float | None]]],
    items: Sequence[Batch],
    stage: str,
) -> str:
    """Читает ли решатель вопрос или смотрит на позицию варианта.

    Порог сдвигает только точку отсечения. Если при перевёрнутом порядке
    ранжирование переворачивается, решает позиция, и перебор порогов бесполезен.
    """
    straight: dict[int, list[float]] = {0: [], 1: []}
    flipped: dict[int, list[float]] = {0: [], 1: []}
    for batch in items:
        if batch.stage != stage:
            continue
        for index, (a, b) in enumerate(cache.get(batch.name, [])):
            if a is None or b is None:
                continue
            label = int(index in batch.ad)
            straight[label].append(a)
            flipped[label].append(b)
    one, two = auroc(straight[1], straight[0]), auroc(flipped[1], flipped[0])
    if one is None or two is None:
        return "{}: порядок вариантов не проверить — нужны и рекламные, и живые тексты".format(stage)
    if min(one, two) >= 0.7:
        tail = "решатель читает вопрос, порог имеет смысл подбирать"
    elif (one - 0.5) * (two - 0.5) < 0 and abs(one - two) >= 0.4:
        tail = "ответ задаёт позиция варианта, а не смысл: zero-shot непригоден, нужен дообученный чекпойнт"
    else:
        tail = "сигнала нет ни при каком пороге"
    return "{}: AUROC прямой {}, перевёрнутый {} — {}".format(stage, one, two, tail)


def sweep(
    agent: Any,
    items: Sequence[Batch],
    limits: Sequence[float],
    stages: Sequence[str] = STAGES,
) -> list[Outcome]:
    """Один и тот же прогон при разных порогах: где решатель перестаёт шуметь.

    Модель зовётся один раз на текст, дальше считается арифметика: перебор
    порогов не должен стоить ещё десяти прогонов.
    """
    cache = probe(agent, items)
    sides = {
        "laya@{:g}".format(limit): side_from_probe(cache, limit) for limit in limits
    }
    for stage in stages:
        log.info(order_check(cache, items, stage))
    return compare(sides, items, stages)


def model_side(gateway: Any) -> Callable[[Batch], set[int]]:
    """Сторона текущей модели: те же функции, что работают в пайплайне."""

    def predict(batch: Batch) -> set[int]:
        if batch.stage == "review_fake":
            import fake_llm  # noqa: PLC0415 — нужен только этой стороне
            import reviewitems  # noqa: PLC0415

            items = tuple(
                reviewitems.ReviewItem(
                    url="https://example/{}".format(index), index=index, body=text
                )
                for index, text in enumerate(batch.texts)
            )
            return set(fake_llm.ad_indexes(gateway, items, force=True))
        import aitext_llm  # noqa: PLC0415

        return set(
            aitext_llm.generated_indexes(gateway, dict(enumerate(batch.texts)), force=True)
        )

    return predict


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Laya против текущей модели")
    parser.add_argument("--stages", default=",".join(STAGES), help="через запятую")
    parser.add_argument("--model", default="", help="имя модели для второй стороны")
    parser.add_argument("--route", default="local", choices=("local", "proxy"))
    parser.add_argument("--laya-model", default="", help="чекпойнт Laya")
    parser.add_argument("--threshold", type=float, default=None, help="порог «да»")
    parser.add_argument("--cases", default="", help="свои кейсы в JSON")
    parser.add_argument("--json", action="store_true", help="печатать отчёт как JSON")
    parser.add_argument(
        "--sweep",
        default="",
        help="пороги через запятую: один прогон, несколько порогов (0.5,0.7,0.9)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stages = tuple(stage.strip() for stage in args.stages.split(",") if stage.strip())
    unknown = [stage for stage in stages if stage not in STAGES]
    if unknown:
        print("неизвестные этапы: {}".format(", ".join(unknown)))
        return 2

    items = batches(stages)
    if args.cases:
        path = Path(args.cases)
        if not path.exists():
            print(
                "файла с кейсами нет: {}\n"
                "формат: [{{\"name\": \"мои отзывы\", \"texts\": [\"...\"], \"ad\": [1]}}]".format(
                    path
                )
            )
            return 2
        try:
            items = items + batches_from_file(path, stages[0])
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            print("кейсы не разобрались ({}): {}".format(path, exc))
            return 2

    agent = laya_judge.load(args.laya_model or None)
    if agent is None:
        print(
            "Laya недоступна: pip install laya и проверь имя чекпойнта "
            "(--laya-model, по умолчанию {}).".format(laya_judge.DEFAULT_MODEL)
        )
        return 1

    if args.sweep:
        limits = [float(part) for part in args.sweep.split(",") if part.strip()]
        rows = sweep(agent, items, limits, stages)
    else:
        sides: dict[str, Callable[[Batch], set[int]]] = {
            "laya": laya_side(agent, args.threshold)
        }
        if args.model:
            try:
                import bench  # noqa: PLC0415 — нужен только при второй стороне
                import llm  # noqa: PLC0415
            except ImportError as exc:
                print(
                    "сторона модели не поехала: в этом окружении нет зависимостей "
                    "проекта ({}).\nПоставь их рядом с Laya: python -m pip install "
                    "-r requirements.txt".format(exc.name or exc)
                )
                return 1

            route = llm.ROUTE_LOCAL if args.route == "local" else llm.ROUTE_PROXY
            sides[args.model] = model_side(bench.gateway_for(args.model, route))

        rows = compare(sides, items, stages)
    path = save_report(rows)
    print(json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2) if args.json else render(rows))
    print("\nотчёт: {}".format(path))
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа
    sys.exit(main())
