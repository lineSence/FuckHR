"""Компании на этапе контактов изучаются параллельно (docs/performance.md, п. 5)."""

from __future__ import annotations

import threading

import contacts
import db
import outreach
import outreach_scan


def row(key: str, company: str, score: float = 50.0) -> dict[str, object]:
    from tests.test_outreach import _row

    return _row(key=key, company=company, score=score)


def test_компании_расходятся_по_потокам(tmp_path, monkeypatch) -> None:
    db.init_schema(db.connect(tmp_path / "t.sqlite3"))
    threads: set[str] = set()
    started = threading.Barrier(3, timeout=5)

    def fake_hits(provider, company, title):  # noqa: ANN001 — подмена сетевого шага
        threads.add(threading.current_thread().name)
        started.wait()  # все три компании должны идти одновременно
        return []

    def fake_find(conn, r, provider, check_mx=False, hits=None):  # noqa: ANN001
        return contacts.Discovery(key=r["key"], company=r["company"]), []

    monkeypatch.setattr(outreach, "company_hits", fake_hits)
    monkeypatch.setattr(outreach, "find_contacts", fake_find)

    found = outreach_scan.discover_all(
        tmp_path / "t.sqlite3",
        {
            "АКМЕ": [row("hh:1", "АКМЕ")],
            "БЕТА": [row("hh:2", "БЕТА")],
            "ГАММА": [row("hh:3", "ГАММА")],
        },
        workers=3,
    )
    assert set(found) == {"hh:1", "hh:2", "hh:3"}
    assert len(threads) == 3


def test_упавшая_компания_не_роняет_этап(tmp_path, monkeypatch) -> None:
    db.init_schema(db.connect(tmp_path / "t.sqlite3"))

    def fake_hits(provider, company, title):  # noqa: ANN001
        if company == "БЕТА":
            raise RuntimeError("поиск отказал")
        return []

    def fake_find(conn, r, provider, check_mx=False, hits=None):  # noqa: ANN001
        return contacts.Discovery(key=r["key"], company=r["company"]), []

    monkeypatch.setattr(outreach, "company_hits", fake_hits)
    monkeypatch.setattr(outreach, "find_contacts", fake_find)

    found = outreach_scan.discover_all(
        tmp_path / "t.sqlite3",
        {"АКМЕ": [row("hh:1", "АКМЕ")], "БЕТА": [row("hh:2", "БЕТА")]},
        workers=2,
    )
    assert set(found) == {"hh:1"}


def test_потолок_потоков_не_превышается(monkeypatch) -> None:
    monkeypatch.setenv("CONTACT_WORKERS", "99")
    assert outreach_scan.contact_workers() == outreach_scan.MAX_CONTACT_WORKERS
    monkeypatch.setenv("CONTACT_WORKERS", "0")
    assert outreach_scan.contact_workers() == 1
