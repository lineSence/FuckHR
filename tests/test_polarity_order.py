"""Порядок слов в признаках. Сети нет, только строки.

Признаки ищутся подстрокой, поэтому «задерживают зарплату» и «зарплату
задерживают» — для кода разные строки. Тест держит обе перестановки, чтобы
живой отзыв не проходил мимо закономерности.
"""

from __future__ import annotations

import pytest

import dossier


def отзыв(text: str) -> dossier.Review:
    return dossier.Review(url="https://dreamjob.ru/c/1", body=text, site="dreamjob.ru")


@pytest.mark.parametrize(
    "text",
    [
        "Задерживают зарплату третий месяц",
        "Зарплату задерживают третий месяц подряд, руководство молчит",
        "Зарплата задерживается на две недели каждый раз",
        "Зарплату платят с задержкой, аванс вообще забыли",
        "Задерживают выплаты по премиям и отпускным",
    ],
)
def test_задержки_зарплаты_ловятся_в_любом_порядке_слов(text: str):
    codes = {p.code for p in dossier.find_patterns([отзыв(text)])}
    assert "salary_delay" in codes
    assert dossier.polarity_of(text) in ("negative", "mixed")


def test_похвала_за_своевременные_выплаты_не_считается_задержкой():
    text = "Зарплата без задержек, платят вовремя, оформление белое"
    codes = {p.code for p in dossier.find_patterns([отзыв(text)])}
    assert "salary_delay" not in codes
    assert "pays_on_time" in codes
    assert dossier.polarity_of(text) == "positive"
