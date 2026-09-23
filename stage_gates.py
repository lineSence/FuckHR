"""Гейты перед этапами модели: не звать её там, где ответ предсказуем.

Два вопроса, оба решаются уже посчитанными векторами и своей же базой — без
разметки руками и без новых зависимостей [CORE-015], [CORE-025].

1. **Близость к профилю.** Вектор вакансии против вектора владельца (критерии
   из profile.yaml плюс подтверждённые блоки резюме). Далёкая вакансия не
   доходит до extract и hr_filter: её всё равно не покажут.
2. **Гейт пустоты по соседям.** По ближайшим соседям из базы видно, чем этап
   кончался на похожих текстах. Если у всех соседей этап вернул пусто, шансов
   мало. Метки здесь ничьи: их проставил сам пайплайн, когда сохранял результат.
3. **Обученный гейт пустоты.** Те же метки, но вместо k соседей — логистическая
   регрессия на тех же векторах (`stage_gate_train.py`, веса в `gate_models`).
   Соседям нужно k текстов ближе 0.85, и на живой базе такая теснота — редкость,
   поэтому гейт по соседям почти не срабатывает; регрессия смотрит на всю
   выборку сразу. Работает только «нет»: пропустить этап можно, а вынести за
   него вердикт нельзя — этапу нужна дословная цитата из текста.

Оба гейта выключены по умолчанию и деградируют в «пропустить дальше»
[CORE-017]: нет векторов, нет эмбеддера, мало соседей — вакансия идёт в модель,
как и раньше. Гейт экономит вызовы [CORE-016], но не имеет права терять
вакансии молча, поэтому каждый отказ пишется в лог.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

import embeddings
import embeddings_store as store
import embeddings_tasks
import linear_model
import llm_embed
import review_gate_store
import settings

log = logging.getLogger("fuckhr")

KIND_OWNER = "owner"
OWNER_KEY = "profile"

# Этап -> SQL, отвечающий «этап что-то дал по этому ключу».
STAGE_OUTCOME = {
    "extract": "SELECT 1 FROM vacancy_conditions WHERE key = ? LIMIT 1",
    "hr_filter": (
        "SELECT 1 FROM vacancy_signals WHERE key = ? AND payload LIKE '%llm_claim%' LIMIT 1"
    ),
}
# Ключи с отчётом этапа: по ним видно, что этап вообще отрабатывал.
STAGE_SEEN = {
    "extract": "SELECT DISTINCT key FROM vacancy_conditions",
    "hr_filter": "SELECT key FROM vacancy_signals",
}


@dataclass(frozen=True)
class GateOptions:
    """Пороги гейтов. Нули означают «выключено»."""

    profile_min: float
    empty_k: int
    empty_sim: float
    learned: bool


def options() -> GateOptions:
    return GateOptions(
        profile_min=max(0.0, min(0.99, settings.as_float(os.getenv("GATE_PROFILE_MIN"), 0.0))),
        empty_k=max(0, min(50, settings.as_int(os.getenv("GATE_EMPTY_K"), 0))),
        empty_sim=max(0.5, min(0.999, settings.as_float(os.getenv("GATE_EMPTY_SIM"), 0.85))),
        learned=settings.flag("GATE_LEARNED"),
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


def outcomes(conn: sqlite3.Connection, stage: str) -> dict[str, bool]:
    """{ключ: дал ли этап результат} по всем вакансиям, где этап отрабатывал."""
    seen_sql, outcome_sql = STAGE_SEEN.get(stage), STAGE_OUTCOME.get(stage)
    if not seen_sql or not outcome_sql:
        return {}
    try:
        keys = [str(row[0]) for row in conn.execute(seen_sql).fetchall()]
        productive = {
            str(key)
            for key in keys
            if conn.execute(outcome_sql, (key,)).fetchone() is not None
        }
    except sqlite3.Error:
        return {}
    return {key: key in productive for key in keys}


def likely_empty(
    conn: sqlite3.Connection,
    gateway: Any,
    stage: str,
    key: str,
    text: str = "",
) -> bool:
    """Вернёт ли этап пустоту, судя по соседям. Сомнение трактуется как «нет».

    Соседей ищем среди вакансий, где этап уже отрабатывал. Нужно ровно k
    соседей ближе порога, и у всех k результат должен быть пустым: одного
    продуктивного соседа хватает, чтобы вакансия пошла в модель.
    """
    opts = options()
    if opts.empty_k <= 0:
        return False
    model = llm_embed.model_name(gateway)
    if not model:
        return False
    vectors = store.load(conn, store.KIND_VACANCY, model)
    query = vectors.pop(key, None)
    if query is None:
        return False
    known = outcomes(conn, stage)
    pool = [(other, vector) for other, vector in vectors.items() if other in known]
    if len(pool) < opts.empty_k:
        return False
    near = embeddings.top_similar(
        query, pool, limit=opts.empty_k, threshold=opts.empty_sim
    )
    if len(near) < opts.empty_k:
        return False
    if any(known.get(other) for other, _ in near):
        return False
    log.info(
        "этап %s пропущен для %s: %s ближайших соседей не дали ничего",
        stage,
        key,
        len(near),
    )
    return True


def learned_empty(
    conn: sqlite3.Connection, gateway: Any, stage: str, key: str
) -> bool:
    """Вернёт ли этап пустоту, судя по обученным весам.

    Порог берётся из замера и лежит рядом с весами (`gate_models.low`): это
    самая высокая граница, при которой на отложенной части не потерялось ни
    одной продуктивной вакансии. Порога нет — гейт молчит: подбирать его на
    глаз здесь нельзя, ценой будет потерянная вакансия [CORE-017].
    """
    if not options().learned:
        return False
    model = llm_embed.model_name(gateway)
    if not model:
        return False
    trained = review_gate_store.load(conn, stage, model)
    if trained is None or trained.low is None:
        return False
    vector = store.load(conn, store.KIND_VACANCY, model).get(key)
    if not vector:
        return False
    chance = linear_model.predict(trained.weights, trained.bias, vector)
    if chance > trained.low:
        return False
    log.info(
        "этап %s пропущен для %s: обученный гейт даёт %.2f при пороге %.2f",
        stage, key, chance, trained.low,
    )
    return True


def keep_for_stage(
    conn: sqlite3.Connection, gateway: Any, stage: str, vacancies: Sequence[Any]
) -> list[Any]:
    """Отсеивает вакансии, на которых этап не имеет смысла.

    Оба гейта выключены по умолчанию, поэтому по умолчанию список возвращается
    как есть: включение — осознанное действие владельца, а не сюрприз.
    """
    opts = options()
    if opts.profile_min <= 0 and opts.empty_k <= 0 and not opts.learned:
        return list(vacancies)
    keys = [str(getattr(item, "key", "")) for item in vacancies]
    scores = relevance(conn, gateway, keys) if opts.profile_min > 0 else {}
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
        if learned_empty(conn, gateway, stage, key):
            continue
        if likely_empty(conn, gateway, stage, key, embeddings_tasks.vacancy_text(item)):
            continue
        kept.append(item)
    if len(kept) != len(vacancies):
        log.info(
            "гейты этапа %s: осталось %s из %s", stage, len(kept), len(vacancies)
        )
    return kept


__all__ = (
    "GateOptions",
    "KIND_OWNER",
    "keep_for_stage",
    "learned_empty",
    "likely_empty",
    "outcomes",
    "options",
    "owner_text",
    "owner_vector",
    "relevance",
)
