"""Общая часть сборки датасета под файнтюн: перехватчик шлюза и проверка.

Одна идея на весь датасет: промпты не переписываются руками. Пример
прогоняется через ту же функцию пайплайна, что работает в проде, а вместо
шлюза подставляется перехватчик — он запоминает сообщения и отдаёт готовый
эталон. В файл попадает ровно тот системный промпт и та обёртка
`injection.safe`, которые модель увидит в рантайме.

Тот же прогон работает проверкой: эталон возвращается в функцию, и если
пайплайн его отбросил (цитата не дословная, число выдумано, номер вне
списка), пример в файл не идёт [CORE-015], [CORE-019].

Отсюда этим пользуются двое: `dataset_export.py` (примеры из своей базы) и
`training/make_dataset.py` (синтетика). Логика одна, поэтому и файл один
[CORE-001].
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import aitext_llm
import detector_llm
import dossier as dossier_mod
import fake_llm
import intake
import llm_tasks
import resume
import resume_llm
import reviewitems

VAL_SHARE = 0.1
SEED = 20260921


class Capture:
    """Шлюз-перехватчик: запоминает сообщения, отдаёт заранее готовый ответ."""

    enabled = True

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.seen: list[tuple[str, list[dict[str, str]]]] = []

    def complete(self, stage, messages, temperature=0.0):
        self.seen.append((stage, [dict(m) for m in messages]))
        return self.answer


@dataclass(frozen=True)
class Hit:
    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class Candidate:
    label: str


@dataclass(frozen=True)
class Draft:
    body: str


def gold_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _blocks(pairs):
    return [
        resume.Block(
            id=i, section=section, position=i, heading="", body=body, confirmed=True
        )
        for i, (section, body) in enumerate(pairs, start=1)
    ]


def run(stage: str, payload: dict, answer: str):
    """Тот же вызов, что делает пайплайн. Возвращает (перехватчик, результат)."""
    gw = Capture(answer)
    if stage == "extract":
        result = llm_tasks.extract_conditions(gw, payload["description"])
    elif stage == "hr_filter":
        result = detector_llm.llm_claims(gw, payload["text"])
    elif stage == "company":
        hits = [Hit(**hit) for hit in payload["hits"]]
        result = llm_tasks.company_brief(gw, payload["company"], hits)
    elif stage == "contacts":
        items = [Candidate(label) for label in payload["candidates"]]
        result = llm_tasks.pick_contact(gw, items, payload.get("role_hint", ""))
    elif stage == "dossier":
        reviews = tuple(
            dossier_mod.Review(
                url=review.get("url", "https://example.test/{}".format(i)),
                site=review.get("site", ""),
                body=review.get("body", ""),
                snippet=review.get("snippet", ""),
                title=review.get("title", ""),
            )
            if isinstance(review, dict)
            else dossier_mod.Review(
                url="https://example.test/{}".format(i), site=review[0], body=review[1]
            )
            for i, review in enumerate(payload["reviews"])
        )
        card = dossier_mod.Dossier(company=payload["company"], reviews=reviews)
        result = dossier_mod.summarize(gw, card)[0]
    elif stage == "review_fake":
        items = tuple(
            reviewitems.ReviewItem(
                url="https://example.test/{}".format(i), index=i, body=body
            )
            for i, body in enumerate(payload["reviews"])
        )
        result = fake_llm.ad_indexes(gw, items, force=True)
    elif stage == "ai_text":
        result = aitext_llm.generated_indexes(
            gw, dict(enumerate(payload["texts"])), force=True
        )
    elif stage == "draft":
        result = llm_tasks.polish_draft(gw, Draft(payload["body"]), payload["facts"])
    elif stage == "intake":
        result = intake.ask(gw, payload["text"], {})
    elif stage == "resume_section":
        result = resume_llm.draft_section(
            gw, payload["section"], payload["answer"], payload.get("role_hint", "")
        )
    elif stage == "resume_tailor":
        result = resume_llm.pick_blocks(
            gw, _blocks(payload["blocks"]), payload["vacancy"]
        )
    else:
        raise ValueError("нет такого этапа: {}".format(stage))
    return gw, result


def accepted(stage: str, payload: dict, gold, result) -> bool:
    """Принял ли пайплайн наш эталон. Отказ = пример учит плохому."""
    if stage == "extract":
        return len(result) == len(gold["conditions"])
    if stage == "hr_filter":
        return len(result) == len(gold["claims"])
    if stage == "company":
        return result is not None and len(result.lines) == len(gold["lines"])
    if stage == "contacts":
        return (
            result is not None
            and result.label == payload["candidates"][gold["choice"] - 1]
        )
    if stage == "dossier":
        return isinstance(result, str) and result.strip() == gold.strip()
    if stage in ("review_fake", "ai_text"):
        want = {
            item["id"]
            for item in gold["items"]
            if item["verdict"] in ("ad", "generated")
        }
        return set(result) == want
    if stage == "draft":
        # Откат пайплайна значит, что «улучшенное» письмо хуже исходного.
        return result.body.strip() == gold.strip()
    if stage == "intake":
        return result is not None and bool(getattr(result, "questions", ()))
    if stage == "resume_section":
        return result is not None and gold["text"].split("\n")[0][:20] in result[0]
    if stage == "resume_tailor":
        return list(result[0]) == gold["order"]
    return False


def example(stage: str, payload: dict, gold) -> tuple[dict | None, str]:
    """Готовая строка датасета или причина отказа.

    Вторым значением идёт причина: пустая строка — пример взят. Причины
    считаются пачками, иначе непонятно, почему из тысячи вакансий вышло сто
    примеров.
    """
    answer = gold if isinstance(gold, str) else gold_json(gold)
    try:
        gw, result = run(stage, payload, answer)
    except Exception as exc:  # noqa: BLE001 — одна кривая запись не валит выгрузку
        return None, "{}: пайплайн упал ({})".format(stage, type(exc).__name__)
    if not gw.seen:
        return None, "{}: пайплайн не дошёл до модели".format(stage)
    if not accepted(stage, payload, gold, result):
        return None, "{}: эталон отбракован пайплайном".format(stage)
    _, messages = gw.seen[0]
    row = {
        "stage": stage,
        "conversations": list(messages) + [{"role": "assistant", "content": answer}],
    }
    return row, ""


def request_key(row: dict) -> str:
    """По чему считаем дубли: последний запрос пользователя."""
    return row["conversations"][-2]["content"].strip()


def write(
    rows: Sequence[dict], out_dir: Path, val_share: float = VAL_SHARE, seed: int = SEED
) -> dict[str, int]:
    """Перемешивает и раскладывает на train/val. Возвращает размеры файлов."""
    out_dir.mkdir(parents=True, exist_ok=True)
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    cut = max(1, int(len(shuffled) * val_share)) if shuffled else 0
    parts = {"val.jsonl": shuffled[:cut], "train.jsonl": shuffled[cut:]}
    for name, part in parts.items():
        with (out_dir / name).open("w", encoding="utf-8") as fh:
            for row in part:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {name: len(part) for name, part in parts.items()}


__all__ = (
    "Capture",
    "accepted",
    "example",
    "gold_json",
    "request_key",
    "run",
    "write",
)
