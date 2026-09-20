"""Раздел «Профили»: карточки вместо одной длинной формы.

Профилей около десятка (ADR-023), и каждый — это весь набор критериев: запросы,
гео, вилка, стек, стоп-слова, веса, порог. Одна форма на всё это занимает
несколько экранов, и десять таких форм подряд читать невозможно.

Поэтому раздел открывается **списком карточек**: имя, включён ли профиль,
короткая сводка (запросы, города, порог) и две кнопки — «Настроить» и
«Выключить». Сама форма живёт на своей странице, `/profile?id=<файл>`, и её
разделы свёрнуты: обычно правят один из семи, а не все.

Выключатель хранится в самом YAML (`enabled: false`), а не в .env и не в базе:
профиль — это файл, и его состояние должно переезжать вместе с файлом.

Идентификатор профиля — имя файла без расширения; оно же попадает в
`vacancy_profiles`. Поэтому имя из браузера сверяется со списком файлов на
диске, а не подставляется в путь: иначе `id=../../.env` открыл бы чужой файл.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import profile_form
import settings
from ui_core import esc

log = logging.getLogger(__name__)

# Каталог по умолчанию, когда владелец добавляет второй профиль, а RUN_PROFILE
# всё ещё указывает на одиночный profile.yaml.
DEFAULT_DIR = "profiles"

# В имени файла оставляем буквы, цифры и дефис: файл потом руками правят,
# ищут в проводнике и подписывают в Telegram-карточке.
SLUG_DROP = re.compile(r"[^\w-]+", re.UNICODE)


@dataclass(frozen=True)
class Entry:
    """Один профиль в списке: то, что видно на карточке."""

    id: str
    path: Path
    title: str
    enabled: bool
    queries: tuple[str, ...]
    areas: str
    min_score: str


def is_catalog(profile_path: str | Path) -> bool:
    return Path(profile_path).is_dir()


def files(profile_path: str | Path) -> list[Path]:
    target = Path(profile_path)
    if target.is_dir():
        return sorted(p for p in target.glob("*.yaml") if p.is_file())
    return [target] if target.exists() else []


def _entry(path: Path) -> Entry:
    data = profile_form.load(path)
    names = []
    for item in data.get("queries") or []:
        text = item.get("text", "") if isinstance(item, dict) else item
        if str(text).strip():
            names.append(str(text).strip())
    geo = data.get("geo") or {}
    areas = ", ".join(profile_form.area_label(code) for code in (geo.get("areas") or []))
    return Entry(
        id=path.stem,
        path=path,
        title=str(data.get("title") or "").strip() or path.stem.replace("_", " "),
        # Ключа может не быть вовсе: старые профили писались до выключателя.
        enabled=bool(data.get("enabled", True)),
        queries=tuple(names),
        areas=areas or "не задано",
        min_score=str(data.get("min_score", "45")),
    )


def entries(profile_path: str | Path) -> list[Entry]:
    return [_entry(path) for path in files(profile_path)]


def resolve(profile_path: str | Path, pid: str) -> Path:
    """Файл профиля по идентификатору из браузера.

    Пустой id — первый включённый профиль: страница должна открываться и без
    выбора. Незнакомый id — ошибка, а не «молча взяли первый»: иначе правки
    уедут не в тот файл.
    """
    found = files(profile_path)
    if not found:
        return Path(profile_path)
    if pid:
        for path in found:
            if path.stem == pid:
                return path
        raise KeyError("профиль {} не найден".format(pid))
    for path in found:
        if _entry(path).enabled:
            return path
    return found[0]


def slug(name: str) -> str:
    out = SLUG_DROP.sub("-", name.strip().lower()).strip("-")
    return out[:40]


def _catalog(profile_path: str | Path) -> tuple[Path, str]:
    """Каталог профилей. Если его нет — заводим и переносим одиночный файл.

    Момент, когда владелец жмёт «Добавить профиль», — единственный, когда
    понятно, что одного файла мало. Просить его после этого руками править
    RUN_PROFILE в настройках — лишний шаг, поэтому каталог заводится сам, а
    старый profile.yaml остаётся на месте нетронутым.
    """
    target = Path(profile_path)
    if target.is_dir():
        return target, ""
    folder = target.parent / DEFAULT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    moved = folder / (target.name if target.suffix else target.name + ".yaml")
    if target.exists() and not moved.exists():
        shutil.copy2(target, moved)
    settings.save({"RUN_PROFILE": str(folder)})
    log.info("профили переехали в каталог %s, RUN_PROFILE обновлён", folder)
    return folder, (
        "Профилей стало больше одного: они лежат в {} и это записано в "
        "настройку «Файл или каталог профилей». Старый {} остался на месте."
    ).format(folder, target)


def create(profile_path: str | Path, name: str) -> tuple[str, str, str]:
    """Новый профиль. Возвращает (id, новый путь к профилям, сообщение)."""
    pid = slug(name)
    if not pid:
        return "", str(profile_path), "Не задано имя профиля."
    folder, note = _catalog(profile_path)
    path = folder / (pid + ".yaml")
    if path.exists():
        return pid, str(folder), "Профиль {} уже есть.".format(pid)
    # Новый профиль пустой, а не копия соседнего: копия выглядит настроенной,
    # и её легко забыть поправить — тогда два профиля соберут одно и то же.
    profile_form.save(
        path,
        {
            "title": name.strip(),
            "enabled": True,
            "queries": [],
            "skills": [],
            "stop_words": [],
            "min_score": 45,
        },
    )
    return pid, str(folder), (note + " " if note else "") + "Профиль «{}» создан: осталось задать запросы и стек.".format(name.strip())


def toggle(profile_path: str | Path, pid: str, on: bool) -> str:
    path = resolve(profile_path, pid)
    data = profile_form.load(path)
    data["enabled"] = on
    profile_form.save(path, data)
    return "Профиль «{}» {}.".format(
        _entry(path).title, "участвует в прогоне" if on else "выключен"
    )


CARD = (
    '<div class="card{off}">'
    "<div class=cardtop><b>{title}</b>{flag}</div>"
    "<div class=muted>{queries}</div>"
    "<div class=muted>{areas} · порог {score}</div>"
    "<div class=cardbtns>"
    '<a class=chip href="/profile?id={pid}">Настроить</a>'
    '<form class=inline method=post action="/profiles/toggle">'
    '<input type=hidden name="id" value="{pid}">'
    '<input type=hidden name="on" value="{next_on}">'
    "<button class=secondary>{action}</button></form>"
    "</div></div>"
)


def render_cards(profile_path: str | Path, note: str = "") -> str:
    """Стартовый экран раздела: все профили блоками."""
    found = entries(profile_path)
    parts = [note] if note else []
    if not found:
        parts.append(
            "<div class=warn>Профилей нет: {} не найден. Создай первый ниже.</div>".format(
                esc(str(profile_path))
            )
        )
    live = sum(1 for e in found if e.enabled)
    parts.append(
        "<p class=muted>Профиль — это весь набор критериев: запросы, города, "
        "вилка, стек, стоп-слова и порог. Каждый включённый профиль — отдельный "
        "обход hh.ru; вакансия, подошедшая двум, остаётся одной записью. "
        "Сейчас включено {live} из {total}.</p>".format(live=live, total=len(found))
    )
    cards = []
    for item in found:
        cards.append(
            CARD.format(
                off="" if item.enabled else " off",
                title=esc(item.title),
                flag="" if item.enabled else '<span class="warn">выключен</span>',
                queries=esc(", ".join(item.queries) or "запросы не заданы"),
                areas=esc(item.areas),
                score=esc(item.min_score),
                pid=esc(item.id),
                next_on="0" if item.enabled else "1",
                action="Выключить" if item.enabled else "Включить",
            )
        )
    parts.append('<div class=cards>{}</div>'.format("".join(cards)))
    parts.append(
        '<form method=post action="/profiles/new" class=addrow>'
        '<input type=text name="name" placeholder="например, аналитик данных" '
        'maxlength="60" required>'
        "<button>Добавить профиль</button></form>"
    )
    return "".join(parts)


__all__ = (
    "DEFAULT_DIR",
    "Entry",
    "create",
    "entries",
    "files",
    "is_catalog",
    "render_cards",
    "resolve",
    "slug",
    "toggle",
)
