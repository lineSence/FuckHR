"""fake_score отзывов компании, гейт перед моделью и запись разметки учителя.

Вынесено из dossier.py по [CORE-024]: файл перешёл 25 КБ. Имя
`dossier.score_reviews` сохранено реэкспортом.

Гейт (`review_gate.py`) стоит перед двумя этапами модели и снимает с неё
тексты, где ответ и так ясен. Разметка учителя пишется только по тем текстам,
которые модель действительно видела: иначе гейт учился бы на своих ответах.
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
import review_gate
from fake_reviews import Verdict
from reviewitems import ReviewItem

log = logging.getLogger(__name__)


def gated_ads(
    conn: object | None,
    gateway: object | None,
    items: Sequence[ReviewItem],
    seen: dict[str, bool],
) -> set[int]:
    """Индексы рекламных отзывов: уверенное решение гейта плюс ответ модели.

    Выключенный этап не спрашивает ни гейт, ни модель: гейт экономит вызовы
    этапа, а не добавляет сигнал там, где его не заказывали.
    """
    if not fake_llm.enabled():
        return set()
    yes, no, _ = review_gate.decide(
        conn, gateway, fake_llm.STAGE, {item.index: item.text for item in items}
    )
    rest = [item for item in items if item.index not in yes and item.index not in no]
    asked = fake_llm.ad_indexes(gateway, rest, seen=seen) if rest else set()
    return set(yes) | asked


def gated_ai(
    conn: object | None,
    gateway: object | None,
    texts: dict[int, str],
    seen: dict[str, bool],
) -> set[int]:
    """То же для этапа «текст написан нейросетью»."""
    if not aitext_llm.enabled():
        return set()
    yes, no, _ = review_gate.decide(conn, gateway, aitext_llm.STAGE, texts)
    rest = {key: text for key, text in texts.items() if key not in yes and key not in no}
    asked = aitext_llm.generated_indexes(gateway, rest, seen=seen) if rest else set()
    return set(yes) | asked


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
        llm_ads=gated_ads(conn, gateway, items, fake_seen),
        ai_texts=gated_ai(
            conn, gateway, {item.index: item.text for item in items}, ai_seen
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


__all__ = ("gated_ads", "gated_ai", "score_reviews")
