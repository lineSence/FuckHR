"""Настройки программы: описание полей, чтение и запись .env.

Зачем отдельный модуль. До него настройки жили в двух местах одновременно:
часть в .env, часть в флагах командной строки. Значение «брать 30 вакансий»
жило только в истории PowerShell, и через неделю уже непонятно, с какими
параметрами был прошлый прогон. Теперь единственный источник — .env, его
правит веб-интерфейс, а CLI только запускает программу.

Границы:

- секреты (токены, ключи, cookie) никогда не отдаются целиком наружу:
  интерфейс получает маску, а пустое поле при сохранении значит «не трогать»,
  иначе первое же сохранение формы вытирало бы токен бота;
- запись сохраняет комментарии и порядок строк: .env пишет и человек тоже;
- чтение для пайплайна идёт через os.getenv, чтобы запуск с другим
  окружением (контейнер, планировщик) продолжал работать без файла.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ENV_PATH = Path(os.getenv("ENV_FILE", ".env"))

TEXT = "text"
SECRET = "secret"
INT = "int"
FLOAT = "float"
BOOL = "bool"

TRUE_VALUES = {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Field:
    """Одна настройка: ключ в .env плюс всё, что нужно для формы."""

    key: str
    label: str
    group: str
    kind: str = TEXT
    default: str = ""
    help: str = ""

    @property
    def is_secret(self) -> bool:
        return self.kind == SECRET


GROUP_RUN = "Запуск"
GROUP_SOURCE = "Источник вакансий"
GROUP_TELEGRAM = "Telegram"
GROUP_LLM = "Модель"
GROUP_SEARCH = "Внешний поиск"
GROUP_PATHS = "Файлы и логи"

# Порядок важен: в таком виде поля рисуются на странице настроек.
FIELDS: tuple[Field, ...] = (
    Field(
        "RUN_LIMIT",
        "Сколько вакансий собирать за прогон",
        GROUP_RUN,
        INT,
        "30",
        "Верхняя граница на один запуск сбора. Цель — не охват, а пять хороших входов.",
    ),
    Field(
        "RUN_DETAILS",
        "Загружать полные описания",
        GROUP_RUN,
        BOOL,
        "1",
        "Без описаний не работают ни детектор HR-сигналов, ни разбор условий.",
    ),
    Field(
        "OUTREACH_LIMIT",
        "Сколько писем готовить за прогон",
        GROUP_RUN,
        INT,
        "5",
        "Пять осмысленных писем работают лучше сотни откликов.",
    ),
    Field(
        "OUTREACH_MIN_SCORE",
        "Порог скоринга для письма",
        GROUP_RUN,
        FLOAT,
        "60",
        "Вакансии ниже порога в письма не попадают.",
    ),
    Field(
        "OUTREACH_ALLOW_GENERIC",
        "Готовить сопроводительное без прямого контакта",
        GROUP_RUN,
        BOOL,
        "1",
        "hh.ru вырезает контакты из текста: без этого флага почти всё отбрасывается.",
    ),
    Field(
        "OUTREACH_CHECK_MX",
        "Проверять MX домена",
        GROUP_RUN,
        BOOL,
        "0",
        "Нужен dnspython. Отсекает угаданные адреса на мёртвых доменах.",
    ),
    Field(
        "RUN_PROFILE",
        "Файл профиля",
        GROUP_RUN,
        TEXT,
        "profile.yaml",
        "Запросы, навыки, порог скоринга и факты о себе.",
    ),
    Field(
        "HH_TOKEN",
        "Токен hh.ru",
        GROUP_SOURCE,
        SECRET,
        "",
        "Необязателен: сбор работает по HTML-страницам (ADR-015).",
    ),
    Field(
        "HH_COOKIE",
        "Cookie hh.ru",
        GROUP_SOURCE,
        SECRET,
        "",
        "Помогает, когда площадка начинает показывать капчу.",
    ),
    Field("HH_USER_AGENT", "User-Agent", GROUP_SOURCE, TEXT, "", "Подпись клиента при запросах."),
    Field("HH_PROXY", "Прокси для hh.ru", GROUP_SOURCE, TEXT, "", "Например http://127.0.0.1:8080"),
    Field(
        "HH_PAUSE",
        "Пауза между запросами, сек",
        GROUP_SOURCE,
        FLOAT,
        "2.0",
        "Меньше двух секунд — быстрый путь к бану.",
    ),
    Field("TELEGRAM_BOT_TOKEN", "Токен бота", GROUP_TELEGRAM, SECRET, "", "Без него карточки только в интерфейсе."),
    Field("TELEGRAM_CHAT_ID", "Чат для карточек", GROUP_TELEGRAM, TEXT, "", "Свой id можно узнать у @userinfobot."),
    Field(
        "LLM_ENABLED",
        "Использовать модель",
        GROUP_LLM,
        BOOL,
        "1",
        "Выключенная модель — штатный режим: пайплайн работает без неё целиком.",
    ),
    Field("LLM_BASE_URL", "Локальный адрес модели", GROUP_LLM, TEXT, "", "Например http://localhost:3001/v1"),
    Field("LLM_API_KEY", "Ключ локальной модели", GROUP_LLM, SECRET, "", "Обычно не нужен."),
    Field(
        "LLM_PROXY_BASE_URL",
        "Адрес внешнего прокси",
        GROUP_LLM,
        TEXT,
        "",
        "LiteLLM через туннель, например http://127.0.0.1:4000/v1",
    ),
    Field("LLM_PROXY_API_KEY", "Ключ прокси", GROUP_LLM, SECRET, "", "Мастер-ключ LiteLLM. Без него будет 401."),
    Field("LLM_PROXY_MODEL_FAST", "Модель для быстрых этапов", GROUP_LLM, TEXT, "", "Разбор условий, черновая работа."),
    Field("LLM_PROXY_MODEL_SMART", "Модель для разбора", GROUP_LLM, TEXT, "", "HR-фильтр и скоринг."),
    Field("LLM_PROXY_MODEL_LONG", "Модель для длинных текстов", GROUP_LLM, TEXT, "", "Справка о компании."),
    Field("LLM_PROXY_MODEL_EMBEDDINGS", "Модель векторов", GROUP_LLM, TEXT, "", "Например bge-m3."),
    Field(
        "LLM_PERSONAL_VIA_PROXY",
        "Пускать персональные этапы на прокси",
        GROUP_LLM,
        BOOL,
        "0",
        "ВНИМАНИЕ: включённый флаг отправляет ФИО и адреса живых людей за пределы машины [CORE-012].",
    ),
    Field("LLM_TIMEOUT", "Таймаут модели, сек", GROUP_LLM, FLOAT, "60", ""),
    Field("LLM_MAX_CALLS", "Лимит вызовов на прогон", GROUP_LLM, INT, "300", "Страховка от бесконечного цикла."),
    Field(
        "SEARCH_PROVIDER",
        "Провайдер поиска",
        GROUP_SEARCH,
        TEXT,
        "searxng",
        "searxng, tavily или brave.",
    ),
    Field(
        "SEARCH_BASE_URL",
        "Адрес SearXNG",
        GROUP_SEARCH,
        TEXT,
        "",
        "Свой инстанс, например http://127.0.0.1:8888 через туннель.",
    ),
    Field("SEARCH_API_KEY", "Ключ провайдера", GROUP_SEARCH, SECRET, "", "Только для tavily и brave."),
    Field("SEARCH_BASIC_AUTH", "Логин:пароль для SearXNG", GROUP_SEARCH, SECRET, "", "Если инстанс закрыт basic-авторизацией."),
    Field("SEARCH_TIMEOUT", "Таймаут поиска, сек", GROUP_SEARCH, FLOAT, "20", ""),
    Field("SEARCH_MAX_CALLS", "Лимит запросов на прогон", GROUP_SEARCH, INT, "60", ""),
    Field("DB_PATH", "Файл базы", GROUP_PATHS, TEXT, "data/fuckhr.sqlite3", ""),
    Field("LOG_PATH", "Файл лога", GROUP_PATHS, TEXT, "data/fuckhr.log", ""),
    Field("FAILURE_DIR", "Куда класть сырой HTML при сбоях", GROUP_PATHS, TEXT, "data/failures", ""),
    Field("ALERT_COOLDOWN_HOURS", "Пауза между тревогами, часов", GROUP_PATHS, INT, "24", ""),
)

FIELD_BY_KEY: dict[str, Field] = {field.key: field for field in FIELDS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(field.group for field in FIELDS))


def groups() -> list[tuple[str, list[Field]]]:
    """Поля, разложенные по группам в порядке объявления."""
    return [
        (group, [field for field in FIELDS if field.group == group]) for group in GROUPS
    ]


def parse_env(text: str) -> dict[str, str]:
    """Разбор .env: KEY=VALUE, комментарии и пустые строки игнорируются.

    Свой разбор, а не dotenv: модуль нужен и в тестах, где файл лежит в
    tmp_path и грузить его в процесс нельзя.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.lower().startswith("export "):
            stripped = stripped[7:].lstrip()
        key, _, raw = stripped.partition("=")
        key = key.strip()
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path: str | Path | None = None) -> dict[str, str]:
    """Текущие значения из файла. Нет файла — пустой словарь, это норма."""
    target = Path(path or ENV_PATH)
    if not target.exists():
        return {}
    return parse_env(target.read_text(encoding="utf-8"))


def quote(value: str) -> str:
    """Кавычки только там, где без них значение развалится."""
    if value == "" or not any(ch in value for ch in " #\"'"):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"{}"'.format(escaped)


def render_env(original: str, updates: Mapping[str, str]) -> str:
    """Новое содержимое .env: известные строки правятся на месте.

    Комментарии и порядок сохраняются: файл читает человек, и превращать
    его в машинный дамп после первого же сохранения формы нельзя.
    """
    remaining = dict(updates)
    out: list[str] = []
    for line in original.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key.lower().startswith("export "):
                key = key[7:].strip()
            if key in remaining:
                out.append("{}={}".format(key, quote(remaining.pop(key))))
                continue
        out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append("{}={}".format(key, quote(value)))
    return "\n".join(out).rstrip("\n") + "\n"


def save(updates: Mapping[str, str], path: str | Path | None = None) -> list[str]:
    """Записывает только изменившиеся ключи и возвращает их имена.

    Значения сразу попадают и в os.environ: интерфейс должен показать новое
    состояние поиска и модели без перезапуска.
    """
    target = Path(path or ENV_PATH)
    current = load(target)
    changed = {
        key: value for key, value in updates.items() if current.get(key, "") != value
    }
    if not changed:
        return []

    original = target.read_text(encoding="utf-8") if target.exists() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_env(original, changed), encoding="utf-8")
    for key, value in changed.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return sorted(changed)


def mask(value: str) -> str:
    """Секрет в виде, по которому понятно «задано и то ли», но нельзя использовать."""
    if not value:
        return "не задано"
    if len(value) <= 8:
        return value[0] + "*" * (len(value) - 1)
    return "{}…{} ({} символов)".format(value[:4], value[-2:], len(value))


def as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in TRUE_VALUES


def as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def get(key: str, default: str | None = None) -> str:
    """Значение для пайплайна: окружение, иначе дефолт из описания поля."""
    if default is None:
        field = FIELD_BY_KEY.get(key)
        default = field.default if field else ""
    value = os.getenv(key)
    return default if value is None or value == "" else value


def flag(key: str) -> bool:
    field = FIELD_BY_KEY.get(key)
    return as_bool(os.getenv(key), as_bool(field.default if field else "", False))


@dataclass(frozen=True)
class CollectOptions:
    """Всё, что раньше было флагами run.py."""

    limit: int
    profile: str
    details: bool
    use_llm: bool


@dataclass(frozen=True)
class OutreachOptions:
    """Всё, что раньше было флагами outreach.py."""

    limit: int
    min_score: float
    profile: str
    allow_generic: bool
    check_mx: bool
    use_llm: bool


def collect_options() -> CollectOptions:
    return CollectOptions(
        limit=as_int(os.getenv("RUN_LIMIT"), 30),
        profile=get("RUN_PROFILE", "profile.yaml"),
        details=flag("RUN_DETAILS"),
        use_llm=flag("LLM_ENABLED"),
    )


def outreach_options() -> OutreachOptions:
    return OutreachOptions(
        limit=as_int(os.getenv("OUTREACH_LIMIT"), 5),
        min_score=as_float(os.getenv("OUTREACH_MIN_SCORE"), 60.0),
        profile=get("RUN_PROFILE", "profile.yaml"),
        allow_generic=flag("OUTREACH_ALLOW_GENERIC"),
        check_mx=flag("OUTREACH_CHECK_MX"),
        use_llm=flag("LLM_ENABLED"),
    )


def missing_required() -> list[str]:
    """Настройки, без которых пайплайн отработает, но вполсилы.

    Намеренно не исключения: отсутствие Telegram или поиска — повод показать
    предупреждение в интерфейсе, а не падать [CORE-017].
    """
    notes: list[str] = []
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        notes.append("Telegram не настроен: карточки останутся только здесь")
    provider = get("SEARCH_PROVIDER", "searxng").lower()
    if provider == "searxng" and not os.getenv("SEARCH_BASE_URL"):
        notes.append("Не задан адрес SearXNG: внешний поиск выключен")
    if provider in {"tavily", "brave"} and not os.getenv("SEARCH_API_KEY"):
        notes.append("Для выбранного провайдера поиска нужен ключ")
    if flag("LLM_ENABLED") and not (
        os.getenv("LLM_BASE_URL") or os.getenv("LLM_PROXY_BASE_URL")
    ):
        notes.append("Модель включена, но адрес не задан")
    if os.getenv("LLM_PROXY_BASE_URL") and not os.getenv("LLM_PROXY_API_KEY"):
        notes.append("У прокси нет ключа: скорее всего будет 401")
    if flag("LLM_PERSONAL_VIA_PROXY"):
        notes.append(
            "Персональные этапы идут на прокси: ФИО и адреса людей покидают машину"
        )
    return notes


def form_updates(
    raw: Mapping[str, Sequence[str]], fields: Iterable[Field] | None = None
) -> dict[str, str]:
    """Превращает разобранную форму в набор значений для save().

    Две неочевидные вещи. Флажки: браузер не присылает выключенный checkbox,
    поэтому отсутствие ключа трактуется как ноль. Секреты: пустое поле — это
    «оставь как было», а не «стереть», иначе сохранение страницы с масками
    вытирало бы все токены. Стереть явно можно словом «очистить».
    """
    updates: dict[str, str] = {}
    for field in fields or FIELDS:
        present = field.key in raw
        value = (raw.get(field.key) or [""])[0].strip()
        if field.kind == BOOL:
            updates[field.key] = "1" if as_bool(value, present) else "0"
            continue
        if not present:
            continue
        if field.is_secret:
            if not value:
                continue
            if value.lower() in {"очистить", "clear"}:
                updates[field.key] = ""
                continue
        updates[field.key] = value
    return updates


__all__ = (
    "BOOL",
    "CollectOptions",
    "ENV_PATH",
    "FIELDS",
    "FIELD_BY_KEY",
    "Field",
    "FLOAT",
    "INT",
    "OutreachOptions",
    "SECRET",
    "TEXT",
    "as_bool",
    "as_float",
    "as_int",
    "collect_options",
    "flag",
    "form_updates",
    "get",
    "groups",
    "load",
    "mask",
    "missing_required",
    "outreach_options",
    "parse_env",
    "render_env",
    "save",
)
