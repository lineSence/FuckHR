"""Раздел «Гейт отзывов»: счётчики, поправки владельца, кнопки задач."""

from __future__ import annotations

import sqlite3

import judge_labels
import ui_gate
from fake_reviews import text_hash

ОТЗЫВ = "Работал год бэкендом, зарплату задерживали на неделю, задачи ставили криво."
РЕКЛАМА = "Лучшая компания мечты! Дружный коллектив, печеньки и бесконечный рост!"


def база() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    judge_labels.ensure_schema(conn)
    judge_labels.record(conn, "review_fake", {ОТЗЫВ: False, РЕКЛАМА: True}, "ООО Ромашка")
    return conn


def test_счётчики_считают_учителя_и_владельца_отдельно():
    conn = база()
    judge_labels.record(conn, "review_fake", {РЕКЛАМА: False}, "ООО Ромашка", "owner")
    данные = ui_gate.counts(conn)
    assert данные["review_fake"]["llm_yes"] == 1
    assert данные["review_fake"]["llm_no"] == 1
    assert данные["review_fake"]["own_no"] == 1
    assert данные["ai_text"]["companies"] == 0


def test_на_проверку_не_попадает_уже_поправленный_текст():
    conn = база()
    тексты = {row["text"] for row in ui_gate.unchecked(conn)}
    assert тексты == {ОТЗЫВ, РЕКЛАМА}
    judge_labels.record(conn, "review_fake", {РЕКЛАМА: False}, "ООО Ромашка", "owner")
    assert {row["text"] for row in ui_gate.unchecked(conn)} == {ОТЗЫВ}


def test_кнопка_ошибка_переворачивает_вердикт_а_верно_повторяет_его():
    conn = база()
    digest = text_hash(РЕКЛАМА)
    форма = {"stage": ["review_fake"], "hash": [digest], "verdict": ["flip"]}
    ui_gate.save_label(conn, форма)
    строки = {(r["source"], r["verdict"]) for r in judge_labels.rows(conn, "review_fake")
              if r["text_hash"] == digest}
    assert ("owner", 0) in строки and ("llm", 1) in строки

    форма = {"stage": ["review_fake"], "hash": [text_hash(ОТЗЫВ)],
             "verdict": ["same"]}
    ui_gate.save_label(conn, форма)
    assert any(r["source"] == "owner" and r["verdict"] == 0
               for r in judge_labels.rows(conn, "review_fake")
               if r["text"] == ОТЗЫВ)


def test_поправка_по_исчезнувшей_метке_не_роняет_страницу():
    conn = база()
    форма = {"stage": ["review_fake"], "hash": ["нет такого"], "verdict": ["flip"]}
    assert "warn" in ui_gate.save_label(conn, форма)


def test_страница_показывает_шаги_и_текст_отзыва():
    conn = база()
    html = ui_gate.render_gate(conn)
    assert "1. Разметка" in html and "3. Обучение гейта" in html
    assert "Лучшая компания мечты" in html
    # Положительных мало — страница говорит, сколько ещё нужно.
    assert "ещё" in html


def test_кнопки_задач_не_принимают_ничего_из_браузера(monkeypatch):
    вызовы = []

    class Задача:
        id = 7

    monkeypatch.setattr(ui_gate.jobs.runner, "start",
                        lambda task, extra=(): (вызовы.append((task, tuple(extra))), Задача())[1])
    assert ui_gate.start_train() == (7, "")
    assert вызовы[0] == ("gate-train", ())
