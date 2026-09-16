"""Весь прогон целиком на поддельном клиенте: секунды вместо десятков минут.

Проверяется именно то, что невозможно увидеть глазами за один запуск: что на втором
прогоне исчезнувшая вакансия получает слепок is_active = 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

import db
import run

ROOT = Path(__file__).resolve().parents[1]
DETAIL = {
    "description": "Python, asyncio, FastAPI, PostgreSQL, Docker, SQL. Удаленно.",
    "key_skills": [{"name": "python"}, {"name": "fastapi"}, {"name": "docker"}],
}


class FakeClient:
    """Замена HHHtmlClient: те же методы и те же счётчики здоровья, но без сети."""

    def __init__(self, vacancies):
        self.vacancies = list(vacancies)
        self.pages_fetched = 1
        self.fallback_pages = 0
        self.empty_pages = 0
        self.blocked = False
        self.failures: list[str] = []
        self.detail_calls = 0

    def search(self, **kwargs):
        yield from self.vacancies

    def vacancy(self, vacancy_id):
        self.detail_calls += 1
        return DETAIL

    def close(self):
        pass


def run_pipeline(monkeypatch, tmp_path, vacancies) -> tuple[int, FakeClient]:
    fake = FakeClient(vacancies)
    monkeypatch.setattr(run, "HHHtmlClient", lambda **kwargs: fake)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pipeline.sqlite3"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "pipeline.log"))
    monkeypatch.setenv("FAILURE_DIR", str(tmp_path / "failures"))
    monkeypatch.setenv("ALERT_STATE_PATH", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--profile", str(ROOT / "profile.yaml"), "--dry-run", "--limit", "5"],
    )
    return run.main(), fake


def test_full_run_stores_vacancies_and_history(monkeypatch, tmp_path, make_vacancy, capsys):
    first = make_vacancy(external_id="1", title="Python разработчик", company="ООО Ромашка")
    second = make_vacancy(external_id="2", title="Backend Python", company="ООО Ландыш")

    code, fake = run_pipeline(monkeypatch, tmp_path, [first, second])
    assert code == 0
    assert fake.detail_calls == 2

    conn = db.connect(tmp_path / "pipeline.sqlite3")
    try:
        counts = db.stats(conn)
        assert counts["vacancies"] == 2
        assert counts["snapshots"] == 2
        assert counts["closed"] == 0
        # --dry-run печатает карточки в стандартный вывод вместо Telegram.
        assert "Backend Python" in capsys.readouterr().out
    finally:
        conn.close()


def test_second_run_marks_disappeared_vacancy(monkeypatch, tmp_path, make_vacancy):
    first = make_vacancy(external_id="1", title="Python разработчик", company="ООО Ромашка")
    second = make_vacancy(external_id="2", title="Backend Python", company="ООО Ландыш")

    run_pipeline(monkeypatch, tmp_path, [first, second])
    # Второй прогон: второй вакансии в выдаче больше нет.
    code, _ = run_pipeline(monkeypatch, tmp_path, [first])
    assert code == 0

    conn = db.connect(tmp_path / "pipeline.sqlite3")
    try:
        assert db.last_snapshot_active(conn, first.key) is True
        assert db.last_snapshot_active(conn, second.key) is False
    finally:
        conn.close()


def test_blocked_source_returns_exit_code_two(monkeypatch, tmp_path, make_vacancy):
    from hh_html import BlockedError

    class BlockedClient(FakeClient):
        def search(self, **kwargs):
            raise BlockedError("капча")

    fake = BlockedClient([])
    monkeypatch.setattr(run, "HHHtmlClient", lambda **kwargs: fake)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "blocked.sqlite3"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "blocked.log"))
    monkeypatch.setenv("ALERT_STATE_PATH", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--profile", str(ROOT / "profile.yaml"), "--dry-run"],
    )
    assert run.main() == 2

    conn = db.connect(tmp_path / "blocked.sqlite3")
    try:
        # Самое важное: капча не должна «закрывать» вакансии и портить историю.
        assert db.stats(conn)["closed"] == 0
    finally:
        conn.close()
