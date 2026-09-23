"""Запись .env: значение возвращается тем же и не дописывает чужих ключей."""

from __future__ import annotations

import pytest

import settings


@pytest.fixture(autouse=True)
def _чистое_окружение(monkeypatch):
    # save() пишет и в os.environ; setenv запоминает исходное состояние для отката.
    for key in ("K", "A", "HH_COOKIE"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


@pytest.mark.parametrize("value", [r"C:\Program Files\model", 'say "hi"', "a b", r"end\\"])
def test_значение_переживает_запись_и_чтение(tmp_path, value):
    path = tmp_path / ".env"
    settings.save({"K": value}, path)
    assert settings.load(path)["K"] == value
    assert settings.save({"K": value}, path) == []


@pytest.mark.parametrize(
    "updates",
    [{"A": "x\nTELEGRAM_CHAT_ID=666"}, {"A": "x\rB=1"}, {"A=B": "1"}, {"A\nB": "1"}],
)
def test_перенос_строки_не_пишется(tmp_path, updates):
    path = tmp_path / ".env"
    with pytest.raises(ValueError):
        settings.save(updates, path)
    assert not path.exists()


def test_cookie_в_несколько_строк_склеивается(tmp_path, monkeypatch):
    import ui_run

    monkeypatch.setattr(settings, "ENV_PATH", tmp_path / ".env")
    ui_run.save_cookie({"cookie": ["a=1;\n b=2;\r\nc=3"]})
    assert settings.load(tmp_path / ".env")["HH_COOKIE"] == "a=1; b=2; c=3"


def test_www_снимается_только_как_префикс():
    import reviewsites

    assert reviewsites.host_of("https://www.dreamjob.ru/x") == "dreamjob.ru"
    assert reviewsites.host_of("https://wb.ru") == "wb.ru"
