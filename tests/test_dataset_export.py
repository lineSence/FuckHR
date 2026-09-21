"""Выгрузка датасета из своей базы: формат, эталоны и границы данных.

Сеть и модель здесь не трогаются: примеры собираются из строк базы, а вместо
шлюза везде работает перехватчик из dataset_core.
"""

from __future__ import annotations

import json
import sqlite3

import conditions
import dataset_core
import dataset_export
import db
import detector


DESCRIPTION = (
    "Ищем backend-разработчика в дружную команду. Формат работы гибридный: "
    "два дня в офисе на Белорусской, остальное из дома. Зарплата от 250 000 "
    "рублей на руки. Пишите на hr@example.com или звоните +7 999 123-45-67. "
    "Стек: Python, PostgreSQL, Kafka. Оформление по ТК РФ с первого дня."
)


def _conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conditions.ensure_schema(conn)
    detector.ensure_schema(conn)
    return conn


def _vacancy(conn: sqlite3.Connection, key: str, company: str = "ООО Ромашка") -> None:
    conn.execute(
        "INSERT INTO vacancies (key, source, external_id, url, title, company,"
        " description, first_seen_at, last_seen_at)"
        " VALUES (?, 'hh', ?, 'https://hh.ru/vacancy/1', 'Backend-разработчик',"
        " ?, ?, '2026-01-01', '2026-01-02')",
        (key, key, company, DESCRIPTION),
    )
    conn.commit()


def test_условие_из_базы_становится_примером() -> None:
    conn = _conn()
    _vacancy(conn, "hh:1")
    conn.execute(
        "INSERT INTO vacancy_conditions (key, field, value, quote)"
        " VALUES ('hh:1', 'format', 'гибрид два дня в офисе',"
        " 'Формат работы гибридный')"
    )
    conn.commit()

    rows, rejected = dataset_export.build(conn, ["extract"], cap=10)

    assert len(rows) == 1, rejected
    row = rows[0]
    assert row["stage"] == "extract"
    roles = [m["role"] for m in row["conversations"]]
    assert roles == ["system", "user", "assistant"]
    gold = json.loads(row["conversations"][-1]["content"])
    assert gold["conditions"][0]["quote"] == "Формат работы гибридный"


def test_контакты_маскируются_и_в_тексте_и_в_цитате() -> None:
    conn = _conn()
    _vacancy(conn, "hh:2")
    conn.execute(
        "INSERT INTO vacancy_conditions (key, field, value, quote)"
        " VALUES ('hh:2', 'process', 'писать на почту',"
        " 'Пишите на hr@example.com или звоните +7 999 123-45-67')"
    )
    conn.commit()

    rows, _ = dataset_export.build(conn, ["extract"], cap=10)

    text = json.dumps(rows[0], ensure_ascii=False)
    assert "hr@example.com" not in text
    assert "999 123-45-67" not in text
    # Цитата осталась дословной относительно замаскированного текста, иначе
    # пайплайн отбросил бы пример и строки бы не было.
    assert rows[0]["conversations"][-1]["content"].count("example.test") == 1


def test_утверждения_детектора_становятся_примером_hr_filter() -> None:
    conn = _conn()
    _vacancy(conn, "hh:3")
    payload = {
        "key": "hh:3",
        "findings": [
            {
                "kind": "llm_claim",
                "claimed": "Оформление по ТК РФ с первого дня",
                "found": "модель отметила утверждение «обещание оформления»; проверить нечем",
                "verdict": detector.NO_DATA,
            },
            {"kind": "salary", "claimed": "зарплата", "found": "ниже рынка"},
        ],
    }
    detector_store = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        "INSERT INTO vacancy_signals (key, created_at, flags, payload)"
        " VALUES ('hh:3', '2026-01-02', '', ?)",
        (detector_store,),
    )
    conn.commit()

    rows, rejected = dataset_export.build(conn, ["hr_filter"], cap=10)

    assert len(rows) == 1, rejected
    gold = json.loads(rows[0]["conversations"][-1]["content"])
    assert gold["claims"] == [
        {
            "label": "обещание оформления",
            "quote": "Оформление по ТК РФ с первого дня",
        }
    ]


def test_один_работодатель_не_забивает_корпус() -> None:
    conn = _conn()
    for number in range(dataset_export.PER_COMPANY + 5):
        key = "hh:{}".format(number)
        _vacancy(conn, key)
        conn.execute(
            "INSERT INTO vacancy_conditions (key, field, value, quote)"
            " VALUES (?, 'format', 'гибрид', 'Формат работы гибридный')",
            (key,),
        )
    conn.commit()

    rows, _ = dataset_export.build(conn, ["extract"], cap=100)

    # Все описания одинаковые, поэтому дубли режутся ещё раньше потолка.
    assert len(rows) <= dataset_export.PER_COMPANY


def test_персональные_этапы_не_выгружаются() -> None:
    # Контакты, письма и резюме в облако не уезжают [CORE-012], [CORE-021].
    for stage in ("contacts", "draft", "intake", "resume_section", "resume_tailor"):
        assert stage not in dataset_export.SOURCES


def test_эталон_отбракованный_пайплайном_не_попадает_в_файл() -> None:
    conn = _conn()
    _vacancy(conn, "hh:9")
    conn.execute(
        "INSERT INTO vacancy_conditions (key, field, value, quote)"
        " VALUES ('hh:9', 'salary', 'от 400 000', 'зарплата от 400 000 рублей')"
    )
    conn.commit()

    rows, rejected = dataset_export.build(conn, ["extract"], cap=10)

    assert rows == []
    assert rejected == {"extract: эталон отбракован пайплайном": 1}


def test_файлы_делятся_на_train_и_val(tmp_path) -> None:
    rows = [
        {
            "stage": "extract",
            "conversations": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u{}".format(i)},
                {"role": "assistant", "content": "a"},
            ],
        }
        for i in range(20)
    ]
    sizes = dataset_core.write(rows, tmp_path)

    assert sizes == {"val.jsonl": 2, "train.jsonl": 18}
    first = json.loads((tmp_path / "train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["conversations"][0]["role"] == "system"


def test_блок_интерфейса_считает_что_есть_в_базе() -> None:
    import dossier
    import ui_dataset

    conn = _conn()
    dossier.ensure_schema(conn)
    _vacancy(conn, "hh:10")
    conn.execute(
        "INSERT INTO vacancy_conditions (key, field, value, quote)"
        " VALUES ('hh:10', 'format', 'гибрид', 'Формат работы гибридный')"
    )
    conn.commit()

    assert ui_dataset.available(conn)["extract"] == 1

    block = ui_dataset.render_dataset(conn)
    assert 'action="/dataset"' in block
    assert "Датасет для дообучения" in block


def test_кнопки_датасета_нет_на_странице_запуска() -> None:
    # Задача без формы бессмысленна: потолок задаётся на странице «Модель».
    import jobs

    assert "dataset" in jobs.TASKS
    assert "dataset" not in [key for key, _, _ in jobs.task_list()]
