"""Обученный гейт пустоты: метки из пайплайна, порог из замера, снятие этапа."""

from __future__ import annotations

import sqlite3

import db
import detector
import embeddings_store as store
import review_gate_store
import stage_gate_train as train
import stage_gates


def база() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    detector.ensure_schema(conn)
    store.ensure_schema(conn)
    review_gate_store.ensure_schema(conn)
    return conn


def вакансия(conn: sqlite3.Connection, key: str, company: str) -> None:
    conn.execute(
        "INSERT INTO vacancies (key, source, external_id, url, title, company,"
        " first_seen_at, last_seen_at) VALUES (?, 'hh', ?, '', 'Питонист', ?,"
        " '2026-09-24', '2026-09-24')",
        (key, key, company),
    )
    conn.commit()


def сигнал(conn: sqlite3.Connection, key: str, payload: str) -> None:
    conn.execute(
        "INSERT INTO vacancy_signals (key, created_at, flags, payload)"
        " VALUES (?, '2026-09-24', '', ?)",
        (key, payload),
    )
    conn.commit()


def test_метки_берутся_из_результатов_этапа():
    conn = база()
    вакансия(conn, "a", "Ромашка")
    вакансия(conn, "b", "Ромашка")
    сигнал(conn, "a", '{"findings": [{"kind": "llm_claim"}]}')
    сигнал(conn, "b", '{"findings": [{"kind": "cliche"}]}')
    assert stage_gates.outcomes(conn, "hr_filter") == {"a": True, "b": False}


def test_неизвестный_этап_меток_не_даёт():
    assert stage_gates.outcomes(база(), "draft") == {}


def test_датасет_делит_по_работодателям_и_считает_безвекторные():
    conn = база()
    for n in range(6):
        company = "Компания {}".format(n)
        вакансия(conn, "v{}".format(n), company)
        сигнал(
            conn,
            "v{}".format(n),
            '{"findings": [{"kind": "llm_claim"}]}' if n % 2 else '{}',
        )
    # Вектор есть только у части вакансий: остальные должны попасть в счётчик.
    store.save(conn, store.KIND_VACANCY, "bge-m3", [("v0", [1.0, 0.0]), ("v1", [0.0, 1.0])])
    обучающая, отложенная, без_вектора = train.dataset(conn, "hr_filter", "bge-m3")
    assert без_вектора == 4
    assert len(обучающая) + len(отложенная) == 2
    # Деление устойчиво: та же компания всегда в той же части.
    assert train.is_test("Компания 0") == train.is_test("Компания 0")


def test_экономия_считает_снятое_и_потери():
    итог = train.skipped([0.05, 0.1, 0.9], [0, 1, 1], low=0.1)
    assert итог == {"снято": 2, "доля": 0.667, "потеряно_продуктивных": 1}
    assert train.skipped([0.5], [1], None) == {}


def test_обученный_гейт_снимает_этап(monkeypatch):
    conn = база()
    store.save(conn, store.KIND_VACANCY, "bge-m3", [("пусто", [-5.0]), ("богатая", [5.0])])
    review_gate_store.save(conn, "hr_filter", "bge-m3", [10.0], 0.0, low=0.2, high=0.8)
    monkeypatch.setattr(stage_gates.llm_embed, "model_name", lambda gateway: "bge-m3")
    monkeypatch.setenv("GATE_LEARNED", "1")
    assert stage_gates.learned_empty(conn, None, "hr_filter", "пусто") is True
    assert stage_gates.learned_empty(conn, None, "hr_filter", "богатая") is False
    # Выключенная настройка — гейт молчит даже с весами в базе.
    monkeypatch.delenv("GATE_LEARNED")
    assert stage_gates.learned_empty(conn, None, "hr_filter", "пусто") is False


def test_без_весов_и_без_порога_гейт_молчит(monkeypatch):
    conn = база()
    store.save(conn, store.KIND_VACANCY, "bge-m3", [("пусто", [-5.0])])
    monkeypatch.setattr(stage_gates.llm_embed, "model_name", lambda gateway: "bge-m3")
    monkeypatch.setenv("GATE_LEARNED", "1")
    assert stage_gates.learned_empty(conn, None, "hr_filter", "пусто") is False
    # Веса есть, а порога нет: подбирать его на глаз нельзя [CORE-017].
    review_gate_store.save(conn, "hr_filter", "bge-m3", [10.0], 0.0)
    assert stage_gates.learned_empty(conn, None, "hr_filter", "пусто") is False


def test_замер_без_продуктивных_говорит_что_мерить_нечего():
    conn = база()
    for n in range(4):
        вакансия(conn, "v{}".format(n), "Компания {}".format(n))
        сигнал(conn, "v{}".format(n), "{}")
    store.save(
        conn,
        store.KIND_VACANCY,
        "bge-m3",
        [("v{}".format(n), [float(n), 1.0]) for n in range(4)],
    )
    часть = train.evaluate(conn, "hr_filter", "bge-m3", epochs=5, save=False)
    # Продуктивных нет вовсе: что бы ни попало в отложенную часть, мерить нечем.
    assert "мерить" in часть["итог"]
    assert часть.get("auroc") is None
    assert "hr_filter" in train.render([часть])


def test_метки_extract_берутся_не_из_таблицы_условий():
    """Источники «отрабатывал» и «дал результат» обязаны быть разными.

    Пока оба читались из vacancy_conditions, отрицательных примеров не
    существовало физически: ключ появлялся там только вместе с условием.
    """
    conn = база()
    import conditions

    conditions.ensure_schema(conn)
    вакансия(conn, "с_условием", "Ромашка")
    вакансия(conn, "пустая", "Ромашка")
    conn.execute(
        "UPDATE vacancies SET description = 'есть описание' WHERE key IN"
        " ('с_условием', 'пустая')"
    )
    conn.execute(
        "INSERT INTO vacancy_conditions (key, field, value, quote)"
        " VALUES ('с_условием', 'salary', '100', 'зарплата 100')"
    )
    conn.commit()
    assert stage_gates.outcomes(conn, "extract") == {
        "с_условием": True,
        "пустая": False,
    }


def test_вакансия_без_описания_в_метки_не_идёт():
    conn = база()
    вакансия(conn, "без_описания", "Ромашка")
    assert stage_gates.outcomes(conn, "extract") == {}


def test_один_класс_в_замере_называется_вырожденным():
    conn = база()
    for n in range(4):
        вакансия(conn, "v{}".format(n), "Компания {}".format(n))
        сигнал(conn, "v{}".format(n), '{"findings": [{"kind": "llm_claim"}]}')
    store.save(
        conn,
        store.KIND_VACANCY,
        "bge-m3",
        [("v{}".format(n), [float(n) + 1.0, 1.0]) for n in range(4)],
    )
    часть = train.evaluate(conn, "hr_filter", "bge-m3", epochs=5, save=False)
    assert "одного класса нет" in часть.get("итог", "") or "мерить нечем" in часть.get("итог", "")
