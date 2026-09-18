"""Общие фикстуры. Ни один тест не ходит в сеть и не трогает рабочую базу."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db as db_module  # noqa: E402
from hh import Vacancy  # noqa: E402


@pytest.fixture(autouse=True)
def без_загрузки_страниц(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чтение страниц отзывов выключено во всех тестах.

    dossier.build по умолчанию открывает найденные ссылки. В тестах это
    означало бы поход в сеть за dreamjob.ru, поэтому загрузка гасится
    переменной окружения, а сам загрузчик проверяется отдельно на подменённом
    транспорте.
    """
    monkeypatch.setenv("REVIEW_FETCH_ENABLED", "0")


@pytest.fixture
def conn(tmp_path: Path):
    connection = db_module.connect(tmp_path / "test.sqlite3")
    db_module.init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def make_vacancy() -> Callable[..., Vacancy]:
    """Вакансия, которая проходит скоринг с базовым profile.yaml."""

    def factory(**overrides: Any) -> Vacancy:
        defaults: dict[str, Any] = {
            "external_id": "1",
            "url": "https://hh.ru/vacancy/1",
            "title": "Python разработчик",
            "company": "ООО Ромашка",
            "company_id": "42",
            "area": "Москва",
            "salary_from": 300000,
            "currency": "RUR",
            "gross": False,
            "schedule": "Удаленная работа",
            "experience": "between1And3",
            "skills": ["python", "asyncio", "fastapi", "postgresql", "docker", "sql"],
            "description": "Python, asyncio, FastAPI, PostgreSQL, Docker, SQL",
            "published_at": "2026-09-16",
        }
        defaults.update(overrides)
        return Vacancy(**defaults)

    return factory
