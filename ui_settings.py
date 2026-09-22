"""Страница настроек: поле, поиск по полям, форма подкатами.

Выделено из ui_views.py по [CORE-024]: шесть десятков полей и два куска
JavaScript-поиска занимали треть файла и с вакансиями никак не связаны.
Реэкспорт из ui_views.py оставлен: старые импорты продолжают работать.
"""

from __future__ import annotations

from typing import Sequence

import settings
from ui_core import esc

# ———— настройки ————


SETTINGS_SEARCH = """
<div class=field>
<input type=search id=setq autocomplete=off
 placeholder="Поиск по настройкам: название, ключ или слово из подсказки">
<div class=hint id=setq-note>Группы свёрнуты: разверни нужную или начни искать.</div>
</div>
"""

# Скрипт идёт после формы: на момент выполнения подкаты должны уже существовать.
SETTINGS_SEARCH_JS = """
<script>
(function () {
  var box = document.getElementById("setq");
  var note = document.getElementById("setq-note");
  var groups = [].slice.call(document.querySelectorAll("details.setgroup"));
  function apply() {
    var q = box.value.trim().toLowerCase();
    var found = 0;
    groups.forEach(function (group) {
      var shown = 0;
      [].slice.call(group.querySelectorAll("[data-find]")).forEach(function (field) {
        var hit = !q || field.dataset.find.indexOf(q) >= 0;
        field.hidden = !hit;
        if (hit) { shown += 1; }
      });
      group.hidden = Boolean(q) && !shown;
      // Пустой запрос возвращает исходное состояние, а не «всё открыто»:
      // иначе после стирания строки страница остаётся километровой.
      group.open = q ? shown > 0 : group.dataset.open === "1";
      found += shown;
    });
    if (!q) {
      note.textContent = "Группы свёрнуты: разверни нужную или начни искать.";
    } else {
      note.textContent = found ? "Найдено настроек: " + found : "Ничего не нашлось.";
    }
  }
  box.addEventListener("input", apply);
  apply();
})();
</script>
"""


def settings_field(field: "settings.Field", current: str) -> str:
    """Одна настройка в форме. data-find — то, по чему её ищет строка поиска."""
    found = " ".join((field.label, field.key, field.help, field.group)).lower()

    if field.kind == settings.BOOL:
        on = settings.as_bool(current, settings.as_bool(field.default))
        control = (
            '<label><input type=checkbox name="{key}" value="1"{checked}> {label}</label>'
        ).format(
            key=esc(field.key),
            checked=" checked" if on else "",
            label=esc(field.label),
        )
        return (
            '<div class=field data-find="{found}">{control}'
            "<div class=hint>{key} · {hint}</div></div>"
        ).format(
            found=esc(found), control=control, key=esc(field.key), hint=esc(field.help)
        )

    if field.is_secret:
        shown = ""
        placeholder = settings.mask(current)
        input_type = "password"
    else:
        shown = current or field.default
        placeholder = field.default
        input_type = "number" if field.kind in (settings.INT, settings.FLOAT) else "text"

    step = ""
    if field.kind == settings.INT:
        step = ' step="1"'
    elif field.kind == settings.FLOAT:
        step = ' step="any"'

    return (
        '<div class=field data-find="{found}"><label>{label}</label>'
        '<input type="{input_type}" name="{key}" value="{value}" '
        'placeholder="{placeholder}"{step}>'
        "<div class=hint>{key} · {hint}</div></div>"
    ).format(
        found=esc(found),
        label=esc(field.label),
        input_type=input_type,
        key=esc(field.key),
        value=esc(shown),
        placeholder=esc(placeholder),
        step=step,
        hint=esc(field.help),
    )


def render_settings(saved: Sequence[str] = ()) -> str:
    """Настройки подкатами: шесть десятков полей одним списком не читаются.

    Раскрыта только группа запуска — та, куда ходят чаще всего. Остальные
    разворачиваются руками или сами, когда их поля попали в поиск. Поля
    свёрнутых групп остаются в форме и сохраняются как обычно: details прячет
    их визуально, браузер их всё равно отправляет.
    """
    values = settings.load()
    parts = []

    if saved:
        parts.append("<div class=ok>Сохранено: {}</div>".format(esc(", ".join(saved))))

    notes = settings.missing_required()
    if notes:
        items = "".join("<li>{}</li>".format(esc(item)) for item in notes)
        parts.append("<div class=warn><ul>{}</ul></div>".format(items))

    parts.append(
        "<p class=muted>Всё сохраняется в {}. Секреты показаны маской: пустое поле "
        "оставляет текущее значение, слово «очистить» стирает его.</p>".format(
            esc(settings.ENV_PATH)
        )
    )
    parts.append(SETTINGS_SEARCH)
    parts.append('<form method=post action="/settings">')

    for position, (group, fields) in enumerate(settings.groups()):
        opened = position == 0
        parts.append(
            (
                '<details class=setgroup data-open="{flag}"{attr}>'
                "<summary>{group} <span class=muted>· настроек: {count} · {hint}</span>"
                "</summary>"
            ).format(
                flag="1" if opened else "0",
                attr=" open" if opened else "",
                group=esc(group),
                count=len(fields),
                hint=esc(settings.GROUP_HINTS.get(group, "")),
            )
        )
        for field in fields:
            parts.append(settings_field(field, values.get(field.key, "")))
        parts.append("</details>")

    parts.append("<p><button>Сохранить</button></p></form>")
    parts.append(SETTINGS_SEARCH_JS)
    parts.append(
        "<p class=muted>Новые значения подхватываются со следующего запуска задачи: "
        "каждая задача — отдельный процесс со свежим .env.</p>"
    )
    return "".join(parts)




__all__ = ("render_settings", "settings_field")
