"""Стилометрия без модели: что она обещает.

Обещания скромные и проверяемые. Текст модели набирает заметно больше признаков,
чем живой отзыв. Короткий текст не оценивается вовсе — на трёх строках признаки
шумят. Счёт не вердикт: он остаётся числом от нуля до единицы, а решение
принимается снаружи [CORE-019].
"""

from __future__ import annotations

import sqlite3

import ai_text_rules
import judge_labels

GENERATED = (
    "Компания обеспечивает комфортную атмосферу и профессиональный рост. "
    "Важно отметить, что процессы выстроены прозрачно и понятно. Кроме того, "
    "руководство всегда открыто к диалогу с сотрудниками. В целом работа "
    "оставляет приятное впечатление и позволяет развиваться."
)
LIVE = (
    "Работал полгода. Зарплату задержали на 2 месяца!!! Начальник орет на всех. "
    "Ушел в декабре. Не советую, тут вечная текучка и переработки без оплаты."
)


def test_текст_модели_набирает_больше_примет() -> None:
    assert ai_text_rules.score(GENERATED) > ai_text_rules.score(LIVE) + 0.3


def test_короткий_текст_не_оценивается() -> None:
    assert ai_text_rules.signals("Нормально тут.") == ai_text_rules.Signals(0.0, ())


def test_счёт_остаётся_числом_от_нуля_до_единицы() -> None:
    for text in (GENERATED, LIVE, "", "а" * 500):
        assert 0.0 <= ai_text_rules.score(text) <= 1.0


def test_замер_считает_auroc_по_накопленной_разметке() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    judge_labels.ensure_schema(conn)
    judge_labels.record(conn, "ai_text", {GENERATED: True, LIVE: False})
    data = ai_text_rules.measure(conn)
    assert (data["positive"], data["negative"]) == (1, 1)
    assert data["auroc"] == 1.0
