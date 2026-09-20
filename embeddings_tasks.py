"""Две задачи, ради которых считаются векторы (ADR-021).

1. Перефразированные отзывы. Шинглы ловят дословный повтор; фабрика, которая
   пересказывает один текст своими словами, для них невидима.
2. Похожие вакансии. Ключ вакансии считается от названия и компании, поэтому
   одна и та же позиция под другим заголовком выглядит как новая.

Обе задачи справочные: сигнал `paraphrase` весит 2.0 из 6.0 и сам по себе
ничего не помечает, а список похожих вакансий не влияет ни на скоринг, ни на
оценку работодателя. Пороги на размеченной выборке не проверялись [CORE-019].

Модели здесь нет: она за `llm_embed`, за которым шлюз [CORE-010]. Нет
эмбеддера — обе функции возвращают пустоту, и всё считается без них [CORE-017].
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Sequence

import embeddings
import embeddings_store as store
import fake_reviews
import settings

log = logging.getLogger("fuckhr")

# Сколько похожих вакансий показывать: список длиннее никто не читает.
SIMILAR_LIMIT = 5


def review_pairs(
    conn: sqlite3.Connection | None,
    gateway: object | None,
    items: Sequence[object],
) -> tuple[tuple[int, int], ...]:
    """Пары `item.index`, которые пересказывают друг друга.

    Ключ вектора — хэш нормализованного текста, а не индекс: один и тот же
    отзыв на двух площадках считается один раз и переживает пересборку досье.
    """
    options = settings.embeddings_options()
    if not options.enabled or conn is None or len(items) < 2:
        return ()

    texts = {}
    keys = []
    for item in items:
        text = str(getattr(item, "text", "") or "")
        digest = fake_reviews.text_hash(text)
        texts[digest] = text
        keys.append(digest)

    model = store.vectorize(conn, gateway, store.KIND_REVIEW, texts)
    if not model:
        return ()
    vectors = store.load(conn, store.KIND_REVIEW, model)
    ordered = [vectors.get(key, ()) for key in keys]
    pairs = embeddings.near_pairs(ordered, options.review_dup)
    return tuple(
        (int(getattr(items[left], "index", left)), int(getattr(items[right], "index", right)))
        for left, right in pairs
    )


def vacancy_text(row: object) -> str:
    """Что именно векторизуется: название, компания и описание одной строкой."""
    parts = [
        str(_field(row, "title") or ""),
        str(_field(row, "company") or ""),
        str(_field(row, "description") or ""),
    ]
    return "\n".join(part for part in parts if part).strip()


def _field(row: object, name: str) -> object:
    try:
        return row[name]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, None)


def index_vacancies(
    conn: sqlite3.Connection, gateway: object | None, limit: int = 500
) -> str:
    """Досчитывает векторы вакансий. Возвращает имя модели или пустую строку."""
    if not settings.embeddings_options().enabled:
        return ""
    rows = conn.execute(
        """
        SELECT key, title, company, description FROM vacancies
        WHERE description IS NOT NULL AND description <> ''
        ORDER BY last_seen_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    texts = {str(row["key"]): vacancy_text(row) for row in rows}
    return store.vectorize(conn, gateway, store.KIND_VACANCY, texts)


def similar_vacancies(
    conn: sqlite3.Connection, gateway: object | None, key: str
) -> list[tuple[str, float]]:
    """[(ключ вакансии, близость)] — только по уже посчитанным векторам.

    Страница ничего не считает и никуда не ходит: векторизация идёт прогоном.
    Иначе открытие вакансии будило бы модель на десятки секунд.
    """
    options = settings.embeddings_options()
    if not options.enabled:
        return []
    import llm_embed

    model = llm_embed.model_name(gateway)
    if not model:
        return []
    vectors = store.load(conn, store.KIND_VACANCY, model)
    query = vectors.pop(key, None)
    if query is None:
        return []
    return embeddings.top_similar(
        query, vectors.items(), limit=SIMILAR_LIMIT, threshold=options.vacancy_sim
    )


__all__ = (
    "SIMILAR_LIMIT",
    "index_vacancies",
    "review_pairs",
    "similar_vacancies",
    "vacancy_text",
)
