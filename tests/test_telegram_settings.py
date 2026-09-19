"""Выключатель, темп и тихие часы Telegram.

Проверяется не отправка, а решение «отправлять ли сейчас»: сеть в тестах не
трогаем. Важно, что запрет мягкий — карточка остаётся неотправленной и уходит
следующим прогоном, а не теряется.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import bot
import settings


def test_время_разбирается_и_мусор_не_роняет(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.as_minutes("23:30") == 23 * 60 + 30
    assert settings.as_minutes("07.05") == 7 * 60 + 5
    assert settings.as_minutes("") is None
    assert settings.as_minutes("вечером") is None
    assert settings.as_minutes("25:00") is None


def test_тихие_часы_через_полночь() -> None:
    opts = settings.TelegramOptions(
        enabled=True, delay=0.6, quiet_from=23 * 60, quiet_to=8 * 60
    )
    assert opts.quiet_at(23 * 60 + 10)
    assert opts.quiet_at(3 * 60)
    assert not opts.quiet_at(12 * 60)
    # Границы не заданы — молчать не о чем.
    assert not settings.TelegramOptions(True, 0.6, None, None).quiet_at(3 * 60)


def test_задержка_зажимается(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_DELAY", "-5")
    assert settings.telegram_options().delay == 0.0
    monkeypatch.setenv("TELEGRAM_DELAY", "999")
    assert settings.telegram_options().delay == 60.0
    monkeypatch.setenv("TELEGRAM_DELAY", "не число")
    assert settings.telegram_options().delay == 0.6


def test_выключенная_отправка_останавливает_карточки(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ENABLED", "0")
    assert "выключена" in bot.hold_reason()
    assert "выключена" in settings.missing_required()[0]


def test_в_тихие_часы_карточки_ждут(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ENABLED", "1")
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    # Окно строим вокруг текущей минуты, иначе тест зависел бы от времени прогона.
    opts = settings.TelegramOptions(
        enabled=True,
        delay=0.6,
        quiet_from=(minutes - 5) % (24 * 60),
        quiet_to=(minutes + 5) % (24 * 60),
    )
    assert "тихие часы" in bot.hold_reason(opts)
