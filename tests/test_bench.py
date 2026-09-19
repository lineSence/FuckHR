"""Проверяется не качество моделей, а честность самой оценки.

Сеть здесь не трогается: вместо модели подставлен transport, который отвечает
заранее заданным текстом. Важно, что оценка ставит ноль там, где модель соврала,
и что бенчмарк не падает, когда модель отвечает мусором.
"""

from __future__ import annotations

import json

import bench
import bench_cases
import llm


def _gateway(answers: dict[str, str]) -> llm.Gateway:
    """Шлюз с подменённым транспортом: ответ выбирается по профилю этапа."""

    def transport(profile: str, messages, temperature: float) -> str:
        return answers.get(profile, "")

    return llm.Gateway(base_url="http://local", transport=transport, max_calls=100)


def _case(name: str) -> bench_cases.Case:
    return next(c for c in bench_cases.CASES if c.name == name)


def test_условие_с_дословной_цитатой_засчитывается() -> None:
    case = _case("extract: гибрид и вилка")
    answer = json.dumps(
        {
            "conditions": [
                {"field": "format", "value": "гибрид", "quote": "два дня в офисе"},
                {
                    "field": "salary",
                    "value": "250-320 на руки",
                    "quote": "Вилка 250 000 — 320 000 на руки",
                },
            ]
        },
        ensure_ascii=False,
    )
    result = bench.run_case(_gateway({llm.FAST: answer}), case)
    assert bench_cases.check(case, result) == (1.0, "поля: format, salary")


def test_выдуманная_вилка_обнуляет_ловушку() -> None:
    case = _case("extract: вилки нет")
    answer = json.dumps(
        {
            "conditions": [
                {
                    "field": "salary",
                    "value": "от 300 000",
                    "quote": "Обсуждаем зарплату на собеседовании",
                }
            ]
        },
        ensure_ascii=False,
    )
    result = bench.run_case(_gateway({llm.FAST: answer}), case)
    score, note = bench_cases.check(case, result)
    assert score == 0.0
    assert "выдумала" in note


def test_кадровик_вместо_тимлида_это_ноль() -> None:
    case = _case("contacts: кадровик первым")
    result = bench.run_case(_gateway({llm.LOCAL: '{"choice": 1}'}), case)
    assert bench_cases.check(case, result)[0] == 0.0
    good = bench.run_case(_gateway({llm.LOCAL: '{"choice": 3}'}), case)
    assert bench_cases.check(case, good)[0] == 1.0


def test_откат_письма_считается_провалом() -> None:
    case = _case("draft: переписать письмо")
    # Модель дописала число, которого не было: polish_draft вернёт черновик.
    invented = case.payload["body"].replace("8 годами", "12 годами")
    result = bench.run_case(_gateway({llm.LOCAL: invented}), case)
    score, note = bench_cases.check(case, result)
    assert score == 0.0
    assert "откат" in note


def test_мусорный_ответ_не_роняет_прогон() -> None:
    rows = bench.run_model("мусор", bench_cases.CASES, gateway=_gateway({}))
    assert len(rows) == len(bench_cases.CASES)
    # contacts при молчании модели честно откатывается на первого по ранжированию,
    # и на кейсе-ловушке это ноль, а на обычном — случайная удача: их не проверяем.
    assert all(row.score == 0.0 for row in rows if row.stage in ("extract", "company", "draft"))


def test_отчёт_выбирает_победителя_по_этапам() -> None:
    rows = [
        bench.Row("быстрая", "extract", "к1", 1.0, "", 0.5),
        bench.Row("медленная", "extract", "к1", 1.0, "", 4.0),
        bench.Row("медленная", "draft", "к2", 1.0, "", 4.0),
        bench.Row("быстрая", "draft", "к2", 0.0, "откат", 0.5),
    ]
    assert bench.winners(rows) == {
        "extract": ("быстрая", 1.0),
        "draft": ("медленная", 1.0),
    }
    report = bench.render(rows)
    assert "extract" in report and "быстрая" in report
