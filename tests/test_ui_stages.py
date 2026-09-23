"""Блок «Когда модель не зовут»: показывает состояние, не зовёт модель."""

from __future__ import annotations

import sqlite3

import db
import ui_stages


def base() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    return conn


def test_block_reports_everything_off(monkeypatch):
    monkeypatch.delenv("GATE_PROFILE_MIN", raising=False)
    monkeypatch.delenv("GLINER_ENABLED", raising=False)
    html = ui_stages.render_stages(base(), gateway=None)
    assert "Когда модель не зовут" in html
    assert "выключен" in html
    assert "GLINER_ENABLED" in html


def test_warns_when_gate_has_nothing_to_measure(monkeypatch):
    """Включённый гейт без векторов молча пропускает всё — это надо сказать."""
    monkeypatch.setenv("GATE_PROFILE_MIN", "0.3")
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "0")
    html = ui_stages.render_stages(base(), gateway=None)
    assert "считать близость не по чему" in html


def test_warns_when_spans_enabled_without_package(monkeypatch):
    monkeypatch.setenv("GLINER_ENABLED", "1")
    monkeypatch.setattr(ui_stages.extract_spans, "installed", lambda: False)
    html = ui_stages.render_stages(base(), gateway=None)
    assert "пакета нет" in html


def test_warns_when_weights_are_missing(monkeypatch):
    monkeypatch.setenv("GLINER_ENABLED", "1")
    monkeypatch.setattr(ui_stages.extract_spans, "installed", lambda: True)
    monkeypatch.setattr(ui_stages.extract_spans, "weights_ready", lambda name="": False)
    html = ui_stages.render_stages(base(), gateway=None)
    assert "весов в кэше нет" in html


def test_weights_ready_reads_cache(tmp_path, monkeypatch):
    """Готовность весов определяется каталогом кэша, без обращения в сеть."""
    import extract_spans

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    assert extract_spans.weights_ready("urchade/gliner_multi-v2.1") is False
    snapshot = (
        tmp_path / "hub" / "models--urchade--gliner_multi-v2.1" / "snapshots" / "abc"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").write_text("x")
    assert extract_spans.weights_ready("urchade/gliner_multi-v2.1") is True
