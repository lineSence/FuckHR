"""Резюме владельца: хранение, экспорт и проверка противоречий (B-01).

Зачем отдельная сущность. До неё единственным самоописанием был блок facts в
`profile.yaml` — плоский список строк. Из него нельзя ни собрать версию под
вакансию, ни понять, сколько у человека опыта. `profile.yaml` остаётся тем, чем
был — критериями поиска; здесь живёт опыт.

Три решения, которые определяют весь модуль.

1. Резюме — не текст, а набор блоков. Версия под вакансию — это подмножество
   и порядок блоков, а не новый текст. Поэтому в resume_versions лежат номера
   блоков, а не копия резюме: правка блока меняет все версии сразу, и
   устаревших копий с ошибкой в дате не остаётся.
2. Всё, что предложила модель, помечено source='ai' и confirmed=0. Наружу — в
   экспорт и в письма — уходят только подтверждённые блоки. Выдуманный опыт в
   письме работодателю — не косметический дефект, а ложь [CORE-019].
3. Модели здесь нет вообще [CORE-015]. Сборка формулировок и отбор блоков живут в
   `resume_llm.py`, и без них резюме полностью работоспособно [CORE-017].

Про profile_id. Сейчас профиль один и везде равен 'default'. Колонка заведена
заранее ради B-08: добавить строки с другим профилем дешевле, чем потом
мигрировать таблицу с данными.
Словарь грейдов живёт в `resume_grades.py` и реэкспортируется отсюда [CORE-024].
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from resume_grades import GRADE_WORDS, grade_claim  # noqa: F401

log = logging.getLogger(__name__)

DEFAULT_PROFILE = "default"

SOURCE_OWNER = "owner"
SOURCE_AI = "ai"

# Порядок секций здесь — это порядок в экспорте и в интервью.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("contacts", "Контакты"),
    ("summary", "Кратко о себе"),
    ("experience", "Опыт работы"),
    ("skills", "Навыки"),
    ("projects", "Проекты"),
    ("education", "Образование"),
)

SECTION_KEYS: tuple[str, ...] = tuple(key for key, _ in SECTIONS)
SECTION_TITLES: dict[str, str] = dict(SECTIONS)
SECTION_ORDER: dict[str, int] = {key: i for i, key in enumerate(SECTION_KEYS)}

# Вопросы интервью: по одному на секцию. Спрашиваем про факты и цифры, а не
# «расскажите о себе»: из общих слов потом нечего ставить в письмо.
QUESTIONS: dict[str, str] = {
    "contacts": (
        "Почта, телеграм, город и формат работы. Одной строкой."
    ),
    "summary": (
        "Кто ты по роли и сколько лет в профессии? Что ты закрываешь без разгона?"
    ),
    "experience": (
        "Последнее место работы: компания, должность, сроки, за что отвечал. "
        "Что изменилось от твоей работы — желательно в цифрах."
    ),
    "skills": (
        "С чем работал руками в последние два года? Только то, что готов "
        "показать на собеседовании."
    ),
    "projects": (
        "Проект или задача, которой не стыдно похвастаться: задача, твоя роль, "
        "результат, ссылка если есть."
    ),
    "education": "Где учился и чему, курсы и сертификаты. Можно коротко.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id  TEXT NOT NULL DEFAULT 'default',
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (profile_id)
);

CREATE TABLE IF NOT EXISTS resume_blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id   INTEGER NOT NULL,
    section     TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    heading     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    started     TEXT NOT NULL DEFAULT '',
    finished    TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'owner',
    confirmed   INTEGER NOT NULL DEFAULT 1,
    answer      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id    INTEGER NOT NULL,
    vacancy_key  TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    block_ids    TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE (resume_id, vacancy_key)
);

CREATE INDEX IF NOT EXISTS idx_resume_blocks_section
    ON resume_blocks (resume_id, section, position);
CREATE INDEX IF NOT EXISTS idx_resume_versions_print
    ON resume_versions (resume_id, fingerprint);
"""


@dataclass(frozen=True)
class Block:
    """Один блок резюме. Текст всегда принадлежит владельцу или ждёт его ответа."""

    id: int
    section: str
    position: int
    heading: str
    body: str
    started: str = ""
    finished: str = ""
    source: str = SOURCE_OWNER
    confirmed: bool = True
    answer: str = ""

    @property
    def by_ai(self) -> bool:
        return self.source == SOURCE_AI

    @property
    def pending(self) -> bool:
        """Предложено моделью и ещё не подтверждено владельцем."""
        return not self.confirmed

    @property
    def label(self) -> str:
        return self.heading or SECTION_TITLES.get(self.section, self.section)

    @property
    def period(self) -> str:
        if not self.started and not self.finished:
            return ""
        return "{} — {}".format(self.started or "?", self.finished or "настоящее время")

    @property
    def text(self) -> str:
        head = " · ".join(part for part in (self.heading, self.period) if part)
        return "{}\n{}".format(head, self.body).strip() if head else self.body


def utcnow() -> str:
    import db

    return db.utcnow()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_or_create(
    conn: sqlite3.Connection, profile_id: str = DEFAULT_PROFILE, title: str = ""
) -> int:
    """Идентификатор резюме для профиля. Создаёт пустое при первом обращении."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT id FROM resumes WHERE profile_id = ?", (profile_id,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO resumes (profile_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (profile_id, title, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _touch(conn: sqlite3.Connection, resume_id: int) -> None:
    conn.execute(
        "UPDATE resumes SET updated_at = ? WHERE id = ?", (utcnow(), resume_id)
    )


def _row_to_block(row: sqlite3.Row) -> Block:
    return Block(
        id=int(row["id"]),
        section=str(row["section"]),
        position=int(row["position"]),
        heading=str(row["heading"] or ""),
        body=str(row["body"] or ""),
        started=str(row["started"] or ""),
        finished=str(row["finished"] or ""),
        source=str(row["source"] or SOURCE_OWNER),
        confirmed=bool(row["confirmed"]),
        answer=str(row["answer"] or ""),
    )


def blocks(
    conn: sqlite3.Connection,
    resume_id: int,
    section: str | None = None,
    confirmed_only: bool = False,
) -> list[Block]:
    """Блоки в порядке секций и позиций.

    Порядок секций считается в Python, а не в SQL: в SQLite нет порядка по
    произвольному списку без громоздкого CASE, а блоков здесь десятки.
    """
    sql = "SELECT * FROM resume_blocks WHERE resume_id = ?"
    params: list[Any] = [resume_id]
    if section:
        sql += " AND section = ?"
        params.append(section)
    if confirmed_only:
        sql += " AND confirmed = 1"
    rows = conn.execute(sql, params).fetchall()
    items = [_row_to_block(row) for row in rows]
    items.sort(key=lambda b: (SECTION_ORDER.get(b.section, 99), b.position, b.id))
    return items


def add_block(
    conn: sqlite3.Connection,
    resume_id: int,
    section: str,
    body: str,
    heading: str = "",
    started: str = "",
    finished: str = "",
    source: str = SOURCE_OWNER,
    confirmed: bool | None = None,
    answer: str = "",
) -> int:
    """Добавляет блок в конец секции.

    Заметьте умолчание confirmed: текст от модели всегда приходит
    неподтверждённым, даже если вызывающая сторона об этом забыла.
    """
    if section not in SECTION_ORDER:
        raise ValueError("неизвестная секция резюме: {!r}".format(section))
    if confirmed is None:
        confirmed = source != SOURCE_AI
    if source == SOURCE_AI:
        confirmed = False
    now = utcnow()
    row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM resume_blocks "
        "WHERE resume_id = ? AND section = ?",
        (resume_id, section),
    ).fetchone()
    position = int(row[0]) + 1
    cur = conn.execute(
        """
        INSERT INTO resume_blocks (
            resume_id, section, position, heading, body, started, finished,
            source, confirmed, answer, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resume_id,
            section,
            position,
            heading.strip(),
            body.strip(),
            started.strip(),
            finished.strip(),
            source,
            int(bool(confirmed)),
            answer.strip(),
            now,
            now,
        ),
    )
    _touch(conn, resume_id)
    conn.commit()
    return int(cur.lastrowid)


def update_block(conn: sqlite3.Connection, block_id: int, **fields: Any) -> None:
    """Правка блока владельцем.

    Любая ручная правка текста делает блок собственностью владельца: если человек
    переписал предложенное моделью, пометка «предложено ИИ» больше не верна.
    """
    allowed = {
        "section",
        "position",
        "heading",
        "body",
        "started",
        "finished",
        "source",
        "confirmed",
        "answer",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return
    if "confirmed" in payload:
        payload["confirmed"] = int(bool(payload["confirmed"]))
    if payload.get("body") is not None and "source" not in payload:
        payload["source"] = SOURCE_OWNER
    sets = ", ".join("{} = ?".format(key) for key in payload)
    values = list(payload.values()) + [utcnow(), block_id]
    conn.execute(
        "UPDATE resume_blocks SET {}, updated_at = ? WHERE id = ?".format(sets), values
    )
    conn.commit()


def confirm_block(conn: sqlite3.Connection, block_id: int) -> None:
    """Владелец подтверждает текст как свой. С этой минуты он уходит наружу."""
    update_block(conn, block_id, confirmed=True, source=SOURCE_OWNER)


def delete_block(conn: sqlite3.Connection, block_id: int) -> None:
    conn.execute("DELETE FROM resume_blocks WHERE id = ?", (block_id,))
    conn.commit()


def reorder(conn: sqlite3.Connection, block_ids: Sequence[int]) -> None:
    """Новые позиции в порядке переданного списка."""
    conn.executemany(
        "UPDATE resume_blocks SET position = ? WHERE id = ?",
        [(index, block_id) for index, block_id in enumerate(block_ids)],
    )
    conn.commit()


def stats(conn: sqlite3.Connection, resume_id: int) -> dict[str, int]:
    items = blocks(conn, resume_id)
    return {
        "blocks": len(items),
        "confirmed": sum(1 for b in items if b.confirmed),
        "pending": sum(1 for b in items if b.pending),
        "sections": len({b.section for b in items if b.confirmed}),
    }


def empty_sections(conn: sqlite3.Connection, resume_id: int) -> list[str]:
    """Секции без ни одного подтверждённого блока."""
    filled = {b.section for b in blocks(conn, resume_id, confirmed_only=True)}
    return [key for key in SECTION_KEYS if key not in filled]


def to_markdown(items: Sequence[Block], title: str = "") -> str:
    """Экспорт в Markdown. Только подтверждённое: фильтрация здесь, а не выше.

    Проверка повторяется нарочно: это последний рубеж перед выходом текста на
    глаза работодателю, и полагаться на аккуратность вызывающего здесь нельзя.
    """
    lines: list[str] = []
    if title:
        lines.append("# {}".format(title))
        lines.append("")
    for key in SECTION_KEYS:
        chunk = [b for b in items if b.section == key and b.confirmed]
        if not chunk:
            continue
        lines.append("## {}".format(SECTION_TITLES[key]))
        lines.append("")
        for block in chunk:
            head = block.heading.strip()
            period = block.period
            if head and period:
                lines.append("**{}** — {}".format(head, period))
                lines.append("")
            elif head:
                lines.append("**{}**".format(head))
                lines.append("")
            body = block.body.strip()
            if body:
                lines.append(body)
                lines.append("")
    return "\n".join(lines).strip() + "\n"


def export(
    conn: sqlite3.Connection, resume_id: int, title: str = ""
) -> str:
    return to_markdown(blocks(conn, resume_id), title)


_NUMBER = re.compile(r"\d")


def facts(
    conn: sqlite3.Connection, profile_id: str = DEFAULT_PROFILE, limit: int = 6
) -> tuple[str, ...]:
    """Факты для письма: короткие строки из подтверждённых блоков.

    Сначала идут строки с цифрами: «сократил сборку с 40 до 6 минут» работает в
    письме, «ответственный и обучаемый» — нет.
    """
    ensure_schema(conn)
    row = conn.execute(
        "SELECT id FROM resumes WHERE profile_id = ?", (profile_id,)
    ).fetchone()
    if row is None:
        return ()
    wanted = ("experience", "projects", "summary", "skills")
    items = [
        b
        for b in blocks(conn, int(row[0]), confirmed_only=True)
        if b.section in wanted and b.body.strip()
    ]
    lines: list[str] = []
    for block in items:
        for raw in block.body.splitlines():
            line = raw.strip(" —-•\t")
            if len(line) > 12:
                lines.append(line)
    lines.sort(key=lambda text: (0 if _NUMBER.search(text) else 1,))
    out: list[str] = []
    for line in lines:
        if line not in out:
            out.append(line)
        if len(out) >= limit:
            break
    return tuple(out)


_PERIOD = re.compile(r"(\d{4})(?:[-./](\d{1,2}))?")


def _months(value: str) -> int | None:
    """«2021-03» или «2021» → номер месяца от начала летосчисления."""
    match = _PERIOD.search(value or "")
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    if not 1 <= month <= 12:
        month = 1
    return year * 12 + (month - 1)


def experience_months(items: Sequence[Block], today: date | None = None) -> int:
    """Стаж в месяцах по блокам опыта.

    Месяцы складываются через множество, а не суммой периодов: при двух
    параллельных местах работы сумма дала бы вдвое больший стаж, а это ровно
    тот самый обман, который мы собираемся ловить.
    """
    today = today or date.today()
    now = today.year * 12 + (today.month - 1)
    covered: set[int] = set()
    for block in items:
        if block.section != "experience":
            continue
        start = _months(block.started)
        if start is None:
            continue
        end = _months(block.finished)
        if end is None:
            end = now if not block.finished.strip() else start
        if end < start:
            start, end = end, start
        covered.update(range(start, min(end, now) + 1))
    return len(covered)


def _profile_data(profile_path: str | Any) -> Mapping[str, Any]:
    from pathlib import Path

    import yaml

    path = Path(profile_path)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def contradictions(
    conn: sqlite3.Connection,
    resume_id: int,
    profile_path: str = "profile.yaml",
    today: date | None = None,
) -> list[str]:
    """Расхождения между опытом и притязаниями профиля поиска.

    Возвращает предупреждения, а не запреты: решение за владельцем. Блокировка
    сохранения здесь была бы вредной: переход в лиды «через голову» бывает
    осознанным решением, а не ошибкой ввода.
    """
    items = blocks(conn, resume_id, confirmed_only=True)
    data = _profile_data(profile_path)
    notes: list[str] = []

    months = experience_months(items, today)
    years = months / 12.0

    queries = data.get("queries") or []
    texts: list[str] = []
    for item in queries:
        if isinstance(item, Mapping):
            texts.append(str(item.get("text") or ""))
        elif item is not None:
            texts.append(str(item))
    haystack = " ".join(texts).lower()

    if months:
        need, word = grade_claim(haystack)
        if need and months + 6 < need:
            notes.append(
                "В запросах есть «{}», а по резюме опыта около {:.1f} лет. "
                "Такие вакансии обычно ждут от {} лет.".format(
                    word, years, need // 12
                )
            )
    elif any(b.section == "experience" for b in items):
        notes.append(
            "В блоках опыта не заполнены сроки — стаж посчитать невозможно, "
            "и проверка притязаний не работает."
        )

    resume_text = " ".join(b.text for b in items).lower()
    skills = data.get("skills") or []
    missing = [
        str(skill).strip()
        for skill in skills
        if str(skill or "").strip() and str(skill).strip().lower() not in resume_text
    ]
    if missing:
        notes.append(
            "В профиле ищем по навыкам, которых нет в резюме: {}. "
            "Либо добавь их в опыт, либо убери из поиска.".format(
                ", ".join(missing[:6])
            )
        )

    empty = empty_sections(conn, resume_id)
    if empty:
        notes.append(
            "Пустые секции: {}.".format(
                ", ".join(SECTION_TITLES[key] for key in empty)
            )
        )

    pending = [b for b in blocks(conn, resume_id) if b.pending]
    if pending:
        notes.append(
            "Не подтверждено предложений модели: {}. В экспорт и письма "
            "они не попадут.".format(len(pending))
        )
    return notes


def fingerprint(items: Sequence[Block], requirements: str) -> str:
    """Ключ кэша версии: состав резюме плюс требования вакансии.

    Без него автосборка версий будет тратить вызов модели на каждую повторно
    увиденную вакансию, а их бюджет ограничен [CORE-016].
    """
    payload = json.dumps(
        {
            "blocks": [
                [b.id, b.heading, b.body, int(b.confirmed)]
                for b in items
                if b.confirmed
            ],
            "requirements": " ".join((requirements or "").lower().split()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_version(
    conn: sqlite3.Connection,
    resume_id: int,
    vacancy_key: str,
    block_ids: Iterable[int],
    print_: str,
    note: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO resume_versions (
            resume_id, vacancy_key, fingerprint, block_ids, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (resume_id, vacancy_key) DO UPDATE SET
            fingerprint = excluded.fingerprint,
            block_ids = excluded.block_ids,
            note = excluded.note,
            created_at = excluded.created_at
        """,
        (
            resume_id,
            vacancy_key,
            print_,
            json.dumps(list(block_ids)),
            note,
            utcnow(),
        ),
    )
    conn.commit()


def load_version(
    conn: sqlite3.Connection, resume_id: int, vacancy_key: str, print_: str | None = None
) -> tuple[list[int], str] | None:
    """Сохранённая версия. При несовпадении отпечатка считается устаревшей."""
    row = conn.execute(
        "SELECT block_ids, note, fingerprint FROM resume_versions "
        "WHERE resume_id = ? AND vacancy_key = ?",
        (resume_id, vacancy_key),
    ).fetchone()
    if row is None:
        return None
    if print_ is not None and str(row["fingerprint"]) != print_:
        return None
    try:
        ids = [int(value) for value in json.loads(row["block_ids"])]
    except (TypeError, ValueError):
        return None
    return ids, str(row["note"] or "")


def version_markdown(
    conn: sqlite3.Connection, resume_id: int, block_ids: Sequence[int], title: str = ""
) -> str:
    """Версия под вакансию: те же блоки, только отобранные и в заданном порядке.

    Текст блоков не меняется никогда и никем: версия — это отбор, а не пересказ.
    """
    by_id = {b.id: b for b in blocks(conn, resume_id, confirmed_only=True)}
    chosen = [by_id[i] for i in block_ids if i in by_id]
    if not chosen:
        return ""
    order = {block.id: index for index, block in enumerate(chosen)}
    ordered = sorted(
        chosen, key=lambda b: (SECTION_ORDER.get(b.section, 99), order[b.id])
    )
    return to_markdown(ordered, title)


__all__ = (
    "Block",
    "DEFAULT_PROFILE",
    "GRADE_WORDS",
    "QUESTIONS",
    "SECTIONS",
    "SECTION_KEYS",
    "SECTION_TITLES",
    "SOURCE_AI",
    "SOURCE_OWNER",
    "add_block",
    "blocks",
    "confirm_block",
    "contradictions",
    "delete_block",
    "empty_sections",
    "ensure_schema",
    "experience_months",
    "export",
    "facts",
    "fingerprint",
    "get_or_create",
    "grade_claim",
    "load_version",
    "reorder",
    "save_version",
    "stats",
    "to_markdown",
    "update_block",
    "version_markdown",
)
