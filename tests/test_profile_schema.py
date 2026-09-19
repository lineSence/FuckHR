"""Проверка профиля: опечатка видна, сломанный тип не доезжает до скоринга.

Главное здесь — разделение ошибки и предупреждения. Опечатка в ключе не должна
останавливать прогон, но и молчать о ней нельзя: именно молчание приводило к
тому, что фильтр работал не по тем правилам, что написаны в файле.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import profile_schema
from score import Profile


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_опечатка_в_ключе_попадает_в_предупреждения() -> None:
    data, notes = profile_schema.validate({"min_scores": 70})

    assert data.min_score == 45.0
    assert any("min_scores" in note and "min_score" in note for note in notes)


def test_незнакомый_ключ_внутри_salary_заметен() -> None:
    _, notes = profile_schema.validate({"salary": {"min_nett": 100}})

    assert any("min_nett" in note for note in notes)


def test_сломанный_тип_это_ошибка() -> None:
    with pytest.raises(profile_schema.ProfileError) as exc:
        profile_schema.validate({"queries": "python"}, source="p.yaml")

    assert "queries" in str(exc.value)
    assert "p.yaml" in str(exc.value)


def test_профиль_целиком_не_словарь() -> None:
    with pytest.raises(profile_schema.ProfileError):
        profile_schema.validate(["python"])


def test_пустой_профиль_даёт_значения_по_умолчанию() -> None:
    data, notes = profile_schema.validate(None)

    assert data.min_score == 45.0
    assert data.salary.allow_missing is True
    assert any("ни одного запроса" in note for note in notes)


def test_навык_строкой_превращается_в_список() -> None:
    data, _ = profile_schema.validate({"skills": "python"})

    assert data.skills == ["python"]


def test_сумма_весов_не_сто_предупреждение() -> None:
    _, notes = profile_schema.validate({"weights": {"skills": 55, "salary": 20}})

    assert any("100" in note for note in notes)


def test_неизвестный_id_опыта_предупреждение() -> None:
    _, notes = profile_schema.validate({"experience_ok": ["between3and6"]})

    assert any("between3and6" in note for note in notes)


def test_порог_вне_диапазона_предупреждение() -> None:
    _, notes = profile_schema.validate({"min_score": 450})

    assert any("min_score" in note for note in notes)


def test_загрузчик_пишет_предупреждение_в_лог(tmp_path, caplog) -> None:
    path = write(tmp_path, "min_scores: 70\nqueries:\n  - text: python\n")

    with caplog.at_level("WARNING"):
        profile = Profile.load(path)

    assert profile.min_score == 45.0
    assert "min_scores" in caplog.text


def test_загрузчик_падает_на_сломанном_типе(tmp_path) -> None:
    path = write(tmp_path, "min_score: повыше\n")

    with pytest.raises(profile_schema.ProfileError):
        Profile.load(path)


def test_запрос_остаётся_словарём_для_обхода(tmp_path) -> None:
    """run.py берёт из запроса ключи через .get — схема не должна ломать это."""
    path = write(
        tmp_path,
        "queries:\n  - text: python\n    area: 113\n    period: 3\n    max_pages: 2\n",
    )

    profile = Profile.load(path)
    query = profile.queries[0]

    assert query["text"] == "python"
    assert query["area"] == 113
    assert query.get("period") == 3
    assert query.get("max_pages") == 2


def test_список_регионов_в_запросе_сохраняется(tmp_path) -> None:
    path = write(tmp_path, "queries:\n  - text: python\n    area: [1, 2]\n")

    profile = Profile.load(path)

    assert profile.queries[0]["area"] == [1, 2]


def test_навыки_приводятся_к_нижнему_регистру(tmp_path) -> None:
    path = write(tmp_path, "skills:\n  - Python\n  - FastAPI\nqueries: []\n")

    profile = Profile.load(path)

    assert profile.skills == ["python", "fastapi"]
