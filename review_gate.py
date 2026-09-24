"""Линейный гейт двух этапов отзывов: не звать модель там, где ответ ясен.

Этапы `review_fake` («заказной отзыв?») и `ai_text` («писала нейросеть?») стоят
вызова модели на каждый отзыв. При этом векторы bge-m3 по тем же текстам уже
считаются для поиска пересказов (`embeddings_tasks.review_pairs`), а на готовом
векторе логистическая регрессия в рантайме стоит одно скалярное произведение.

Замер 24.09.2026 на отложенной части разметки (docs/review-gate.md): AUROC
0.815 и 0.806, ложных срабатываний нет начиная с порога 0.5. Дообученная Laya
на той же выборке дала 0.79–0.83, то есть то же самое ценой второго энкодера в
памяти, — поэтому её убрали, а гейт оставили линейным [CORE-025].

Что делает гейт. Уверенное «да» (выше `high`) и уверенное «нет» (ниже `low`)
принимаются без модели, середина уходит в шлюз, как раньше [CORE-016]. Порог
«да» и порог «нет» разные: ошибка «назвал живой отзыв рекламой» дороже, чем
лишний вызов.

Чего гейт не делает. Разметку учителя он не пишет: метки копятся только там,
где модель действительно спросили, иначе гейт учился бы на своих же ответах.
Значит, чем больше он экономит, тем медленнее копится разметка на будущее —
это плата, и она видна на странице «Гейт отзывов».

Без весов, без эмбеддера или при выключенной настройке функции возвращают
«ничего не решено», и оба этапа работают как до гейта [CORE-017].
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Mapping, Sequence

import ai_text_rules
import embeddings_store as store
import linear_model
import llm_embed
import review_gate_store
import settings
from fake_reviews import text_hash

STAGES = ("review_fake", "ai_text")

# Этап ai_text учится не на одном векторе, а на векторе плюс приметы
# стилометрии (`ai_text_rules.features`): сами по себе приметы дают AUROC 0.712
# и почти не срабатывают на пороге, но обученные веса их калибруют. Версия в
# имени модели отделяет такие веса от старых: размерность другая, и старые
# несравнимы [LLM-011].
RULES_VERSION = "+rules1"


def model_key(stage: str, model: str) -> str:
    """Под каким именем лежат веса этапа: у ai_text это модель плюс версия примет."""
    return "{}{}".format(model, RULES_VERSION) if stage == ai_text_rules.STAGE else model


def augment(stage: str, vector: Sequence[float], text: str) -> Sequence[float]:
    """Вектор текста плюс приметы — вход связки. Прочие этапы не трогаются."""
    if stage != ai_text_rules.STAGE:
        return vector
    return tuple(vector) + ai_text_rules.features(text)
# По замеру ложных нет уже при 0.5, но запас в обе стороны стоит одного вызова.
DEFAULT_LOW = 0.2
DEFAULT_HIGH = 0.6

log = logging.getLogger("fuckhr")


@dataclass(frozen=True)
class GateOptions:
    enabled: bool
    low: float
    high: float

    @property
    def usable(self) -> bool:
        """Перевёрнутые пороги — это не гейт, а решето: лучше выключить.

        Равные пороги допустимы: на хорошо разделимой выборке середины нет, и
        гейт решает всё сам. Порог «нет» выше порога «да» — уже противоречие.
        """
        return self.enabled and 0.0 <= self.low <= self.high <= 1.0


def options() -> GateOptions:
    return GateOptions(
        enabled=settings.flag("REVIEW_GATE_ENABLED"),
        low=settings.as_float(os.getenv("REVIEW_GATE_LOW"), DEFAULT_LOW),
        high=settings.as_float(os.getenv("REVIEW_GATE_HIGH"), DEFAULT_HIGH),
    )


def vectors_for(
    conn: sqlite3.Connection, gateway: object | None, texts: Mapping[str, str]
) -> tuple[str, dict[str, Sequence[float]]]:
    """(имя модели, {хэш текста: вектор}). Кэш общий с поиском пересказов."""
    model = store.vectorize(conn, gateway, store.KIND_REVIEW, dict(texts))
    if not model:
        return "", {}
    known = store.load(conn, store.KIND_REVIEW, model)
    return model, {digest: known[digest] for digest in texts if digest in known}


def scores(
    conn: sqlite3.Connection | None,
    gateway: object | None,
    stage: str,
    texts: Mapping[object, str],
) -> dict[object, float]:
    """Вероятность «да» по каждому ключу. Пусто — решать нечем [CORE-017]."""
    if conn is None or not texts or stage not in STAGES:
        return {}
    model = llm_embed.model_name(gateway)
    if not model:
        return {}
    trained = review_gate_store.load(conn, stage, model_key(stage, model))
    if trained is None:
        log.info("гейт %s молчит: веса для %s не обучены", stage, model)
        return {}
    wanted = {text_hash(text): text for text in texts.values() if (text or "").strip()}
    if not wanted:
        return {}
    _, known = vectors_for(conn, gateway, wanted)
    if not known:
        return {}
    out: dict[object, float] = {}
    for key, text in texts.items():
        vector = known.get(text_hash(text or ""))
        if vector:
            out[key] = linear_model.predict(
                trained.weights, trained.bias, augment(stage, vector, text or "")
            )
    return out


def decide(
    conn: sqlite3.Connection | None,
    gateway: object | None,
    stage: str,
    texts: Mapping[object, str],
) -> tuple[set, set, dict[object, float]]:
    """(уверенные «да», уверенные «нет», все посчитанные вероятности).

    Ключи, которых нет ни в одном множестве, — середина: их спрашивают у
    модели, как раньше. Выключенный гейт возвращает пустоту и ничего не считает.
    """
    opts = options()
    if not opts.usable:
        return set(), set(), {}
    chances = scores(conn, gateway, stage, texts)
    yes = {key for key, value in chances.items() if value >= opts.high}
    no = {key for key, value in chances.items() if value <= opts.low}
    if chances:
        log.info(
            "гейт %s: %s из %s решено без модели (да %s, нет %s)",
            stage, len(yes) + len(no), len(texts), len(yes), len(no),
        )
    return yes, no, chances


def solve(
    conn: sqlite3.Connection | None,
    gateway: object | None,
    stage: str,
    texts: Mapping[object, str],
) -> set | None:
    """Ответ этапа без модели вовсе. None — решать нечем, зовите модель.

    Отличие от гейта: тот берёт на себя только уверенные края и требует нуля
    ложных, поэтому в перекрытии классов не решает ничего. Решатель отвечает по
    одному порогу, подобранному на разметке по балансу точности и полноты
    (`linear_model.decision`, колонка `decide`). Так можно и нужно поступать
    там, где ответ весит один балл в счёте компании, а не выносит вердикт
    [CORE-019].
    """
    if conn is None or not texts or not settings.flag("AI_TEXT_SOLVER"):
        return None
    if stage != ai_text_rules.STAGE:
        return None
    model = llm_embed.model_name(gateway)
    trained = (
        review_gate_store.load(conn, stage, model_key(stage, model)) if model else None
    )
    if trained is None:
        log.info("решатель %s молчит: веса не обучены", stage)
        return None
    limit = trained.decide if trained.decide is not None else 0.5
    chances = scores(conn, gateway, stage, texts)
    if not chances:
        return None
    out = {key for key, value in chances.items() if value >= limit}
    log.info(
        "решатель %s: %s из %s без единого вызова (порог %.2f)",
        stage, len(out), len(texts), limit,
    )
    return out


# Имена математики остаются видимыми отсюда: они были частью модуля до
# выделения `linear_model.py`, и звать её через два имени незачем.
auroc = linear_model.auroc
counts = linear_model.counts
predict = linear_model.predict
sigmoid = linear_model.sigmoid

__all__ = (
    "DEFAULT_HIGH",
    "DEFAULT_LOW",
    "GateOptions",
    "STAGES",
    "auroc",
    "counts",
    "decide",
    "options",
    "predict",
    "scores",
    "sigmoid",
    "vectors_for",
)
