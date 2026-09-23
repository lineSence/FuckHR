"""Маршрутизация шлюза и функции поверх модели — без единого HTTP-запроса.

Тесты здесь сторожат две вещи, из которых вторая важнее:

1. запрос уходит на тот адрес, который разрешён этапу;
2. ответ модели нигде не попадает в результат без сверки с исходным текстом.
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

import conditions
import llm
import llm_cache
import llm_tasks
import outreach
import websearch

LOCAL_URL = "http://localhost:3001/v1"
PROXY_URL = "http://127.0.0.1:4000/v1"


class FakeGateway:
    """Шлюз, который отдаёт заготовленный ответ и запоминает этапы."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.stages: list[str] = []

    def complete(
        self, stage: str, messages: Sequence[dict[str, str]], temperature: float = 0.0
    ) -> str | None:
        self.stages.append(stage)
        return self.answer


# --- маршрутизация ---------------------------------------------------------


def test_обычные_этапы_уходят_на_прокси() -> None:
    gateway = llm.Gateway(
        base_url=LOCAL_URL,
        proxy_base_url=PROXY_URL,
        proxy_models={llm.SMART: "claude-3-7-sonnet"},
    )
    route = gateway.route_for("hr_filter")
    assert route is not None
    assert (route.name, route.base_url, route.model) == (
        llm.ROUTE_PROXY,
        PROXY_URL,
        "claude-3-7-sonnet",
    )


def test_без_имени_модели_используется_профиль() -> None:
    # У LiteLLM можно объявить алиасы auto:fast/auto:smart и не настраивать ничего.
    gateway = llm.Gateway(proxy_base_url=PROXY_URL)
    route = gateway.route_for("extract")
    assert route is not None
    assert route.model == llm.FAST


def test_персональные_этапы_остаются_локальными() -> None:
    gateway = llm.Gateway(base_url=LOCAL_URL, proxy_base_url=PROXY_URL)
    for stage in ("contacts", "dossier", "draft"):
        route = gateway.route_for(stage)
        assert route is not None
        assert route.name == llm.ROUTE_LOCAL, stage


def test_эмбеддинги_остаются_локальными_при_настроенном_прокси() -> None:
    # Модель векторов пиннится навсегда [LLM-011], а состав моделей на прокси
    # владелец не контролирует. Плюс ollama-имя на прокси даёт 400.
    gateway = llm.Gateway(
        base_url=LOCAL_URL,
        proxy_base_url=PROXY_URL,
        proxy_models={llm.EMBEDDINGS: "text-embedding-3-large"},
    )
    route = gateway.route_for("embeddings")
    assert route is not None
    assert (route.name, route.base_url) == (llm.ROUTE_LOCAL, LOCAL_URL)


def test_эмбеддинги_без_локального_адреса_уходят_на_прокси() -> None:
    # Запасной маршрут: лучше считать на прокси, чем не считать вовсе [CORE-017].
    gateway = llm.Gateway(
        proxy_base_url=PROXY_URL, proxy_models={llm.EMBEDDINGS: "bge-m3"}
    )
    route = gateway.route_for("embeddings")
    assert route is not None
    assert (route.name, route.model) == (llm.ROUTE_PROXY, "bge-m3")


def test_отклонённая_моделью_прокси_пара_больше_не_берётся() -> None:
    # Тот же приём, что у чата: 400 по сути запроса в прогоне не исправится.
    gateway = llm.Gateway(
        proxy_base_url=PROXY_URL, proxy_models={llm.EMBEDDINGS: "bge-m3"}
    )
    gateway.reject(llm.ROUTE_PROXY, "bge-m3", "HTTP 400")
    assert gateway.route_for("embeddings") is None


def test_персональные_этапы_без_локальной_модели_пропускаются() -> None:
    # Лучше остаться без шлифовки письма, чем тихо отправить ФИО наружу.
    gateway = llm.Gateway(proxy_base_url=PROXY_URL)
    assert gateway.route_for("draft") is None
    assert gateway.complete("draft", [{"role": "user", "content": "привет"}]) is None
    assert gateway.usage.skipped == 1


def test_явное_разрешение_пускает_пд_на_прокси() -> None:
    gateway = llm.Gateway(
        base_url=LOCAL_URL, proxy_base_url=PROXY_URL, personal_via_proxy=True
    )
    route = gateway.route_for("draft")
    assert route is not None
    assert route.name == llm.ROUTE_PROXY


def test_профиль_пд_остаётся_local_only_при_любом_маршруте() -> None:
    # Маршрут и профиль — разные сущности: проверка профиля не ослабляется.
    assert llm.profile_for("draft") == llm.LOCAL


def test_кэш_разделён_по_маршрутам(conn: sqlite3.Connection) -> None:
    messages = [{"role": "user", "content": "привет"}]
    local = llm_cache.digest(llm.FAST, messages, 0.0, llm.ROUTE_LOCAL, llm.FAST)
    proxy = llm_cache.digest(llm.FAST, messages, 0.0, llm.ROUTE_PROXY, "gpt-4o-mini")
    assert local != proxy


def test_описание_маршрутов_покрывает_все_этапы() -> None:
    gateway = llm.Gateway(base_url=LOCAL_URL, proxy_base_url=PROXY_URL)
    rows = gateway.describe_routes()
    assert {r[0] for r in rows} == set(llm.STAGE_PROFILES)
    assert dict((r[0], r[2]) for r in rows)["draft"] == llm.ROUTE_LOCAL


# --- условия из описания -------------------------------------------------


DESCRIPTION = (
    "Ищем python-разработчика. Гибрид: два дня в офисе на Тульской, "
    "остальное из дома. Стек: FastAPI и PostgreSQL."
)


def test_условия_без_цитаты_в_тексте_отбрасываются() -> None:
    answer = """
    {"conditions": [
      {"field": "format", "value": "гибрид 2 дня", "quote": "два дня в офисе на Тульской"},
      {"field": "salary", "value": "до 400000", "quote": "зарплата до 400000 рублей"}
    ]}
    """
    got = llm_tasks.extract_conditions(FakeGateway(answer), DESCRIPTION)
    # Второе условие модель выдумала целиком — его быть не должно.
    assert [c.field for c in got] == ["format"]
    assert got[0].value == "гибрид 2 дня"


def test_поле_которого_не_просили_уходит_в_прочее() -> None:
    """График и деньги теперь берутся из полей источника.

    Модель про это не знает и иногда присылает их всё равно. Строку не
    выбрасываем: «стабильный доход» вместо цифры — находка для детектора,
    просто не условие работы.
    """
    answer = """
    {"conditions": [
      {"field": "salary", "value": "стабильный доход", "quote": "стабильный доход и премии"}
    ]}
    """
    text = DESCRIPTION + " Обещаем стабильный доход и премии."

    got = llm_tasks.extract_conditions(FakeGateway(answer), text)

    assert [c.field for c in got] == ["other"]
    assert "salary" not in conditions.MODEL_FIELDS


def test_условия_без_модели_это_пустота() -> None:
    assert llm_tasks.extract_conditions(FakeGateway(None), DESCRIPTION) == ()
    assert llm_tasks.extract_conditions(FakeGateway("{}"), "") == ()


# --- справка по компании ------------------------------------------------


HITS = (
    websearch.Hit(
        title="Акме — о компании",
        url="https://acme.ru/about",
        snippet="Делаем логистическую платформу на Python и PostgreSQL.",
    ),
)


def test_справка_собирается_только_из_выдачи() -> None:
    answer = """
    {"lines": [
      "Логистическая платформа на Python и PostgreSQL",
      "Команда из 450 инженеров"
    ]}
    """
    brief = llm_tasks.company_brief(FakeGateway(answer), "Акме", HITS)
    assert brief is not None
    # Числа 450 во фрагментах нет — строка с ним отбрасывается.
    assert len(brief.lines) == 1
    assert brief.sources == ("https://acme.ru/about",)
    assert "https://acme.ru/about" in brief.text


def test_справка_без_выдачи_не_зовёт_модель() -> None:
    gateway = FakeGateway('{"lines": ["что-то"]}')
    assert llm_tasks.company_brief(gateway, "Акме", ()) is None
    assert gateway.stages == []


# --- выбор адресата ----------------------------------------------------


class Person:
    def __init__(self, label: str) -> None:
        self.label = label


CANDIDATES = (Person("Анна, HR"), Person("Пётр, руководитель разработки"))


def test_адресат_берётся_по_номеру_из_списка() -> None:
    gateway = FakeGateway('{"choice": 2, "reason": "решает по найму"}')
    chosen = llm_tasks.pick_contact(gateway, CANDIDATES, role_hint="Python-разработчик")
    assert chosen is CANDIDATES[1]
    assert gateway.stages == ["contacts"]


def test_мусор_вместо_номера_даёт_первого_кандидата() -> None:
    # Ранжирование детерминированное, и оно же остаётся при любом сбое модели.
    for answer in (None, "не знаю", '{"choice": 99}', '{"choice": "Пётр"}'):
        assert llm_tasks.pick_contact(FakeGateway(answer), CANDIDATES) is CANDIDATES[0]


def test_один_кандидат_не_требует_модели() -> None:
    gateway = FakeGateway('{"choice": 1}')
    assert llm_tasks.pick_contact(gateway, CANDIDATES[:1]) is CANDIDATES[0]
    assert llm_tasks.pick_contact(gateway, ()) is None
    assert gateway.stages == []


# --- шлифовка письма ---------------------------------------------------


BODY = (
    "Здравствуйте! Увидел вашу вакансию. Переписал выгрузку отчётов и "
    "ускорил её в 12 раз. Готов рассказать подробнее."
)


def _draft() -> outreach.Draft:
    return outreach.Draft(subject="Python-разработчик", body=BODY)


def test_шлифовка_применяется_если_цифры_не_изменились() -> None:
    better = (
        "Здравствуйте! Увидел вакансию и решил написать напрямую. "
        "Переписал выгрузку отчётов и ускорил её в 12 раз. Расскажу подробнее."
    )
    got = llm_tasks.polish_draft(FakeGateway(better), _draft(), facts=[])
    assert got.body == better
    assert got.subject == "Python-разработчик"


def test_новые_цифры_в_письме_отменяют_правку() -> None:
    lie = BODY + " Руковожу командой из 7 человек."
    got = llm_tasks.polish_draft(FakeGateway(lie), _draft(), facts=[])
    assert got.body == BODY


def test_цифра_из_facts_разрешена() -> None:
    text = BODY + " Собрал команду из 7 человек."
    got = llm_tasks.polish_draft(
        FakeGateway(text), _draft(), facts=["Собрал команду из 7 человек"]
    )
    assert got.body == text


def test_огрызок_и_пустота_не_ломают_черновик() -> None:
    assert llm_tasks.polish_draft(FakeGateway(None), _draft()).body == BODY
    assert llm_tasks.polish_draft(FakeGateway("Ок"), _draft()).body == BODY
    assert llm_tasks.polish_draft(FakeGateway("А" * 5000), _draft()).body == BODY


def test_числа_сравниваются_без_разделителей() -> None:
    # «250 000» и «250000» — одна и та же зарплата, иначе правка никогда не пройдёт.
    assert llm_tasks.numbers("250 000") == llm_tasks.numbers("250000")
    assert llm_tasks.numbers("в 12 раз") == {"12"}
