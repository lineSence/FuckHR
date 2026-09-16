"""Детектор HR-брехни: детерминированное ядро (шаг 3 пайплайна, ADR-009).

Модуль называется detector.py, а не signal.py, специально: файл signal.py в
корне проекта перекрыл бы стандартный модуль signal, который импортирует
asyncio — прогон падал бы в неожиданном месте и на непонятной строке.

Что здесь есть и чего здесь нет:
- есть извлечение проверяемых утверждений регулярками и сопоставление их с
  историей публикаций из vacancy_snapshots [HRD-001], [HRD-002];
- есть статус «недостаточно данных» как полноценный результат [HRD-004];
- нет ни одного вывода, сделанного моделью. LLM может только предложить цитату
  утверждения, и цитата проверяется на вхождение в текст вакансии [CORE-019].

Вся логика — обычный Python до любого LLM-вызова [LLM-005], [CORE-015].
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import db

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancy_signals (
    key         TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    flags       TEXT NOT NULL,
    payload     TEXT NOT NULL
);
"""

# Порог, ниже которого история слишком коротка, чтобы делать выводы [HRD-004].
MIN_DAYS_TRACKED = 30
# Столько перепубликаций за период считаем поводом усомниться в «стабильности».
REPUBLISH_ALARM = 3
# Вилка шире этого множителя означает, что вилки фактически нет.
WIDE_BAND_RATIO = 2.0

NOT_SUPPORTED = "not_supported"
SUPPORTED = "supported"
NO_DATA = "insufficient_data"

VERDICT_RU = {
    NOT_SUPPORTED: "утверждение не подтверждается найденными данными",
    SUPPORTED: "утверждение согласуется с данными",
    NO_DATA: "недостаточно данных для вывода",
}

# Утверждения, которые вообще можно проверить. Тон вакансии не оценивается
# принципиально: «звучит плохо» — не результат [HRD-001].
CLAIM_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "stable_team",
        "стабильная дружная команда",
        (
            r"дружн\w*\s+(?:\w+\s+)?команд",
            r"стабильн\w*\s+(?:\w+\s+)?команд",
            r"команда\s*[—–-]\s*семья",
            r"без\s+токсичн",
            r"низк\w*\s+текучк",
        ),
    ),
    (
        "no_overtime",
        "работа без переработок",
        (
            r"без\s+переработ",
            r"work[\s-]?life",
            r"баланс\w*\s+(?:работы|между\s+работой)",
            r"не\s+задерживаем",
            r"уходим\s+вовремя",
        ),
    ),
    (
        "white_salary",
        "белая зарплата и официальное оформление",
        (
            r"бел\w*\s+(?:зарплат\w*|зп|оклад\w*)",
            r"официальн\w*\s+оформлен",
            r"оформление\s+по\s+тк",
            r"полностью\s+официальн",
        ),
    ),
    (
        "fast_growth",
        "быстрый карьерный рост",
        (
            r"быстр\w*\s+(?:карьерн\w*\s+)?рост",
            r"рост\s+до\s+(?:тимлид\w*|senior|лида)",
            r"прозрачн\w*\s+карьерн",
        ),
    ),
)

# Формулировки из той же вакансии, которые противоречат «без переработок».
# Это сопоставление текста с текстом, без внешних источников — самый дешёвый
# и самый надёжный вид проверки.
OVERTIME_HINTS: tuple[tuple[str, str], ...] = (
    ("стрессоустойчивость", r"стрессоустойчив"),
    ("многозадачность", r"многозадачн"),
    ("ненормированный график", r"ненормированн"),
    ("готовность к переработкам", r"готовност\w*\s+к\s+переработ"),
    ("режим дедлайнов", r"режим\w*\s+дедлайн"),
    ("горящие глаза", r"горящ\w*\s+глаз"),
)

# Признаки senior-стека: в паре с junior-грейдом дают расхождение [HRD-002].
SENIOR_TOKENS: tuple[str, ...] = (
    "kubernetes",
    "kafka",
    "clickhouse",
    "terraform",
    "микросервис",
    "высоконагруж",
    "архитектур",
    "code review",
    "наставнич",
    "менторств",
)
JUNIOR_EXPERIENCE = ("noExperience", "between1And3")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Таблица выводов живёт рядом с детектором, а не в общей схеме.

    Так модуль можно выключить целиком, ничего не ломая в хранилище.
    """
    conn.executescript(SCHEMA)
    conn.commit()


@dataclass(frozen=True)
class Claim:
    """Проверяемое утверждение с точной цитатой из вакансии."""

    key: str
    label: str
    quote: str


@dataclass(frozen=True)
class Finding:
    """Сопоставление утверждения с данными в форме [HRD-003]."""

    kind: str
    claimed: str
    found: str
    verdict: str
    confidence: str
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class History:
    """Сухая выжимка из слепков — единственный внешний источник на шаге 3."""

    republished: int = 0
    reopen_cycles: int = 0
    days_tracked: int = 0
    snapshots: int = 0
    dated_snapshots: int = 0
    salary_changes: int = 0
    active: bool = True


@dataclass
class Report:
    key: str
    findings: tuple[Finding, ...] = ()
    history: History = field(default_factory=History)

    @property
    def flags(self) -> tuple[str, ...]:
        return tuple(f.kind for f in self.findings if f.verdict == NOT_SUPPORTED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "history": asdict(self.history),
            "findings": [asdict(f) for f in self.findings],
            "flags": list(self.flags),
        }


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Читает поле у hh.Vacancy, sqlite3.Row или обычного словаря."""
    if isinstance(obj, sqlite3.Row):
        value = obj[name] if name in obj.keys() else default
    elif isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def vacancy_text(vacancy: Any) -> str:
    skills = _field(vacancy, "skills", "") or ""
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except (ValueError, TypeError):
            skills = [skills]
    parts = [
        str(_field(vacancy, "title", "") or ""),
        str(_field(vacancy, "description", "") or ""),
        " ".join(str(s) for s in skills) if isinstance(skills, (list, tuple)) else "",
    ]
    return "\n".join(p for p in parts if p)


def _snippet(text: str, start: int, end: int, window: int = 45) -> str:
    left = max(0, start - window)
    right = min(len(text), end + window)
    chunk = " ".join(text[left:right].split())
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{chunk}{suffix}"


def extract_claims(text: str) -> tuple[Claim, ...]:
    """Достаёт утверждения с цитатами. Одно утверждение — один раз."""
    found: list[Claim] = []
    for key, label, patterns in CLAIM_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                found.append(
                    Claim(key=key, label=label, quote=_snippet(text, match.start(), match.end()))
                )
                break
    return tuple(found)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def history(conn: sqlite3.Connection, key: str, months: int = 8) -> History:
    """Собирает историю публикаций по слепкам.

    Главное здесь — days_tracked: без него невозможно отличить «вакансия
    висит четвёртый месяц» от «мы следим за ней два дня».
    """
    rows = conn.execute(
        """
        SELECT seen_at, published_at, salary_from, salary_to, is_active
        FROM vacancy_snapshots
        WHERE key = ?
        ORDER BY seen_at, id
        """,
        (key,),
    ).fetchall()
    if not rows:
        return History(snapshots=0)

    first = _parse_ts(rows[0]["seen_at"])
    last = _parse_ts(rows[-1]["seen_at"])
    days = (last - first).days if first and last else 0

    dated = sum(1 for r in rows if (r["published_at"] or "").strip())
    salary_changes = 0
    cycles = 1
    previous_active: bool | None = None
    previous_salary: tuple[Any, Any] | None = None
    for row in rows:
        current_active = bool(row["is_active"])
        if previous_active is False and current_active is True:
            cycles += 1
        previous_active = current_active
        salary = (row["salary_from"], row["salary_to"])
        if previous_salary is not None and salary != previous_salary:
            salary_changes += 1
        previous_salary = salary

    return History(
        republished=db.republish_count(conn, key, months),
        reopen_cycles=cycles,
        days_tracked=days,
        snapshots=len(rows),
        dated_snapshots=dated,
        salary_changes=salary_changes,
        active=bool(rows[-1]["is_active"]),
    )


def _confidence(hist: History) -> str:
    """Уверенность зависит от длины и качества истории, а не от тона вакансии."""
    if hist.days_tracked >= 60 and hist.dated_snapshots >= 3:
        return "высокая"
    if hist.days_tracked >= MIN_DAYS_TRACKED:
        return "средняя"
    return "низкая"


def _history_source(hist: History) -> str:
    return (
        f"слепки vacancy_snapshots: {hist.snapshots} шт., "
        f"наблюдаем {hist.days_tracked} дн."
    )


def _check_stable_team(claim: Claim, hist: History) -> Finding:
    if hist.days_tracked < MIN_DAYS_TRACKED:
        return Finding(
            kind=claim.key,
            claimed=claim.quote,
            found=f"истории пока мало: {_history_source(hist)}",
            verdict=NO_DATA,
            confidence="низкая",
            sources=("vacancy_snapshots",),
        )
    if hist.republished >= REPUBLISH_ALARM:
        return Finding(
            kind=claim.key,
            claimed=claim.quote,
            found=(
                f"вакансия публиковалась {hist.republished} раз за период; "
                f"возвращалась в выдачу {hist.reopen_cycles} раз"
            ),
            verdict=NOT_SUPPORTED,
            confidence=_confidence(hist),
            sources=("vacancy_snapshots",),
        )
    return Finding(
        kind=claim.key,
        claimed=claim.quote,
        found=f"перепубликаций не видно ({hist.republished}); {_history_source(hist)}",
        verdict=SUPPORTED,
        confidence=_confidence(hist),
        sources=("vacancy_snapshots",),
    )


def _check_no_overtime(claim: Claim, text: str) -> Finding:
    hits = [
        label for label, pattern in OVERTIME_HINTS if re.search(pattern, text, re.IGNORECASE)
    ]
    if hits:
        return Finding(
            kind=claim.key,
            claimed=claim.quote,
            found="в тексте той же вакансии: " + ", ".join(hits),
            verdict=NOT_SUPPORTED,
            confidence="средняя",
            sources=("текст вакансии",),
        )
    return Finding(
        kind=claim.key,
        claimed=claim.quote,
        found="противоречий в тексте нет, внешних данных об этом у нас пока нет",
        verdict=NO_DATA,
        confidence="низкая",
        sources=("текст вакансии",),
    )


def _check_white_salary(claim: Claim, vacancy: Any, hist: History) -> Finding:
    salary_from = _field(vacancy, "salary_from")
    salary_to = _field(vacancy, "salary_to")
    if not salary_from and not salary_to:
        return Finding(
            kind=claim.key,
            claimed=claim.quote,
            found="вилка в вакансии не указана вообще",
            verdict=NOT_SUPPORTED,
            confidence="средняя",
            sources=("поля вакансии",),
        )
    if hist.salary_changes:
        return Finding(
            kind=claim.key,
            claimed=claim.quote,
            found=f"вилка менялась между слепками {hist.salary_changes} раз",
            verdict=NO_DATA,
            confidence=_confidence(hist),
            sources=("vacancy_snapshots",),
        )
    return Finding(
        kind=claim.key,
        claimed=claim.quote,
        found="вилка указана и не менялась",
        verdict=SUPPORTED,
        confidence=_confidence(hist),
        sources=("поля вакансии",),
    )


def _facts(vacancy: Any, text: str, hist: History) -> list[Finding]:
    """Факты без заявлений: то, что видно из полей и истории напрямую."""
    out: list[Finding] = []
    salary_from = _field(vacancy, "salary_from")
    salary_to = _field(vacancy, "salary_to")

    if salary_from and salary_to and salary_to >= salary_from * WIDE_BAND_RATIO:
        out.append(
            Finding(
                kind="salary_band_wide",
                claimed="",
                found=f"вилка {salary_from}–{salary_to}: разброс больше {WIDE_BAND_RATIO:g}×",
                verdict=NOT_SUPPORTED,
                confidence="высокая",
                sources=("поля вакансии",),
            )
        )

    experience = _field(vacancy, "experience", "")
    senior_hits = [t for t in SENIOR_TOKENS if t in text.lower()]
    if experience in JUNIOR_EXPERIENCE and len(senior_hits) >= 3:
        out.append(
            Finding(
                kind="grade_mismatch",
                claimed="",
                found=f"грейд «{experience}» при требованиях: " + ", ".join(senior_hits[:5]),
                verdict=NOT_SUPPORTED,
                confidence="средняя",
                sources=("поля вакансии",),
            )
        )

    if hist.active and hist.days_tracked >= 90:
        out.append(
            Finding(
                kind="long_running",
                claimed="",
                found=f"вакансия висит в выдаче {hist.days_tracked} дней подряд",
                verdict=NOT_SUPPORTED,
                confidence=_confidence(hist),
                sources=("vacancy_snapshots",),
            )
        )
    return out


def assess(vacancy: Any, hist: History | None = None) -> Report:
    """Сопоставляет утверждения вакансии с историей и полями.

    Пустой отчёт — нормальный результат: значит, проверяемых утверждений в
    вакансии нет, а придумывать их мы не станем [HRD-004].
    """
    hist = hist or History()
    text = vacancy_text(vacancy)
    findings: list[Finding] = []
    for claim in extract_claims(text):
        if claim.key == "stable_team":
            findings.append(_check_stable_team(claim, hist))
        elif claim.key == "no_overtime":
            findings.append(_check_no_overtime(claim, text))
        elif claim.key == "white_salary":
            findings.append(_check_white_salary(claim, vacancy, hist))
        else:
            findings.append(
                Finding(
                    kind=claim.key,
                    claimed=claim.quote,
                    found="проверяемых данных по этому утверждению у нас нет",
                    verdict=NO_DATA,
                    confidence="низкая",
                )
            )
    findings.extend(_facts(vacancy, text, hist))
    return Report(
        key=str(_field(vacancy, "key", "")), findings=tuple(findings), history=hist
    )


def format_report(report: Report) -> str:
    """Форма вывода по [HRD-003]: заявлено — найдено — вывод с уверенностью."""
    if not report.findings:
        return "Проверяемых утверждений не найдено."
    blocks: list[str] = []
    for f in report.findings:
        head = f"Заявлено: «{f.claimed}»" if f.claimed else f"Факт: {f.kind}"
        blocks.append(
            "\n".join(
                [
                    head,
                    f"Найдено: {f.found}",
                    f"Вывод: {VERDICT_RU[f.verdict]} (уверенность: {f.confidence})",
                ]
            )
        )
    return "\n\n".join(blocks)


def telegram_lines(payload: Report | dict[str, Any], limit: int = 3) -> list[str]:
    """Короткие строки для карточки: только неподтверждённые утверждения.

    «Недостаточно данных» в Telegram не идёт — иначе карточка превратится в
    отчёт о нашей неготовности вместо информации о вакансии.
    """
    data = payload.to_dict() if isinstance(payload, Report) else payload
    lines: list[str] = []
    for f in data.get("findings", []):
        if f.get("verdict") != NOT_SUPPORTED:
            continue
        claimed = (f.get("claimed") or "").strip()
        prefix = f"«{claimed}» — " if claimed else ""
        lines.append(f"⚠\ufe0f {prefix}{f.get('found', '')} ({f.get('confidence', '')})")
        if len(lines) >= limit:
            break
    return lines


def store(conn: sqlite3.Connection, report: Report) -> None:
    conn.execute(
        """
        INSERT INTO vacancy_signals (key, created_at, flags, payload)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            created_at = excluded.created_at,
            flags = excluded.flags,
            payload = excluded.payload
        """,
        (
            report.key,
            db.utcnow(),
            ",".join(report.flags),
            json.dumps(report.to_dict(), ensure_ascii=False),
        ),
    )
    conn.commit()


def load(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM vacancy_signals WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except ValueError:
        log.warning("битый отчёт детектора для %s", key)
        return None


def load_lines(conn: sqlite3.Connection, key: str, limit: int = 3) -> list[str]:
    payload = load(conn, key)
    return telegram_lines(payload, limit) if payload else []


def coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(отчётов с хотя бы одним флагом, всего отчётов)."""
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN flags <> '' THEN 1 ELSE 0 END) AS flagged,
            COUNT(*) AS total
        FROM vacancy_signals
        """
    ).fetchone()
    return int(row["flagged"] or 0), int(row["total"] or 0)
