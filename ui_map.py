"""Карта: где физически находятся вакансии.

Список отвечает на вопрос «что есть», карта — на вопрос «сколько ехать». Это
разные вопросы, поэтому карта — отдельный раздел, а не вкладка в списке.

Две честные оговорки, которые видны прямо на странице, а не только в документации:

- вакансий без адреса (удалёнка, «адрес сообщим позже») на карте нет, и их
  число написано рядом: иначе карта тихо прячет половину выборки;
- точка — это адрес из вакансии, иногда юридический адрес компании, а не место
  работы.

Тайлы и библиотека Leaflet грузятся из сети — это единственное место в интерфейсе,
которому нужен интернет в браузере. Без сети страница не падает, а показывает
те же точки таблицей адресов [CORE-017].

Шаблоны — только str.format, но JS собирается через replace: в коде скрипта фигурные
скобки встречаются на каждой строке, и удваивать их было бы нечитаемо.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any
from urllib.parse import urlencode

import geo
from ui_core import esc, table

DEFAULT_TILES = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUTION = (
    'Данные &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
)
LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
SCORE_STEPS = ((0.0, "любой"), (40.0, "от 40"), (60.0, "от 60"), (80.0, "от 80"))
# Москва и обзорный масштаб: начальный вид до того, как приедут точки.
CENTER = (55.75, 37.62)


def tile_url() -> str:
    """Адрес тайлов. Меняется в .env, если OSM недоступен или есть свой источник."""
    return os.getenv("MAP_TILE_URL", "").strip() or DEFAULT_TILES


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def view(params: dict[str, Any]) -> dict[str, Any]:
    """Вид карты из адресной строки: ссылку можно сохранить и отправить."""
    return {
        "min": max(0.0, _float(params.get("min"), 0.0)),
        "area": str(params.get("area") or "").strip(),
        "q": str(params.get("q") or "").strip(),
        "key": str(params.get("key") or "").strip(),
    }


def _query(chosen: dict[str, Any]) -> str:
    pairs = [(name, chosen[name]) for name in ("min", "area", "q", "key") if chosen[name]]
    return ("?" + urlencode(pairs)) if pairs else ""


def points_json(conn: sqlite3.Connection, params: dict[str, Any]) -> str:
    """Точки отдельным ответом: страница открывается сразу, а не ждёт две тысячи меток."""
    geo.ensure_schema(conn)
    chosen = view(params)
    found = geo.points(conn, chosen["min"], chosen["area"], chosen["q"])
    return json.dumps(
        {"points": found, "focus": chosen["key"]}, ensure_ascii=False
    )


MAP_JS = """
(function () {
  var box = document.getElementById('map');
  var fallback = document.getElementById('maplist');
  if (!window.L) {
    box.innerHTML = '<p class=muted>Карта не загрузилась: нет доступа к сети. Список адресов ниже.</p>';
    if (fallback) { fallback.open = true; }
    return;
  }
  var map = L.map('map').setView([__LAT__, __LNG__], 9);
  L.tileLayer('__TILES__', { maxZoom: 18, attribution: '__ATTR__' }).addTo(map);
  function clean(value) {
    var node = document.createElement('span');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }
  fetch('__URL__').then(function (response) {
    return response.json();
  }).then(function (data) {
    var items = (data && data.points) || [];
    var bounds = [];
    var focused = null;
    items.forEach(function (item) {
      var marker = L.marker([item.lat, item.lng]).addTo(map);
      var lines = [
        '<b>' + clean(item.title) + '</b>',
        clean(item.company),
        clean(item.address || item.area),
        item.metro ? 'метро: ' + clean(item.metro) : '',
        'скор: ' + clean(Math.round(item.score)),
        '<a href="/vacancy?key=' + encodeURIComponent(item.key) + '">карточка</a>'
      ];
      marker.bindPopup(lines.filter(Boolean).join('<br>'));
      bounds.push([item.lat, item.lng]);
      if (data.focus && item.key === data.focus) { focused = marker; }
    });
    if (focused) {
      map.setView(focused.getLatLng(), 15);
      focused.openPopup();
    } else if (bounds.length) {
      map.fitBounds(bounds, { padding: [30, 30] });
    }
  }).catch(function () {
    box.innerHTML = '<p class=muted>Точки не загрузились. Список адресов ниже.</p>';
    if (fallback) { fallback.open = true; }
  });
})();
"""


def _script(chosen: dict[str, Any]) -> str:
    body = MAP_JS
    body = body.replace("__LAT__", str(CENTER[0])).replace("__LNG__", str(CENTER[1]))
    body = body.replace("__TILES__", tile_url()).replace("__ATTR__", ATTRIBUTION)
    body = body.replace("__URL__", "/map/points.json" + _query(chosen))
    return "<script>{}</script>".format(body)


def _option(value: str, label: str, current: str) -> str:
    mark = " selected" if str(value) == str(current) else ""
    return '<option value="{}"{}>{}</option>'.format(esc(value), mark, esc(label))


def _filters(conn: sqlite3.Connection, chosen: dict[str, Any]) -> str:
    """Те же три вопроса, что и в списке: где, насколько интересно, про что."""
    cities = "".join(
        [_option("", "все города", chosen["area"])]
        + [
            _option(name, "{} ({})".format(name, total), chosen["area"])
            for name, total in geo.areas(conn)
        ]
    )
    scores = "".join(
        _option(
            "" if value == 0.0 else str(int(value)),
            label,
            "" if chosen["min"] == 0.0 else str(int(chosen["min"])),
        )
        for value, label in SCORE_STEPS
    )
    return (
        '<form method=get action="/map" class=filters>'
        '<label class=filt>Город<br><select name=area>{cities}</select></label>'
        '<label class=filt>Скор<br><select name=min>{scores}</select></label>'
        '<label class=filt>Поиск<br>'
        '<input type=search name=q value="{q}" placeholder="должность или компания"></label>'
        "<label class=filt><br><button>Показать</button></label>"
        '<label class=filt><br><a class=chip href="/map">Сбросить</a></label>'
        "</form>"
    ).format(cities=cities, scores=scores, q=esc(chosen["q"]))


def _list(found: list[dict[str, Any]]) -> str:
    """Те же точки текстом — работает без сети и читается с клавиатуры."""
    rows = [
        [
            '<a href="/vacancy?key={}">{}</a>'.format(esc(item["key"]), esc(item["title"])),
            esc(item["company"]),
            esc(item["address"] or item["area"]),
            esc(item["metro"]),
            '<span class=score>{}</span>'.format(int(round(item["score"]))),
        ]
        for item in found[:200]
    ]
    if not rows:
        return "<p class=muted>Под эти условия точек нет.</p>"
    return table(["Вакансия", "Компания", "Адрес", "Метро", "Скор"], rows)


def render_map(conn: sqlite3.Connection, params: dict[str, Any]) -> str:
    geo.ensure_schema(conn)
    chosen = view(params)
    found = geo.points(conn, chosen["min"], chosen["area"], chosen["q"])
    mapped, total = geo.coverage(conn)
    blind = max(0, total - mapped)

    note = "На карте {} точек из {} вакансий с адресом.".format(len(found), mapped)
    if blind:
        note += (
            " Ещё {} вакансий без адреса — удалёнка или адрес не указан;"
            " для старых записей помогает задача «Адреса для карты» на странице запуска."
        ).format(blind)

    warn = ""
    if mapped == 0:
        warn = (
            "<div class=warn>Ни у одной вакансии нет координат. Они появляются при сборе или"
            " после задачи «Адреса для карты».</div>"
        )

    return (
        '<link rel=stylesheet href="{css}">'
        '<script src="{js}"></script>'
        "{filters}{warn}"
        '<p class=muted>{note}</p>'
        '<div id=map style="height:560px;border:1px solid var(--line);border-radius:4px"></div>'
        "{script}"
        "<details id=maplist><summary>Список адресов <span class=muted>то же самое без карты</span></summary>{rows}</details>"
    ).format(
        css=LEAFLET_CSS,
        js=LEAFLET_JS,
        filters=_filters(conn, chosen),
        warn=warn,
        note=esc(note),
        script=_script(chosen),
        rows=_list(found),
    )


def link(conn: sqlite3.Connection, key: str) -> str:
    """Строка адреса для карточки вакансии со ссылкой на конкретную точку."""
    geo.ensure_schema(conn)
    item = geo.one(conn, key)
    if item is None:
        return ""
    parts = []
    if item["address"]:
        parts.append(esc(item["address"]))
    if item["metro"]:
        parts.append("метро " + esc(item["metro"]))
    if item["lat"] is not None:
        parts.append(
            '<a href="/map?key={}">показать на карте</a>'.format(esc(item["key"]))
        )
    if not parts:
        return ""
    return "<p class=muted>{}</p>".format(" &middot; ".join(parts))


def hint(conn: sqlite3.Connection) -> str:
    """Короткая строка над списком вакансий: сколько из них видно на карте."""
    geo.ensure_schema(conn)
    mapped, total = geo.coverage(conn)
    if not mapped:
        return ""
    return (
        '<p class=muted><a href="/map">На карте</a> — {} из {} вакансий с адресом.</p>'
    ).format(mapped, total)


__all__ = (
    "ATTRIBUTION",
    "CENTER",
    "DEFAULT_TILES",
    "SCORE_STEPS",
    "hint",
    "link",
    "points_json",
    "render_map",
    "tile_url",
    "view",
)
