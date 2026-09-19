"""Сравнение моделей на задачах пайплайна: какая куда годится.

    python bench.py --models qwen2.5-7b,gpt-4o-mini        # обе через прокси
    python bench.py --models llama3.1 --route local        # локальный адрес
    python bench.py --models a,b --stages extract,draft --repeat 3

Зачем. Профили FAST/SMART/LONG/LOCAL ([LLM-003]) отвечают на вопрос «какого
класса модель нужна этапу», но не на вопрос «какая именно». Ответ на второй
зависит от языка, длины контекста и склонности выдумывать, и проверяется только
прогоном. Оценка детерминированная: те же проверки, что стоят в пайплайне —
цитата дословно, никаких новых чисел, адресат из списка.

Это не юнит-тесты: команда ходит в сеть и тратит вызовы. Офлайн-проверки самой
оценки лежат в tests/test_bench.py.

Кэш ответов намеренно выключен: сравнивать нужно модели, а не попадания в кэш.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Sequence

import bench_cases
import llm
import llm_tasks
from bench_cases import CASES, Case

log = logging.getLogger("bench")

STAGES = ("extract", "company", "contacts", "draft")


@dataclass(frozen=True)
class Row:
    """Один прогон одного кейса на одной модели."""

    model: str
    stage: str
    case: str
    score: float
    note: str
    seconds: float


def gateway_for(model: str, route: str = llm.ROUTE_PROXY) -> llm.Gateway:
    """Шлюз, у которого все профили указывают на одну модель.

    personal_via_proxy включён осознанно: кейсы выдуманы и персональных данных
    не содержат, иначе этапы contacts/draft на прокси просто не поехали бы
    [CORE-012]. Для локального маршрута имя модели берёт сам адрес.
    """
    models = {profile: model for profile in llm.PROXY_MODEL_ENV}
    if route == llm.ROUTE_LOCAL:
        return llm.Gateway(
            base_url=os.getenv("LLM_BASE_URL") or None,
            api_key=os.getenv("LLM_API_KEY") or None,
            timeout=float(os.getenv("LLM_TIMEOUT", "60")),
            max_calls=10_000,
        )
    return llm.Gateway(
        proxy_base_url=os.getenv("LLM_PROXY_BASE_URL") or None,
        proxy_api_key=os.getenv("LLM_PROXY_API_KEY") or None,
        proxy_models=models,
        personal_via_proxy=True,
        timeout=float(os.getenv("LLM_TIMEOUT", "60")),
        max_calls=10_000,
    )


def run_case(gateway: Any, case: Case) -> Any:
    """Гоняет кейс через ту же функцию пайплайна, что работает в проде."""
    if case.stage == "extract":
        return llm_tasks.extract_conditions(gateway, case.payload["description"])
    if case.stage == "company":
        return llm_tasks.company_brief(
            gateway, case.payload["company"], case.payload["hits"]
        )
    if case.stage == "contacts":
        return llm_tasks.pick_contact(
            gateway, case.payload["candidates"], case.payload.get("role_hint", "")
        )
    if case.stage == "draft":
        draft = bench_cases.Draft(body=case.payload["body"])
        return llm_tasks.polish_draft(gateway, draft, case.payload.get("facts", ()))
    raise ValueError("неизвестный этап: {}".format(case.stage))


def run_model(
    model: str,
    cases: Sequence[Case] = CASES,
    route: str = llm.ROUTE_PROXY,
    repeat: int = 1,
    gateway: Any = None,
) -> list[Row]:
    """Прогон всех кейсов. Падение модели — ноль по кейсу, а не конец бенчмарка."""
    gw = gateway or gateway_for(model, route)
    rows: list[Row] = []
    for _ in range(max(1, repeat)):
        for case in cases:
            started = time.monotonic()
            try:
                result = run_case(gw, case)
                score, note = bench_cases.check(case, result)
            except Exception as exc:  # noqa: BLE001 — сравнение не должно падать
                log.warning("%s на кейсе %s: %s", model, case.name, exc)
                score, note = 0.0, "ошибка: {}".format(exc)[:80]
            rows.append(
                Row(
                    model=model,
                    stage=case.stage,
                    case=case.name,
                    score=round(score, 3),
                    note=note,
                    seconds=round(time.monotonic() - started, 2),
                )
            )
    return rows


def by_stage(rows: Sequence[Row]) -> dict[tuple[str, str], float]:
    """Средний балл по (этап, модель)."""
    bucket: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        bucket.setdefault((row.stage, row.model), []).append(row.score)
    return {key: sum(v) / len(v) for key, v in bucket.items()}


def winners(rows: Sequence[Row]) -> dict[str, tuple[str, float]]:
    """Лучшая модель на каждый этап. При равных баллах выигрывает быстрая."""
    means = by_stage(rows)
    best: dict[str, tuple[float, float, str]] = {}
    for (stage, model), score in means.items():
        seconds = _speed(rows, stage, model)
        key = (score, -seconds, model)
        if stage not in best or key > best[stage]:
            best[stage] = key
    return {stage: (key[2], round(key[0], 3)) for stage, key in best.items()}


def _speed(rows: Sequence[Row], stage: str, model: str) -> float:
    values = [r.seconds for r in rows if r.stage == stage and r.model == model]
    return sum(values) / len(values) if values else 0.0


def render(rows: Sequence[Row]) -> str:
    """Отчёт в markdown: сводка по этапам, победители и разбор кейсов."""
    models = sorted({row.model for row in rows})
    stages = [s for s in STAGES if any(r.stage == s for r in rows)]
    means = by_stage(rows)

    head = "| Этап | " + " | ".join(models) + " |"
    sep = "| --- " * (len(models) + 1) + "|"
    lines = [head, sep]
    for stage in stages:
        cells = []
        for model in models:
            value = means.get((stage, model))
            cells.append("—" if value is None else "{:.2f}".format(value))
        lines.append("| {} | {} |".format(stage, " | ".join(cells)))

    lines.append("")
    for stage, (model, score) in sorted(winners(rows).items()):
        lines.append("- **{}** → {} ({:.2f})".format(stage, model, score))

    lines.append("")
    lines.append("| Модель | Кейс | Балл | Сек | Что вышло |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in rows:
        lines.append(
            "| {} | {} | {:.2f} | {} | {} |".format(
                row.model, row.case, row.score, row.seconds, row.note
            )
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сравнение моделей на задачах пайплайна")
    parser.add_argument("--models", required=True, help="имена моделей через запятую")
    parser.add_argument("--route", default=llm.ROUTE_PROXY, choices=[llm.ROUTE_PROXY, llm.ROUTE_LOCAL])
    parser.add_argument("--stages", default="", help="этапы через запятую")
    parser.add_argument("--repeat", type=int, default=1, help="прогонов на кейс")
    parser.add_argument("--json", default="", help="куда сложить сырые строки")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass

    wanted = [s.strip() for s in args.stages.split(",") if s.strip()]
    cases = [c for c in CASES if not wanted or c.stage in wanted]
    if not cases:
        print("нет кейсов под такие этапы: {}".format(args.stages))
        return 2

    rows: list[Row] = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print("гоняю {} ({} кейсов × {})".format(model, len(cases), args.repeat))
        rows.extend(run_model(model, cases, args.route, args.repeat))

    print()
    print(render(rows))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([row.__dict__ for row in rows], fh, ensure_ascii=False, indent=2)
        print("\nсырые строки: {}".format(args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ("Row", "by_stage", "gateway_for", "main", "render", "run_case", "run_model", "winners")
