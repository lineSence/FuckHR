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
    assert "дорисовала" in note


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


def test_имена_моделей_из_формы_фильтруются() -> None:
    """Единственное место, где строка из браузера идёт в командную строку."""
    import ui_forms

    assert ui_forms.bench_models("qwen2.5:7b, openai/gpt-4o-mini") == [
        "qwen2.5:7b",
        "openai/gpt-4o-mini",
    ]
    assert ui_forms.bench_models("rm -rf /; cat .env") == []
    assert ui_forms.bench_models("a, a, a") == ["a"]
    assert len(ui_forms.bench_models(",".join("m{}".format(i) for i in range(20)))) == 6


def test_задача_сравнения_не_висит_кнопкой_на_запуске() -> None:
    """У bench своя форма: без имён моделей кнопка была бы обманом."""
    import jobs

    assert "bench" in jobs.TASKS
    assert "bench" not in [key for key, _, _ in jobs.task_list()]


def test_форма_сравнения_показывает_все_этапы() -> None:
    import ui_forms

    html = ui_forms.render_bench_form()
    assert all(stage in html for stage in bench.STAGES)
    assert 'action="/bench"' in html


def test_отчёт_показывает_таблицу_а_не_лог(tmp_path) -> None:
    """Результат сравнения читается глазами: баллы в таблице, а не в логе."""
    import ui_bench

    rows = [
        bench.Row("быстрая", "extract", "к1", 1.0, "", 0.5),
        bench.Row("медленная", "extract", "к1", 0.2, "выдумала вилку", 4.0),
    ]
    path = tmp_path / "last.json"
    bench.save_report(str(path), rows, ["быстрая", "медленная"], bench_cases.CASES, 1, "proxy")

    report = ui_bench.load_report(path)
    assert report is not None
    html = ui_bench.render_report(report)
    assert "<table>" in html and "Лучший балл" in html
    assert "быстрая" in html and "выдумала вилку" in html
    assert 'class="danger">0.20' in html


def test_битый_отчёт_не_роняет_страницу(tmp_path) -> None:
    import ui_bench

    path = tmp_path / "last.json"
    path.write_text("{не json", encoding="utf-8")
    assert ui_bench.load_report(path) is None
    assert ui_bench.load_report(tmp_path / "нет-такого.json") is None


def test_прогресс_печатается_счётчиком() -> None:
    """Полоску загрузки интерфейс берёт из строк вида «[3/30]»."""
    import jobs

    assert jobs.parse_progress("[3/30] быстрая · к1 · 1.00 за 0.4 с") == (3, 30)


def test_рекомендация_учитывает_скорость_при_равном_балле() -> None:
    """Разница в две сотых балла ничего не значит, секунды на вызов — значат."""
    rows = [
        bench.Row("медленная", "extract", "к1", 1.0, "", 8.0),
        bench.Row("быстрая", "extract", "к1", 0.98, "", 0.7),
        bench.Row("быстрая", "company", "к2", 0.3, "выдумала цифру", 0.5),
        bench.Row("медленная", "company", "к2", 1.0, "", 6.0),
    ]
    picks = bench.recommend(rows)
    assert picks["extract"][0] == "быстрая"
    # На company разрыв большой: скорость не спасает.
    assert picks["company"][0] == "медленная"


def test_модель_ставится_на_этап_а_не_на_профиль() -> None:
    """Имя этапа сильнее имени профиля: у extract и resume_section один профиль."""
    gateway = llm.Gateway(
        proxy_base_url="http://proxy/v1",
        proxy_models={llm.FAST: "общая"},
        stage_models={"extract": "своя-на-extract"},
    )
    assert gateway.model_for("extract") == ("своя-на-extract", "этап")
    assert gateway.model_for("resume_section") == ("общая", "профиль")
    routes = {row[0]: (row[3], row[4]) for row in gateway.describe_routes()}
    assert routes["extract"] == ("своя-на-extract", "этап")


def test_форма_подстановки_предлагает_ключи_env() -> None:
    import ui_bench

    rows = [bench.Row("быстрая", "extract", "к1", 1.0, "", 0.7)]
    html = ui_bench.render_apply_form(rows)
    assert "LLM_STAGE_MODEL_EXTRACT" in html
    assert 'action="/llm/apply"' in html
    assert "LLM_STAGE_MODEL_EXTRACT" in ui_bench.ENV_KEYS


def test_ловушка_про_вилку_ловит_число_а_не_поле() -> None:
    """Про зарплату в тексте сказано: условие с дословной цитатой — не ошибка.

    Ловушка существует ради выдуманной суммы. Раньше она снимала балл за само
    поле salary, и пройти её честным разбором было нельзя.
    """
    case = _case("extract: вилки нет")
    honest = json.dumps(
        {
            "conditions": [
                {
                    "field": "salary",
                    "value": "обсуждается на собеседовании",
                    "quote": "Обсуждаем зарплату на собеседовании",
                }
            ]
        },
        ensure_ascii=False,
    )
    invented = json.dumps(
        {
            "conditions": [
                {
                    "field": "salary",
                    "value": "от 200 000 на руки",
                    "quote": "Обсуждаем зарплату на собеседовании",
                }
            ]
        },
        ensure_ascii=False,
    )
    good = bench.run_case(_gateway({llm.FAST: honest}), case)
    assert bench_cases.check(case, good) == (1.0, "чисто: 1")

    bad = bench.run_case(_gateway({llm.FAST: invented}), case)
    score, note = bench_cases.check(case, bad)
    assert score == 0.0 and "200000" in note
