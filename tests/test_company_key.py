"""Одна контора — одно досье, разные конторы — разные."""

import pytest

import company_key


@pytest.mark.parametrize(
    "левое, правое",
    [
        ("ООО «Ромашка»", "Ромашка"),
        ("Ромашка, ООО", "ООО Ромашка"),
        ("АО ТехноЛаб (группа компаний)", "ТехноЛаб"),
        ("Acme LLC", "ACME"),
        ("Пёстрый Кот", "Пестрый кот"),
    ],
)
def test_один_и_тот_же_работодатель(левое, правое):
    assert company_key.same(левое, правое)


@pytest.mark.parametrize(
    "левое, правое",
    [
        ("ООО Ромашка", "ООО Лютик"),
        ("Ромашка-Строй", "Ромашка-Телеком"),
        ("АО Банк Север", "АО Банк Юг"),
        ("ООО", "Ромашка"),
        ("", "Ромашка"),
    ],
)
def test_разные_работодатели_не_склеиваются(левое, правое):
    assert not company_key.same(левое, правое)


def test_правовая_форма_не_считается_названием():
    assert company_key.normalize("ООО «Ромашка»") == "ромашка"
    assert company_key.normalize("ООО") == ""


def test_ищется_уже_известное_название():
    известные = ["ООО «Ромашка»", "АО Лютик"]
    assert company_key.best_match("Ромашка", известные) == "ООО «Ромашка»"
    assert company_key.best_match("ООО Василёк", известные) is None
    assert company_key.best_match("", известные) is None
    assert company_key.best_match("Ромашка", []) is None
