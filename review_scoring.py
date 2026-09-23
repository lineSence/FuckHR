"""fake_score отзывов компании и запись разметки учителя.

Вынесено из dossier.py по [CORE-024]: файл перешёл 25 КБ. Имя
`dossier.score_reviews` сохранено реэкспортом.
"""

from __future__ import annotations

import logging
from typing import Sequence

import aitext_llm
import embeddings_tasks
import fake_llm
import fake_reviews
import fake_store
import judge_labels
from fake_reviews import Verdict
from reviewitems import ReviewItem

log = logging.getLogger(__name__)


def score_reviews(
    company: str,
    items: Sequence[ReviewItem],
    conn: object | None = None,
    gateway: object | None = None,
) -> tuple[Verdict, ...]:
    """Считает fake_score. Хэши чужих компаний берутся из базы, если она есть."""
    if not items:
        return ()
    known: dict[str, str] = {}
    if conn is not None:
        try:
            known = fake_store.known_hashes(conn, company)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — детекция важнее одного сигнала
            log.warning("хэши отзывов не прочитаны: %s", exc)
    fake_seen: dict[str, bool] = {}
    ai_seen: dict[str, bool] = {}
    verdicts = fake_reviews.score_items(
        items,
        known_hashes=known,
        llm_ads=fake_llm.ad_indexes(gateway, items, seen=fake_seen),
        ai_texts=aitext_llm.generated_indexes(
            gateway, {item.index: item.text for item in items}, seen=ai_seen
        ),
        near_pairs=embeddings_tasks.review_pairs(conn, gateway, items),
    )
    if conn is not None and (fake_seen or ai_seen):
        try:
            judge_labels.record(conn, fake_llm.STAGE, fake_seen, company)  # type: ignore[arg-type]
            judge_labels.record(conn, aitext_llm.STAGE, ai_seen, company)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — [CORE-017]
            log.warning("разметка учителя не записана: %s", exc)
    return verdicts


__all__ = ("score_reviews",)
