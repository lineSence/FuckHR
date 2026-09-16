"""Канарейка: понять, что сбор сломался, до того как об этом скажет тишина.

После переноса запуска в Task Scheduler (ADR-014) прогоны идут без человека.
Сломанный парсер, капча и пустая выдача выглядят снаружи одинаково — «сегодня
ничего подходящего». Скрейпинг хрупок по устройству (ADR-015), поэтому молчание
должно быть событием, а не состоянием по умолчанию.

Здесь только чистые функции: сборка условий и троттлинг. Отправка — в bot.py,
чтобы это всё можно было проверить тестами без сети.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

EMPTY_DESCRIPTION_LIMIT = 0.3
MIN_ENRICHED_FOR_RATIO = 5
DEFAULT_COOLDOWN_HOURS = 24.0


@dataclass
class RunStats:
    """Что прогон знает о собственном здоровье."""

    collected: int = 0
    passed: int = 0
    blocked: bool = False
    fallback_pages: int = 0
    enriched: int = 0
    empty_descriptions: int = 0
    failures: list[str] = field(default_factory=list)


@dataclass
class Alert:
    kind: str
    text: str


def check(
    stats: RunStats,
    empty_limit: float = EMPTY_DESCRIPTION_LIMIT,
    min_enriched: int = MIN_ENRICHED_FOR_RATIO,
) -> list[Alert]:
    """Список поводов написать владельцу. Пустой список — прогон здоров."""
    alerts: list[Alert] = []

    if stats.blocked:
        alerts.append(
            Alert(
                "blocked",
                "hh.ru закрылся капчей или блокировкой.\n"
                "Что делать: открой hh.ru в браузере, пройди капчу, обнови HH_COOKIE, "
                "подними HH_PAUSE до 4–5 секунд.",
            )
        )
    elif stats.collected == 0:
        alerts.append(
            Alert(
                "empty",
                "Сбор вернул ноль вакансий по всем запросам профиля.\n"
                "Что делать: запусти probe_hh.py и сравни структуру страницы с парсером.",
            )
        )

    if stats.fallback_pages:
        alerts.append(
            Alert(
                "fallback",
                f"JSON состояния не найден на {stats.fallback_pages} стр.: разбор идёт по разметке, "
                "данные бедные (без вилки и описания).\n"
                "Что делать: проверь STATE_PATTERNS в hh_html.py по сохранённой странице.",
            )
        )

    if stats.enriched >= min_enriched:
        ratio = stats.empty_descriptions / stats.enriched
        if ratio > empty_limit:
            alerts.append(
                Alert(
                    "descriptions",
                    f"У {ratio:.0%} вакансий пустое описание ({stats.empty_descriptions} из "
                    f"{stats.enriched}).\nЧто делать: скорее всего изменилась карточка вакансии — "
                    "проверь извлечение description в hh_html.HHHtmlClient.vacancy.",
                )
            )

    if alerts and stats.failures:
        alerts[-1].text += "\nСохранённые страницы: " + ", ".join(stats.failures[:3])
    return alerts


def load_state(path: str | Path) -> dict[str, str]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: str | Path, state: dict[str, str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_due(
    state: dict[str, str],
    alerts: list[Alert],
    now: datetime | None = None,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
) -> list[Alert]:
    """Отсеивает то, о чём уже писали недавно, и отмечает отправленное в state.

    Два прогона в сутки при сломанном парсере не должны превращаться в поток
    одинаковых сообщений: предупреждение, которое приходит всё время, перестают читать.
    """
    now = now or datetime.now(timezone.utc)
    due: list[Alert] = []
    for alert in alerts:
        previous = state.get(alert.kind)
        if previous:
            try:
                sent_at = datetime.fromisoformat(previous)
            except ValueError:
                sent_at = None
            if sent_at is not None:
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                if now - sent_at < timedelta(hours=cooldown_hours):
                    continue
        state[alert.kind] = now.isoformat(timespec="seconds")
        due.append(alert)
    return due


def format_message(alerts: list[Alert]) -> str:
    body = "\n\n".join(alert.text for alert in alerts)
    return "\u26a0\ufe0f FuckHR: сбор работает не так\n\n" + body
