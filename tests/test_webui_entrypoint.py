"""Страж на точки входа запускаемых файлов.

Потерянный блок `if __name__ == "__main__"` — поломка, которую не видно ни в
логах, ни в коде возврата: `python webui.py` молча выходит с нулём, и со стороны
это выглядит как «программа ничего не делает». Дешёвле проверить текстом, чем
поднимать сервер в тесте.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ("webui.py", "run.py")


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_у_запускаемых_файлов_есть_точка_входа(name: str):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert '__name__ == "__main__"' in text, name
    assert "main()" in text, name


def test_интерфейс_слушает_только_локальный_адрес():
    import ui_core

    assert ui_core.HOST == "127.0.0.1"
