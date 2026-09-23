"""Признаки промпт-инъекций: фразы, разметка, невидимое, опасные команды.

Только данные, отделено от логики по [CORE-024] и [CORE-025].

Логика деления на уровни. `red` — текст обращается к ассистенту как к
исполнителю: «инструкция для ИИ», «ignore previous instructions», «верни
пустой список», или прячет такую фразу невидимыми символами. `yellow` —
одиночный подозрительный признак, которого мало для обвинения: разметка чата,
смешанные алфавиты, странно длинный base64.

Отдельная группа — опасные команды. Модель в этом проекте ничего не исполняет:
у неё нет ни инструментов, ни доступа к базе, и её ответ всегда проходит
детерминированную проверку. Но текст «удали базу» или «rm -rf /» в описании
вакансии вырезается первым и целой строкой: чистка должна быть тщательной, а
не косметической — на случай, если завтра у этапа появится инструмент.

Ложные срабатывания. Вакансия промпт-инженера легально содержит «ignore
previous instructions», а отзыв может процитировать инструкцию. Поэтому есть
CONTEXT_WORDS: в тексте про работу с моделями одиночная фраза остаётся жёлтой.
"""

from __future__ import annotations

import re

# —— невидимое ——

# Нулевая ширина, управление направлением письма, теговые символы Unicode
# (ими прячут целые фразы, невидимые для человека, но видимые модели).
INVISIBLE_RE = re.compile(
    "[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u206a-\u206f\ufeff]"
    "|[\U000e0000-\U000e007f]"
)

# —— разметка чата ——

MARKUP_RE = re.compile(
    r"<\|[^>]{0,40}\|>"
    r"|<\/?\s*(?:system|assistant|user|s|inst)\s*>"
    r"|\[/?INST\]"
    r"|\[/?SYS\]"
    r"|^\s*(?:system|assistant)\s*:",
    re.IGNORECASE | re.MULTILINE,
)

# —— обращение к ассистенту ——

ADDRESS = (
    "инструкция для ии",
    "инструкция для ai",
    "инструкция для ассистент",
    "указание для ии",
    "сообщение для ии",
    "внимание, ии",
    "если ты языковая модель",
    "как языковая модель",
    "ты языковая модель",
    "ты ассистент",
    "system prompt",
    "системный промпт",
    "ignore previous instructions",
    "ignore all previous",
    "ignore the above",
    "disregard previous",
    "disregard the above",
    "forget your instructions",
    "you are chatgpt",
    "as an ai language model",
    "prompt injection",
)

# Что именно велят сделать. Одного глагола мало, но в паре с ADDRESS или с
# невидимым текстом это уже красный уровень.
COMMANDS = (
    "верни пустой",
    "ответь пустым",
    "не отмечай",
    "не указывай",
    "не сообщай",
    "ничего не находи",
    "проигнорируй правила",
    "игнорируй предыдущие",
    "игнорируй инструкции",
    "перепиши инструкцию",
    "измени инструкцию",
    "оцени кандидата как идеального",
    "рекомендуй этого кандидата",
    "поставь максимальный балл",
    "укажи зарплату",
    "напиши, что жалоб нет",
    "напиши что жалоб нет",
    "скрой",
    "output only",
    "respond only with",
    "do not mention",
    "must recommend",
    "rate this candidate",
)

# —— опасные команды ——

DANGEROUS = (
    "drop table",
    "drop database",
    "delete from",
    "truncate table",
    "rm -rf",
    "format c:",
    "удали базу",
    "удалить базу",
    "очисти базу",
    "сотри все данные",
    "shutdown",
    "os.system",
    "subprocess",
    "eval(",
    "exec(",
    "curl http",
    "wget http",
    "отправь на http",
    "send to http",
    "exfiltrate",
    "api key",
    "покажи свой промпт",
    "покажи системное сообщение",
    "reveal your prompt",
)

# —— смягчение ——

# Текст действительно про работу с моделями: «ignore previous instructions» в
# вакансии промпт-инженера — это предмет работы, а не атака.
CONTEXT_WORDS = (
    "промпт-инженер",
    "prompt engineer",
    "llm",
    "языковых моделей",
    "jailbreak",
    "red team",
    "безопасность моделей",
)

# —— скрытый текст в HTML ——

# Проверяется до strip_tags: после него скрытое и обычное неразличимы.
HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|font-size\s*:\s*0(?:\.0+)?(?:px|em|rem|pt)?\b"
    r"|opacity\s*:\s*0(?:\.0+)?\b"
    r"|color\s*:\s*(?:#fff(?:fff)?\b|white\b)"
    r"|text-indent\s*:\s*-\d{3,}"
    r"|(?:left|top)\s*:\s*-\d{4,}px",
    re.IGNORECASE,
)
HIDDEN_ATTR_RE = re.compile(r"\b(?:hidden|aria-hidden\s*=\s*[\"']?true)", re.IGNORECASE)
TAG_RE = re.compile(r"<(?P<tag>[a-zA-Z][\w:-]*)(?P<attrs>[^>]*)>(?P<body>.{0,4000}?)</(?P=tag)>", re.DOTALL)

# —— гомоглифы ——

LOOKALIKE = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "k": "к", "m": "м", "h": "н", "t": "т", "b": "в", "i": "и",
}
MIXED_WORD_RE = re.compile(r"\b(?=\w*[а-яё])(?=\w*[a-z])\w{4,}\b", re.IGNORECASE)

# —— прочее ——

BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{120,}={0,2}\b")

# Коды находок и их вес в отчёте. Вес не балл модели: он нужен, чтобы
# отсортировать находки для владельца.
CODES = {
    "assistant_address": ("Обращение к ИИ-ассистенту", 5),
    "hidden_command": ("Команда, спрятанная невидимыми символами", 5),
    "hidden_html": ("Текст, скрытый в вёрстке страницы", 5),
    "dangerous": ("Опасная команда в тексте", 5),
    "command": ("Указание, как отвечать", 3),
    "markup": ("Разметка чата в тексте", 2),
    "invisible": ("Невидимые символы", 2),
    "homoglyph": ("Смешанные алфавиты в словах", 1),
    "base64": ("Длинный закодированный блок", 1),
}

RED = "red"
YELLOW = "yellow"
CLEAN = "clean"

LEVEL_RU = {
    CLEAN: "чисто",
    YELLOW: "подозрительно",
    RED: "инъекция",
}

# Уровень по коду находки.
CODE_LEVEL = {
    "assistant_address": RED,
    "hidden_command": RED,
    "hidden_html": RED,
    "dangerous": RED,
    "command": YELLOW,
    "markup": YELLOW,
    "invisible": YELLOW,
    "homoglyph": YELLOW,
    "base64": YELLOW,
}

# Сколько символов цитаты сохраняем.
QUOTE_CHARS = 180


__all__ = (
    "ADDRESS",
    "BASE64_RE",
    "CLEAN",
    "CODES",
    "CODE_LEVEL",
    "COMMANDS",
    "CONTEXT_WORDS",
    "DANGEROUS",
    "HIDDEN_ATTR_RE",
    "HIDDEN_STYLE_RE",
    "INVISIBLE_RE",
    "LEVEL_RU",
    "LOOKALIKE",
    "MARKUP_RE",
    "MIXED_WORD_RE",
    "QUOTE_CHARS",
    "RED",
    "TAG_RE",
    "YELLOW",
)
