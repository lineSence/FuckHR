"""Тесты тональности: отрицание не должно читаться как похвала.

Отдельный файл, потому что класс ошибки общий для всего проекта: маркеры и
признаки ищутся подстрокой, и «рекомендую» лежит внутри «не рекомендую».
Один такой промах красит контору с задержками зарплаты в mixed, а mixed уже не
даёт красный статус так же уверенно, как negative.
"""

from __future__ import annotations

import dossier


def test_отрицание_не_считается_похвалой():
    assert dossier.polarity_of("Задерживают зарплату, не рекомендую") == "negative"
    assert dossier.polarity_of("Не платят вовремя") == "negative"
    assert dossier.polarity_of("Не рекомендую") == "negative"


def test_похвала_без_отрицания_остаётся_похвалой():
    assert dossier.polarity_of("Платят вовремя, рекомендую") == "positive"
    # Отрицание считается только рядом с маркером, а не где-то в тексте.
    assert dossier.polarity_of("Платят вовремя, не к чему придраться") == "positive"


def test_смешанный_отзыв_остаётся_смешанным():
    assert dossier.polarity_of("Платят вовремя, но переработки") == "mixed"
    assert dossier.polarity_of("Плюсы: гибкий график. Минусы: текучка") == "mixed"


def test_зелёный_признак_под_отрицанием_не_становится_флагом():
    отзывы = [
        dossier.Review(
            url="https://dreamjob.ru/c/1",
            snippet="Не платят вовремя",
            site="dreamjob.ru",
            polarity=dossier.polarity_of("Не платят вовремя"),
        ),
        dossier.Review(
            url="https://orabote.top/c/2",
            snippet="Зарплату задерживают, платят вовремя только руководству",
            site="orabote.top",
            polarity="negative",
        ),
    ]
    patterns = dossier.find_patterns(отзывы)
    оплата = next((p for p in patterns if p.code == "pays_on_time"), None)
    # Первый отзыв не должен давать зелёный признак: там отрицание.
    assert оплата is None or оплата.hits == 1
    assert any(p.code == "salary_delay" for p in patterns)
