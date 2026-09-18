"""Тесты резюме (B-01).

Проверяется не «работает ли вообще», а четыре места, где ошибка стоит дорого:

1. Стаж при параллельных работах. Сумма периодов даёт вдвое больший опыт —
   ровно тот обман, который мы собираемся ловить у других.
2. Неподтверждённое не уходит наружу. Выдуманный опыт в письме работодателю —
   не косметика, а ложь [CORE-019].
3. Модель не дописывает числа. Новое число — это выдуманный стаж или проценты.
4. Версия под вакансию кэшируется. Без этого автосборка съедает бюджет вызовов
   на повторно увиденных вакансиях [CORE-016].

Шлюз везде поддельный: тесты в сеть не ходят.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import resume
import resume_llm


class FakeGateway:
    """Шлюз с заранее заданными ответами и счётчиком вызовов."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    def complete(self, stage: str, messages, temperature: float = 0.0) -> str | None:
        self.calls.append(stage)
        if not self.answers:
            return None
        return self.answers.pop(0)


def test_стаж_не_суммирует_пересекающиеся_периоды() -> None:
    items = [
        resume.Block(
            id=1,
            section="experience",
            position=0,
            heading="Основная работа",
            body="бэкенд",
            started="2021-01",
            finished="2021-12",
        ),
        resume.Block(
            id=2,
            section="experience",
            position=1,
            heading="Подработка в те же годы",
            body="тот же стек",
            started="2021-06",
            finished="2022-06",
        ),
    ]

    months = resume.experience_months(items, today=date(2026, 1, 1))

    # Январь 2021 — июнь 2022 включительно. Сумма периодов дала бы 25.
    assert months == 18


def test_стаж_без_даты_окончания_считается_до_сегодня() -> None:
    items = [
        resume.Block(
            id=1,
            section="experience",
            position=0,
            heading="Сейчас",
            body="работаю",
            started="2025-01",
            finished="",
        )
    ]

    assert resume.experience_months(items, today=date(2025, 12, 1)) == 12


def test_экспорт_не_берёт_неподтверждённое(conn: sqlite3.Connection) -> None:
    resume_id = resume.get_or_create(conn)
    resume.add_block(
        conn, resume_id, "summary", body="Мои слова", source=resume.SOURCE_OWNER
    )
    ai_id = resume.add_block(
        conn, resume_id, "skills", body="Предложено моделью", source=resume.SOURCE_AI
    )

    markdown = resume.export(conn, resume_id)
    assert "Мои слова" in markdown
    assert "Предложено моделью" not in markdown

    # После подтверждения текст становится собственностью владельца и уходит наружу.
    resume.confirm_block(conn, ai_id)
    assert "Предложено моделью" in resume.export(conn, resume_id)


def test_блок_от_модели_всегда_ждёт_подтверждения(conn: sqlite3.Connection) -> None:
    resume_id = resume.get_or_create(conn)
    # Вызывающая сторона настаивает на confirmed=True — модуль ей не верит.
    block_id = resume.add_block(
        conn,
        resume_id,
        "summary",
        body="Текст от модели",
        source=resume.SOURCE_AI,
        confirmed=True,
    )

    block = next(b for b in resume.blocks(conn, resume_id) if b.id == block_id)
    assert block.pending is True
    assert block.by_ai is True


def test_факты_с_цифрами_идут_первыми(conn: sqlite3.Connection) -> None:
    resume_id = resume.get_or_create(conn)
    resume.add_block(
        conn,
        resume_id,
        "experience",
        body="Ответственный и обучаемый сотрудник\n"
        "Сократил сборку с 40 до 6 минут",
    )

    facts = resume.facts(conn)

    assert facts
    assert "40" in facts[0]


def test_факты_берутся_только_из_подтверждённого(conn: sqlite3.Connection) -> None:
    resume_id = resume.get_or_create(conn)
    resume.add_block(
        conn,
        resume_id,
        "experience",
        body="Модель придумала рост конверсии на 30 процентов",
        source=resume.SOURCE_AI,
    )

    assert resume.facts(conn) == ()


def test_черновик_секции_отбрасывается_если_модель_дописала_числа() -> None:
    gateway = FakeGateway(
        json.dumps(
            {
                "text": "Вёл команду из 7 человек и ускорил сборку на 40 процентов",
                "added": [],
            },
            ensure_ascii=False,
        )
    )

    result = resume_llm.draft_section(
        gateway, "experience", "Вёл бэкенд платёжей, писал сервисы на Python"
    )

    assert result is None


def test_черновик_секции_принимается_без_новых_чисел() -> None:
    gateway = FakeGateway(
        json.dumps(
            {
                "text": "Развивал бэкенд платёжей на Python",
                "added": ["Развивал"],
            },
            ensure_ascii=False,
        )
    )

    result = resume_llm.draft_section(
        gateway, "experience", "Вёл бэкенд платёжей, писал сервисы на Python"
    )

    assert result is not None
    text, added = result
    assert "платёжей" in text
    assert added == ["Развивал"]


def test_без_модели_ответ_сохраняется_дословно(conn: sqlite3.Connection) -> None:
    resume_id = resume.get_or_create(conn)

    block_id = resume_llm.save_draft(
        conn, resume_id, "summary", "Бэкендер, семь лет на Python", gateway=None
    )

    block = next(b for b in resume.blocks(conn, resume_id) if b.id == block_id)
    assert block.body == "Бэкендер, семь лет на Python"
    assert block.confirmed is True
    assert block.by_ai is False


def test_ответ_владельца_сохраняется_рядом_с_черновиком(conn: sqlite3.Connection) -> None:
    resume_id = resume.get_or_create(conn)
    gateway = FakeGateway(
        json.dumps(
            {"text": "Бэкенд-разработчик на Python", "added": []}, ensure_ascii=False
        )
    )

    block_id = resume_llm.save_draft(
        conn, resume_id, "summary", "пишу на python", gateway=gateway
    )

    block = next(b for b in resume.blocks(conn, resume_id) if b.id == block_id)
    assert block.pending is True
    # Без исходного ответа подтверждение было бы формальностью.
    assert block.answer == "пишу на python"


def _наполнить(conn: sqlite3.Connection) -> int:
    resume_id = resume.get_or_create(conn)
    resume.add_block(conn, resume_id, "contacts", body="me@example.com")
    resume.add_block(conn, resume_id, "summary", body="Бэкендер на Python")
    resume.add_block(conn, resume_id, "skills", body="Python, PostgreSQL")
    resume.add_block(conn, resume_id, "experience", body="Платёжи", heading="Ромашка")
    resume.add_block(conn, resume_id, "projects", body="Парсер вакансий")
    return resume_id


def test_версия_под_вакансию_берётся_из_кэша(conn: sqlite3.Connection) -> None:
    resume_id = _наполнить(conn)
    gateway = FakeGateway(
        json.dumps({"order": [2, 4], "reason": "стек совпадает"}, ensure_ascii=False)
    )

    first = resume_llm.version_for(
        conn, resume_id, "hh-1", "Нужен Python и PostgreSQL", gateway=gateway
    )
    second = resume_llm.version_for(
        conn, resume_id, "hh-1", "Нужен Python и PostgreSQL", gateway=gateway
    )

    assert first == second
    # Второй раз модель не вызывалась: отпечаток совпал.
    assert gateway.calls == [resume_llm.STAGE_TAILOR]


def test_версия_пересчитывается_после_правки_резюме(conn: sqlite3.Connection) -> None:
    resume_id = _наполнить(conn)
    answer = json.dumps({"order": [2], "reason": "стек"}, ensure_ascii=False)
    gateway = FakeGateway(answer, answer)

    resume_llm.version_for(conn, resume_id, "hh-1", "Python", gateway=gateway)
    resume.add_block(conn, resume_id, "projects", body="Новый проект")
    resume_llm.version_for(conn, resume_id, "hh-1", "Python", gateway=gateway)

    assert len(gateway.calls) == 2


def test_без_модели_версия_равна_мастер_резюме(conn: sqlite3.Connection) -> None:
    resume_id = _наполнить(conn)

    ids = resume_llm.version_for(conn, resume_id, "hh-2", "Python", gateway=None)

    assert ids == [b.id for b in resume.blocks(conn, resume_id, confirmed_only=True)]
    saved = resume.load_version(conn, resume_id, "hh-2")
    assert saved is not None
    assert "без модели" in saved[1]


def test_версия_всегда_содержит_контакты(conn: sqlite3.Connection) -> None:
    resume_id = _наполнить(conn)
    items = resume.blocks(conn, resume_id, confirmed_only=True)
    contacts_id = next(b.id for b in items if b.section == "contacts")
    # Модель сочла контакты нерелевантными — резюме без связи бесполезно.
    gateway = FakeGateway(
        json.dumps({"order": [2, 3], "reason": "стек"}, ensure_ascii=False)
    )

    picked = resume_llm.pick_blocks(gateway, items, "Python")

    assert picked is not None
    assert contacts_id in picked[0]


def test_маркдаун_версии_не_меняет_текст_блоков(conn: sqlite3.Connection) -> None:
    resume_id = _наполнить(conn)
    items = resume.blocks(conn, resume_id, confirmed_only=True)
    chosen = [b.id for b in items if b.section in {"contacts", "skills"}]

    markdown = resume.version_markdown(conn, resume_id, chosen)

    assert "me@example.com" in markdown
    assert "Python, PostgreSQL" in markdown
    assert "Парсер вакансий" not in markdown


def test_противоречия_предупреждают_про_неподтверждённое(
    conn: sqlite3.Connection, tmp_path
) -> None:
    resume_id = resume.get_or_create(conn)
    resume.add_block(
        conn, resume_id, "summary", body="Предложено", source=resume.SOURCE_AI
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text("queries: []\nskills: []\n", encoding="utf-8")

    notes = resume.contradictions(conn, resume_id, str(profile))

    assert any("Не подтверждено" in note for note in notes)


def test_противоречия_замечают_притязания_на_лида(
    conn: sqlite3.Connection, tmp_path
) -> None:
    resume_id = resume.get_or_create(conn)
    resume.add_block(
        conn,
        resume_id,
        "experience",
        body="Первая работа",
        started="2025-01",
        finished="2025-06",
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "queries:\n  - text: тимлид python\nskills: []\n", encoding="utf-8"
    )

    notes = resume.contradictions(
        conn, resume_id, str(profile), today=date(2025, 6, 1)
    )

    assert any("тимлид" in note for note in notes)
