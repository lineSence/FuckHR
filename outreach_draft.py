"""Текст письма и карточки: сборка артефакта, который копирует владелец.

Отдельно от прогона этапа по [CORE-024]. Здесь нет ни базы, ни сети, ни
модели — только строки. Отправки нет и не будет (ADR-012, [CORE-023]).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Sequence

import contacts

MAX_LETTER_CHARS = 1200  # из справочника contact-discovery, §5

NO_FACTS_HINT = (
    "[заполни блок facts в profile.yaml — без фактов о себе письмо отправлять не стоит]"
)

# Канал «отклик на площадке»: не контакт человека, а признанное отсутствие контакта.
APPLY_CHANNEL = "apply"
NO_CONTACT_NOTE = (
    "Прямого контакта нет — отклик через площадку, письмо ниже как сопроводительное"
)


@dataclass(frozen=True)
class Draft:
    """Готовый черновик. Ничего, чего нет в данных, в нём оказаться не может."""

    subject: str
    body: str
    kind: str = "first"

    @property
    def text(self) -> str:
        return f"Тема: {self.subject}\n\n{self.body}"


def apply_candidate(row: sqlite3.Row) -> contacts.Candidate:
    """Заглушка вместо контакта: ссылка на вакансию и явная пометка в notes.

    Нужна, чтобы отсутствие контакта было записано в лог контактов как факт, а не
    исчезало из истории. role_rank остаётся худшим: такая «находка» никогда не
    должна опережать живого человека при ранжировании [OUT-001].
    """
    return contacts.Candidate(
        channel_kind=APPLY_CHANNEL,
        channel_value=row["url"] or "",
        role="отклик на площадке",
        role_rank=99,
        source_url=row["url"] or None,
        confidence="low",
        notes="прямого контакта не нашлось",
    )


def _role_address(candidate: contacts.Candidate) -> str:
    if candidate.person:
        return candidate.person.split()[0]
    if candidate.channel_kind == APPLY_CHANNEL:
        return "Здравствуйте"
    if candidate.role_rank >= 7:
        return "коллеги"
    return "здравствуйте"


def build_draft(
    row: sqlite3.Row,
    candidate: contacts.Candidate,
    facts: Sequence[str] = (),
    reason: str | None = None,
) -> Draft:
    """Собирает письмо по структуре из справочника: задача → факты → шаг → выход.

    Никакой «увлечённости миссией» и никаких достижений, которых нет в резюме
    или profile.yaml.
    """
    title = (row["title"] or "ваша вакансия").strip()
    generic = candidate.channel_kind == APPLY_CHANNEL

    lines = [f"{_role_address(candidate)}, пишу про вакансию «{title}»."]
    lines.append(
        "Могу закрывать задачи по этому стеку без разгона: "
        + (facts[0] if facts else NO_FACTS_HINT)
    )
    if len(facts) > 1:
        lines.append(facts[1])
    if reason:
        lines.append(f"Повод писать именно вам: {reason}")
    lines.append(
        "Если задача актуальна — готов на 20 минут разговора или пришлю код по близкой задаче."
    )
    if generic:
        # Просить переадресации у отклика бессмысленно: его читает не тот, кто наймёт.
        lines.append(
            "Если удобнее обсудить голосом — напишите, в какое время созвониться."
        )
    else:
        lines.append("Если найм в эту команду — не ваша зона, подскажите, кто её ведёт.")

    body = "\n\n".join(lines)
    if len(body) > MAX_LETTER_CHARS:
        body = body[: MAX_LETTER_CHARS - 1].rstrip() + "…"
    subject = (
        f"{title} — сопроводительное" if generic else f"{title} — напрямую, без HR-воронки"
    )
    return Draft(subject=subject, body=body)


def build_follow_up(row: sqlite3.Row, draft: Draft) -> Draft:
    """Единственный разрешённый второй контакт. Третьего не будет [OUT-004]."""
    title = (row["title"] or "вакансию").strip()
    body = (
        f"Добрый день. Писал неделю назад про «{title}» — поднимаю ветку один раз.\n\n"
        "Если задача закрыта или неактуальна — ответ не нужен, больше не потревожу."
    )
    return Draft(subject=f"Re: {draft.subject}", body=body, kind="follow_up")


def format_card(
    row: sqlite3.Row,
    discovery: contacts.Discovery,
    draft: Draft | None,
    signal_lines: Sequence[str] = (),
    condition_lines: Sequence[str] = (),
) -> str:
    """Карточка из справочника, §4. Плайн-текст: уходит без parse_mode."""
    parts = [
        f"Вакансия: {row['title']}, {row['company'] or 'компания не указана'}",
        f"Скоринг: {row['score']:.0f}/100",
    ]
    if condition_lines:
        parts.append("Условия:\n" + "\n".join(condition_lines))
    if signal_lines:
        parts.append("HR-флаги:\n" + "\n".join(signal_lines))

    generic = any(c.channel_kind == APPLY_CHANNEL for c in discovery.candidates)
    if generic:
        parts.append(NO_CONTACT_NOTE)
    else:
        parts.extend(contacts.format_contact_lines(discovery))
    for reason in discovery.dropped[:3]:
        parts.append(f"Отброшено: {reason}")
    if draft is not None:
        label = "Сопроводительное" if generic else "Черновик письма"
        parts.append(f"{label} (отправляешь сам):\n" + draft.text)
    parts.append(f"Ссылка: {row['url']}")
    return "\n\n".join(parts)
