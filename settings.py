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

from settings_fields import (  # noqa: F401 — публичные имена остаются в settings
    BOOL,
    FIELD_BY_KEY,
    FIELDS,
    FLOAT,
    GROUP_DEEP,
    GROUP_DETECTOR,
    GROUP_EMBED,
    GROUP_HINTS,
    GROUP_LLM,
    GROUP_LLM_STAGES,
    GROUP_PATHS,
    GROUP_PREFILTER,
    GROUP_RUN,
    GROUP_SCORE,
    GROUP_SEARCH,
    GROUP_SOURCE,
    GROUP_TELEGRAM,
    GROUPS,
    INT,
    SECRET,
    TEXT,
    Field,
)

TRUE_VALUES = {"1", "true", "yes", "on", "да"}


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
class LoopOptions:
    """Режим цикла: повторять сбор по кругу.

    cycles = 0 означает «пока не остановят вручную»: лимит циклов и лимит
    вакансий читаются одинаково, ноль — это отсутствие границы.
    """

    enabled: bool
    cycles: int
    pause: float


@dataclass(frozen=True)
class OutreachOptions:
    """Всё, что раньше было флагами outreach.py."""

    limit: int
    min_score: float
    profile: str
    allow_generic: bool
    check_mx: bool
    use_llm: bool


@dataclass(frozen=True)
class TelegramOptions:
    """Отправка карточек: выключатель, темп и тихие часы.

    Границы тихих часов — минуты от полуночи, None означает «не заданы».
    Отправка в тихие часы не теряется: карточка не помечается отправленной и
    уходит следующим прогоном [CORE-017].
    """

    enabled: bool
    delay: float
    quiet_from: int | None
    quiet_to: int | None

    def quiet_at(self, minutes: int) -> bool:
        """Попадает ли момент в тихие часы. Интервал через полночь — нормальный случай."""
        if self.quiet_from is None or self.quiet_to is None:
            return False
        if self.quiet_from == self.quiet_to:
            return False
        if self.quiet_from < self.quiet_to:
            return self.quiet_from <= minutes < self.quiet_to
        return minutes >= self.quiet_from or minutes < self.quiet_to


@dataclass(frozen=True)
class PrefilterOptions:
    """Отсев до загрузки описания: что считается мусором на выдаче."""

    enabled: bool
    min_score: float
    fuzzy: int


@dataclass(frozen=True)
class DetectorOptions:
    """Пороги детектора HR-брехни. Ниже порогов вывод — догадка, а не факт."""

    enabled: bool
    min_days: int
    republish_alarm: int
    wide_band: float
    use_llm_claims: bool


@dataclass(frozen=True)
class EmbeddingsOptions:
    """Векторы текстов (ADR-021): считать ли и с какой близости верить."""

    enabled: bool
    review_dup: float
    vacancy_sim: float


@dataclass(frozen=True)
class CompanyScoreOptions:
    """Общая оценка работодателя (ADR-018): считать и учитывать ли её."""

    enabled: bool
    in_score: bool
    penalty: float


def collect_options() -> CollectOptions:
    return CollectOptions(
        limit=as_int(os.getenv("RUN_LIMIT"), 30),
        profile=get("RUN_PROFILE", "profile.yaml"),
        details=flag("RUN_DETAILS"),
        use_llm=flag("LLM_ENABLED"),
    )


def loop_options() -> LoopOptions:
    return LoopOptions(
        enabled=flag("RUN_LOOP_ENABLED"),
        cycles=max(0, as_int(os.getenv("RUN_LOOP_CYCLES"), 0)),
        pause=max(0.0, as_float(os.getenv("RUN_LOOP_PAUSE"), 300.0)),
    )


def as_minutes(value: object) -> int | None:
    """«ЧЧ:ММ» → минуты от полуночи. Мусор молча становится None: время в
    настройках вводят руками, и падать из-за опечатки страница не должна."""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.replace(".", ":").split(":")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def telegram_options() -> TelegramOptions:
    return TelegramOptions(
        enabled=flag("TELEGRAM_ENABLED"),
        delay=max(0.0, min(60.0, as_float(os.getenv("TELEGRAM_DELAY"), 0.6))),
        quiet_from=as_minutes(os.getenv("TELEGRAM_QUIET_FROM")),
        quiet_to=as_minutes(os.getenv("TELEGRAM_QUIET_TO")),
    )


def prefilter_options() -> PrefilterOptions:
    return PrefilterOptions(
        enabled=flag("PREFILTER_ENABLED"),
        min_score=as_float(os.getenv("PREFILTER_MIN_SCORE"), 0.0),
        fuzzy=max(50, min(100, as_int(os.getenv("PREFILTER_FUZZY"), 88))),
    )


def detector_options() -> DetectorOptions:
    return DetectorOptions(
        enabled=flag("DETECTOR_ENABLED"),
        min_days=max(1, as_int(os.getenv("DETECTOR_MIN_DAYS"), 30)),
        republish_alarm=max(2, as_int(os.getenv("DETECTOR_REPUBLISH_ALARM"), 3)),
        wide_band=max(1.1, as_float(os.getenv("DETECTOR_WIDE_BAND"), 2.0)),
        use_llm_claims=flag("DETECTOR_LLM_CLAIMS") and flag("LLM_ENABLED"),
    )


def company_score_options() -> CompanyScoreOptions:
    return CompanyScoreOptions(
        enabled=flag("COMPANY_SCORE_ENABLED"),
        in_score=flag("COMPANY_SCORE_IN_SCORE"),
        penalty=max(0.0, as_float(os.getenv("COMPANY_SCORE_PENALTY"), 15.0)),
    )


def embeddings_options() -> EmbeddingsOptions:
    """Пороги близости зажаты в 0.5..0.999: ниже это уже не «тот же текст»,
    а выше — не срабатывает никогда, и обе крайности выглядят как «не работает»."""
    return EmbeddingsOptions(
        enabled=flag("EMBEDDINGS_ENABLED"),
        review_dup=max(0.5, min(0.999, as_float(os.getenv("EMBEDDINGS_REVIEW_DUP"), 0.93))),
        vacancy_sim=max(0.5, min(0.999, as_float(os.getenv("EMBEDDINGS_VACANCY_SIM"), 0.80))),
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
    if not flag("TELEGRAM_ENABLED"):
        notes.append("Отправка в Telegram выключена: карточки остаются здесь")
    elif not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
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
    "CompanyScoreOptions",
    "DetectorOptions",
    "EmbeddingsOptions",
    "PrefilterOptions",
    "ENV_PATH",
    "FIELDS",
    "FIELD_BY_KEY",
    "Field",
    "FLOAT",
    "GROUPS",
    "GROUP_HINTS",
    "INT",
    "OutreachOptions",
    "SECRET",
    "TEXT",
    "as_bool",
    "as_float",
    "as_int",
    "as_minutes",
    "collect_options",
    "company_score_options",
    "detector_options",
    "embeddings_options",
    "flag",
    "form_updates",
    "get",
    "groups",
    "load",
    "loop_options",
    "mask",
    "missing_required",
    "outreach_options",
    "parse_env",
    "prefilter_options",
    "render_env",
    "save",
    "telegram_options",
)
