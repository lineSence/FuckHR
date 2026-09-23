"""Блок «Когда модель не зовут»: гейты и разметчик спанами.

Отдельный файл, потому что ui_forms.py подходит к 25 КБ [CORE-024], а вопрос
здесь свой: таблица маршрутов отвечает «куда пойдёт запрос», этот блок —
«пойдёт ли вообще». Без него включённый гейт выглядит как молчание пайплайна:
этап не вызван, и по маршрутам этого не видно.

Ни одного вызова модели и ни одного запроса в сеть: читаются настройки, таблица
векторов и каталог кэша весов [CORE-016].
"""

from __future__ import annotations

import sqlite3
from typing import Any

import embeddings_store
import extract_spans
import settings
import stage_gates
from ui_core import esc, table


def _owner_ready(conn: sqlite3.Connection, gateway: Any) -> tuple[bool, bool]:
    """(есть чем описать владельца, посчитан ли его вектор).

    Вектор здесь не считается: это вызов модели, а страница только показывает.
    """
    try:
        text = bool(stage_gates.owner_text(conn))
    except Exception:  # noqa: BLE001 — профиль может быть любым [CORE-017]
        text = False
    counted = False
    try:
        import llm_embed

        model = llm_embed.model_name(gateway)
        if model:
            counted = bool(
                embeddings_store.load(conn, stage_gates.KIND_OWNER, model)
            )
    except Exception:  # noqa: BLE001 — нет шлюза или таблицы [CORE-017]
        counted = False
    return text, counted


def _vacancy_vectors(conn: sqlite3.Connection) -> int:
    try:
        return sum(
            count
            for kind, _model, count in embeddings_store.counts(conn)
            if kind == embeddings_store.KIND_VACANCY
        )
    except Exception:  # noqa: BLE001
        return 0


def render_stages(conn: sqlite3.Connection, gateway: Any = None) -> str:
    """Гейты и разметчик: включено ли, и хватает ли для этого данных."""
    if gateway is None:
        try:
            import llm

            gateway = llm.Gateway.from_env(conn)
        except Exception:  # noqa: BLE001 — без шлюза блок всё равно полезен
            gateway = None
    opts = stage_gates.options()
    owner_text, owner_vector = _owner_ready(conn, gateway)
    vectors = _vacancy_vectors(conn)
    vectors_on = settings.embeddings_options().enabled

    rows = [
        [
            "Гейт близости к профилю",
            esc("{:g}".format(opts.profile_min)) if opts.profile_min else "выключен",
            "нужен вектор владельца и векторы вакансий",
        ],
        [
            "Векторы",
            "считаются" if vectors_on else "выключены (EMBEDDINGS_ENABLED)",
            esc("вакансий с вектором: {}".format(vectors)),
        ],
        [
            "Вектор владельца",
            "посчитан" if owner_vector else "нет",
            "профиль и подтверждённое резюме"
            if owner_text
            else "описать владельца нечем: пусты profile.yaml и резюме",
        ],
    ]

    warn = ""
    if opts.profile_min and not (vectors_on and owner_text):
        warn = (
            "<div class=warn>Гейт близости включён, а считать близость не по чему: "
            "без векторов и описания владельца он молча пропускает все вакансии "
            "дальше.</div>"
        )

    span_rows = [
        [
            "Разметка условий спанами",
            "включена" if extract_spans.enabled() else "выключена (GLINER_ENABLED)",
            "включённая переводит этап extract с генерации на разметку",
        ],
        [
            "Пакет gliner",
            "установлен" if extract_spans.installed() else "нет",
            esc("pip install gliner"),
        ],
        [
            "Веса",
            "скачаны" if extract_spans.weights_ready() else "нет в кэше",
            esc(extract_spans.cache_dir()),
        ],
        [
            "Модель разметчика",
            esc(extract_spans.model_name()),
            esc("порог {:g}".format(extract_spans.threshold())),
        ],
    ]

    span_warn = ""
    if extract_spans.enabled() and not extract_spans.installed():
        span_warn = (
            "<div class=warn>Разметка включена, но пакета нет: этап extract всё "
            "равно идёт генеративной моделью, как раньше.</div>"
        )
    elif extract_spans.enabled() and not extract_spans.weights_ready():
        span_warn = (
            "<div class=warn>Пакет есть, весов в кэше нет: первая вакансия прогона "
            "потянет 1.2 ГБ с Hugging Face.</div>"
        )

    return "".join(
        [
            "<h2>Когда модель не зовут</h2>",
            "<p class=muted>Гейты экономят вызовы там, где ответ предсказуем, а "
            "разметчик заменяет генерацию на цитату из текста. Всё выключено по "
            "умолчанию; каждый отказ от вызова виден в логе прогона "
            "(<code>docs/gates.md</code>).</p>",
            table(["Что", "Состояние", "Чем живёт"], rows),
            warn,
            table(["Разметчик", "Состояние", "Подробность"], span_rows),
            span_warn,
            "<p class=muted>Проверить разметчик, ничего не включая: "
            "<code>python extract_spans.py --text «...» --force</code>.</p>",
        ]
    )


__all__ = ("render_stages",)
