"""Contact discovery: кому писать и куда, минуя HR-воронку.

Граница модуля: он находит человека и рабочий канал, ведёт лог и дедуп.
Текст письма собирает outreach.py, отправляет владелец руками (ADR-012,
[OUT-006]). Здесь нет ни SMTP, ни очереди отправки — и не должно появиться.

Всё, что можно сделать регуляркой и таблицей, сделано регуляркой и таблицей
[CORE-015]. Модель и внешний поиск — в отдельных модулях и только для того,
чего в данных нет.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    company TEXT,
    person TEXT,
    role TEXT,
    role_rank INTEGER NOT NULL DEFAULT 99,
    channel_kind TEXT NOT NULL,
    channel_value TEXT NOT NULL,
    source_url TEXT,
    confidence TEXT NOT NULL DEFAULT 'low',
    guessed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'drafted',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_key ON contacts(key);
CREATE INDEX IF NOT EXISTS idx_contacts_person ON contacts(person, company);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
"""

# Статусы из [OUT-007]. sent_manually ставит только владелец:
# система факта отправки не видит.
DRAFTED = "drafted"
SENT = "sent_manually"
REPLIED = "replied"
BLOCKED = "blocked"
SKIPPED = "skipped"
STATUSES = (DRAFTED, SENT, REPLIED, BLOCKED, SKIPPED)

REPEAT_AFTER_DAYS = 90  # [OUT-007]: тот же человек — не раньше трёх месяцев

# Приоритет ролей из [OUT-001]: меньше rank — ближе к нанимающему менеджеру.
ROLE_RANKS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "руководитель направления", (
        "руководитель разработки", "руководитель отдела", "руководитель направления",
        "head of engineering", "head of development", "head of backend", "engineering manager",
    )),
    (2, "тимлид", ("тимлид", "тим-лид", "team lead", "teamlead", "tech lead", "ведущий разработчик")),
    (3, "технический директор", ("cto", "технический директор", "vp of engineering")),
    (4, "коллега на той же роли", (
        "python", "backend", "бекенд", "разработчик", "developer", "engineer",
    )),
    (8, "рекрутер", ("рекрутер", "recruiter", "hrbp", "hr-менеджер", "talent")),
)

# Адреса, по которым письмо упадёт ровно в ту воронку, которую обходим.
HR_MAILBOXES = (
    "hr", "job", "jobs", "vacancy", "vacancies", "career", "careers", "resume",
    "cv", "recruit", "recruiting", "rabota", "personal",
)
GENERIC_MAILBOXES = ("info", "office", "mail", "contact", "contacts", "hello", "welcome")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}", re.UNICODE)
TELEGRAM_RE = re.compile(r"(?:t\.me/|telegram[:\s]+@?|@)([A-Za-z][A-Za-z0-9_]{4,31})")
GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})")
# Телефоны не собираем вообще: личный мобильный запрещён [CORE-013], а
# отличить личный от рабочего по цифрам нельзя.
PHONE_RE = re.compile(r"(?:\+7|8)[\s(-]?\d{3}[\s)-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")

BANNED_CHANNEL_HOSTS = (
    "vk.com", "ok.ru", "instagram.com", "facebook.com", "tiktok.com", "twitter.com", "x.com",
)

NAME_RE = re.compile(r"\b([\u0410-\u042f\u0401][\u0430-\u044f\u0451]{2,})\s+([\u0410-\u042f\u0401][\u0430-\u044f\u0451]{2,})\b")

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "iu", "я": "ia",
}

EMAIL_TEMPLATES = (
    "{first}.{last}@{domain}",
    "{f}.{last}@{domain}",
    "{first}@{domain}",
    "{f}{last}@{domain}",
    "{first}_{last}@{domain}",
)

GITHUB_PROFILE = "https://github.com/"

CONFIDENCE_RU = {
    "high": "высокая",
    "medium": "средняя",
    "low": "низкая",
}


@dataclass(frozen=True)
class Candidate:
    """Кандидат на контакт. Не факт, а гипотеза с уровнем уверенности."""

    channel_kind: str  # email | telegram | github | site
    channel_value: str
    person: str | None = None
    role: str | None = None
    role_rank: int = 99
    source_url: str | None = None
    confidence: str = "low"
    guessed: bool = False
    notes: str | None = None

    @property
    def label(self) -> str:
        who = self.person or "имя не найдено"
        role = self.role or "роль не определена"
        return f"{who}, {role}"


@dataclass
class Discovery:
    """Итог этапа по одной вакансии."""

    key: str
    company: str | None
    candidates: tuple[Candidate, ...] = ()
    dropped: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_direct(self) -> bool:
        """Есть ли хоть один канал не в HR и не в общую почту."""
        return any(c.role_rank <= 4 for c in self.candidates)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Создаёт таблицу контактов отдельно от основной схемы."""
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def translit(word: str) -> str:
    """Имя кириллицей → латиница для шаблона почты."""
    return "".join(TRANSLIT.get(ch, ch if ch.isascii() else "") for ch in word.lower())


def role_rank(title: str | None) -> tuple[int, str | None]:
    """Сопоставляет должность с приоритетом из [OUT-001]."""
    if not title:
        return 99, None
    low = title.lower()
    for rank, label, needles in ROLE_RANKS:
        if any(n in low for n in needles):
            return rank, label
    return 99, None


def domain_of(url: str | None) -> str | None:
    """Домен компании из её сайта. Джобборды — не домен компании."""
    if not url:
        return None
    host = urlsplit(url if "//" in url else f"//{url}").netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    if any(host.endswith(bad) for bad in ("hh.ru", "career.habr.com", "habr.com", "dreamjob.ru")):
        return None
    return host


def mailbox(email: str) -> str:
    return email.split("@", 1)[0].lower()


def classify_email(email: str) -> tuple[int, str, str]:
    """Возвращает (rank, role, confidence) для найденного адреса."""
    box = mailbox(email)
    head = re.split(r"[._+-]", box)[0]
    if head in HR_MAILBOXES:
        return 8, "HR-ящик компании", "high"
    if head in GENERIC_MAILBOXES:
        return 7, "общая почта компании", "high"
    return 5, "личный рабочий ящик", "medium"


def valid_email(email: str) -> bool:
    """Форматная проверка без сети и без SMTP-пробы [OUT-008]."""
    if len(email) > 254 or email.count("@") != 1:
        return False
    return bool(EMAIL_RE.fullmatch(email))


def domain_has_mx(domain: str) -> bool | None:
    """MX-запись домена. None = проверить не удалось, это не отказ [CORE-017]."""
    try:
        import dns.resolver  # ленивый импорт: без dnspython модуль всё равно работает
    except ImportError:
        log.info("dnspython не установлен, MX не проверяем")
        return None
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
    except Exception as exc:  # noqa: BLE001 — сеть не должна ронять этап
        log.info("MX для %s не получен: %s", domain, exc)
        return None
    return bool(list(answers))


def guess_emails(person: str, domain: str) -> tuple[str, ...]:
    """Адреса по публичному шаблону. Брутфорс запрещён, поэтому список короткий."""
    parts = [p for p in re.split(r"\s+", person.strip()) if p]
    if len(parts) < 2 or not domain:
        return ()
    first, last = translit(parts[0]), translit(parts[-1])
    if not first or not last:
        return ()
    out: list[str] = []
    for tpl in EMAIL_TEMPLATES:
        email = tpl.format(first=first, last=last, f=first[0], domain=domain)
        if valid_email(email) and email not in out:
            out.append(email)
    return tuple(out)


def extract_channels(text: str, source_url: str | None = None) -> tuple[tuple[Candidate, ...], tuple[str, ...]]:
    """Тянет рабочие каналы из уже собранного текста.

    Вторым элементом возвращает причины отказов: по ним видно, что этап
    отбросил сам, а не просмотрел.
    """
    candidates: list[Candidate] = []
    dropped: list[str] = []
    seen: set[str] = set()

    for match in PHONE_RE.finditer(text or ""):
        dropped.append(f"телефон {match.group(0)} пропущен: личные номера запрещены")

    for host in BANNED_CHANNEL_HOSTS:
        if host in (text or "").lower():
            dropped.append(f"ссылка на {host} пропущена: личные соцсети не канал")

    for email in EMAIL_RE.findall(text or ""):
        email = email.strip(".,;").lower()
        if not valid_email(email) or email in seen:
            continue
        seen.add(email)
        rank, role, confidence = classify_email(email)
        candidates.append(
            Candidate(
                channel_kind="email",
                channel_value=email,
                role=role,
                role_rank=rank,
                source_url=source_url,
                confidence=confidence,
            )
        )

    for nick in TELEGRAM_RE.findall(text or ""):
        value = f"@{nick}"
        if value.lower() in seen:
            continue
        seen.add(value.lower())
        candidates.append(
            Candidate(
                channel_kind="telegram",
                channel_value=value,
                role="указанный рабочий Telegram",
                role_rank=6,
                source_url=source_url,
                confidence="medium",
                notes="роль владельца аккаунта не подтверждена",
            )
        )

    for login in GITHUB_RE.findall(text or ""):
        value = login.lower()
        if value in seen:
            continue
        seen.add(value)
        candidates.append(
            Candidate(
                channel_kind="github",
                channel_value=login,
                role="публичный профиль GitHub",
                role_rank=6,
                source_url=GITHUB_PROFILE + login,
                confidence="medium",
            )
        )

    return tuple(candidates), tuple(dropped)


def extract_names(text: str, limit: int = 5) -> tuple[str, ...]:
    """Имена рядом с должностью руководителя. Грубо, но проверяемо."""
    out: list[str] = []
    for sentence in re.split(r"[\n.;!?]", text or ""):
        rank, _ = role_rank(sentence)
        if rank > 4:
            continue
        for first, last in NAME_RE.findall(sentence):
            name = f"{first} {last}"
            if name not in out:
                out.append(name)
            if len(out) >= limit:
                return tuple(out)
    return tuple(out)


def rank_candidates(candidates: Iterable[Candidate], limit: int = 3) -> tuple[Candidate, ...]:
    """1–3 кандидата: сначала ближе к нанимающему, гадание — ниже факта."""
    order = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(
        candidates,
        key=lambda c: (c.role_rank, int(c.guessed), order.get(c.confidence, 3), c.channel_value),
    )
    return tuple(ranked[:limit])


def discover(
    key: str,
    company: str | None,
    vacancy_text: str,
    company_pages: Sequence[tuple[str, str]] = (),
    site_url: str | None = None,
    check_mx: bool = False,
) -> Discovery:
    """Детерминированный проход: сначала то, что уже есть в данных (ADR-011).

    company_pages — пары (url, текст) со страниц «Команда»/«О нас»/блога,
    если их собрал предыдущий шаг. Внешний поиск сюда не входит.
    """
    found: list[Candidate] = []
    dropped: list[str] = []

    vacancy_channels, vacancy_dropped = extract_channels(vacancy_text)
    found.extend(vacancy_channels)
    dropped.extend(vacancy_dropped)

    domain = domain_of(site_url)
    for url, text in company_pages:
        page_channels, page_dropped = extract_channels(text, source_url=url)
        found.extend(page_channels)
        dropped.extend(page_dropped)
        domain = domain or domain_of(url)

        for name in extract_names(text):
            for email in guess_emails(name, domain or ""):
                found.append(
                    Candidate(
                        channel_kind="email",
                        channel_value=email,
                        person=name,
                        role="руководитель по странице команды",
                        role_rank=2,
                        source_url=url,
                        confidence="low",
                        guessed=True,
                        notes="адрес выведен по шаблону, не подтверждён",
                    )
                )
                break  # один вариант на человека: перебор запрещён [OUT-008]

    if check_mx and domain:
        has_mx = domain_has_mx(domain)
        if has_mx is False:
            before = len(found)
            found = [c for c in found if not (c.guessed and c.channel_value.endswith(f"@{domain}"))]
            if before != len(found):
                dropped.append(f"у домена {domain} нет MX-записи, угаданные адреса отброшены")

    return Discovery(
        key=key,
        company=company,
        candidates=rank_candidates(found),
        dropped=tuple(dropped),
    )


def is_blocked(conn: sqlite3.Connection, company: str | None, person: str | None = None) -> bool:
    """Отказ и «не писать» окончательны [OUT-009]."""
    rows = conn.execute(
        "SELECT company, person FROM contacts WHERE status = ?", (BLOCKED,)
    ).fetchall()
    for row in rows:
        if company and row["company"] and row["company"] == company:
            return True
        if person and row["person"] and row["person"] == person:
            return True
    return False


def recently_contacted(
    conn: sqlite3.Connection,
    person: str | None,
    company: str | None,
    now: datetime | None = None,
) -> bool:
    """Повторный контакт с тем же человеком — не раньше трёх месяцев [OUT-007]."""
    if not person and not company:
        return False
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=REPEAT_AFTER_DAYS)).isoformat()
    if person:
        row = conn.execute(
            "SELECT 1 FROM contacts WHERE person = ? AND created_at >= ? LIMIT 1",
            (person, cutoff),
        ).fetchone()
        if row:
            return True
    # Одно письмо на компанию за раз: рассылка по сотрудникам закроет компанию [OUT-004].
    if company:
        row = conn.execute(
            "SELECT 1 FROM contacts WHERE company = ? AND status IN (?, ?, ?) AND created_at >= ? LIMIT 1",
            (company, DRAFTED, SENT, REPLIED, cutoff),
        ).fetchone()
        if row:
            return True
    return False


def store(
    conn: sqlite3.Connection,
    key: str,
    company: str | None,
    candidate: Candidate,
    status: str = DRAFTED,
) -> int:
    """Пишет контакт в лог и возвращает id записи."""
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус: {status}")
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO contacts
            (key, company, person, role, role_rank, channel_kind, channel_value,
             source_url, confidence, guessed, status, created_at, updated_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key, company, candidate.person, candidate.role, candidate.role_rank,
            candidate.channel_kind, candidate.channel_value, candidate.source_url,
            candidate.confidence, int(candidate.guessed), status, now, now, candidate.notes,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_status(conn: sqlite3.Connection, contact_id: int, status: str) -> None:
    """Статус меняет владелец кнопкой: система отправки не видит [OUT-006]."""
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус: {status}")
    conn.execute(
        "UPDATE contacts SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), contact_id),
    )
    conn.commit()


def load(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contacts WHERE key = ? ORDER BY role_rank, id", (key,)
    ).fetchall()


def coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(вакансий с прямым контактом, вакансий с любым контактом)."""
    direct = conn.execute(
        "SELECT COUNT(DISTINCT key) FROM contacts WHERE role_rank <= 4"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(DISTINCT key) FROM contacts").fetchone()[0]
    return int(direct), int(total)


def format_contact_lines(discovery: Discovery, limit: int = 3) -> list[str]:
    """Строки для карточки. Гадание всегда подписано как гадание."""
    if not discovery.candidates:
        return ["Контакт: прямого контакта нет — остаётся отклик через обычную воронку"]
    lines: list[str] = []
    for cand in discovery.candidates[:limit]:
        conf = CONFIDENCE_RU.get(cand.confidence, cand.confidence)
        suffix = " · адрес угадан по шаблону" if cand.guessed else ""
        source = f" ← {cand.source_url}" if cand.source_url else ""
        lines.append(
            f"Контакт: {cand.label} (уверенность: {conf}){suffix}\n"
            f"Канал: {cand.channel_kind} {cand.channel_value}{source}"
        )
    return lines
