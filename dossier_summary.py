"""Сводка по досье: словами и строками карточки.

Отдельно от dossier.py по [CORE-024] и по смыслу: здесь формат вывода, там
сборка данных. Модель участвует только тут и только в пересказе — ни один флаг,
ни одна цифра от неё не зависят [CORE-015], [LLM-009]. Шлюз выключен или
ответил ошибкой — остаётся детерминированная сводка [CORE-017].

Досье принимается по утиному типу, а не импортом dossier.Dossier: иначе цикл
импортов.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import fake_rules
from dossier_rules import MAX_LLM_CHARS, MAX_LLM_REVIEWS, RISK_RU

if TYPE_CHECKING:
    from dossier import Dossier

import injection

log = logging.getLogger(__name__)


def summarize(gateway: Any, dossier: "Dossier") -> tuple[str | None, str]:
    """Сводка словами. Возвращает (текст, кем собрана).

    Модель видит только название компании и тексты публичных отзывов. Она не
    меняет ни флаги, ни риск: иначе один галлюцинированный абзац перекрашивал бы
    компанию из красной в зелёную.

    В выдержки идут сначала прочитанные страницы и только потом сниппеты: если
    отдать модели рекламные заголовки отзовиков, она справедливо ответит, что
    данных недостаточно.
    """
    deterministic = format_summary(dossier)
    if gateway is None or not getattr(gateway, "enabled", False) or not dossier.reviews:
        return deterministic, "правила"

    ordered = sorted(dossier.reviews, key=lambda r: (not r.has_body, -len(r.text)))
    excerpts = []
    skipped = 0
    for review in ordered[:MAX_LLM_REVIEWS]:
        text = (review.body or review.text).strip()
        # Отзыв с инъекцией модели не показываем вовсе (ADR-020): один
        # отбеливающий абзац дешевле накрутки сотни отзывов.
        if injection.scan(text).red:
            skipped += 1
            continue
        excerpts.append("[{}] {}".format(review.site_name, text[:MAX_LLM_CHARS]))
    if skipped:
        log.warning("отзывов с инъекцией не отдаём модели: %s", skipped)
    if not excerpts:
        return deterministic, "правила"

    if dossier.read_count:
        preface = "Ниже тексты отзывов сотрудников о работодателе «{company}»."
    else:
        preface = (
            "Ниже только заголовки и сниппеты выдачи по работодателю «{company}»: "
            "сами страницы отзывов открыть не удалось."
        )
    prompt = (
        preface + "\n"
        "Назови 3–5 повторяющихся закономерностей одним списком, без введения.\n"
        "Правила: опирайся только на текст; если данных мало — скажи это прямо; "
        "не выдумывай цифр; не упоминай имён людей.\n\n{body}"
    ).format(company=dossier.company, body="\n\n".join(excerpts))

    try:
        answer = gateway.complete(  # type: ignore[attr-defined]
            # Этап «dossier», а не «company»: в отзывах встречаются имена
            # сотрудников, и профиль этапа (LOCAL, PERSONAL_STAGES) должен
            # решать, уходит ли это на внешний прокси [CORE-012].
            "dossier",
            [{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — досье важнее красивой сводки
        log.warning("сводка по отзывам не собрана: %s", exc)
        return deterministic, "правила"

    if not answer:
        return deterministic, "правила"
    return answer.strip(), "модель"


def format_summary(dossier: "Dossier") -> str:
    """Сводка без модели: только то, что посчитано."""
    if not dossier.reviews:
        return "Отзывов не найдено — о работодателе неизвестно ничего."
    parts = [
        "Отзывов: {}".format(dossier.review_count),
    ]
    if dossier.read_count:
        parts.append(
            "прочитано страниц: {} из {}".format(
                dossier.read_count, dossier.review_count
            )
        )
    else:
        parts.append("тексты страниц не прочитаны, только выдача поиска")
    if dossier.avg_rating is not None:
        parts.append("средняя оценка {:.1f} из 5".format(dossier.avg_rating))
    parts += _fake_parts(dossier)
    red = dossier.red_flags
    green = dossier.green_flags
    if red:
        parts.append(
            "повторяется: "
            + "; ".join("{} ({})".format(p.label.lower(), p.hits) for p in red[:4])
        )
    if green:
        parts.append(
            "в плюс: "
            + "; ".join("{} ({})".format(p.label.lower(), p.hits) for p in green[:3])
        )
    if not red and not green:
        parts.append("повторяющихся сюжетов не видно")
    return ". ".join(parts) + "."


def _fake_parts(dossier: "Dossier") -> list[str]:
    """Строки про накрутку. Метка всегда раскрывается: чем именно она вызвана."""
    mark = dossier.mark
    if not mark.total or mark.level == fake_rules.MARK_NONE:
        return []
    parts = ["{}: {}".format(mark.label, "; ".join(sign.text for sign in mark.signs))]
    if (
        mark.avg_all is not None
        and mark.avg_clean is not None
        and abs(mark.avg_all - mark.avg_clean) >= 0.1
    ):
        parts.append(
            "средняя без подозрительных {:.1f} против {:.1f} по всем".format(
                mark.avg_clean, mark.avg_all
            )
        )
    return parts


def format_lines(dossier: "Dossier", limit: int = 3) -> list[str]:
    """Строки для карточки в Telegram и для страницы вакансии."""
    lines = [
        "Работодатель: {} · отзывов {}".format(
            RISK_RU.get(dossier.risk, dossier.risk), dossier.review_count
        )
    ]
    if dossier.avg_rating is not None:
        lines[0] += " · оценка {:.1f}".format(dossier.avg_rating)
    if dossier.mark.flagged:
        lines.append(
            "— {}: оценке площадки верить нельзя".format(fake_rules.MARK_FLAG_LABEL)
        )
    for pattern in dossier.red_flags[:limit]:
        lines.append("— {} (упоминаний: {})".format(pattern.label, pattern.hits))
    for pattern in dossier.green_flags[:2]:
        lines.append("+ {} (упоминаний: {})".format(pattern.label, pattern.hits))
    return lines


__all__ = ("format_lines", "format_summary", "summarize")
