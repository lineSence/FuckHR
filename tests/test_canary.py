"""Канарейка: молчание о сломанном сборе хуже лишнего сообщения."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import canary

NOW = datetime(2026, 9, 16, 21, 0, tzinfo=timezone.utc)


def kinds(alerts):
    return [a.kind for a in alerts]


def test_healthy_run_is_silent():
    stats = canary.RunStats(collected=40, passed=12, enriched=12, empty_descriptions=1)
    assert canary.check(stats) == []


def test_blocked_run_reports_once_not_twice():
    stats = canary.RunStats(collected=0, blocked=True)
    assert kinds(canary.check(stats)) == ["blocked"]


def test_empty_result_is_suspicious():
    assert kinds(canary.check(canary.RunStats(collected=0))) == ["empty"]


def test_fallback_and_empty_descriptions():
    stats = canary.RunStats(
        collected=30, fallback_pages=2, enriched=10, empty_descriptions=6
    )
    assert kinds(canary.check(stats)) == ["fallback", "descriptions"]


def test_small_sample_does_not_trigger_ratio():
    stats = canary.RunStats(collected=10, enriched=3, empty_descriptions=3)
    assert canary.check(stats) == []


def test_cooldown_blocks_repeat_within_day():
    state: dict[str, str] = {}
    alerts = canary.check(canary.RunStats(collected=0, blocked=True))

    assert canary.filter_due(state, alerts, now=NOW)
    assert canary.filter_due(state, alerts, now=NOW + timedelta(hours=12)) == []
    assert canary.filter_due(state, alerts, now=NOW + timedelta(hours=25))


def test_state_roundtrip(tmp_path):
    path = tmp_path / "alerts.json"
    canary.save_state(path, {"blocked": NOW.isoformat()})
    assert canary.load_state(path) == {"blocked": NOW.isoformat()}
    assert canary.load_state(tmp_path / "missing.json") == {}


def test_failure_paths_are_mentioned():
    stats = canary.RunStats(collected=0, failures=["data/failures/a.html"])
    assert "a.html" in canary.check(stats)[0].text
