"""Вакансии цели с hh.ru в базу и обход слежения (ADR-025).

Здесь живёт единственное место, где вакансии компании-цели попадают в базу.
Его дёргают двое: кнопка «Собрать вакансии» (через `target_scan.py`) и ночной
прогон для целей, у которых включено слежение. Логика одна, поэтому и код один.

Что важно в поведении:

- скор считается по всем профилям, но ничего не отсекает: вакансии цели
  показываются целиком, так решил владелец. Порог по-прежнему решает, кому
  готовить письмо, — то есть обычный путь вакансии не меняется;
- описания вакансий страницами не догружаются: шаг должен быть быстрым, а
  текст подтянется, когда вакансия понадобится письмом;
- слежение идёт не чаще раза в сутки на цель [CORE-014], [CORE-016].
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Sequence

import db
import hh_employer
import profiles
import settings
import targets

log = logging.getLogger(__name__)


def load_bundle(path: str | None = None) -> list[profiles.Loaded]:
    """Профили для справочного скора. Их отсутствие не повод падать."""
    try:
        return profiles.load_all(path or settings.get("RUN_PROFILE", "profile.yaml"))
    except (FileNotFoundError, ValueError, OSError) as exc:
        log.warning("профили не прочитались (%s): вакансии цели сохраню без скора", exc)
        return []


def scan(
    conn: sqlite3.Connection,
    client: Any,
    target: targets.Target,
    bundle: Sequence[profiles.Loaded] = (),
    max_pages: int = 0,
    watch: bool = False,
) -> tuple[int, int]:
    """Вакансии одной цели. Возвращает (сколько увидели, сколько новых у цели)."""
    # Тот же порог похожести, что и в прогоне: у цели и у сбора скор должен
    # считаться одинаково, иначе одна вакансия получит два разных балла.
    fuzzy = settings.prefilter_options().fuzzy
    keys: list[str] = []
    for vacancy in hh_employer.vacancies(client, target.employer_id, max_pages):
        matches = profiles.score_all(vacancy, bundle, None, fuzzy) if bundle else []
        chosen = profiles.best(matches)
        db.upsert_vacancy(
            conn,
            vacancy,
            chosen[1].score if chosen else 0.0,
            chosen[1].reasons if chosen else [],
        )
        if matches:
            db.save_matches(
                conn, vacancy.key, [(pid, v.score, v.reasons) for pid, v in matches]
            )
        keys.append(vacancy.key)
    fresh = targets.link(conn, target.id, keys)
    targets.mark_scan(conn, target.id, watch=watch)
    log.info("цель %s: вакансий %s, новых %s", target.company, len(keys), fresh)
    return len(keys), fresh


def sweep(conn: sqlite3.Connection, client: Any) -> list[tuple[str, int]]:
    """Обход целей со слежением. Возвращает пары (компания, сколько новых).

    Вызывается прогоном. Цели, которые смотрели меньше суток назад,
    пропускаются: новые вакансии не появляются ежечасно, а лишний обход — это
    риск капчи для всего прогона.
    """
    due = targets.due(conn)
    if not due:
        return []
    bundle = load_bundle()
    out: list[tuple[str, int]] = []
    for target in due:
        try:
            _, fresh = scan(conn, client, target, bundle, watch=True)
        except Exception as exc:  # noqa: BLE001 — цель не должна ронять прогон
            log.warning("слежение за «%s» не вышло: %s", target.company, exc)
            continue
        if fresh:
            out.append((target.company, fresh))
    log.info("слежение за целями: обошли %s, с новыми вакансиями %s", len(due), len(out))
    return out


__all__ = ("load_bundle", "scan", "sweep")
