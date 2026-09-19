"""Скоринг детерминирован ([CORE-015]), значит его можно прибить тестами насмерть.

Профиль берётся из tests/fixtures/profile.yaml, а не из рабочего profile.yaml в корне.
Раньше брался рабочий, и тесты падали от настройки фильтра под себя: сменишь навыки
с python на «менеджер КРО» — и тестовая вакансия перестаёт проходить порог, хотя в коде
ничего не ломалось. Проверяем формулу, а не вкусы владельца.

За рабочим файлом остаётся одна проверка — что он вообще читается загрузчиком
после редактирования через интерфейс. Значения в нём тесты не комментируют.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from score import Profile, evaluate

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "profile.yaml"


@pytest.fixture(scope="module")
def profile() -> Profile:
    return Profile.load(FIXTURE)


def test_good_vacancy_passes_threshold(profile, make_vacancy):
    verdict = evaluate(make_vacancy(), profile)
    assert not verdict.rejected
    assert verdict.score >= profile.min_score


def test_stop_word_rejects_before_scoring(profile, make_vacancy):
    verdict = evaluate(make_vacancy(description="Требуется знание 1c и python"), profile)
    assert verdict.rejected
    assert "1c" in (verdict.reject_reason or "")
    assert verdict.score == 0.0


def test_salary_far_below_target_rejects(profile, make_vacancy):
    verdict = evaluate(make_vacancy(salary_from=90000), profile)
    assert verdict.rejected


def test_missing_salary_is_penalty_not_rejection(profile, make_vacancy):
    with_salary = evaluate(make_vacancy(), profile)
    without = evaluate(make_vacancy(salary_from=None, salary_to=None), profile)
    assert not without.rejected
    assert without.score < with_salary.score
    assert "вилка не указана" in without.reasons


def test_onsite_scores_lower_than_remote(profile, make_vacancy):
    remote = evaluate(make_vacancy(), profile)
    onsite = evaluate(
        make_vacancy(
            schedule="Полный день",
            description="Python, asyncio, FastAPI, PostgreSQL, Docker, SQL, офис",
        ),
        profile,
    )
    assert onsite.score < remote.score


def test_рабочий_профиль_читается_загрузчиком():
    """Страховка от сломанного YAML после ручных правок или сохранения из формы.

    Конкретные запросы, навыки и порог здесь сознательно не проверяются: это личный
    файл владельца, и он вправе менять там всё.
    """
    working = ROOT / "profile.yaml"
    if not working.exists():
        pytest.skip("рабочего profile.yaml нет, проверять нечего")

    loaded = Profile.load(working)

    assert isinstance(loaded.queries, list)
    assert loaded.min_score >= 0


def test_стоп_слово_ловится_в_теле_в_другой_форме(profile, make_vacancy):
    """На выдаче тела нет, и весь мусор виден только в описании."""
    verdict = evaluate(
        make_vacancy(description="<p>Работа <b>вахтой</b> 60/30, python</p>"), profile
    )
    assert verdict.rejected
    assert "вахта" in (verdict.reject_reason or "")
    assert "в теле" in (verdict.reject_reason or "")


def test_стоп_слово_в_названии_помечено_как_название(profile, make_vacancy):
    verdict = evaluate(make_vacancy(title="Стажировка Python"), profile)
    assert verdict.rejected
    assert "в названии" in (verdict.reject_reason or "")


def test_стоп_фраза_ищется_целиком(profile, make_vacancy):
    """Фраза не должна срабатывать по одному своему слову."""
    ok = evaluate(make_vacancy(description="Python, работа с отделом продаж"), profile)
    assert not ok.rejected
    bad = evaluate(make_vacancy(description="Нужен менеджер по продажам"), profile)
    assert bad.rejected


def test_короткое_стоп_слово_не_ловит_лишнее(profile, make_vacancy):
    """«1c» не должно срабатывать на «1сек» или числах в тексте."""
    verdict = evaluate(
        make_vacancy(description="Python, отклик за 1 день, 100 000 запросов"), profile
    )
    assert not verdict.rejected
