"""Детектор проверяется без сети и без LLM: ядро детерминировано.

Смысл этих тестов — не ждать прогона на сотнях вакансий, чтобы увидеть,
что вывод сформировался неправильно.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

import detector
import detector_llm


def _snap(
    conn: sqlite3.Connection,
    key: str,
    seen_at: str,
    is_active: int = 1,
    published_at: str | None = None,
    salary_from: int | None = None,
    salary_to: int | None = None,
) -> None:
    """Слепок пишется напрямую: тесту нужны точные даты и флаг is_active."""
    conn.execute(
        """
        INSERT INTO vacancy_snapshots
            (key, seen_at, external_id, published_at, company_id, salary_from, salary_to, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (key, seen_at, "1", published_at, "42", salary_from, salary_to, is_active),
    )
    conn.commit()


class FakeGateway:
    """Шлюз, который всегда отвечает заранее заданной строкой."""

    def __init__(self, raw: str | None) -> None:
        self.raw = raw
        self.calls = 0

    def complete(
        self, stage: str, messages: Sequence[dict[str, str]], temperature: float = 0.0
    ) -> str | None:
        self.calls += 1
        return self.raw


def test_extract_claims_ловит_проверяемые_утверждения() -> None:
    text = "Ищем в дружную команду. Работаем без переработок и авралов."
    keys = {claim.key for claim in detector.extract_claims(text)}
    assert {"stable_team", "no_overtime"} <= keys


def test_частые_перепубликации_опровергают_стабильную_команду(make_vacancy: Any) -> None:
    hist = detector.History(
        republished=5, reopen_cycles=4, days_tracked=90, snapshots=8, dated_snapshots=5
    )
    report = detector.assess(
        make_vacancy(description="У нас стабильная команда и Python"), hist
    )
    finding = next(f for f in report.findings if f.kind == "stable_team")
    assert finding.verdict == detector.NOT_SUPPORTED
    assert "5" in finding.found
    assert "stable_team" in report.flags


def test_короткая_история_даёт_недостаточно_данных(make_vacancy: Any) -> None:
    hist = detector.History(republished=9, days_tracked=3, snapshots=2)
    report = detector.assess(
        make_vacancy(description="Дружная команда, Python"), hist
    )
    finding = next(f for f in report.findings if f.kind == "stable_team")
    # При двух слепках любой вывод был бы выдумкой [HRD-004].
    assert finding.verdict == detector.NO_DATA
    assert report.flags == ()


def test_обещание_белой_зарплаты_без_вилки(make_vacancy: Any) -> None:
    report = detector.assess(
        make_vacancy(
            salary_from=None,
            description="Белая зарплата и официальное оформление с первого дня",
        )
    )
    finding = next(f for f in report.findings if f.kind == "white_salary")
    assert finding.verdict == detector.NOT_SUPPORTED
    assert "вилка" in finding.found


def test_противоречие_в_тексте_про_переработки(make_vacancy: Any) -> None:
    report = detector.assess(
        make_vacancy(
            description="Работаем без переработок. Требуется стрессоустойчивость и многозадачность"
        )
    )
    finding = next(f for f in report.findings if f.kind == "no_overtime")
    assert finding.verdict == detector.NOT_SUPPORTED
    assert "стрессоустойчивость" in finding.found


def test_расхождение_грейда_и_требований(make_vacancy: Any) -> None:
    report = detector.assess(
        make_vacancy(
            experience="noExperience",
            description="Нужны Kubernetes, Kafka и микросервисная архитектура",
        )
    )
    assert "grade_mismatch" in report.flags


def test_history_считает_возвраты_в_выдачу(conn: sqlite3.Connection) -> None:
    detector.ensure_schema(conn)
    key = "hh:1"
    _snap(conn, key, "2026-06-01T10:00:00", is_active=1, published_at="2026-06-01")
    _snap(conn, key, "2026-06-15T10:00:00", is_active=0)
    _snap(conn, key, "2026-07-01T10:00:00", is_active=1, published_at="2026-07-01")
    _snap(conn, key, "2026-07-20T10:00:00", is_active=0)
    _snap(conn, key, "2026-08-30T10:00:00", is_active=1, published_at="2026-08-30")

    hist = detector.history(conn, key)
    assert hist.snapshots == 5
    assert hist.reopen_cycles == 3
    assert hist.active is True
    assert hist.days_tracked == 90
    assert hist.dated_snapshots == 3


def test_отчёт_хранится_и_превращается_в_строки_телеграма(
    conn: sqlite3.Connection, make_vacancy: Any
) -> None:
    detector.ensure_schema(conn)
    vacancy = make_vacancy(description="Стабильная команда, Python")
    hist = detector.History(republished=4, days_tracked=70, snapshots=6, dated_snapshots=4)
    detector.store(conn, detector.assess(vacancy, hist))

    lines = detector.load_lines(conn, vacancy.key)
    assert lines and any("4" in line for line in lines)
    flagged, total = detector.coverage(conn)
    assert (flagged, total) == (1, 1)


def test_format_report_держит_форму_HRD_003(make_vacancy: Any) -> None:
    hist = detector.History(republished=4, days_tracked=70, snapshots=6)
    text = detector.format_report(
        detector.assess(make_vacancy(description="Дружная команда"), hist)
    )
    assert "Заявлено" in text
    assert "Найдено" in text
    assert "Вывод" in text


def test_llm_цитата_которой_нет_в_тексте_отбрасывается() -> None:
    text = "Мы предлагаем гибкий график и оплату спорта."
    gateway = FakeGateway(
        '{"claims": ['
        '{"label": "график", "quote": "гибкий график и оплату спорта"},'
        '{"label": "обеды", "quote": "бесплатные обеды каждый день"}]}'
    )
    claims = detector_llm.llm_claims(gateway, text)
    assert [c.label for c in claims] == ["график"]


def test_llm_вывод_не_попадает_в_телеграм(make_vacancy: Any) -> None:
    vacancy = make_vacancy(description="Мы даём оплату обучения и ментора на испытательном")
    gateway = FakeGateway(
        '{"claims": [{"label": "обучение", "quote": "оплату обучения и ментора"}]}'
    )
    report = detector_llm.with_llm_claims(detector.assess(vacancy), vacancy, gateway)
    assert any(f.kind == "llm_claim" for f in report.findings)
    # Выводы модели имеют статус «недостаточно данных» и не уходят в карточку.
    assert all("llm" not in line for line in detector.telegram_lines(report))
