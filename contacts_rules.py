"""Словари и регулярки этапа contact discovery.

Отдельно от логики по [CORE-024]: правила и таблицы меняются чаще кода и
читаются сами по себе. Публичные имена доступны и через contacts.py.
"""

from __future__ import annotations

import re

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

# Ранги 1–3 из ROLE_RANKS: только по ним предложение считается «руководительским».
# Ранг 4 — это слова «python» и «разработчик»: по ним руководителя не опознать
# [OUT-001], а угаданная должность без источника запрещена [LEG-004].
LEAD_RANKS = tuple((rank, label, needles) for rank, label, needles in ROLE_RANKS if rank <= 3)

# Адреса, по которым письмо упадёт ровно в ту воронку, которую обходим.
HR_MAILBOXES = (
    "hr", "job", "jobs", "vacancy", "vacancies", "career", "careers", "resume",
    "cv", "recruit", "recruiting", "rabota", "personal",
)
GENERIC_MAILBOXES = ("info", "office", "mail", "contact", "contacts", "hello", "welcome")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}", re.UNICODE)
# Ник ловится только там, где канал действительно опубликован: ссылка t.me,
# явное слово «telegram» или отдельно стоящий @ник. Голая альтернатива @
# превращала локальную часть любого адреса в «рабочий Telegram» [OUT-003].
TELEGRAM_RE = re.compile(r"(?:t\.me/|telegram[:\s]+@?)([A-Za-z][A-Za-z0-9_]{4,31})", re.IGNORECASE)
TELEGRAM_NICK_RE = re.compile(r"(?<![\w.+-])@([A-Za-z][A-Za-z0-9_]{4,31})(?![\w.@-])")
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
