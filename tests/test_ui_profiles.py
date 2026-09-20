"""Раздел «Профили»: карточки, выключатель и добавление.

Проверяется поведение, из-за которого раздел и переделывался: список открывается
блоками, профиль выключается не удаляя файл, выключенный не попадает в прогон,
а незнакомый id из адреса не открывает чужой файл.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import profiles
import ui_profile
import ui_profiles


def write(path: Path, **extra: object) -> Path:
    data = {"queries": [{"text": "python"}], "min_score": 50}
    data.update(extra)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_entries_reads_title_and_switch(tmp_path: Path) -> None:
    write(tmp_path / "backend.yaml", title="Бэкенд")
    write(tmp_path / "data.yaml", enabled=False)
    found = {item.id: item for item in ui_profiles.entries(tmp_path)}
    assert found["backend"].title == "Бэкенд"
    assert found["backend"].enabled is True
    # Имени нет — берётся имя файла, а не пустая карточка.
    assert found["data"].title == "data"
    assert found["data"].enabled is False


def test_toggle_keeps_file(tmp_path: Path) -> None:
    path = write(tmp_path / "backend.yaml")
    ui_profiles.toggle(tmp_path, "backend", False)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["enabled"] is False
    assert data["queries"], "критерии не должны потеряться при выключении"


def test_disabled_profile_skipped(tmp_path: Path) -> None:
    write(tmp_path / "backend.yaml")
    write(tmp_path / "data.yaml", enabled=False)
    assert [item.id for item in profiles.load_all(tmp_path)] == ["backend"]


def test_all_disabled_is_an_error(tmp_path: Path) -> None:
    # Молчаливый прогон без единого профиля выглядел бы как «ничего не нашлось».
    write(tmp_path / "backend.yaml", enabled=False)
    with pytest.raises(ValueError):
        profiles.load_all(tmp_path)


def test_create_and_resolve(tmp_path: Path) -> None:
    write(tmp_path / "backend.yaml")
    pid, path, note = ui_profiles.create(tmp_path, "Аналитик Данных")
    assert pid == "аналитик-данных"
    assert Path(path) == tmp_path
    assert "создан" in note
    assert ui_profiles.resolve(tmp_path, pid).name == "аналитик-данных.yaml"


def test_resolve_rejects_unknown_id(tmp_path: Path) -> None:
    write(tmp_path / "backend.yaml")
    with pytest.raises(KeyError):
        ui_profiles.resolve(tmp_path, "../../.env")


def test_resolve_prefers_enabled(tmp_path: Path) -> None:
    write(tmp_path / "a.yaml", enabled=False)
    write(tmp_path / "b.yaml")
    assert ui_profiles.resolve(tmp_path, "").name == "b.yaml"


def test_cards_have_switch_and_add_button(tmp_path: Path) -> None:
    write(tmp_path / "backend.yaml", title="Бэкенд")
    write(tmp_path / "data.yaml", enabled=False)
    html = ui_profiles.render_cards(tmp_path)
    assert html.count("class=\"card") == 2
    assert "Выключить" in html and "Включить" in html
    assert "Добавить профиль" in html
    assert "Сейчас включено 1 из 2" in html


def test_form_sections_are_collapsible(tmp_path: Path) -> None:
    path = write(tmp_path / "backend.yaml")
    html = ui_profile.render_profile(str(path), pid="backend")
    assert html.count("<details class=sect") == 7
    # Сохранение должно вернуться в тот же профиль, а не в первый попавшийся.
    assert '<input type=hidden name="id" value="backend">' in html


def test_title_saved_from_form(tmp_path: Path) -> None:
    path = write(tmp_path / "backend.yaml")
    ui_profile.save_profile(str(path), {"title": ["Бэкенд"], "min_score": ["50"]})
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["title"] == "Бэкенд"
