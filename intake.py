"""Разговор о поиске: свободный текст владельца → критерии поиска и резюме.

Зачем. До этого «Профиль» и «Резюме» были двумя формами на тридцать полей, и
заполнять их надо было, уже зная, чего хочешь. Человек же приходит со словами
«ищу backend на Python, восемь лет, удалёнка, от 250 на руки» — из этого
критерии и блоки резюме выводятся, а непонятное дешевле спросить, чем угадать.

Что здесь есть и чего нет. Здесь диалог, разбор ответа модели и запись в базу.
Модель ничего не применяет сама: она возвращает предложение, владелец отмечает
галочками и жмёт кнопку [CORE-019]. Решения о том, как выглядит страница, живут
в `ui_intake.py`.

Главная защита — числа. Всё, что модель предлагает в факты и блоки резюме,
обязано состоять из чисел, которые владелец назвал сам: выдуманный стаж в
резюме и в письме работодателю — это ложь, а не косметика.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import profile_schema
import resume

log = logging.getLogger("intake")

STAGE = "intake"
MAX_QUESTIONS = 3
MAX_TEXT = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

PROMPT = (
    "Ты помогаешь человеку настроить поиск работы и собрать резюме.\n"
    "Тебе дают его слова о себе и о том, что он ищет, и текущие настройки.\n"
    "Правила:\n"
    "- не выдумывай опыт, места работы, цифры и сроки: бери только то, что он "
    "сказал сам;\n"
    "- чего не хватает — спрашивай, до трёх коротких вопросов за раз;\n"
    "- запросы для поиска пиши так, как их пишут в строке поиска вакансий;\n"
    "- пустые поля не заполняй наугад, лучше спроси.\n"
    'JSON: {"questions": ["..."], "summary": "одна строка",'
    ' "profile": {"queries": ["..."], "skills": ["..."], "nice_to_have": ["..."],'
    ' "stop_words": ["..."], "salary_min_net": 0, "remote_ok": true,'
    ' "experience_ok": ["between3And6"], "min_score": 45},'
    ' "facts": ["..."],'
    ' "resume": [{"section": "experience", "heading": "...", "body": "..."}]}'
)

# Поля профиля, которые разрешено предлагать. Всё остальное из ответа модели
# молча выбрасывается: список закрытый, чтобы в profile.yaml не появлялись ключи,
# которых не понимает ни схема, ни скоринг.
PROFILE_KEYS = (
    "queries",
    "skills",
    "nice_to_have",
    "stop_words",
    "salary_min_net",
    "remote_ok",
    "experience_ok",
    "min_score",
)

NUMBER_RE = re.compile(r"\d+")


@dataclass
class Plan:
    """Предложение модели после одной реплики владельца."""

    questions: tuple[str, ...] = ()
    summary: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    facts: tuple[str, ...] = ()
    blocks: tuple[dict[str, str], ...] = ()
    dropped: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.profile or self.facts or self.blocks)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def log_message(conn: sqlite3.Connection, role: str, text: str) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO intake_log (role, text) VALUES (?, ?)",
        (role, (text or "").strip()[:MAX_TEXT]),
    )
    conn.commit()


def history(conn: sqlite3.Connection, limit: int = 20) -> list[tuple[str, str]]:
    """Диалог от старых к новым: (роль, текст). Служебные записи не попадают."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT role, text FROM intake_log WHERE role IN ('owner', 'ai') "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in reversed(rows)]


def save_plan(conn: sqlite3.Connection, plan: "Plan") -> None:
    """Последнее предложение живёт в том же журнале под ролью plan.

    Иначе вопросы модели исчезали при обновлении страницы, и отвечать было
    некуда: поля ответа должны быть на месте и после F5.
    """
    log_message(conn, "plan", json.dumps(_plan_dict(plan), ensure_ascii=False))


def last_plan(conn: sqlite3.Connection) -> "Plan | None":
    ensure_schema(conn)
    row = conn.execute(
        "SELECT text FROM intake_log WHERE role = 'plan' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(str(row[0]))
    except ValueError:
        return None
    return Plan(
        questions=tuple(data.get("questions") or ()),
        summary=str(data.get("summary") or ""),
        profile=dict(data.get("profile") or {}),
        facts=tuple(data.get("facts") or ()),
        blocks=tuple(data.get("resume") or ()),
        dropped=tuple(data.get("dropped") or ()),
    )


def _plan_dict(plan: "Plan") -> dict[str, Any]:
    return {
        "questions": list(plan.questions),
        "summary": plan.summary,
        "profile": plan.profile,
        "facts": list(plan.facts),
        "resume": list(plan.blocks),
        "dropped": list(plan.dropped),
    }


def owner_words(conn: sqlite3.Connection) -> str:
    """Всё, что владелец написал сам. Источник правды для проверки чисел."""
    return "\n".join(text for role, text in history(conn, 100) if role == "owner")


def clear(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM intake_log")
    conn.commit()


def _numbers(text: str) -> set[str]:
    glued = re.sub(r"(?<=\d)[\s\u00a0](?=\d)", "", text or "")
    return set(NUMBER_RE.findall(glued))


def _strings(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:120])
    return out[:limit]


def _plan_profile(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Только известные поля и только правдоподобные значения."""
    out: dict[str, Any] = {}
    for key in ("queries", "skills", "nice_to_have", "stop_words"):
        items = _strings(raw.get(key))
        if items:
            out[key] = items

    codes = [
        code
        for code in _strings(raw.get("experience_ok"))
        if code in profile_schema.EXPERIENCE_IDS
    ]
    if codes:
        out["experience_ok"] = codes

    salary = raw.get("salary_min_net")
    if isinstance(salary, (int, float)) and 10_000 <= float(salary) <= 2_000_000:
        out["salary_min_net"] = int(salary)

    if isinstance(raw.get("remote_ok"), bool):
        out["remote_ok"] = raw["remote_ok"]

    score = raw.get("min_score")
    if isinstance(score, (int, float)) and 0 <= float(score) <= 100:
        out["min_score"] = float(score)
    return out


def parse(raw: str, said: str) -> Plan:
    """Ответ модели → предложение. Всё сомнительное отбрасывается здесь.

    said — слова самого владельца: числа в фактах и блоках резюме сверяются с
    ними, иначе модель округляет «почти восемь лет» до десяти.
    """
    try:
        data = json.loads(_json_blob(raw))
    except ValueError:
        log.warning("intake: ответ не разобрался как JSON")
        return Plan()
    if not isinstance(data, dict):
        return Plan()

    known = _numbers(said)
    dropped: list[str] = []

    facts = []
    for text in _strings(data.get("facts"), limit=10):
        invented = sorted(_numbers(text) - known)
        if invented:
            dropped.append("факт с чужими числами: {}".format(text[:60]))
            continue
        facts.append(text)

    blocks = []
    for item in data.get("resume") or []:
        if not isinstance(item, dict) or len(blocks) >= 8:
            continue
        section = str(item.get("section") or "").strip()
        body = str(item.get("body") or "").strip()
        if section not in resume.SECTION_KEYS or len(body) < 10:
            continue
        invented = sorted(_numbers(body) - known)
        if invented:
            dropped.append(
                "блок «{}» с числами, которых ты не называл: {}".format(
                    section, ", ".join(invented)
                )
            )
            continue
        blocks.append(
            {
                "section": section,
                "heading": str(item.get("heading") or "").strip()[:120],
                "body": body[:1200],
            }
        )

    return Plan(
        questions=tuple(_strings(data.get("questions"), limit=MAX_QUESTIONS)),
        summary=str(data.get("summary") or "").strip()[:300],
        profile=_plan_profile(data.get("profile") or {}),
        facts=tuple(facts),
        blocks=tuple(blocks),
        dropped=tuple(dropped),
    )


def _json_blob(raw: str) -> str:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 and end > start else text


def ask(
    gateway: Any,
    said: str,
    current: Mapping[str, Any],
    context: str = "",
) -> Plan:
    """Один вызов модели: слова владельца плюс текущие настройки.

    context — то же самое, но с вопросами модели рядом с ответами: так понятнее
    ей. Числа при этом сверяются только с said, то есть со словами владельца:
    иначе модель могла бы назвать число в своём же вопросе и потом сослаться на
    него как на факт о человеке.
    """
    if gateway is None:
        return Plan()
    state = json.dumps(
        {
            "queries": [
                q.get("text") if isinstance(q, dict) else q
                for q in (current.get("queries") or [])
            ],
            "skills": current.get("skills") or [],
            "salary_min_net": (current.get("salary") or {}).get("min_net"),
            "remote_ok": (current.get("geo") or {}).get("remote_ok"),
            "experience_ok": current.get("experience_ok") or [],
            "facts": current.get("facts") or [],
        },
        ensure_ascii=False,
    )
    raw = gateway.complete(
        STAGE,
        [
            {"role": "system", "content": PROMPT},
            {"role": "system", "content": "Сейчас в настройках: " + state},
            {"role": "user", "content": (context or said)[:MAX_TEXT]},
        ],
    )
    return parse(raw or "", said)


def apply_profile(data: dict[str, Any], patch: Mapping[str, Any]) -> list[str]:
    """Вносит предложение в данные профиля. Возвращает список изменений.

    Списки дополняются, а не заменяются: владелец мог добавить навык руками, и
    разговор о зарплате не повод его стереть.
    """
    changed: list[str] = []

    for key in ("skills", "nice_to_have", "stop_words"):
        new = [item for item in patch.get(key, []) if item not in (data.get(key) or [])]
        if new:
            data[key] = list(data.get(key) or []) + new
            changed.append("{}: +{}".format(key, len(new)))

    queries = list(data.get("queries") or [])
    texts = {
        (q.get("text") if isinstance(q, dict) else str(q)) for q in queries
    }
    added = 0
    for text in patch.get("queries", []):
        if text in texts:
            continue
        queries.append({"text": text, "area": 113, "period": 7, "max_pages": 0})
        added += 1
    if added:
        data["queries"] = queries
        changed.append("запросы: +{}".format(added))

    if "salary_min_net" in patch:
        salary = dict(data.get("salary") or {})
        salary["min_net"] = patch["salary_min_net"]
        data["salary"] = salary
        changed.append("минимум на руки: {}".format(patch["salary_min_net"]))

    if "remote_ok" in patch:
        geo = dict(data.get("geo") or {})
        geo["remote_ok"] = patch["remote_ok"]
        data["geo"] = geo
        changed.append("удалёнка: {}".format("да" if patch["remote_ok"] else "нет"))

    if "experience_ok" in patch:
        data["experience_ok"] = list(patch["experience_ok"])
        changed.append("опыт: {}".format(", ".join(patch["experience_ok"])))

    if "min_score" in patch:
        data["min_score"] = patch["min_score"]
        changed.append("порог: {:.0f}".format(patch["min_score"]))

    return changed


def apply_facts(data: dict[str, Any], facts: Sequence[str]) -> int:
    """Добавляет факты о себе, не трогая уже написанные."""
    current = list(data.get("facts") or [])
    new = [f for f in facts if f not in current]
    if new:
        data["facts"] = current + new
    return len(new)


def apply_blocks(
    conn: sqlite3.Connection, blocks: Sequence[Mapping[str, str]]
) -> int:
    """Кладёт блоки в резюме как предложения модели: source=ai, не подтверждены."""
    if not blocks:
        return 0
    resume_id = resume.get_or_create(conn)
    for block in blocks:
        resume.add_block(
            conn,
            resume_id,
            block["section"],
            block["body"],
            heading=block.get("heading", ""),
            source=resume.SOURCE_AI,
        )
    return len(blocks)


__all__ = (
    "MAX_QUESTIONS",
    "PROFILE_KEYS",
    "Plan",
    "apply_blocks",
    "apply_facts",
    "apply_profile",
    "ask",
    "clear",
    "ensure_schema",
    "history",
    "last_plan",
    "log_message",
    "owner_words",
    "parse",
    "save_plan",
)
