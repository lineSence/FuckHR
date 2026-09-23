"""Интерфейс отвечает только своим страницам: CSRF и DNS rebinding."""

from __future__ import annotations

import pytest

from ui_guard import problem

PORT = 8765
OWN = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Host": "localhost:8765"},
        OWN,
        {"Host": "127.0.0.1:8765", "Referer": "http://127.0.0.1:8765/settings"},
        {**OWN, "Sec-Fetch-Site": "same-origin"},
    ],
)
def test_свой_запрос_проходит(headers):
    assert problem("POST", headers, PORT) == ""


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example:8765"},
        {"Host": "127.0.0.1:8765", "Origin": "https://evil.example"},
        {"Host": "127.0.0.1:8765", "Origin": "null"},
        {"Host": "127.0.0.1:8765", "Origin": "http://localhost:3000"},
        {"Host": "127.0.0.1:8765", "Referer": "https://evil.example/x"},
        {**OWN, "Sec-Fetch-Site": "cross-site"},
        {**OWN, "Sec-Fetch-Site": "same-site"},
    ],
)
def test_чужая_форма_отклоняется(headers):
    assert problem("POST", headers, PORT)


def test_get_проверяет_только_host():
    assert problem("GET", {"Host": "127.0.0.1:8765", "Origin": "https://evil.example"}, PORT) == ""
    assert problem("GET", {"Host": "rebind.evil.example:8765"}, PORT)
