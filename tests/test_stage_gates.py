"""Гейт перед этапами модели: экономит вызовы и не теряет вакансии молча.

Сети нет: вместо эмбеддера подставлен шлюз с готовыми векторами. Проверяется
главное свойство — по умолчанию гейт выключен и список проходит насквозь,
а включённый гейт объясняет каждый отказ.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

import conditions
import db
import embeddings_store as store
import extract_spans
import stage_gates
from llm_tasks import Condition

MODEL = "bge-m3"


@dataclass(frozen=True)
class FakeVacancy:
    key: str
    title: str = "Python-разработчик"
    company: str = "Контора"
    description: str = "Описание вакансии"


class FakeGateway:
    """Шлюз, у которого есть только имя модели эмбеддера."""

    class _Route:
        name = "local"
        base_url = "http://localhost/v1"
        api_key = ""
        model = MODEL

    timeout = 5.0

    def route_for(self, stage: str):
        return self._Route() if stage == "embeddings" else None


def _conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conditions.ensure_schema(conn)
    store.ensure_schema(conn)
    return conn


def _vector(conn: sqlite3.Connection, kind: str, key: str, values) -> None:
    store.save(conn, kind, MODEL, [(key, values)])


def test_по_умолчанию_гейт_пропускает_всё(monkeypatch) -> None:
    monkeypatch.delenv("GATE_PROFILE_MIN", raising=False)
    conn = _conn()
    items = [FakeVacancy("hh:1"), FakeVacancy("hh:2")]

    assert stage_gates.keep_for_stage(conn, FakeGateway(), "extract", items) == items


def test_далёкая_от_профиля_вакансия_не_идёт_в_модель(monkeypatch) -> None:
    monkeypatch.setenv("GATE_PROFILE_MIN", "0.5")
    conn = _conn()
    _vector(conn, stage_gates.KIND_OWNER, "profile:6", [1.0, 0.0])
    _vector(conn, store.KIND_VACANCY, "hh:near", [1.0, 0.0])
    _vector(conn, store.KIND_VACANCY, "hh:far", [0.0, 1.0])

    monkeypatch.setattr(stage_gates, "owner_text", lambda *a, **kw: "python")
    kept = stage_gates.keep_for_stage(
        conn, FakeGateway(), "extract", [FakeVacancy("hh:near"), FakeVacancy("hh:far")]
    )

    assert [item.key for item in kept] == ["hh:near"]


def test_без_вектора_вакансия_проходит(monkeypatch) -> None:
    # Нет данных — нет решения: гейт не имеет права резать вслепую [CORE-017].
    monkeypatch.setenv("GATE_PROFILE_MIN", "0.9")
    conn = _conn()
    _vector(conn, stage_gates.KIND_OWNER, "profile:6", [1.0, 0.0])
    monkeypatch.setattr(stage_gates, "owner_text", lambda *a, **kw: "python")

    kept = stage_gates.keep_for_stage(
        conn, FakeGateway(), "extract", [FakeVacancy("hh:unknown")]
    )

    assert [item.key for item in kept] == ["hh:unknown"]


def test_текст_владельца_собирается_из_профиля_и_резюме() -> None:
    conn = _conn()
    import resume

    resume.ensure_schema(conn)
    conn.execute(
        "INSERT INTO resumes (id, profile_id, title, created_at, updated_at)"
        " VALUES (1, 'default', 'Резюме', '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO resume_blocks (resume_id, section, position, heading, body,"
        " confirmed, created_at, updated_at)"
        " VALUES (1, 'experience', 1, 'Тимлид', 'Вёл команду из пяти человек',"
        " 1, '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO resume_blocks (resume_id, section, position, heading, body,"
        " confirmed, created_at, updated_at)"
        " VALUES (1, 'experience', 2, 'Черновик', 'Написала модель',"
        " 0, '2026-01-01', '2026-01-01')"
    )
    conn.commit()

    text = stage_gates.owner_text(conn, "нет-такого-файла.yaml")

    assert "Вёл команду из пяти человек" in text
    # Неподтверждённый блок описывает не владельца, а догадку модели.
    assert "Написала модель" not in text


def test_разметка_спанами_выключена_по_умолчанию(monkeypatch) -> None:
    monkeypatch.delenv("GLINER_ENABLED", raising=False)
    assert extract_spans.enabled() is False
    assert extract_spans.conditions("Формат работы гибридный, два дня в офисе") == ()


def test_спан_берётся_только_дословный(monkeypatch) -> None:
    monkeypatch.setenv("GLINER_ENABLED", "1")
    text = "Формат работы гибридный: два дня в офисе на Белорусской."

    class FakeModel:
        def predict_entities(self, body, labels, threshold):
            return [
                {"text": "Формат работы гибридный", "label": "формат работы", "score": 0.9},
                {"text": "удалёнка навсегда", "label": "формат работы", "score": 0.8},
            ]

    items = extract_spans.conditions(text, model=FakeModel())

    assert [item.quote for item in items] == ["Формат работы гибридный"]
    assert items[0].field == "format"
    assert isinstance(items[0], Condition)


def test_готовые_веса_запрещают_ходить_в_hugging_face(monkeypatch, tmp_path) -> None:
    """Семь запросов к huggingface.co на каждом старте — это шум в логе и
    задержка без интернета. Уже заданное владельцем значение не трогаем."""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(extract_spans, "weights_ready", lambda name="": False)
    assert extract_spans.prefer_offline("urchade/gliner_multi-v2.1") is False
    assert "HF_HUB_OFFLINE" not in os.environ

    monkeypatch.setattr(extract_spans, "weights_ready", lambda name="": True)
    assert extract_spans.prefer_offline("urchade/gliner_multi-v2.1") is True
    assert os.environ["HF_HUB_OFFLINE"] == "1"

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    extract_spans.prefer_offline("urchade/gliner_multi-v2.1")
    assert os.environ["HF_HUB_OFFLINE"] == "0"
