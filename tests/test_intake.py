"""Разговор о поиске: проверяется не качество формулировок, а границы доверия.

Модель здесь предлагает, а не решает. Значит, важны две вещи: выдуманные числа
не доходят до резюме и фактов, и предложение не затирает то, что владелец
написал руками.
"""

from __future__ import annotations

import json
import sqlite3

import intake
import resume

SAID = (
    "Ищу backend на Python, 8 лет опыта, последние 3 года highload в финтехе. "
    "Хочу удалёнку, от 250 тысяч на руки, без 1С."
)


def _answer(**parts: object) -> str:
    return json.dumps(parts, ensure_ascii=False)


def test_выдуманные_числа_не_попадают_в_резюме_и_факты() -> None:
    raw = _answer(
        facts=["8 лет в backend-разработке", "поднял выручку на 40%"],
        resume=[
            {"section": "summary", "body": "Backend на Python, 8 лет опыта."},
            {"section": "experience", "body": "Руководил командой из 25 человек."},
        ],
    )
    plan = intake.parse(raw, SAID)
    assert plan.facts == ("8 лет в backend-разработке",)
    assert [b["section"] for b in plan.blocks] == ["summary"]
    assert len(plan.dropped) == 2


def test_в_профиль_проходят_только_известные_поля() -> None:
    raw = _answer(
        profile={
            "queries": ["backend python"],
            "salary_min_net": 250000,
            "min_score": 900,
            "experience_ok": ["moreThan6", "выдуманный_код"],
            "любимый_цвет": "синий",
        }
    )
    patch = intake.parse(raw, SAID).profile
    assert patch["queries"] == ["backend python"]
    assert patch["salary_min_net"] == 250000
    assert patch["experience_ok"] == ["moreThan6"]
    assert "min_score" not in patch  # 900 — не балл
    assert "любимый_цвет" not in patch


def test_предложение_дополняет_а_не_затирает() -> None:
    data = {
        "skills": ["python", "своё-руками"],
        "queries": [{"text": "python разработчик", "area": 113}],
    }
    changed = intake.apply_profile(
        data, {"skills": ["python", "asyncio"], "queries": ["backend python"]}
    )
    assert data["skills"] == ["python", "своё-руками", "asyncio"]
    assert [q["text"] for q in data["queries"]] == [
        "python разработчик",
        "backend python",
    ]
    assert changed


def test_блоки_из_разговора_ждут_подтверждения() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    resume.ensure_schema(conn)
    added = intake.apply_blocks(
        conn, [{"section": "summary", "heading": "", "body": "Backend на Python."}]
    )
    assert added == 1
    block = resume.blocks(conn, resume.get_or_create(conn))[0]
    assert block.source == resume.SOURCE_AI and not block.confirmed


def test_разговор_пишется_и_чистится() -> None:
    conn = sqlite3.connect(":memory:")
    intake.ensure_schema(conn)
    intake.log_message(conn, "owner", SAID)
    intake.log_message(conn, "ai", "Понял.")
    assert [role for role, _ in intake.history(conn)] == ["owner", "ai"]
    assert intake.owner_words(conn) == SAID
    intake.clear(conn)
    assert intake.history(conn) == []


def test_этап_разговора_объявлен_и_не_персональный() -> None:
    """Данные владельца о себе — как resume_*: облако разрешено осознанно."""
    import llm

    assert llm.profile_for(intake.STAGE) == llm.SMART
    assert intake.STAGE not in llm.PERSONAL_STAGES
