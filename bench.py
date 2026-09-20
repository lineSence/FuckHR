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
from pathlib import Path
from typing import Any, Sequence

import bench_cases
import bench_hard
import bench_metrics
import detector_llm
import dossier as dossier_mod
import intake
import llm
import llm_tasks
import resume
import resume_llm
from bench_cases import Case

# Полный набор: базовые задачи плюс сложные (уровни 2–3). Разделены по
# [CORE-024] и по смыслу: базовые отвечают «умеет ли вообще», сложные —
# «отличается ли качеством».
CASES: tuple[Case, ...] = tuple(bench_cases.CASES) + tuple(bench_hard.CASES)

log = logging.getLogger("bench")

STAGES = (
    "intake",
    "extract",
    "hr_filter",
    "company",
    "contacts",
    "dossier",
    "review_fake",
    "ai_text",
    "draft",
    "resume_section",
    "resume_tailor",
)

# Отчёт последнего прогона: его читает страница «Модель» (ui_bench).
REPORT_PATH = Path(__file__).resolve().parent / "data" / "bench" / "last.json"


@dataclass(frozen=True)
class Row:
    """Один прогон одного кейса на одной модели."""

    model: str
    stage: str
    case: str
    score: float
    note: str
    seconds: float
    level: int = 1


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
    if case.stage == "intake":
        return intake.ask(gateway, case.payload["text"], {})
    if case.stage == "hr_filter":
        return detector_llm.llm_claims(gateway, case.payload["text"])
    if case.stage == "resume_section":
        return resume_llm.draft_section(
            gateway,
            case.payload["section"],
            case.payload["answer"],
            case.payload.get("role_hint", ""),
        )
    if case.stage == "resume_tailor":
        blocks = [
            resume.Block(
                id=number,
                section=section,
                position=number,
                heading="",
                body=body,
                confirmed=True,
            )
            for number, (section, body) in enumerate(case.payload["blocks"], start=1)
        ]
        return resume_llm.pick_blocks(gateway, blocks, case.payload["vacancy"])
    if case.stage == "dossier":
        reviews = tuple(
            dossier_mod.Review(url="https://example/{}".format(i), site=site, body=body)
            for i, (site, body) in enumerate(case.payload["reviews"])
        )
        card = dossier_mod.Dossier(company=case.payload["company"], reviews=reviews)
        return dossier_mod.summarize(gateway, card)[0]
    if case.stage == "review_fake":
        import fake_llm
        import reviewitems

        items = tuple(
            reviewitems.ReviewItem(url="https://example/{}".format(i), index=i, body=body)
            for i, body in enumerate(case.payload["reviews"])
        )
        return fake_llm.ad_indexes(gateway, items, force=True)
    if case.stage == "ai_text":
        import aitext_llm

        texts = dict(enumerate(case.payload["texts"]))
        return aitext_llm.generated_indexes(gateway, texts, force=True)
    raise ValueError("неизвестный этап: {}".format(case.stage))


def run_model(
    model: str,
    cases: Sequence[Case] = CASES,
    route: str = llm.ROUTE_PROXY,
    repeat: int = 1,
    gateway: Any = None,
    on_row: Any = None,
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
                    level=int(case.level),
                )
            )
            if on_row is not None:
                on_row(rows[-1])
    return rows


def by_stage(rows: Sequence[Row]) -> dict[tuple[str, str], float]:
    """Балл по (этап, модель): взвешенное среднее по уровням сложности.

    Простое среднее вернулось бы к прежней беде: десять лёгких кейсов
    перевешивают три сложных, все приличные модели упираются в потолок, и
    выбор снова делается по секундомеру. Веса — в bench_metrics.LEVEL_WEIGHT.
    """
    return bench_metrics.weighted(rows)


def recommend(
    rows: Sequence[Row], tolerance: float = 0.05
) -> dict[str, tuple[str, float, float]]:
    """Модель на каждый этап: сначала балл, при почти равном балле — скорость.

    Настройка живёт на этапе (`llm.STAGE_MODEL_ENV`), а не только на профиле:
    у extract и resume_section один класс задачи, но победители разные.
    tolerance — насколько балл может быть ниже лучшего, чтобы считаться ничьей:
    две сотых на шести кейсах ничего не значат, а секунды на каждом вызове
    значат [CORE-016].
    """
    means = by_stage(rows)
    out: dict[str, tuple[str, float, float]] = {}
    for stage in {stage for stage, _ in means}:
        candidates = [(m, s) for (st, m), s in means.items() if st == stage]
        top = max(score for _, score in candidates)
        near = [(m, s) for m, s in candidates if s >= top - tolerance]
        model, score = min(
            near, key=lambda ms: (round(_speed(rows, stage, ms[0]), 2), ms[0])
        )
        out[stage] = (model, round(score, 3), round(_speed(rows, stage, model), 2))
    return out


def winners(rows: Sequence[Row]) -> dict[str, tuple[str, float]]:
    """Лучший балл на каждый этап. При равных баллах выигрывает быстрая."""
    return {stage: (model, score) for stage, (model, score, _) in recommend(rows, 0.0).items()}


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
    levels = bench_metrics.levels_present(rows)
    if len(levels) > 1:
        profile = bench_metrics.by_level(rows)
        lines.append(
            "| Модель | " + " | ".join(bench_metrics.LEVEL_RU[l] for l in levels) + " |"
        )
        lines.append("| --- " * (len(levels) + 1) + "|")
        for model in models:
            cells = [
                bench_metrics.fmt(profile.get((model, level))) for level in levels
            ]
            lines.append("| {} | {} |".format(model, " | ".join(cells)))
        lines.append("")

    extra = bench_metrics.columns(rows)
    names = list(extra)
    lines.append("| Модель | " + " | ".join(names) + " | сек |")
    lines.append("| --- " * (len(names) + 2) + "|")
    for model in models:
        cells = [bench_metrics.fmt(extra[name].get(model)) for name in names]
        speed = [r.seconds for r in rows if r.model == model]
        cells.append("{:.1f}".format(sum(speed) / len(speed)) if speed else "—")
        lines.append("| {} | {} |".format(model, " | ".join(cells)))
    lines.append("")
    lines.append(
        "Колонки справочные и в балл не входят: стабильность — повторы одного "
        "кейса, порядок — тот же список наоборот, отказы — кейсы, где верный "
        "ответ «никого» или «спросить»."
    )

    warning = bench_metrics.separation(rows)
    if warning:
        lines.append("")
        lines.append("**{}**".format(warning))

    lines.append("")
    lines.append("Подставить в настройки:")
    for stage, (model, score, seconds) in sorted(recommend(rows).items()):
        lines.append(
            "- {} = {} ({:.2f}, {:.1f} с) → {}".format(
                stage, model, score, seconds, llm.STAGE_MODEL_ENV.get(stage, "—")
            )
        )

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


def save_report(
    path: str,
    rows: Sequence[Row],
    models: Sequence[str],
    cases: Sequence[Case],
    repeat: int,
    route: str,
) -> None:
    """Отчёт для страницы «Модель»: строки плюс параметры прогона."""
    report = {
        "finished_at": time.time(),
        "models": list(models),
        "stages": sorted({c.stage for c in cases}),
        "repeat": repeat,
        "route": route,
        "rows": [row.__dict__ for row in rows],
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сравнение моделей на задачах пайплайна")
    parser.add_argument("--models", required=True, help="имена моделей через запятую")
    parser.add_argument("--route", default=llm.ROUTE_PROXY, choices=[llm.ROUTE_PROXY, llm.ROUTE_LOCAL])
    parser.add_argument("--stages", default="", help="этапы через запятую")
    parser.add_argument("--repeat", type=int, default=1, help="прогонов на кейс")
    parser.add_argument(
        "--levels",
        default="",
        help="уровни сложности через запятую (1 — быстрая проверка, по умолчанию все)",
    )
    parser.add_argument(
        "--json",
        default=str(REPORT_PATH),
        help="куда сложить отчёт (его читает страница «Модель»)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass

    wanted = [s.strip() for s in args.stages.split(",") if s.strip()]
    levels = {int(p) for p in args.levels.replace(" ", "").split(",") if p.isdigit()}
    cases = [
        c
        for c in CASES
        if (not wanted or c.stage in wanted) and (not levels or c.level in levels)
    ]
    if not cases:
        print("нет кейсов под такие этапы: {}".format(args.stages))
        return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    # Счётчик печатается в формате «[3/30]»: интерфейс вычитывает прогресс из
    # логов задачи, отдельного протокола между процессами нет.
    total = len(models) * len(cases) * max(1, args.repeat)
    state = {"done": 0}

    def tick(row: Row) -> None:
        state["done"] += 1
        print(
            "[{}/{}] {} · {} · {:.2f} за {:.1f} с".format(
                state["done"], total, row.model, row.case, row.score, row.seconds
            ),
            flush=True,
        )

    rows: list[Row] = []
    for model in models:
        print("гоняю {} ({} кейсов × {})".format(model, len(cases), args.repeat))
        rows.extend(run_model(model, cases, args.route, args.repeat, on_row=tick))

    print()
    print(render(rows))
    if args.json:
        save_report(args.json, rows, models, cases, args.repeat, args.route)
        print("\nотчёт: {}".format(args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = (
    "CASES",
    "REPORT_PATH",
    "Row",
    "by_stage",
    "gateway_for",
    "main",
    "recommend",
    "render",
    "run_case",
    "run_model",
    "save_report",
    "winners",
)
