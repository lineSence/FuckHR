"""Признаки сгенерированного текста без модели: стилометрия отзыва правилами.

Этап `ai_text` спрашивает у модели, писала ли отзыв нейросеть. Вопрос узкий, а
ответ весит один балл в счёте компании (`company_score_rules`), — держать ради
него вызов на каждый текст дорого [CORE-016]. При этом генеративный текст
выдаёт себя вещами, которые считаются арифметикой [CORE-015]:

- **ровность.** У живого отзыва длины предложений скачут в разы: «Ужас. Зарплату
  задержали на два месяца, а потом сказали, что я сам виноват». У сгенерированного
  они выровнены — модель пишет период за периодом.
- **связки-вода.** «Кроме того», «важно отметить», «в целом», «не только… но и»,
  «стоит отметить» — в живой речи редкость, в тексте модели каркас.
- **нет конкретики.** Ни сумм, ни дат, ни имён, ни названий отделов. Человек
  жалуется на конкретное, модель — на «недостаточную прозрачность процессов».
- **нет разговорного.** Ни одной опечатки, ни сокращений, ни эмоций, ни
  многоточий и восклицаний, зато настоящее тире и «ё» на месте.
- **симметрия.** Ровно столько же плюсов, сколько минусов, и оба списка
  одинаковой длины — любимая композиция модели.

Счёт — доля сработавших признаков, приведённая к 0..1. Это не вердикт: сам по
себе счёт ничего не помечает, он идёт входом в общую оценку, как и остальные
детерминированные сигналы [CORE-019].

Замер по своей базе (разметка уже накоплена этапом, новых вызовов не нужно):
`python ai_text_rules.py` — AUROC правил на метках `judge_labels`.
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Sequence

import db
import linear_model
import settings

STAGE = "ai_text"
MIN_CHARS = 120  # короче — считать нечего, признаки шумят

FILLERS = (
    "кроме того", "важно отметить", "стоит отметить", "в целом", "таким образом",
    "следует отметить", "в заключение", "не только", "в первую очередь",
    "играет важную роль", "обратить внимание", "позволяет", "обеспечивает",
    "в современном", "динамично развива", "профессиональный рост",
    "комфортная атмосфера", "своевременн", "подводя итог",
)
COLLOQUIAL = (
    "))", "(((", "!!", "...", "блин", "короче", "типа", "вообще", "нафиг", "капец",
    "жесть", "норм", "офигел", "кстати", "щас", "прикин", "ужас", "кошмар",
)
PROS_CONS = ("плюсы", "минусы", "достоинства", "недостатки", "преимущества")
SENTENCE_SPLIT = re.compile(r"[.!?…]+")
WORD = re.compile(r"[а-яёa-z]+", re.IGNORECASE)
NUMBER = re.compile(r"\d")
NAME = re.compile(r"(?<![.!?]\s)(?<!^)\b[А-ЯЁ][а-яё]{2,}\b")


@dataclass(frozen=True)
class Signals:
    """Что сработало и какой вышел счёт. Список нужен для объяснения в отчёте."""

    score: float
    hits: tuple[str, ...]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in SENTENCE_SPLIT.split(text) if part.strip()]


def _evenness(sentences: Sequence[str]) -> float:
    """Ровность длин предложений: 1 — все одинаковые, 0 — разброс как у человека."""
    lengths = [len(WORD.findall(item)) for item in sentences if item]
    lengths = [value for value in lengths if value]
    if len(lengths) < 3:
        return 0.0
    mean = sum(lengths) / len(lengths)
    if mean <= 0:
        return 0.0
    deviation = math.sqrt(sum((value - mean) ** 2 for value in lengths) / len(lengths))
    # Коэффициент вариации у живых отзывов обычно выше 0.5.
    return max(0.0, 1.0 - deviation / mean / 0.5)


def signals(text: str) -> Signals:
    """Признаки генерации по одному тексту. Короткий текст — счёт 0 [CORE-017]."""
    body = (text or "").strip()
    if len(body) < MIN_CHARS:
        return Signals(0.0, ())
    low = body.lower()
    sentences = _sentences(body)
    words = WORD.findall(low)
    hits: list[str] = []

    fillers = sum(1 for item in FILLERS if item in low)
    if fillers >= 2:
        hits.append("связки-вода: {}".format(fillers))
    if not any(item in low for item in COLLOQUIAL):
        hits.append("ни одного разговорного оборота")
    if not NUMBER.search(body):
        hits.append("ни одной цифры")
    if not NAME.search(body):
        hits.append("ни одного имени собственного")
    even = _evenness(sentences)
    if even >= 0.5:
        hits.append("длины предложений выровнены")
    if len(sentences) >= 4 and all(len(WORD.findall(item)) >= 6 for item in sentences):
        hits.append("нет ни одного короткого предложения")
    if sum(1 for item in PROS_CONS if item in low) >= 2:
        hits.append("композиция «плюсы и минусы»")
    if "—" in body or "ё" in low:
        hits.append("типографика без опечаток")
    if words:
        unique = len(set(words)) / len(words)
        if unique >= 0.75 and len(words) >= 60:
            hits.append("словарь без повторов")

    # Признаков девять; счёт — их доля. Порог вердикта здесь не решается.
    return Signals(round(min(1.0, len(hits) / 9.0), 3), tuple(hits))


def score(text: str) -> float:
    return signals(text).score


def measure(conn: sqlite3.Connection) -> dict[str, object]:
    """AUROC правил на накопленной разметке. Ни сети, ни модели [CORE-019]."""
    rows = conn.execute(
        "SELECT text, verdict, source FROM judge_labels WHERE stage = ?", (STAGE,)
    ).fetchall()
    positive: list[float] = []
    negative: list[float] = []
    for row in rows:
        value = score(row["text"])
        (positive if row["verdict"] else negative).append(value)
    scores = positive + negative
    labels = [1] * len(positive) + [0] * len(negative)
    low, high = linear_model.choose(scores, labels)
    return {
        "positive": len(positive),
        "negative": len(negative),
        "auroc": linear_model.auroc(positive, negative),
        "low": low,
        "high": high,
        "wrong_at_half": linear_model.counts(scores, labels, 0.5)[0],
        "missed_at_half": linear_model.counts(scores, labels, 0.5)[1],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AUROC стилометрии на разметке ai_text")
    parser.add_argument("--db", default=settings.get("DB_PATH", "data/fuckhr.sqlite3"))
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    try:
        data = measure(conn)
    finally:
        conn.close()
    print(
        "разметка ai_text: {} «да», {} «нет»".format(data["positive"], data["negative"])
    )
    if data["auroc"] is None:
        print("одного из классов нет — мерить нечего")
        return 0
    print("AUROC правил: {}".format(data["auroc"]))
    print(
        "на пороге 0.5: ложных {}, пропущено {}".format(
            data["wrong_at_half"], data["missed_at_half"]
        )
    )
    print("пороги без ложных и без пропусков: low {}, high {}".format(data["low"], data["high"]))
    return 0


__all__ = ("MIN_CHARS", "STAGE", "Signals", "main", "measure", "score", "signals")


if __name__ == "__main__":
    raise SystemExit(main())
