"""Пайплайн с моделью и без неё.

Зачем фейковый шлюз, а не живая модель: прогон по сотне вакансий с
генерацией идёт минутами и каждый раз даёт разный текст. Проверять надо не
качество формулировок, а стыки: вызвали ли нужные этапы, доехал ли ответ
до базы и карточки, и работает ли всё без модели вообще [CORE-017].
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

import conditions
import llm_tasks
import outreach

DESCRIPTION = (
    "Ищем Python-разработчика. Гибрид два дня в офисе в Москве, "
    "стек FastAPI и PostgreSQL. Резюме на cto@romashka.ru или lead@romashka.ru, "
    "общие вопросы — hr@romashka.ru."
)


def _row(**values: Any) -> sqlite3.Row:
    data: dict[str, Any] = {
        "key": "hh:1",
        "title": "Python-разработчик",
        "company": "ООО Ромашка",
        "url": "https://hh.ru/vacancy/1",
        "description": DESCRIPTION,
        "score": 72.0,
    }
    data.update(values)
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    columns = ", ".join('? AS "{}"'.format(name) for name in data)
    return memory.execute("SELECT " + columns, tuple(data.values())).fetchone()


class FakeGateway:
    """Ответы по этапам и журнал вызовов. Совместим с llm.Gateway.complete."""

    def __init__(self, **answers: str) -> None:
        self.answers = answers
        self.stages: list[str] = []
        self.prompts: list[str] = []

    def complete(
        self, stage: str, messages: Sequence[dict[str, str]], temperature: float = 0.0
    ) -> str:
        self.stages.append(stage)
        self.prompts.append(messages[-1]["content"])
        return self.answers.get(stage, "")


@dataclass(frozen=True)
class FakeHit:
    title: str
    url: str
    snippet: str


class FakeProvider:
    """Внешний поиск без сети."""

    def __init__(self, hits: Sequence[FakeHit] = (), enabled: bool = True) -> None:
        self.hits = list(hits)
        self.enabled = enabled
        self.disabled_reason = "заглушка"
        self.queries: list[str] = []

    def search_many(self, queries: Sequence[str], limit: int = 5) -> list[FakeHit]:
        self.queries.extend(queries)
        return list(self.hits)


HITS = (
    FakeHit(
        title="ООО Ромашка — команда",
        url="https://romashka.ru/team",
        snippet="Сервис доставки, бэкенд на Python. Контакт: cto@romashka.ru",
    ),
)


def test_условия_из_описания_доезжают_до_базы_и_строк_карточки(conn) -> None:
    conditions.ensure_schema(conn)
    gateway = FakeGateway(
        extract=(
            '{"conditions": ['
            '{"field": "format", "value": "гибрид 2 дня в офисе", '
            '"quote": "Гибрид два дня в офисе в Москве"},'
            '{"field": "stack", "value": "FastAPI, PostgreSQL", '
            '"quote": "стек FastAPI и PostgreSQL"}]}'
        )
    )

    items = llm_tasks.extract_conditions(gateway, DESCRIPTION)
    assert conditions.store(conn, "hh:1", items) == 2

    lines = conditions.lines(conn, "hh:1")
    assert any("гибрид" in line.lower() for line in lines)
    assert conditions.coverage(conn)[0] == 1


def test_условие_без_цитаты_в_базу_не_попадает(conn) -> None:
    """Модель склонна дописывать то, чего в тексте нет. Такое не считается."""
    conditions.ensure_schema(conn)
    gateway = FakeGateway(
        extract=(
            '{"conditions": [{"field": "salary", "value": "до 400000", '
            '"quote": "вилка до 400000 рублей"}]}'
        )
    )

    assert llm_tasks.extract_conditions(gateway, DESCRIPTION) == ()
    assert conditions.store(conn, "hh:1", ()) == 0
    assert conditions.lines(conn, "hh:1") == []


def test_без_шлюза_этап_письма_работает_как_раньше(conn) -> None:
    """[CORE-017]: выключенная модель — штатный режим, а не отказ в обслуживании."""
    import contacts

    contacts.ensure_schema(conn)
    discovery, draft, skip = outreach.process_row(
        conn, _row(), ("сократил ответ API в 12 раз",), FakeProvider(enabled=False)
    )

    assert skip is None
    assert draft is not None
    assert discovery.candidates


def test_модель_меняет_адресата_из_найденных(conn) -> None:
    """Выбор идёт номером по списку: нового человека модель придумать не может."""
    import contacts

    contacts.ensure_schema(conn)
    base, _, _ = outreach.process_row(conn, _row(), (), FakeProvider(enabled=False))
    assert len(base.candidates) > 1, "в описании три адреса, должно быть несколько кандидатов"

    last = len(base.candidates)
    gateway = FakeGateway(contacts='{"choice": %d, "reason": "ведёт разработку"}' % last)
    discovery, draft, skip = outreach.process_row(
        conn, _row(), (), FakeProvider(enabled=False), gateway=gateway
    )

    assert skip is None
    assert "contacts" in gateway.stages
    assert discovery.candidates[0].channel_value == base.candidates[-1].channel_value
    # Выбранный становится первым, остальные не теряются.
    assert len(discovery.candidates) == len(base.candidates)
    assert draft is not None


def test_бред_вместо_номера_оставляет_первого_по_ранжированию(conn) -> None:
    import contacts

    contacts.ensure_schema(conn)
    base, _, _ = outreach.process_row(conn, _row(), (), FakeProvider(enabled=False))
    gateway = FakeGateway(contacts="сложный вопрос, надо подумать")

    discovery, _, _ = outreach.process_row(
        conn, _row(), (), FakeProvider(enabled=False), gateway=gateway
    )
    assert discovery.candidates[0].channel_value == base.candidates[0].channel_value


def test_справка_о_компании_становится_поводом_в_письме(conn) -> None:
    import contacts

    contacts.ensure_schema(conn)
    gateway = FakeGateway(
        company='{"lines": ["бэкенд сервиса доставки на Python", "есть страница команды"]}'
    )
    provider = FakeProvider(HITS)

    _, draft, skip = outreach.process_row(conn, _row(), (), provider, gateway=gateway)

    assert skip is None
    assert draft is not None
    assert "company" in gateway.stages
    assert "бэкенд сервиса доставки" in draft.body
    assert provider.queries, "поиск должен быть вызван один раз на оба этапа"


def test_правка_письма_применяется(conn) -> None:
    import contacts

    contacts.ensure_schema(conn)
    polished = (
        "Здравствуйте. Пишу про вакансию Python-разработчика. "
            "Сократил ответ API в 12 раз, готов показать код по близкой задаче. "
            "Если найм в эту команду не ваша зона — подскажите, кто её ведёт."
    )
    gateway = FakeGateway(draft=polished)

    _, draft, _ = outreach.process_row(
        conn,
        _row(),
        ("сократил ответ API в 12 раз",),
        FakeProvider(enabled=False),
        gateway=gateway,
    )

    assert draft is not None
    assert draft.body == polished
    assert "draft" in gateway.stages


def test_выдуманные_цифры_в_письме_откатываются_к_шаблону(conn) -> None:
    """Главный тест всего этапа: выдуманный опыт — ложь работодателю [CORE-019]."""
    import contacts

    contacts.ensure_schema(conn)
    facts = ("сократил ответ API в 12 раз",)
    _, plain, _ = outreach.process_row(conn, _row(), facts, FakeProvider(enabled=False))

    gateway = FakeGateway(
        draft=(
            "Здравствуйте. Пишу про вакансию Python-разработчика. "
            "15 лет опыта и команда из 40 человек под руководством. "
            "Готов показать код по близкой задаче и обсудить детали."
        )
    )
    _, draft, _ = outreach.process_row(
        conn, _row(), facts, FakeProvider(enabled=False), gateway=gateway
    )

    assert draft is not None and plain is not None
    assert draft.body == plain.body
    assert "15" not in draft.body


def test_пустой_ответ_модели_не_ломает_пайплайн(conn) -> None:
    """Модель может ответить пустотой на любом этапе — результат должен остаться."""
    import contacts

    contacts.ensure_schema(conn)
    gateway = FakeGateway()
    discovery, draft, skip = outreach.process_row(
        conn, _row(), (), FakeProvider(HITS), gateway=gateway
    )

    assert skip is None
    assert draft is not None
    assert discovery.candidates


def test_условия_показываются_в_карточке(conn) -> None:
    import contacts

    contacts.ensure_schema(conn)
    conditions.ensure_schema(conn)
    discovery, draft, _ = outreach.process_row(
        conn, _row(), (), FakeProvider(enabled=False)
    )

    card = outreach.format_card(
        _row(),
        discovery,
        draft,
        condition_lines=("формат: гибрид 2 дня в офисе",),
    )

    assert "Условия:" in card
    assert "гибрид 2 дня в офисе" in card
