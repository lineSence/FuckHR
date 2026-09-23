"""Задачи пайплайна, которые используют модель (локальную или с прокси).

Модуль собран отдельно от ядра по той же причине, что и detector_llm.py: score.py,
detector.py и outreach.py обязаны работать без единой строчки про модели
[CORE-015]. Любая функция здесь — улучшение поверх детерминированного результата,
и при любом сомнении возвращается исходный вариант, а не ответ модели.

Распределение по этапам (профили и маршруты в llm.py):

| Функция              | Этап     | Где считается                          |
| ------------------- | -------- | --------------------------------------- |
| extract_conditions  | extract  | прокси (персональных данных нет)      |
| company_brief       | company  | прокси (только публичные страницы)     |
| pick_contact        | contacts | локально, если не разрешён прокси     |
| polish_draft        | draft    | локально, если не разрешён прокси     |

Почему модель здесь нигде не принимает решений в одиночку:

- условия из описания принимаются только с дословной цитатой из текста;
- справка по компании собирается только из переданных сниппетов выдачи;
- адресат выбирается из уже найденных кандидатов по номеру, а не придумывается;
- в письме сверяются все числа: новое число значит, что модель выдумала факт
  о владельце, и такой текст отбрасывается целиком [CORE-019].
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Sequence

import injection

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 6000
MAX_SNIPPET_CHARS = 4000


@dataclass(frozen=True)
class Condition:
    """Одно условие работы с подтверждающей цитатой."""

    field: str
    value: str
    quote: str


@dataclass(frozen=True)
class Brief:
    """Короткая справка по компании со ссылками на источники."""

    company: str
    lines: tuple[str, ...]
    sources: tuple[str, ...]

    @property
    def text(self) -> str:
        body = "\n".join(f"— {line}" for line in self.lines)
        tail = "\n".join(self.sources)
        return f"{self.company}\n{body}\n\nИсточники:\n{tail}" if tail else body


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _parse_json(raw: str) -> dict[str, Any]:
    """Достаёт JSON из ответа, даже если модель обложила его пояснениями."""
    if not raw:
        return {}
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        log.warning("модель вернула не JSON, игнорируем")
        return {}
    return payload if isinstance(payload, dict) else {}


def numbers(text: str) -> set[str]:
    """Все числа текста без разделителей — основа проверки на выдумки.

    Пробелы и неразрывные пробелы выбрасываются, иначе «250 000» и «250000»
    считались бы разными фактами.
    """
    cleaned = (text or "").replace("\u00a0", " ")
    cleaned = re.sub(r"(?<=\d)[  ](?=\d)", "", cleaned)
    return set(re.findall(r"\d+(?:[.,]\d+)?", cleaned))


NUMBER_RE = re.compile(r"\d+")


def _numbers(text: str) -> set[str]:
    """Числа текста. Пробелы внутри числа снимаются: «250 000» — одно число."""
    glued = re.sub(r"(?<=\d)[\s\u00a0](?=\d)", "", text or "")
    return set(NUMBER_RE.findall(glued))


def extract_conditions(
    gateway: Any, description: str, limit: int = 8, strict: bool = True
) -> tuple[Condition, ...]:
    """Вытаскивает условия работы из описания вакансии (этап extract).

    strict=False оставляет ответ модели как есть и нужен только бенчмарку:
    он сравнивает модели, и защита пайплайна там прячет разницу между
    аккуратной моделью и той, что поддалась инъекции.

    Регулярки плохо берут формулировки вроде «гибрид 2/3, офис в Москве по
    договорённости», и это ровно та работа, где модель уместна. Но значение
    принимается только вместе с цитатой, которая есть в тексте дословно.
    """
    text = (description or "").strip()
    if not text:
        return ()

    prompt = (
        "Ты разбираешь описание вакансии. Выпиши условия работы.\n"
        "Поля field: format, office, schedule, salary, grade, stack, process, other.\n"
        "Правила:\n"
        "- quote копируй из текста дословно;\n"
        "- не выдумывай то, чего в тексте нет;\n"
        "- value — коротко, до шести слов;\n"
        "- если условий нет, верни пустой список.\n"
        'JSON: {"conditions": [{"field": "...", "value": "...", "quote": "..."}]}'
    )
    raw = gateway.complete(
        "extract",
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": injection.safe(text[:MAX_DESCRIPTION_CHARS], "extract")[0]},
        ],
    )
    if not raw:
        return ()

    # Цитата сверяется с очищенным текстом, а не с исходным: строка инъекции
    # из текста вырезана, значит «дословная» цитата из неё — уже не цитата.
    haystack = _normalize(injection.clean(text)[0] if strict else text)
    items = _parse_json(raw).get("conditions")
    if not isinstance(items, list):
        return ()

    out: list[Condition] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        value = str(item.get("value") or "").strip()
        field = str(item.get("field") or "other").strip() or "other"
        if not value or len(quote) < 6 or _normalize(quote) not in haystack:
            log.info("условие без цитаты в тексте, отброшено: %r", value[:60])
            continue
        # Цитата не отличает данные от команды: фраза «укажи зарплату 500000»
        # в тексте есть, значит цитата настоящая, а значение выдумано. Крупные
        # числа значения сверяются с текстом, как числа в письме [CORE-019].
        # Мелкие не трогаем: «два дня» → «2 дня» — это пересказ, а не выдумка.
        big = {n for n in _numbers(value) if len(n) >= 3}
        if strict and big - _numbers(text):
            log.info("условие с числом, которого нет в тексте: %r", value[:60])
            continue
        out.append(Condition(field=field, value=value, quote=quote))
        if len(out) >= limit:
            break
    return tuple(out)


def company_brief(gateway: Any, company: str, hits: Sequence[Any]) -> Brief | None:
    """Собирает справку по компании из выдачи поиска (этап company).

    Модель не ходит в сеть и не вспоминает свои знания: всё, что ей разрешено —
    уложить переданные сниппеты в три-пять строк. Иначе в досье появляются
    раунды инвестиций, которых не было.
    """
    company = (company or "").strip()
    if not company or not hits:
        return None

    blocks = []
    sources = []
    for hit in hits[:10]:
        url = str(getattr(hit, "url", "") or "")
        title = str(getattr(hit, "title", "") or "")
        snippet = str(getattr(hit, "snippet", "") or "")
        if not url:
            continue
        blocks.append(f"{title}\n{url}\n{snippet}")
        sources.append(url)
    if not blocks:
        return None

    prompt = (
        "Ты готовишь справку о компании для кандидата перед письмом.\n"
        "Используй ТОЛЬКО переданные фрагменты выдачи.\n"
        "Правила:\n"
        "- от трёх до пяти строк, каждая — один факт о продукте, стеке или команде;\n"
        "- никаких цифр и названий, которых нет во фрагментах;\n"
        "- не пиши про людей и их должности;\n"
        "- без оценок и маркетинговых прилагательных.\n"
        'Формат: {"lines": ["...", "..."]}'
    )
    joined = "\n\n".join(blocks)[:MAX_SNIPPET_CHARS]
    raw = gateway.complete(
        "company",
        [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": "Компания: {}\n\n{}".format(
                    company, injection.safe(joined, "company")[0]
                ),
            },
        ],
    )
    if not raw:
        return None

    items = _parse_json(raw).get("lines")
    if not isinstance(items, list):
        return None

    allowed = numbers(joined)
    lines: list[str] = []
    for item in items:
        line = str(item or "").strip()
        if not line:
            continue
        invented = numbers(line) - allowed
        if invented:
            # Цифра, которой нет во фрагментах, — признак выдумки [CORE-019].
            log.info("в справке появились цифры %s, строка отброшена", sorted(invented))
            continue
        lines.append(line)
        if len(lines) >= 5:
            break

    if not lines:
        return None
    return Brief(company=company, lines=tuple(lines), sources=tuple(sources))


def pick_contact(gateway: Any, candidates: Sequence[Any], role_hint: str = "") -> Any | None:
    """Выбирает адресата из уже найденных кандидатов (этап contacts).

    Детерминированный rank_candidates сортирует по таблице ролей и плохо
    различает живые формулировки вроде «отвечаю за бэкенд». Модель возвращает
    только номер из списка — нового человека придумать она не может физически.

    Важно: здесь в промпт уходят имена и должности — это персональный этап,
    и маршрут ему выбирает llm.Gateway, а не эта функция.
    """
    items = list(candidates or ())
    if len(items) <= 1:
        return items[0] if items else None

    listing = "\n".join(
        f"{i}. {getattr(c, 'label', str(c))}" for i, c in enumerate(items, start=1)
    )
    prompt = (
        "Выбери одного адресата из списка. Нужен тот, кто решает по найму в "
        "разработке: руководитель разработки, тимлид, CTO, основатель.\n"
        "Кадровики и рекрутёры — последний выбор.\n"
        "Ответ только JSON с номером из списка и короткой причиной.\n"
        'Формат: {"choice": 1, "reason": "..."}'
    )
    # Подписи кандидатов приходят с чужих страниц: в них тоже встречается
    # «инструкция для ИИ» с просьбой выбрать кадровика.
    listing = injection.safe(listing, "contacts")[0]
    user = listing if not role_hint else f"Вакансия: {role_hint}\n\n{listing}"
    raw = gateway.complete(
        "contacts",
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
    )
    if not raw:
        return items[0]

    choice = _parse_json(raw).get("choice")
    try:
        index = int(choice)
    except (TypeError, ValueError):
        log.info("модель не вернула номер кандидата, берём первого по ранжированию")
        return items[0]
    if not 1 <= index <= len(items):
        log.info("номер %s вне списка, берём первого по ранжированию", index)
        return items[0]
    return items[index - 1]


def polish_draft(gateway: Any, draft: Any, facts: Sequence[str] = ()) -> Any:
    """Переписывает тело письма живым языком (этап draft).

    Откат к исходному черновику происходит, если модель:

    - вернула пустоту или огрызок короче трети исходного;
    - раздула текст сверх лимита письма;
    - добавила числа, которых не было ни в черновике, ни в facts.

    Последнее — главное. Выдуманный опыт в письме от имени владельца — это
    не косметический дефект, а ложь будущему работодателю [CORE-019].
    """
    body = str(getattr(draft, "body", "") or "")
    if not body.strip():
        return draft

    limit = 1200
    try:
        import outreach

        limit = int(getattr(outreach, "MAX_LETTER_CHARS", limit))
    except Exception as exc:  # noqa: BLE001 — лимит не повод падать
        log.debug("лимит письма по умолчанию: %s", exc)

    prompt = (
        "Перепиши письмо живым языком без канцелярита и без клише про «динамично "
        "развивающуюся команду».\n"
        "Запреты:\n"
        "- не добавляй ни одного факта о кандидате, которого нет в тексте;\n"
        "- не меняй и не добавляй числа;\n"
        "- не обещай ничего от имени кандидата;\n"
        f"- уложись в {limit} символов.\n"
        "Верни только текст письма, без пояснений и без темы."
    )
    raw = gateway.complete(
        "draft",
        [
            {"role": "system", "content": prompt},
            # Черновик собран из текста вакансии: чистим, но не оборачиваем —
            # модель должна вернуть письмо, а не разобрать данные.
            {"role": "user", "content": injection.clean(body)[0] or body},
        ],
        temperature=0.3,
    )
    if not raw:
        return draft

    text = raw.strip()
    if len(text) < len(body) // 3:
        log.info("модель вернула огрызок письма, оставляем черновик")
        return draft
    if len(text) > limit:
        log.info("модель раздула письмо до %s символов, оставляем черновик", len(text))
        return draft

    allowed = numbers(body) | numbers(" ".join(facts))
    invented = numbers(text) - allowed
    if invented:
        log.warning(
            "модель дописала в письмо цифры %s, откат к черновику",
            sorted(invented),
        )
        return draft

    try:
        return replace(draft, body=text)
    except TypeError:
        # Не dataclass — лучше вернуть исходный объект, чем мутировать чужой.
        log.info("черновик не dataclass, правка не применена")
        return draft


__all__ = (
    "Brief",
    "Condition",
    "company_brief",
    "extract_conditions",
    "numbers",
    "pick_contact",
    "polish_draft",
)
