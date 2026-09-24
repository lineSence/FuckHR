"""Сводка прогона для интерфейса: полоски по фазам, цифры, беды по смыслу.

Строки взяты из настоящего лога — в двух форматах сразу: консольном (уровень и
модуль) и файловом (ещё время и поток). Сводку читают из обоих.
"""

from __future__ import annotations

import run_view

LINES = [
    "INFO fuckhr настройки: лимит 250, профиль profiles, описания да, модель да",
    "INFO fuckhr [1/2] запрос: Ревизор (профилей: ревизор)",
    "INFO fuckhr увидели: 280, прошло предфильтр: 250",
    "INFO fuckhr [12/497] Администратор магазина — Пятёрочка",
    "INFO fuckhr [37/497] Фотограф — LikeMe Studio",
    "2026-09-23 21:33:30,351 WARNING fuckhr [detail_1] нет деталей по 137463130: беда",
    "2026-09-23 21:33:31,351 WARNING fuckhr [detail_0] нет деталей по 137708329: беда",
    "WARNING llm [llm_3] local/auto:smart ответил ошибкой (1/3, этап hr_filter): HTTP 404",
    "ERROR llm local отклонил запрос с model='auto:smart' — повторы не помогут",
    "INFO fuckhr [3/4] модель: условия",
    "INFO fuckhr [10/20] модель: утверждения",
    "INFO fuckhr [2/11] досье: Ромашка — претензий не видно",
    "INFO fuckhr модель: вызовов 258, из кэша 0, ошибок 0, пропущено 0",
]


def test_фазы_идут_в_своём_порядке_и_не_путаются() -> None:
    data = run_view.summary(LINES)

    assert data.phases == [
        ("Запросы", 1, 2),
        ("Условия", 3, 4),
        ("Утверждения", 10, 20),
        ("Досье компаний", 2, 11),
        # Строка вакансии — просто «[n/m] Название — Компания», и она не должна
        # съедать счётчики именованных фаз.
        ("Вакансии", 37, 497),
    ]


def test_сводные_строки_берутся_последние() -> None:
    data = run_view.summary(LINES + ["INFO fuckhr увидели: 300, прошло предфильтр: 260"])
    facts = dict(data.facts)

    assert facts["Предфильтр"] == "увидели: 300, прошло предфильтр: 260"
    assert facts["Модель"].startswith("модель: вызовов 258")
    # Уровень, модуль и поток из сообщения убраны: в таблице они только мешают.
    assert "INFO" not in facts["Настройки"]


def test_одинаковые_беды_считаются_вместе_а_ошибки_идут_первыми() -> None:
    data = run_view.summary(LINES)

    assert (data.errors, data.warnings) == (1, 3)
    assert data.problems[0][2] is True
    # Две вакансии с разными номерами — одна и та же беда. А короткие числа
    # остаются собой: «auto:smart» и «1/3» в сообщении должны читаться.
    assert ("нет деталей по N: беда", 2, False) in data.problems
    assert any("(1/3, этап hr_filter)" in key for key, _, _ in data.problems)


def test_пустой_вывод_не_роняет_страницу() -> None:
    data = run_view.summary([])

    assert data.phases == [] and data.facts == [] and data.problems == []
    assert "шаги ещё не сообщались" in run_view.render(data, running=True)
    assert "Ошибок и предупреждений не было" in run_view.render(data, running=False)


def test_в_разметке_есть_полоски_и_таблицы() -> None:
    html = run_view.render(run_view.summary(LINES), running=True)

    assert "<progress value=\"37\" max=\"497\"" in html
    assert "Что насчитал прогон" in html
    assert "ошибок 1, предупреждений 3" in html
