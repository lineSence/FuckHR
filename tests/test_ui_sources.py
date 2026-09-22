"""Галочки площадок на главной странице и их метрика."""

from __future__ import annotations

import sqlite3

import db
import source_store
import ui_sources


def base() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    source_store.ensure_schema(conn)
    return conn


def test_block_lists_every_site_and_metric():
    conn = base()
    source_store.remember_many(
        conn, [("k1", "hh.ru", "1", ""), ("k1", "rabota", "2", ""), ("k2", "trudvsem", "3", "")]
    )
    html = ui_sources.render_sources(conn)
    assert "Площадки в сборе" in html
    assert "Работа России" in html and "SuperJob" in html
    assert "Только здесь" in html


def test_site_without_key_is_disabled(monkeypatch):
    monkeypatch.setenv("SUPERJOB_KEY", "")
    html = ui_sources.render_sources(base())
    assert "не задан SUPERJOB_KEY" in html
    assert "disabled" in html


def test_save_writes_chosen_codes(monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr(
        ui_sources.sources.settings, "save", lambda updates: saved.update(updates) or list(updates)
    )
    ui_sources.save({"source_hh": ["1"], "source_trudvsem": ["1"]})
    assert saved == {"SOURCE_SITES": "hh,trudvsem"}


def test_save_without_checkboxes_keeps_hh(monkeypatch):
    """Снять все галочки нельзя: прогон без источника бессмыслен."""
    saved: dict[str, str] = {}
    monkeypatch.setattr(
        ui_sources.sources.settings, "save", lambda updates: saved.update(updates) or list(updates)
    )
    ui_sources.save({})
    assert saved == {"SOURCE_SITES": "hh"}
