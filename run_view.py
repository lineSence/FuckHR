"""Сводка прогона для веб-интерфейса: полоски и цифры вместо ленты строк.

В терминале лента уместна: там её читают на ходу и грепают. На странице
запуска она бесполезна — четыре тысячи строк, из которых важны тридцать, и
пролистать их мышкой быстрее, чем найти в них ошибку.

Здесь те же строки разбираются в три блока: на чём прогон сейчас (полоски по
фазам), что он насчитал (последняя сводная строка каждого вида) и что пошло не
так (предупреждения, сгруппированные по смыслу). Сам лог никуда не девается —
лежит под катом и в файле.

Разбор идёт по тексту лога, а не по протоколу между процессами: задача — это
подпроцесс, и заводить ради полосок канал сообщений дороже, чем читать то, что
он и так печатает [CORE-025].
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ui_core import esc, table

# Фаза -> как её счётчик выглядит в логе. Карточки последние: их строка —
# просто «[3/98] Название — Компания», и она поймала бы остальные.
PHASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Запросы", re.compile(r"\[(\d+)/(\d+)\] запрос:")),
    ("Условия", re.compile(r"\[(\d+)/(\d+)\] модель: условия")),
    ("Утверждения", re.compile(r"\[(\d+)/(\d+)\] модель: утверждения")),
    ("Досье компаний", re.compile(r"\[(\d+)/(\d+)\] досье:")),
    ("Контакты", re.compile(r"\[(\d+)/(\d+)\] контакты:")),
    ("Адреса на карте", re.compile(r"\[(\d+)/(\d+)\] адрес:")),
    (
        "Вакансии",
        re.compile(r"\[(\d+)/(\d+)\] (?!запрос:|модель:|досье:|контакты:|адрес:)\S"),
    ),
)

# Сводные строки прогона: что показать и по какому куску её узнать. Берётся
# последняя такая строка — прогон её и переписывает по мере работы.
FACTS: tuple[tuple[str, str], ...] = (
    ("Настройки", "настройки: лимит"),
    ("Предфильтр", "увидели: "),
    ("Другие площадки", "другие площадки: увидели"),
    ("Карточки", "карточек к загрузке:"),
    ("Описания из базы", "описаний взято из базы:"),
    ("По площадкам", "по площадкам —"),
    ("Векторы", "посчитано векторов"),
    ("Очередь досье", "в очередь на досье:"),
    ("Досье", "досье: собрано в этом прогоне"),
    ("Контакты", "контакты: канал нашёлся у"),
    ("Работодатели", "оценка работодателей:"),
    ("Модель", "модель: вызовов"),
    ("К отправке", "новых вакансий:"),
    ("База", "итого в базе:"),
)

# Уровень ищется регуляркой, а не поиском « ERROR »: в консольном формате он
# стоит в самом начале строки, и подстрока с пробелом слева его не находит.
LEVEL_RE = re.compile(r"(?:^|\s)(ERROR|WARNING)\s")
# Уровень, модуль и имя потока в начале строки: «WARNING llm [llm_3] текст».
# Формат консоли и формат файла различаются временем и потоком, поэтому оба
# куска необязательные: сводку читают и из журнала, и из живой задачи.
HEAD_RE = re.compile(
    r"^\s*(?:\d{4}-\d\d-\d\d [\d:,]+ )?(?:ERROR|WARNING|INFO|DEBUG)\s+\S+\s*"
    r"(?:\[[^\]]+\]\s*)?"
)
# Заменяются только длинные числа — идентификаторы вакансий и компаний. Номер
# версии модели («gemma4:31b») и попытка «1/3» короткие и остаются собой,
# иначе сообщение перестаёт читаться.
DIGITS_RE = re.compile(r"\d{3,}")


@dataclass
class Summary:
    phases: list[tuple[str, int, int]] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)
    problems: list[tuple[str, int, bool]] = field(default_factory=list)
    warnings: int = 0
    errors: int = 0


def _message(line: str) -> str:
    """Строка без уровня и модуля: в сводке они только занимают место."""
    return HEAD_RE.sub("", line).strip()


def _kind(line: str) -> str:
    """Ключ группировки: та же беда с другим номером вакансии — одна беда."""
    return DIGITS_RE.sub("N", _message(line))[:120]


def summary(lines: list[str]) -> Summary:
    """Разбирает вывод задачи. Пустой вывод даёт пустую сводку, не исключение."""
    out = Summary()
    phases: dict[str, tuple[int, int]] = {}
    facts: dict[str, str] = {}
    problems: dict[str, tuple[int, bool]] = {}
    for line in lines:
        for label, pattern in PHASES:
            match = pattern.search(line)
            if match is None:
                continue
            phases[label] = (int(match.group(1)), int(match.group(2)))
            break
        message = _message(line)
        for label, mark in FACTS:
            if mark in line:
                facts[label] = message
                break
        level = LEVEL_RE.search(line)
        if level is not None:
            bad = level.group(1) == "ERROR"
            out.errors += 1 if bad else 0
            out.warnings += 0 if bad else 1
            key = _kind(line)
            count, was_error = problems.get(key, (0, False))
            problems[key] = (count + 1, was_error or bad)
    out.phases = [
        (label, phases[label][0], phases[label][1])
        for label, _ in PHASES
        if label in phases
    ]
    out.facts = [(label, facts[label]) for label, _ in FACTS if label in facts]
    # Сначала ошибки, потом частое: чинить надо сверху.
    out.problems = sorted(
        ((key, count, bad) for key, (count, bad) in problems.items()),
        key=lambda item: (not item[2], -item[1]),
    )
    return out


def render(data: Summary, running: bool = False) -> str:
    """HTML сводки. Порядок блоков — «где идём», «что насчитали», «что сломалось»."""
    parts: list[str] = []
    if data.phases:
        rows = []
        for label, done, total in data.phases:
            percent = int(round(100.0 * done / total)) if total else 0
            rows.append(
                '<div class=bar><b>{label}</b> '
                '<progress value="{done}" max="{total}"></progress>'
                '<span class=muted>{done} из {total} · {percent}%</span></div>'.format(
                    label=esc(label), done=done, total=total, percent=percent
                )
            )
        parts.append("".join(rows))
    elif running:
        parts.append(
            "<div class=bar><progress></progress>"
            "<span class=muted>шаги ещё не сообщались</span></div>"
        )

    if data.facts:
        parts.append("<h3>Что насчитал прогон</h3>")
        parts.append(
            table(["Что", "Значение"], [[esc(label), esc(text)] for label, text in data.facts])
        )

    if data.problems:
        rows = []
        for key, count, bad in data.problems[:10]:
            rows.append(
                [
                    "ошибка" if bad else "предупреждение",
                    esc(key),
                    str(count),
                ]
            )
        parts.append(
            "<h3>Что пошло не так — ошибок {}, предупреждений {}</h3>".format(
                data.errors, data.warnings
            )
        )
        parts.append(table(["Уровень", "Сообщение", "Раз"], rows))
    elif not running:
        parts.append("<p class=muted>Ошибок и предупреждений не было.</p>")
    return "".join(parts)


__all__ = ("FACTS", "LEVEL_RE", "PHASES", "Summary", "render", "summary")
