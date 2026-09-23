"""Гейт перед этапами модели: не звать её там, где ответ предсказуем.

Остался один вопрос, и он решается уже посчитанными векторами, без разметки
руками и без новых зависимостей [CORE-015], [CORE-025]: **близость к профилю**.
Вектор вакансии против вектора владельца (критерии из profile.yaml плюс
подтверждённые блоки резюме). Далёкая вакансия не доходит до extract и
hr_filter: её всё равно не покажут.

Гейты пустоты — по соседям и обученный — были здесь же и удалены после замера:
на 80 вакансиях AUROC 0.579 у `hr_filter` и 0.480 у `extract`, то есть монетка
(docs/gates.md, «Гейт пустоты: опровергнут замером»). Вектор bge-m3 кодирует
тему вакансии, а находка этапа зависит от конкретных формулировок.

Гейт выключен по умолчанию и деградирует в «пропустить дальше» [CORE-017]: нет
векторов, нет эмбеддера — вакансия идёт в модель, как и раньше. Гейт экономит
вызовы [CORE-016], но не имеет права терять вакансии молча, поэтому каждый
отказ пишется в лог.
"""


from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

import embeddings
import embeddings_store as store
import llm_embed
import settings

log = logging.getLogger("fuckhr")

KIND_OWNER = "owner"
OWNER_KEY = "profile"

@dataclass(frozen=True)
class GateOptions:
    """Порог гейта. Ноль означает «выключено»."""

    profile_min: float


def options() -> GateOptions:
    return GateOptions(
        profile_min=max(0.0, min(0.99, settings.as_float(os.getenv("GATE_PROFILE_MIN"), 0.0))),
    )


def owner_text(conn: sqlite3.Connection, profile_path: str = "profile.yaml") -> str:
    """Чем описан владелец: критерии поиска и подтверждённые блоки резюме.

    Резюме берётся только подтверждённое: черновик от модели описывает не
    владельца, а её представление о нём.
    """
    parts: list[str] = []
    try:
        import profiles

        for loaded in profiles.load_all(profile_path):
            profile = loaded.profile
            if not profile.enabled:
                continue
            parts.append(loaded.name)
            parts.extend(str(query.get("text", "")) for query in profile.queries)
            parts.extend(profile.skills)
            parts.extend(profile.nice_to_have)
    except Exception as exc:  # noqa: BLE001 — профиль может быть любым [CORE-017]
        log.debug("профиль для гейта не прочитан: %s", exc)
    try:
        rows = conn.execute(
            "SELECT heading, body FROM resume_blocks WHERE confirmed = 1 ORDER BY position"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        parts.append("{} {}".format(row[0] or "", row[1] or "").strip())
    return "\n".join(part for part in parts if part).strip()


def owner_vector(
    conn: sqlite3.Connection, gateway: Any, profile_path: str = "profile.yaml"
) -> Sequence[float] | None:
    """Вектор владельца. Считается один раз и живёт в той же таблице."""
    model = llm_embed.model_name(gateway)
    text = owner_text(conn, profile_path)
    if not model or not text:
        return None
    known = store.load(conn, KIND_OWNER, model)
    # Ключ содержит длину текста: правка profile.yaml должна пересчитать вектор,
    # а не молча остаться со старым.
    key = "{}:{}".format(OWNER_KEY, len(text))
    if key in known:
        return known[key]
    store.vectorize(conn, gateway, KIND_OWNER, {key: text})
    return store.load(conn, KIND_OWNER, model).get(key)


def relevance(
    conn: sqlite3.Connection, gateway: Any, keys: Sequence[str]
) -> dict[str, float]:
    """{ключ вакансии: близость к владельцу}. Нет вектора — ключа нет в ответе."""
    owner = owner_vector(conn, gateway)
    model = llm_embed.model_name(gateway)
    if owner is None or not model:
        return {}
    vectors = store.load(conn, store.KIND_VACANCY, model)
    return {
        key: embeddings.cosine(owner, vectors[key]) for key in keys if key in vectors
    }


def keep_for_stage(
    conn: sqlite3.Connection, gateway: Any, stage: str, vacancies: Sequence[Any]
) -> list[Any]:
    """Отсеивает вакансии, на которых этап не имеет смысла.

    Гейт выключен по умолчанию, поэтому по умолчанию список возвращается как
    есть: включение — осознанное действие владельца, а не сюрприз.
    """
    opts = options()
    if opts.profile_min <= 0:
        return list(vacancies)
    keys = [str(getattr(item, "key", "")) for item in vacancies]
    scores = relevance(conn, gateway, keys)
    kept = []
    for item in vacancies:
        key = str(getattr(item, "key", ""))
        near = scores.get(key)
        if near is not None and near < opts.profile_min:
            log.info(
                "этап %s пропущен для %s: близость к профилю %.2f < %.2f",
                stage,
                key,
                near,
                opts.profile_min,
            )
            continue
        kept.append(item)
    if len(kept) != len(vacancies):
        log.info("гейт этапа %s: осталось %s из %s", stage, len(kept), len(vacancies))
    return kept


__all__ = (
    "GateOptions",
    "KIND_OWNER",
    "keep_for_stage",
    "options",
    "owner_text",
    "owner_vector",
    "relevance",
)
