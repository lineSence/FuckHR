"""Схема profile.yaml: типы, опечатки и явные несуразности.

Зачем отдельный файл. Профиль читают трое: скоринг (score.py), форма в
интерфейсе (profile_form.py) и проверка противоречий резюме (resume.py).
Представление о «правильном профиле» должно быть одно, иначе валидность файла
зависит от того, кто его открыл.

Что ошибка, а что предупреждение ([CORE-017]):

- неверный тип (queries строкой, min_score словарём) — ProfileError, прогон не
  начинается. Скоринг по неправильно понятому профилю хуже отсутствия скоринга:
  карточки выглядят настоящими, а фильтр работал не по тем правилам;
- незнакомый ключ, сумма весов не 100, ни одного запроса, неизвестный id опыта —
  предупреждение в лог. Это личный файл владельца, и он вправе держать его
  недозаполненным.

Опечатка в ключе — главный случай, ради которого файл появился. `min_scores`
вместо `min_score` раньше молча давал порог по умолчанию: YAML читался, ошибки
не было, фильтр тихо работал не так, как написано в файле. Теперь в логе видно
и сам ключ, и на какой известный он похож.

Неизвестные ключи не удаляются и не переписываются: файл правится и руками, и
формой, и удалять из него непонятное — не наше дело.
"""

from __future__ import annotations

import difflib
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

import profile_fields


class ProfileError(ValueError):
    """Профиль нельзя понять. Текст многострочный и годится и в лог, и в UI."""


def _as_str_list(value: Any) -> Any:
    """Одно слово строкой вместо списка — обычная ручная правка, не ошибка."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return value


def _as_int_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, (int, str)):
        return [value]
    return value


StrList = Annotated[list[str], BeforeValidator(_as_str_list)]
IntList = Annotated[list[int], BeforeValidator(_as_int_list)]


class Salary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    min_net: int = 0
    currency: str = "RUR"
    allow_missing: bool = True


class Geo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    areas: IntList = []
    remote_ok: bool = True


class Query(BaseModel):
    """Один обход hh.ru. area — число или список регионов, как принимает hh."""

    model_config = ConfigDict(extra="ignore")

    text: str = ""
    area: int | IntList | None = None
    period: int = 7
    max_pages: int = 0  # 0 — до конца выдачи
    extra: dict[str, Any] | None = None


class ProfileSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Имя и выключатель нужны там, где профилей несколько (ADR-023): имя
    # подписывает карточку, выключатель убирает профиль из прогона, не удаляя
    # файл. Пустое имя — берётся имя файла, поэтому старые профили не трогаем.
    title: str = ""
    enabled: bool = True
    queries: list[Query] = []
    skills: StrList = []
    nice_to_have: StrList = []
    stop_words: StrList = []
    salary: Salary = Salary()
    geo: Geo = Geo()
    experience_ok: StrList = []
    weights: dict[str, int] = {}
    importance: dict[str, int] = {}
    min_score: float = 45.0
    facts: StrList = []


KNOWN_TOP: tuple[str, ...] = tuple(ProfileSchema.model_fields)
KNOWN_SALARY: tuple[str, ...] = tuple(Salary.model_fields)
KNOWN_GEO: tuple[str, ...] = tuple(Geo.model_fields)
KNOWN_QUERY: tuple[str, ...] = tuple(Query.model_fields)
EXPERIENCE_IDS: tuple[str, ...] = tuple(code for code, _ in profile_fields.EXPERIENCE)
WEIGHT_KEYS: tuple[str, ...] = tuple(key for key, _ in profile_fields.WEIGHTS)


def _unknown_keys(raw: Any, known: tuple[str, ...], where: str) -> list[str]:
    """Незнакомые ключи с подсказкой на похожий известный."""
    if not isinstance(raw, dict):
        return []
    notes: list[str] = []
    for key in raw:
        name = str(key)
        if name in known:
            continue
        near = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
        hint = ", похоже на «{}»".format(near[0]) if near else ""
        notes.append("{}незнакомый ключ «{}» игнорируется{}".format(where, name, hint))
    return notes


def warnings(raw: Any) -> list[str]:
    """Всё, о чём стоит сказать, но из-за чего не стоит останавливать прогон."""
    if not isinstance(raw, dict):
        return []

    notes = _unknown_keys(raw, KNOWN_TOP, "")
    notes += _unknown_keys(raw.get("salary"), KNOWN_SALARY, "salary: ")
    notes += _unknown_keys(raw.get("geo"), KNOWN_GEO, "geo: ")

    queries = raw.get("queries")
    if isinstance(queries, list):
        for number, query in enumerate(queries, start=1):
            notes += _unknown_keys(query, KNOWN_QUERY, "запрос {}: ".format(number))
            if isinstance(query, dict) and not str(query.get("text") or "").strip():
                notes.append("запрос {}: пустой text, обход пропустит его".format(number))
    if not queries:
        notes.append("ни одного запроса — сбор ничего не найдёт")

    weights = raw.get("weights")
    if isinstance(weights, dict) and weights:
        notes += _unknown_keys(weights, WEIGHT_KEYS, "weights: ")
        numbers = []
        for value in weights.values():
            try:
                numbers.append(float(value))
            except (TypeError, ValueError):
                numbers = []
                break
        total = sum(numbers)
        if numbers and abs(total - 100.0) > 0.5:
            notes.append(
                "сумма весов {:.0f}, а не 100 — баллы будут несравнимы с порогом".format(total)
            )

    experience = raw.get("experience_ok")
    if isinstance(experience, (list, tuple)):
        for item in experience:
            name = str(item)
            if name not in EXPERIENCE_IDS:
                near = difflib.get_close_matches(name, EXPERIENCE_IDS, n=1, cutoff=0.6)
                hint = ", похоже на «{}»".format(near[0]) if near else ""
                notes.append(
                    "experience_ok: «{}» не id опыта с hh.ru{}".format(name, hint)
                )

    try:
        score = float(raw.get("min_score", 45))
    except (TypeError, ValueError):
        score = 45.0
    if not 0.0 <= score <= 100.0:
        notes.append(
            "min_score {:.0f} вне диапазона 0–100: порог недостижим или бесполезен".format(score)
        )

    return notes


def _message(source: str, exc: ValidationError) -> str:
    lines = ["{}: профиль не читается".format(source)]
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc") or ()) or "корень файла"
        lines.append("  {}: {}".format(where, error.get("msg")))
    return "\n".join(lines)


def validate(
    raw: Any, source: str = "profile.yaml"
) -> tuple[ProfileSchema, list[str]]:
    """Разобранный профиль и список предупреждений.

    Бросает ProfileError, если типы не сходятся: это правится только руками.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProfileError(
            "{}: ожидался список настроек вида «ключ: значение», а получился {}".format(
                source, type(raw).__name__
            )
        )
    try:
        data = ProfileSchema.model_validate(raw)
    except ValidationError as exc:
        raise ProfileError(_message(source, exc)) from exc
    return data, warnings(raw)


__all__ = (
    "Geo",
    "ProfileError",
    "ProfileSchema",
    "Query",
    "Salary",
    "validate",
    "warnings",
)
