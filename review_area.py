"""Сфера автора отзыва: чьими глазами он написан.

Зачем. Средняя оценка крупного работодателя складывается из отзывов кассиров,
курьеров и разработчиков, а это разные вселенные. Владелец идёт в конкретную
вакансию, и сорок отзывов про недостачу и график смен не говорят ничего о том,
каково там в разработке. Задача модуля — отделить отзывы своей стороны компании
от чужой.

Оргструктуру мы не знаем и знать не можем, поэтому речь не об отделе, а о
сфере: разработка, розница, продажи, поддержка, бэк-офис. Метка определяется
словарями и порогом, а не моделью [CORE-015], и по умолчанию честно остаётся
`unknown`: сфера не определена — нормальный исход, а не ошибка.

Второй признак ортогонален первому. Задержка зарплаты, серые схемы и
сокращения — это про компанию целиком, кем бы автор ни был. Такой отзыв
помечается `scope = company` и в разбивку по сферам не идёт: иначе функция
начнёт прятать именно те красные флаги, ради которых весь проект.

О людях здесь ничего не остаётся. Должность из шапки отзыва используется как
самый сильный признак, но в базу уходит только код сферы и список сработавших
маркеров, не текст должности и не автор [CORE-012], [CORE-013].
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

AREA_UNKNOWN = "unknown"
SCOPE_AREA = "area"
SCOPE_COMPANY = "company"

AREA_RU: dict[str, str] = {
    "it": "разработка и ИТ",
    "retail": "розница и склад",
    "sales": "продажи",
    "support": "поддержка и колл-центр",
    "office": "офис и бэк-офис",
}

# Вес должности выше веса лексики: «Программист» в шапке отзыва весит больше,
# чем случайное «питон» в тексте кладовщика.
ROLE_WEIGHT = 3.0
TEXT_WEIGHT = 1.0
# Лидер обязан набрать MIN_SCORE и опередить второго на MARGIN. Иначе unknown:
# лучше промолчать, чем приписать отзыв не той сфере.
MIN_SCORE = 2.0
MARGIN = 1.0

# Маркер без пробела — корень слова, ищется со любым окончанием. Маркер с
# пробелом — фраза, ищется как есть. Каждый маркер считается один раз: иначе
# один болтливый отзыв перевешивает десять коротких.
MARKERS: dict[str, tuple[str, ...]] = {
    "it": (
        "программист", "разработчик", "тестировщик", "бэкенд", "backend",
        "фронтенд", "frontend", "девопс", "devops", "тимлид", "легаси",
        "рефакторинг", "деплой", "спринт", "джира", "jira", "микросервис",
        "продакшн", "продакшен", "релиз", "ревью", "техдолг", "питон",
        "python", "java", "javascript", "докер", "docker", "kubernetes",
        "скрам", "аджайл", "фича", "баг", "автотест",
    ),
    "retail": (
        "смена", "магазин", "касса", "кассир", "инвентаризац", "недостач",
        "склад", "кладовщик", "грузчик", "комплектовщик", "выкладк", "фасовк",
        "приемк", "товаровед", "прилавк", "логист", "торговый зал",
        "старший смены", "начальник смены",
    ),
    "sales": (
        "менеджер по продаж", "план продаж", "холодные звонк", "обзвон",
        "воронк", "crm", "срм", "сделк", "дилер", "b2b", "коммерческий отдел",
        "выполнение плана", "клиентская база",
    ),
    "support": (
        "колл центр", "коллцентр", "call центр", "оператор", "первая линия",
        "тикет", "скрипт разговора", "входящие звонк", "служба поддержк",
        "диспетчер", "чат с клиент",
    ),
    "office": (
        "бухгалтер", "кадровик", "делопроизводств", "юрист", "секретар",
        "офис менеджер", "документооборот", "отчетност", "табел", "закупк",
        "администратор офиса",
    ),
}

# Темы, которые касаются всей компании, а не сферы автора. Такой отзыв важен
# независимо от того, кем работал написавший.
WIDE_MARKERS: tuple[str, ...] = (
    "задержива", "задержка зарплат", "задержки зарплат", "не выплатил",
    "не платят", "в конверте", "серая зарплат", "черная зарплат",
    "сокращени", "сокращают", "увольняют", "массовые увольнен",
    "не по тк", "без трудового договора", "обманыва", "собственник",
    "владелец компании", "топ менеджмент", "банкрот", "задолженност",
)

# Должность в шапке отзыва: «Должность: Программист», «Работал(а) кассиром».
ROLE_RE = re.compile(
    r"(?:должность|позиция|кем\s+работал[аи]?|работал[аи]?)\s*[:\-—]?\s*"
    r"([^\n.;|]{3,60})",
    re.IGNORECASE,
)

_WORD_RE: dict[str, "re.Pattern[str]"] = {}


@dataclass(frozen=True)
class Area:
    """Сфера одного отзыва и охват его темы."""

    code: str = AREA_UNKNOWN
    scope: str = SCOPE_AREA
    score: float = 0.0
    hits: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.code != AREA_UNKNOWN

    @property
    def label(self) -> str:
        return AREA_RU.get(self.code, "сфера не определена")

    @property
    def company_wide(self) -> bool:
        return self.scope == SCOPE_COMPANY


def codes() -> tuple[str, ...]:
    return tuple(AREA_RU)


def normalize(text: str) -> str:
    """Нижний регистр, ё как е, пунктуация пробелами: маркеры ищутся по словам."""
    low = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", " ", low)


def _marker_re(marker: str) -> "re.Pattern[str]":
    pattern = _WORD_RE.get(marker)
    if pattern is None:
        body = re.escape(marker)
        pattern = re.compile(r"(?<![0-9a-zа-я])" + body + r"[а-я]*")
        _WORD_RE[marker] = pattern
    return pattern


def _found(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(m for m in markers if _marker_re(m).search(text))


def role_in(text: str) -> str:
    """Должность из шапки отзыва. Наружу не уходит: только в счёт сферы."""
    match = ROLE_RE.search(text or "")
    return match.group(1).strip() if match else ""


def classify(text: str, role: str = "") -> Area:
    """Сфера отзыва по словарям. Не уверены — unknown, и это нормально."""
    body = normalize(text)
    head = normalize(role or role_in(text))
    wide = _found(body, WIDE_MARKERS)
    scope = SCOPE_COMPANY if wide else SCOPE_AREA

    scored: list[tuple[float, str, tuple[str, ...]]] = []
    for code, markers in MARKERS.items():
        in_text = _found(body, markers)
        in_role = _found(head, markers) if head else ()
        score = len(in_text) * TEXT_WEIGHT + len(in_role) * ROLE_WEIGHT
        if score:
            scored.append((score, code, tuple(dict.fromkeys(in_role + in_text))))
    if not scored:
        return Area(scope=scope, hits=wide)
    scored.sort(key=lambda row: (-row[0], row[1]))
    best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best[0] < MIN_SCORE or best[0] - second < MARGIN:
        return Area(scope=scope, score=best[0], hits=wide)
    return Area(code=best[1], scope=scope, score=best[0], hits=best[2] + wide)


def classify_item(item: Any) -> Any:
    """Тот же отзыв с заполненными полями сферы."""
    area = classify(str(getattr(item, "text", "")), str(getattr(item, "role", "")))
    try:
        return replace(item, area=area.code, area_scope=area.scope, area_hits=area.hits)
    except TypeError:  # не dataclass или нет полей — сфера просто не считается
        return item


def owner_code(value: str | None = None) -> str:
    """Сфера владельца из настройки. Пусто или мусор — разбивки не будет."""
    if value is None:
        import settings  # noqa: PLC0415 — каталог настроек читает этот модуль

        value = settings.get("REVIEW_AREA", "")
    code = str(value).strip().lower()
    return code if code in AREA_RU else ""


def label(code: str) -> str:
    return AREA_RU.get(code, "сфера не определена")


def counts(items: Any) -> Mapping[str, int]:
    """Сколько отзывов в каждой сфере. Для интерфейса и логов."""
    out: dict[str, int] = {}
    for item in items or ():
        code = str(getattr(item, "area", "") or AREA_UNKNOWN)
        out[code] = out.get(code, 0) + 1
    return out


__all__ = (
    "AREA_RU",
    "AREA_UNKNOWN",
    "Area",
    "MARKERS",
    "SCOPE_AREA",
    "SCOPE_COMPANY",
    "WIDE_MARKERS",
    "classify",
    "classify_item",
    "codes",
    "counts",
    "label",
    "normalize",
    "owner_code",
    "role_in",
)
