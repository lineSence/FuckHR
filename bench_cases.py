"""Задачи для сравнения моделей: входы, ожидания и проверки.

Почему не «спросить модель, какая модель лучше». Единственная полезная оценка
здесь — прошла ли задача те же проверки, что стоят в пайплайне: цитата дословно
из текста, никаких новых чисел, адресат из списка. Это считается детерминированно
и одинаково для любой модели [CORE-015].

Набор намеренно маленький: половина кейсов — ловушки. Модель, которая красиво
пишет, но дорисовывает вилку, для этого проекта хуже, чем скучная и точная.

Все тексты выдуманы: бенчмарк гоняют и на внешнем прокси, персональных данных
в кейсах нет [CORE-012].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Case:
    """Одна задача: что подаём на вход и что считаем успехом."""

    name: str
    stage: str
    payload: dict[str, Any]
    expect: dict[str, Any] = field(default_factory=dict)
    trap: bool = False


@dataclass(frozen=True)
class Hit:
    """Сниппет выдачи — ровно то, что видит company_brief."""

    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class Person:
    """Кандидат в адресаты. label — то, что уходит в промпт."""

    label: str


@dataclass
class Draft:
    """Черновик письма. dataclass, потому что polish_draft делает replace()."""

    body: str


HYBRID = (
    "Ищем python-разработчика в команду биллинга. Формат гибридный: два дня в "
    "офисе на Тульской, остальное из дома. Вилка 250 000 — 320 000 на руки. "
    "Стек: FastAPI, PostgreSQL, Kafka. Оформление по ТК РФ, график 5/2."
)

NO_SALARY = (
    "Нужен backend-разработчик на Python. Работа полностью удалённая, команда "
    "из семи человек. Обсуждаем зарплату на собеседовании по итогам вилки "
    "кандидата. Стек: Django, PostgreSQL, Redis."
)

HITS = (
    Hit(
        url="https://acme.dev/about",
        title="ACME — платформа логистики",
        snippet="ACME делает платформу для планирования доставки. Команда 40 человек.",
    ),
    Hit(
        url="https://acme.dev/blog/stack",
        title="Наш стек",
        snippet="Бэкенд на Python и Go, очереди на Kafka, данные в PostgreSQL.",
    ),
    Hit(
        url="https://habr.com/acme",
        title="Как мы считаем маршруты",
        snippet="Разбор алгоритма маршрутизации и нагрузочного тестирования.",
    ),
)

PEOPLE = (
    Person("Ирина Соколова — менеджер по подбору персонала"),
    Person("Павел Дьяченко — CTO, отвечает за платформу"),
    Person("info@acme.dev — общий ящик компании"),
)

PEOPLE_TRAP = (
    Person("Анна Крылова — HR business partner, ведёт наём в IT"),
    Person("recruiting@acme.dev — почта отдела подбора"),
    Person("Сергей Мах — тимлид бэкенда, собеседует сам"),
)

LETTER = (
    "Здравствуйте! Меня зовут Алексей, я backend-разработчик с 8 годами опыта. "
    "Увидел вашу вакансию по биллингу. Делал похожий сервис: FastAPI, PostgreSQL, "
    "очереди на Kafka, нагрузка около 300 запросов в секунду. Готов за 20 минут "
    "разобрать ваш узкий участок или прислать код. Если это не ваша зона — "
    "подскажите, кто ведёт направление."
)

CASES: tuple[Case, ...] = (
    Case(
        "extract: гибрид и вилка",
        "extract",
        {"description": HYBRID},
        {"fields": ("format", "salary"), "min_count": 2},
    ),
    Case(
        "extract: вилки нет",
        "extract",
        {"description": NO_SALARY},
        {"forbidden_fields": ("salary",)},
        trap=True,
    ),
    Case(
        "company: справка по сниппетам",
        "company",
        {"company": "ACME", "hits": HITS},
        {"min_lines": 3, "forbidden_words": ("лидер", "динамично", "инвестиц")},
    ),
    Case(
        "contacts: выбрать CTO",
        "contacts",
        {"candidates": PEOPLE, "role_hint": "Python-разработчик"},
        {"choice": 2},
    ),
    Case(
        "contacts: кадровик первым",
        "contacts",
        {"candidates": PEOPLE_TRAP, "role_hint": "Python-разработчик"},
        {"choice": 3},
        trap=True,
    ),
    Case(
        "draft: переписать письмо",
        "draft",
        {"body": LETTER, "facts": ("8 лет опыта",)},
        {"changed": True},
    ),
)


def check(case: Case, result: Any) -> tuple[float, str]:
    """Оценка от 0 до 1 и короткая причина. Проверки те же, что в пайплайне."""
    if case.stage == "extract":
        return _check_extract(case, result)
    if case.stage == "company":
        return _check_company(case, result)
    if case.stage == "contacts":
        return _check_contacts(case, result)
    if case.stage == "draft":
        return _check_draft(case, result)
    return 0.0, "неизвестный этап"


def _check_extract(case: Case, result: Any) -> tuple[float, str]:
    items = tuple(result or ())
    got = {item.field for item in items}
    forbidden = set(case.expect.get("forbidden_fields", ())) & got
    if forbidden:
        return 0.0, "выдумала поле: {}".format(", ".join(sorted(forbidden)))
    wanted = tuple(case.expect.get("fields", ()))
    if not wanted:
        # Ловушка: успех — разобрать описание и при этом не дорисовать вилку.
        # Молчание тоже провал, иначе «ничего не ответила» выглядело бы победой.
        return (1.0, "чисто: {}".format(len(items))) if items else (0.0, "пусто")
    if len(items) < int(case.expect.get("min_count", 1)):
        return 0.0, "условий мало: {}".format(len(items))
    hit = len(got & set(wanted))
    return hit / len(wanted), "поля: {}".format(", ".join(sorted(got)) or "—")


def _check_company(case: Case, result: Any) -> tuple[float, str]:
    if result is None:
        return 0.0, "справки нет (или все строки отброшены)"
    lines = tuple(result.lines)
    low = " ".join(lines).lower()
    dirty = [w for w in case.expect.get("forbidden_words", ()) if w in low]
    if dirty:
        return 0.0, "маркетинг: {}".format(", ".join(dirty))
    need = int(case.expect.get("min_lines", 3))
    if len(lines) < need:
        return len(lines) / need, "строк: {}".format(len(lines))
    return 1.0, "строк: {}".format(len(lines))


def _check_contacts(case: Case, result: Any) -> tuple[float, str]:
    people = list(case.payload["candidates"])
    want = people[int(case.expect["choice"]) - 1]
    if result is None:
        return 0.0, "адресат не выбран"
    ok = getattr(result, "label", "") == want.label
    return (1.0 if ok else 0.0), getattr(result, "label", "")[:48]


def _check_draft(case: Case, result: Any) -> tuple[float, str]:
    text = str(getattr(result, "body", "") or "")
    if not text:
        return 0.0, "пусто"
    if text.strip() == case.payload["body"].strip():
        # polish_draft откатывается сам: выдуманные числа, огрызок или перебор
        # по длине. Для модели это провал задачи, а не безобидный ноль.
        return 0.0, "откат к черновику"
    return 1.0, "{} символов".format(len(text))


__all__ = ("CASES", "Case", "Draft", "Hit", "Person", "check")
