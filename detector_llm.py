"""Необязательная LLM-обвязка над детерминированным детектором.

Модуль лежит отдельно от detector.py специально: ядро должно оставаться
импортируемым и тестируемым без единой строчки про модели [CORE-015].

Роль модели здесь максимально узкая: она только показывает пальцем на фразу,
которая похожа на проверяемое утверждение. Модель не выносит вердиктов и
не придумывает числа [CORE-019]. Каждая цитата проверяется подстрокой в
исходном тексте: пересказ и галлюцинации выбрасываются молча.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

import detector
from detector import Claim, Finding, Report

import injection

log = logging.getLogger(__name__)

STAGE = "hr_filter"

PROMPT = (
    "Ты разбираешь текст вакансии. Найди утверждения о работе и команде, "
    "которые можно проверить внешними данными.\n"
    "Правила:\n"
    "- quote копируй из текста дословно, без пересказа;\n"
    "- не оценивай тон и не делай выводов;\n"
    "- не выдумывай числа и факты о компании;\n"
    "- если таких утверждений нет, верни пустой список.\n"
    'JSON: {"claims": [{"label": "короткое название", "quote": "точная цитата"}]}'
)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _parse(raw: str) -> list[dict[str, Any]]:
    """Достаёт JSON из ответа, даже если модель завернула его в пояснения."""
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        log.warning("модель вернула не JSON, игнорируем")
        return []
    claims = payload.get("claims")
    return [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []


def llm_claims(gateway: Any, text: str, limit: int = 5) -> tuple[Claim, ...]:
    """Спрашивает у модели цитаты и оставляет только реально существующие."""
    if not text.strip():
        return ()
    raw = gateway.complete(
        STAGE,
        [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": injection.safe(text[:6000], "hr_filter")[0]},
        ],
    )
    if not raw:
        return ()

    # Как и в extract: цитата из вырезанной строки инъекции не считается
    # цитатой из текста вакансии (ADR-020).
    haystack = _normalize(injection.clean(text)[0])
    out: list[Claim] = []
    for item in _parse(raw):
        quote = str(item.get("quote") or "").strip()
        label = str(item.get("label") or "утверждение из текста").strip()
        if len(quote) < 8 or _normalize(quote) not in haystack:
            # Модель пересказала или выдумала цитату — такое не показываем.
            log.info("цитата не нашлась в тексте вакансии, отброшено: %r", quote[:80])
            continue
        out.append(Claim(key="llm_claim", label=label, quote=quote))
        if len(out) >= limit:
            break
    return tuple(out)


def wanted(gateway: Any, use_llm_claims: bool, passed: bool) -> bool:
    """Звать ли модель за утверждениями по этой вакансии.

    Этап hr_filter шёл на каждую собранную вакансию и был самым дорогим местом
    прогона: сотня вызовов профиля smart на сотню карточек. При этом находки
    `llm_claim` всегда получают вердикт «недостаточно данных» и видны только
    там, где вакансию вообще показывают. Вакансия, не прошедшая порог профиля,
    не попадает ни в очередь досье, ни в контакты — значит и утверждения по ней
    никто не прочитает, а вызов уже потрачен [CORE-016].

    Правило детерминированное и совпадает с тем, по которому собираются досье
    [CORE-015]: прошла порог — спрашиваем, не прошла — остаётся отчёт детектора.
    """
    return bool(gateway is not None and use_llm_claims and passed)


def with_llm_claims(report: Report, vacancy: Any, gateway: Any) -> Report:
    """Дополняет отчёт утверждениями, которые не ловят регулярки.

    Вердикт таких пунктов всегда «недостаточно данных»: проверять их пока нечем,
    а в Telegram они не попадут вовсе — detector.telegram_lines отбирает только
    неподтверждённые. Так модель физически не может написать владельцу
    «компания врёт» от своего имени.
    """
    text = detector.vacancy_text(vacancy)
    known = {f.claimed for f in report.findings}
    extra: list[Finding] = []
    for claim in llm_claims(gateway, text):
        if claim.quote in known:
            continue
        extra.append(
            Finding(
                kind="llm_claim",
                claimed=claim.quote,
                found=f"модель отметила утверждение «{claim.label}»; проверить его пока нечем",
                verdict=detector.NO_DATA,
                confidence="низкая",
                sources=("текст вакансии", "llm"),
            )
        )
    if not extra:
        return report
    return Report(
        key=report.key,
        findings=tuple(report.findings) + tuple(extra),
        history=report.history,
    )


def stage_profile() -> str:
    """Удобство для логов и тестов: на каком профиле едет эта обвязка."""
    import llm

    return llm.profile_for(STAGE)


__all__: Sequence[str] = (
    "llm_claims",
    "wanted",
    "with_llm_claims",
    "stage_profile",
    "STAGE",
)
