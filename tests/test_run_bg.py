"""Фоновые этапы прогона: этапы модели порциями и досье по мере появления.

Без сети и без модели: фоновые функции подменяются заглушками. Стережётся одно
свойство — то, что раньше считалось пачкой в конце, считается параллельно, а в
базу по-прежнему пишет главный поток.
"""

from __future__ import annotations

import db
import dossier
import run_bg
from hh import Vacancy


def vacancy(number: int) -> Vacancy:
    return Vacancy(
        source="hh.ru",
        external_id=str(number),
        url=f"https://hh.ru/vacancy/{number}",
        title=f"Оператор 1С №{number}",
        description="Оформление по ТК РФ",
    )


def test_порции_уходят_в_фон_по_мере_набора(monkeypatch) -> None:
    sent: list[int] = []

    def fake_batch(db_path, use_llm, vacancies, budget=None):
        sent.append(len(vacancies))
        return {v.key: ["условие"] for v in vacancies}

    monkeypatch.setattr(run_bg, "_extract_batch", fake_batch)
    stages = run_bg.Stages("data/x.db", use_llm=False, size=2)
    for number in range(5):
        stages.extract(vacancy(number))
    conditions, claims = stages.collect()
    # Две полные порции ушли по ходу дела, остаток — при сборе результатов.
    assert sorted(sent) == [1, 2, 2]
    assert len(conditions) == 5
    assert claims == {}


def test_сбой_порции_не_роняет_прогон(monkeypatch) -> None:
    def broken(db_path, use_llm, vacancies, budget=None):
        raise RuntimeError("шлюз молчит")

    monkeypatch.setattr(run_bg, "_extract_batch", broken)
    stages = run_bg.Stages("data/x.db", use_llm=False, size=1)
    stages.extract(vacancy(1))
    conditions, _claims = stages.collect()
    assert conditions == {}


def test_досье_ставится_в_очередь_один_раз_и_сохраняется(tmp_path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    asked: list[str] = []

    def fake_one(db_path, company, site_url, use_llm, limit, force=False, budget=None):
        asked.append(company)
        return dossier.Dossier(company=company, risk=dossier.RISK_UNKNOWN)

    monkeypatch.setattr(run_bg.research, "_research_one", fake_one)
    monkeypatch.setattr(
        run_bg.websearch.SearchProvider, "from_env", staticmethod(lambda conn: _Provider())
    )
    job = run_bg.Research(conn, tmp_path / "t.db", use_llm=False)
    assert job.submit("Ромашка") is True
    assert job.submit("Ромашка") is False  # та же компания второй раз не платится
    out = job.collect()
    assert asked == ["Ромашка"]
    assert list(out) == ["Ромашка"]
    # Записал главный поток: досье видно в базе сразу после сбора.
    assert dossier.load(conn, "Ромашка") is not None
    conn.close()


def test_без_поиска_досье_не_ставится_в_очередь(tmp_path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    monkeypatch.setattr(
        run_bg.websearch.SearchProvider,
        "from_env",
        staticmethod(lambda conn: _Provider(enabled=False)),
    )
    job = run_bg.Research(conn, tmp_path / "t.db", use_llm=False)
    assert job.submit("Ромашка") is False
    assert job.collect() == {}
    conn.close()


class _Provider:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.disabled_reason = "поиск выключен"
