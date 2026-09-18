"""Модель в работе над резюме: черновик секции и отбор блоков под вакансию.

Модуль отделён от `resume.py` по той же причине, что `llm_tasks.py` от `score.py`:
хранение и экспорт обязаны работать без единой строчки про модели [CORE-015].
Без шлюза помощник вырождается в обычную форму, а версия под вакансию — в мастер-
резюме целиком [CORE-017].

Два шага и разные границы у каждого.

`draft_section` — один вызов на секцию. Модели разрешено переформулировать ответ
владельца и добавлять типовые формулировки обязанностей, но не факты. Поэтому
результат всегда ложится в базу с пометкой source='ai' и confirmed=0, а числа
сверяются с исходным ответом: новое число — это выдуманный стаж, бюджет или
размер команды [CORE-019].

`pick_blocks` — один вызов на версию под вакансию. Модель возвращает только номера
блоков и их порядок. Ни одного символа текста от неё в версию не попадает: так
решено в B-01 (только перестановка и отбор, без переписывания под язык вакансии).

Про бюджет. Версия собирается для каждой прошедшей скоринг вакансии, и это
десятки вызовов в сутки. Сдерживают три вещи: отпечаток в resume_versions
(повторная вакансия вызова не стоит), кэш шлюза по хэшу промпта и общий
потолок LLM_MAX_CALLS [CORE-016].
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Sequence

import resume

log = logging.getLogger(__name__)

STAGE_SECTION = "resume_section"
STAGE_TAILOR = "resume_tailor"

MAX_ANSWER_CHARS = 4000
MAX_VACANCY_CHARS = 4000
MAX_BLOCK_CHARS = 600


def _parse_json(raw: str) -> dict[str, Any]:
    """Тот же разбор, что в llm_tasks: модели любят обкладывать JSON пояснениями."""
    if not raw:
        return {}
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        log.warning("модель вернула не JSON, игнорируем")
        return {}
    return payload if isinstance(payload, dict) else {}


def numbers(text: str) -> set[str]:
    """Числа без разделителей: «250 000» и «250000» — один и тот же факт."""
    cleaned = (text or "").replace("\u00a0", " ")
    cleaned = re.sub(r"(?<=\d)[  ](?=\d)", "", cleaned)
    return set(re.findall(r"\d+(?:[.,]\d+)?", cleaned))


SECTION_RULES = {
    "contacts": "Только то, что написал владелец. Ничего не добавляй.",
    "summary": "До трёх предложений: роль, стек, что человек закрывает сам.",
    "experience": (
        "От трёх до пяти пунктов списка. Каждый начинай с глагола действия. "
        "Результат важнее списка обязанностей."
    ),
    "skills": "Перечисли через запятую, сгруппировав родственное. Без уровней владения.",
    "projects": "Задача, роль, результат. До четырёх строк.",
    "education": "Одна-две строки без рассуждений.",
}


def draft_section(
    gateway: Any, section: str, answer: str, role_hint: str = ""
) -> tuple[str, list[str]] | None:
    """Черновик секции из ответа на вопрос интервью.

    Возвращает (текст, что дописано) или None, если модели нет, она промолчала
    или соврала. None — не авария: вызывающая сторона просто сохраняет ответ
    владельца как есть.

    Список дописанного нужен интерфейсу: владелец должен видеть не только что
    получилось, но и где ему приписали чужие слова.
    """
    text = (answer or "").strip()
    if gateway is None or not text:
        return None
    if section not in resume.SECTION_TITLES:
        return None

    prompt = (
        "Ты помогаешь человеку оформить секцию резюме «{title}».\n"
        "{rule}\n"
        "Жёсткие запреты:\n"
        "- не придумывай места работы, сроки, технологии и названия;\n"
        "- не добавляй и не меняй числа;\n"
        "- не выдумывай размер команды, нагрузку и эффект в процентах;\n"
        "- без канцелярита и без «ответственный и обучаемый».\n"
        "Формулировки обязанностей можно достроить, если они прямо следуют из "
        "ответа; каждую такую строку перечисли в added.\n"
        'Формат: {{"text": "...", "added": ["..."]}}'
    ).format(
        title=resume.SECTION_TITLES[section],
        rule=SECTION_RULES.get(section, ""),
    )
    user = text[:MAX_ANSWER_CHARS]
    if role_hint:
        user = "Искомая роль: {}\n\n{}".format(role_hint, user)

    raw = gateway.complete(
        STAGE_SECTION,
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    if not raw:
        return None

    payload = _parse_json(raw)
    drafted = str(payload.get("text") or "").strip()
    if not drafted:
        return None

    invented = numbers(drafted) - numbers(text)
    if invented:
        # Число, которого не было в ответе, — это выдуманный факт о владельце.
        log.warning(
            "в секции %s модель дописала числа %s, черновик отброшен",
            section,
            sorted(invented),
        )
        return None
    if len(drafted) > len(text) * 6 + 400:
        log.info("черновик секции %s раздут, оставляем ответ владельца", section)
        return None

    added_raw = payload.get("added")
    added = [
        str(item).strip()
        for item in (added_raw if isinstance(added_raw, list) else [])
        if str(item or "").strip()
    ]
    return drafted, added


def save_draft(
    conn: sqlite3.Connection,
    resume_id: int,
    section: str,
    answer: str,
    gateway: Any | None = None,
    heading: str = "",
    started: str = "",
    finished: str = "",
    role_hint: str = "",
) -> int:
    """Ответ интервью → блок резюме. Возвращает id блока.

    С моделью сохраняется её черновик с пометкой «предложено ИИ», без модели —
    дословный ответ владельца, сразу подтверждённый. Исходный ответ хранится в
    любом случае: без него невозможно понять, что человек говорил на самом деле.
    """
    text = (answer or "").strip()
    if not text:
        raise ValueError("пустой ответ сохранять нечего")

    drafted = draft_section(gateway, section, text, role_hint) if gateway else None
    if drafted is None:
        return resume.add_block(
            conn,
            resume_id,
            section,
            body=text,
            heading=heading,
            started=started,
            finished=finished,
            source=resume.SOURCE_OWNER,
            answer=text,
        )

    body, added = drafted
    if added:
        log.info("секция %s: модель дописала %s формулировок", section, len(added))
    return resume.add_block(
        conn,
        resume_id,
        section,
        body=body,
        heading=heading,
        started=started,
        finished=finished,
        source=resume.SOURCE_AI,
        answer=text,
    )


def _listing(items: Sequence[resume.Block]) -> str:
    lines = []
    for index, block in enumerate(items, start=1):
        body = " ".join(block.body.split())[:MAX_BLOCK_CHARS]
        lines.append(
            "{index}. [{section}] {label}: {body}".format(
                index=index,
                section=resume.SECTION_TITLES.get(block.section, block.section),
                label=block.label,
                body=body,
            )
        )
    return "\n".join(lines)


def pick_blocks(
    gateway: Any, items: Sequence[resume.Block], vacancy_text: str, keep: int = 8
) -> tuple[list[int], str] | None:
    """Номера блоков в порядке важности для этой вакансии.

    Модель возвращает только цифры из списка. Чужие номера отбрасываются,
    контакты возвращаются принудительно: резюме без способа связаться —
    бесполезная бумага, каким бы умным ни был отбор.
    """
    pool = [b for b in items if b.confirmed]
    if gateway is None or not pool or not (vacancy_text or "").strip():
        return None
    if len(pool) <= 3:
        return None

    prompt = (
        "Есть резюме из пронумерованных блоков и текст вакансии.\n"
        "Выбери блоки, которые важны именно для неё, и расставь по убыванию "
        "важности.\n"
        "Правила:\n"
        "- возвращай только номера из списка, ничего не переписывай;\n"
        "- не больше {keep} номеров;\n"
        "- не выбрасывай опыт только из-за другой отрасли, если стек совпадает;\n"
        "- reason — одна строка, почему такой порядок.\n"
        'Формат: {{"order": [3, 1, 7], "reason": "..."}}'
    ).format(keep=keep)

    raw = gateway.complete(
        STAGE_TAILOR,
        [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": "Вакансия:\n{}\n\nБлоки:\n{}".format(
                    (vacancy_text or "")[:MAX_VACANCY_CHARS], _listing(pool)
                ),
            },
        ],
    )
    if not raw:
        return None

    payload = _parse_json(raw)
    order_raw = payload.get("order")
    if not isinstance(order_raw, list):
        return None

    chosen: list[int] = []
    for value in order_raw:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= index <= len(pool):
            block_id = pool[index - 1].id
            if block_id not in chosen:
                chosen.append(block_id)
        if len(chosen) >= keep:
            break
    if not chosen:
        return None

    # Контакты возвращаются даже если модель сочла их нерелевантными.
    for block in pool:
        if block.section == "contacts" and block.id not in chosen:
            chosen.insert(0, block.id)
    reason = str(payload.get("reason") or "").strip()
    return chosen, reason


def version_for(
    conn: sqlite3.Connection,
    resume_id: int,
    vacancy_key: str,
    vacancy_text: str,
    gateway: Any | None = None,
) -> list[int]:
    """Блоки версии под вакансию с кэшем по отпечатку.

    Без модели или при её молчании возвращается мастер-резюме целиком: хуже
    подогнано, но полностью правдиво [CORE-017].
    """
    items = resume.blocks(conn, resume_id, confirmed_only=True)
    if not items:
        return []
    print_ = resume.fingerprint(items, vacancy_text)

    cached = resume.load_version(conn, resume_id, vacancy_key, print_)
    if cached is not None:
        return cached[0]

    picked = pick_blocks(gateway, items, vacancy_text) if gateway else None
    if picked is None:
        ids = [b.id for b in items]
        resume.save_version(
            conn, resume_id, vacancy_key, ids, print_, "без модели: мастер-резюме"
        )
        return ids

    ids, reason = picked
    resume.save_version(conn, resume_id, vacancy_key, ids, print_, reason)
    return ids


__all__ = (
    "STAGE_SECTION",
    "STAGE_TAILOR",
    "draft_section",
    "numbers",
    "pick_blocks",
    "save_draft",
    "version_for",
)
