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
<label><input type=checkbox id=setdiff> только изменённые</label>
<div class=hint id=setq-note>Группы свёрнуты: разверни нужную или начни искать.</div>
</div>
"""

# Скрипт идёт после формы: на момент выполнения подкаты должны уже существовать.
SETTINGS_SEARCH_JS = """
<script>
(function () {
  var box = document.getElementById("setq");
  var only = document.getElementById("setdiff");
  var note = document.getElementById("setq-note");
  var groups = [].slice.call(document.querySelectorAll("details.setgroup"));
  function apply() {
    var q = box.value.trim().toLowerCase();
    // «Только изменённые» — ответ на вопрос «что я тут накрутил полгода назад»,
    // с которым в настройки приходят чаще, чем за конкретным полем.
    var diff = only.checked;
    var narrow = Boolean(q) || diff;
    var found = 0;
    groups.forEach(function (group) {
      var shown = 0;
      [].slice.call(group.querySelectorAll("[data-find]")).forEach(function (field) {
        var hit = (!q || field.dataset.find.indexOf(q) >= 0)
          && (!diff || field.dataset.changed === "1");
        field.hidden = !hit;
        if (hit) { shown += 1; }
      });
      group.hidden = narrow && !shown;
      // Пустой запрос возвращает исходное состояние, а не «всё открыто»:
      // иначе после стирания строки страница остаётся километровой.
      group.open = narrow ? shown > 0 : group.dataset.open === "1";
      found += shown;
    });
    if (!narrow) {
      note.textContent = "Группы свёрнуты: разверни нужную или начни искать.";
    } else if (!found) {
      note.textContent = diff && !q
        ? "Ни одна настройка не отличается от значения по умолчанию."
        : "Ничего не нашлось.";
    } else {
      note.textContent = "Найдено настроек: " + found;
    }
  }
  box.addEventListener("input", apply);
  only.addEventListener("change", apply);
  apply();
})();
</script>
"""


def changed(field: "settings.Field", current: str) -> bool:
    """Отличается ли значение от значения по умолчанию.

    Секрет считается изменённым по факту наличия: сравнивать его с пустым
    значением по умолчанию можно, а показывать — нет.
    """
    if field.is_secret:
        return bool(current.strip())
    if field.kind == settings.BOOL:
        return settings.as_bool(current, settings.as_bool(field.default)) != (
            settings.as_bool(field.default)
        )
    return bool(current.strip()) and current.strip() != field.default.strip()


def settings_field(field: "settings.Field", current: str) -> str:
    """Одна настройка в форме. data-find — то, по чему её ищет строка поиска."""
    found = " ".join((field.label, field.key, field.help, field.group)).lower()
    mark = ' data-changed="1"' if changed(field, current) else ""

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
            '<div class=field data-find="{found}"{mark}>{control}'
            "<div class=hint>{key} · {hint}</div></div>"
        ).format(
            found=esc(found),
            mark=mark,
            control=control,
            key=esc(field.key),
            hint=esc(field.help),
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
        '<div class=field data-find="{found}"{mark}><label>{label}</label>'
        '<input type="{input_type}" name="{key}" value="{value}" '
        'placeholder="{placeholder}"{step}>'
        "<div class=hint>{key} · {hint}</div></div>"
    ).format(
        found=esc(found),
        mark=mark,
        label=esc(field.label),
        input_type=input_type,
        key=esc(field.key),
        value=esc(shown),
        placeholder=esc(placeholder),
        step=step,
        hint=esc(field.help),
    )


def review_sites_block(values: "dict[str, str]") -> str:
    """Галочки «искать отзывы только на».

    Отдельный блок, а не поле-строка: площадок пять, у каждой своя цена, и
    выбирать их глазами по подписи «должность и оценка из разметки» проще, чем
    вписывать адреса через запятую. Само значение лежит в той же настройке
    REVIEW_ONLY_SITES и видно в группе полей ниже.
    """
    import reviewsites

    chosen = set(reviewsites.selected())
    on = settings.as_bool(values.get("REVIEW_ONLY_PARSED", ""), False)
    boxes = []
    for site in reviewsites.SITES:
        note = site.note
        if site.guarded and not (values.get("REVIEW_FETCH_PROXY") or "").strip():
            note += " · без прокси не откроется"
        boxes.append(
            (
                '<label><input type=checkbox name="review_site_{code}" value="1"'
                "{checked}> {label}</label> <span class=muted>{note}</span><br>"
            ).format(
                code=esc(site.host),
                checked=" checked" if site.host in chosen else "",
                label=esc(site.label),
                note=esc(note),
            )
        )
    return (
        '<details class=setgroup data-open="0"><summary>Искать отзывы только на '
        "<span class=muted>· площадки с готовым парсером</span></summary>"
        '<input type=hidden name="review_sites_form" value="1">'
        '<div class=field data-find="искать отзывы только на площадки парсер '
        'review_only_parsed">{flag}{boxes}'
        "<div class=hint>Снятая галочка означает «не искать там вовсе». Режим "
        "выключен — площадки всё равно опрашиваются, плюс два широких запроса "
        "на статьи и треды.</div></div></details>"
    ).format(
        flag=(
            '<label><input type=checkbox name="REVIEW_ONLY_PARSED" value="1"{on}> '
            "Только эти площадки</label><br><br>"
        ).format(on=" checked" if on else ""),
        boxes="".join(boxes),
    )


def review_sites_value(form: "dict[str, list[str]]") -> "dict[str, str]":
    """Отмеченные галочки → значение REVIEW_ONLY_SITES. Формы нет — пусто."""
    import reviewsites

    if not (form.get("review_sites_form") or [""])[0]:
        return {}
    chosen = [
        site.host
        for site in reviewsites.SITES
        if (form.get("review_site_" + site.host) or [""])[0]
    ]
    # Ни одной галочки — значение пустое: это «все, у кого есть парсер», а не
    # «ни одной площадки». Пустой список означал бы досье без отзывов вовсе.
    return {"REVIEW_ONLY_SITES": ",".join(chosen)}


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
    parts.append(review_sites_block(values))

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




__all__ = (
    "changed",
    "render_settings",
    "review_sites_block",
    "review_sites_value",
    "settings_field",
)
