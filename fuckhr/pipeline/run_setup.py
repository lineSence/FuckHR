"""Обвязка прогона: шлюз модели и тревоги канарейки.

Здесь нет шагов конвейера — только то, что готовит прогон и что сообщает
о его поломке. Логи — `logs.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fuckhr.bot import bot as tg
from fuckhr.llm import llm
from fuckhr.pipeline import canary

log = logging.getLogger("fuckhr")


def build_gateway(conn, disabled: bool) -> llm.Gateway | None:
    """Шлюз или None. None — штатный режим, а не авария.

    Кэш живёт в той же базе, что и вакансии: повторный прогон по тем же
    описаниям не должен стоить ни одного вызова [LLM-006].
    """
    if disabled:
        log.info("модель выключена в настройках (LLM_ENABLED)")
        return None
    gateway = llm.Gateway.from_env(conn)
    if not gateway.enabled:
        log.info("модель не настроена (%s), идём без неё", gateway.disabled_reason)
        return None
    for stage, profile, route, model, source in gateway.describe_routes():
        if stage in {"extract", "hr_filter", "company"}:
            log.info(
                "этап %s: профиль %s, маршрут %s, модель %s (имя из: %s)",
                stage, profile, route, model, source,
            )
    return gateway


def notify_if_broken(stats: canary.RunStats, dry_run: bool) -> list[canary.Alert]:
    """Считает поводы для тревоги и пишет в Telegram не чаще раза в сутки."""
    alerts = canary.check(stats)
    for alert in alerts:
        log.warning("канарейка [%s]: %s", alert.kind, alert.text.replace("\n", " "))
    if not alerts or dry_run:
        return alerts

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("канарейке некуда писать: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return alerts

    state_path = Path(os.getenv("ALERT_STATE_PATH", "data/alerts.json"))
    state = canary.load_state(state_path)
    due = canary.filter_due(
        state, alerts, cooldown_hours=float(os.getenv("ALERT_COOLDOWN_HOURS", "24"))
    )
    if not due:
        log.info("о этих сбоях уже писали недавно, молчим")
        return alerts
    if asyncio.run(tg.send_alert(token, chat_id, canary.format_message(due))):
        canary.save_state(state_path, state)
    return alerts


__all__ = ("build_gateway", "notify_if_broken")
