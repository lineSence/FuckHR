"""Разбор даты публикации. Самый тихий источник порчи данных во всём проекте."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hh import normalize_published_at

NOW = datetime(2026, 9, 16, 21, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-09-01T10:20:30+0300", "2026-09-01T10:20:30"),
        ("2026-09-01", "2026-09-01"),
        ("сегодня", "2026-09-16"),
        ("вчера в 18:30", "2026-09-15"),
        ("позавчера", "2026-09-14"),
        ("3 дня назад", "2026-09-13"),
        ("2 часа назад", "2026-09-16"),
        ("12 сентября", "2026-09-12"),
        ("5 марта 2025", "2025-03-05"),
        ("непонятно что", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_published_at(raw, expected):
    assert normalize_published_at(raw, now=NOW) == expected


def test_epoch_milliseconds():
    # Так hh.ru отдаёт время в JSON состояния страницы.
    assert normalize_published_at(1757000000000, now=NOW).startswith("2025-09-04")


def test_nested_object():
    node = {"@timestamp": 1757000000000}
    assert normalize_published_at(node, now=NOW).startswith("2025-09-04")
    assert normalize_published_at({"iso": "2026-08-08"}, now=NOW) == "2026-08-08"


def test_month_without_year_does_not_jump_into_future():
    january = datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert normalize_published_at("20 декабря", now=january) == "2025-12-20"
