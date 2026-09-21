"""Сборка синтетического датасета под файнтюн локальной модели (Unsloth/Colab).

    python training/make_dataset.py --out data/train

Промпты не переписываются руками. Каждый пример прогоняется через ту же
функцию пайплайна, что работает в проде, а вместо шлюза подставляется
перехватчик: он запоминает сообщения и отдаёт эталонный ответ. Поэтому в
датасете лежит ровно тот системный промпт и та обёртка `injection.safe`,
которые модель увидит в рантайме, а не их пересказ.

Тот же прогон работает проверкой: ответ возвращается в функцию, и если
пайплайн его отбрасывает (цитата не дословная, число выдумано, номер вне
списка), пример в файл не попадает [CORE-015], [CORE-019].

Данные полностью выдуманы: ни одной живой компании и ни одного живого
человека [CORE-013] — файл уезжает в облако Colab.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dataset_core  # noqa: E402
from training import dataset_cases as cases  # noqa: E402
from training import dataset_cases_owner as owner  # noqa: E402

# Сколько примеров на этап. Перекос в сторону extract и hr_filter намеренный:
# это самые частые вызовы пайплайна. Ловушки — отдельными строками, чтобы
# доля «правильный ответ — пустой список» была видна, а не растворилась.
PLAN = {
    "extract": 100,
    "extract_trap": 24,
    "hr_filter": 84,
    "hr_filter_trap": 26,
    "company": 60,
    "contacts": 70,
    "contacts_trap": 20,
    "dossier": 50,
    "review_fake": 64,
    "review_fake_trap": 16,
    "ai_text": 80,
    "draft": 90,
    "intake": 60,
    "resume_section": 45,
    "resume_tailor": 60,
}

def build() -> tuple[list[dict], list[str]]:
    """Все примеры плюс список отбракованных: почему пример не взят."""
    plan = [
        ("extract", cases.extract_cases(PLAN["extract"])),
        ("extract", cases.extract_traps(PLAN["extract_trap"])),
        ("hr_filter", cases.hr_filter_cases(PLAN["hr_filter"])),
        ("hr_filter", cases.hr_filter_traps(PLAN["hr_filter_trap"])),
        ("company", cases.company_cases(PLAN["company"])),
        ("contacts", cases.contacts_cases(PLAN["contacts"])),
        ("contacts", cases.contacts_traps(PLAN["contacts_trap"])),
        ("dossier", cases.dossier_cases(PLAN["dossier"])),
        ("review_fake", cases.review_fake_cases(PLAN["review_fake"])),
        ("review_fake", cases.review_fake_traps(PLAN["review_fake_trap"])),
        ("ai_text", cases.ai_text_cases(PLAN["ai_text"])),
        ("draft", owner.draft_cases(PLAN["draft"])),
        ("intake", owner.intake_cases(PLAN["intake"])),
        ("resume_section", owner.resume_section_cases(PLAN["resume_section"])),
        ("resume_tailor", owner.resume_tailor_cases(PLAN["resume_tailor"])),
    ]
    rows: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for stage, items in plan:
        for payload, gold in items:
            row, why = dataset_core.example(stage, payload, gold)
            if row is None:
                rejected.append(why)
                continue
            key = dataset_core.request_key(row)
            if key in seen:
                rejected.append("{}: дубль запроса".format(stage))
                continue
            seen.add(key)
            rows.append(row)
    return rows, rejected


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Синтетический датасет для файнтюна")
    parser.add_argument("--out", default="data/train", help="куда сложить jsonl")
    args = parser.parse_args(argv)

    rows, rejected = build()
    sizes = dataset_core.write(rows, ROOT / args.out)

    by_stage: dict[str, int] = {}
    for row in rows:
        by_stage[row["stage"]] = by_stage.get(row["stage"], 0) + 1
    print("примеров: {}".format(len(rows)))
    for stage, count in sorted(by_stage.items()):
        print("  {:<16}{}".format(stage, count))
    print("файлы: {}".format(", ".join("{} — {}".format(k, v) for k, v in sizes.items())))
    if rejected:
        print("отбраковано: {}".format(len(rejected)))
        for line in sorted(set(rejected)):
            print("  {} × {}".format(rejected.count(line), line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
