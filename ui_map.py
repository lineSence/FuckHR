"""Карта: где физически находятся вакансии.

Список отвечает на вопрос «что есть», карта — на вопрос «сколько ехать». Это
разные вопросы, поэтому карта — отдельный раздел, а не вкладка в списке.

Фильтры здесь те же, что в списке вакансий, и это главное решение страницы:
свои три поля на карте значили, что «свежие» в двух разделах — два разных ответа.
Поверх общих фильтров добавлены два вопроса, которые осмысленны только на карте:
станция метро и радиус от точки (geo_query.py).

Цвет метки — оценка работодателя. Без цвета карта отвечает только «где», а
выбор делается по «где и к кому»; легенда рядом, иначе цвета надо угадывать.

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
from typing import Any, Mapping

import company_score_rules as CSR
import filters
import geo
import geo_query
import jobs
import ui_filters
from ui_core import esc, table

# Строка уходит в Leaflet как есть: подстановки s/z/x/y делает он сам. Через
# str.format этот адрес никогда не проходит, поэтому скобки не удваиваются.
DEFAULT_TILES = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUTION = (
    'Данные &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
)
LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
SCORE_STEPS = ((0.0, "любой"), (40.0, "от 40"), (60.0, "от 60"), (80.0, "от 80"))
# Москва и обзорный масштаб: начальный вид до того, как приедут точки.
CENTER = (55.75, 37.62)
# Задача сбора адресов — та же, что и в CLI, с ключом --all внутри jobs.TASKS.
BACKFILL_TASK = "geo-backfill"
# Радиус по умолчанию при первом клике по карте.
DEFAULT_RADIUS = 5

# Серый — это «не смотрели», а не «нормально»: отсутствие оценки не похоже ни на
# зелёный, ни на красный.
NO_LEVEL = "none"
LEVEL_COLORS = {
    CSR.LEVEL_GREEN: "#2e7d32",
    CSR.LEVEL_YELLOW: "#b8860b",
    CSR.LEVEL_RED: "#c62828",
    CSR.LEVEL_UNKNOWN: "#6b7280",
    NO_LEVEL: "#9aa0a6",
}
# Поля быстрой строки: три вопроса из четырёх задают именно их.
QUICK_KEYS = ("area", "metro", "min_score", "q", "radius")


def tile_url() -> str:
    """Адрес тайлов. Меняется в .env, если OSM недоступен или есть свой источник."""
    return os.getenv("MAP_TILE_URL", "").strip() or DEFAULT_TILES


def view(params: Mapping[str, Any]) -> dict[str, str]:
    """Вид карты из адресной строки: ссылку можно сохранить и отправить.

    Быстрые виды те же, что у списка: `?view=fresh` должен значить одно и то же
    в обоих разделах. Старая ссылка с `min=60` продолжает работать: карта жила
    со своими параметрами, и ломать сохранённые ссылки не за что [CORE-017].
    """
    chosen = ui_filters.apply_preset(filters.VACANCY_PRESETS, params)
    legacy = str(chosen.pop("min", "") or "").strip()
    if legacy and not chosen.get("min_score"):
        chosen["min_score"] = legacy
    return chosen


def _url(chosen: Mapping[str, str], drop: str = "") -> str:
    query = filters.query_string(chosen, drop=drop)
    return "/map" + ("?" + query if query else "")


def points_json(conn: sqlite3.Connection, params: dict[str, Any]) -> str:
    """Точки отдельным ответом: страница открывается сразу, а не ждёт две тысячи меток."""
    chosen = view(params)
    found = geo_query.select(conn, chosen)
    return json.dumps(
        {
            "points": found.points,
            "focus": str(params.get("key") or "").strip(),
            "center": list(found.center) if found.center else None,
            "radius": found.radius,
        },
        ensure_ascii=False,
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
  var colors = __COLORS__;
  var base = '__BASE__';
  var radius = __RADIUS__;
  var map = L.map('map').setView([__LAT__, __LNG__], 9);
  L.tileLayer('__TILES__', { maxZoom: 18, attribution: '__ATTR__' }).addTo(map);
  function clean(value) {
    var node = document.createElement('span');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  }
  map.on('click', function (event) {
    var sep = base.indexOf('?') >= 0 ? '&' : '?';
    window.location = base + sep + 'clat=' + event.latlng.lat.toFixed(5) +
      '&clng=' + event.latlng.lng.toFixed(5) + '&radius=' + radius;
  });
  fetch('__URL__').then(function (response) {
    return response.json();
  }).then(function (data) {
    var items = (data && data.points) || [];
    var bounds = [];
    var focused = null;
    if (data.center) {
      L.circle(data.center, {
        radius: data.radius * 1000, color: '#3f6fb5', weight: 1, fillOpacity: 0.05
      }).addTo(map);
      L.circleMarker(data.center, {
        radius: 5, color: '#3f6fb5', fillColor: '#3f6fb5', fillOpacity: 1
      }).addTo(map).bindPopup('Центр поиска. Клик по карте переносит его.');
      bounds.push(data.center);
    }
    items.forEach(function (item) {
      var marker = L.circleMarker([item.lat, item.lng], {
        radius: 7,
        weight: 1,
        color: '#ffffff',
        fillColor: colors[item.level || 'none'] || colors.none,
        fillOpacity: 0.9
      }).addTo(map);
      var lines = [
        '<b>' + clean(item.title) + '</b>',
        clean(item.company),
        clean(item.address || item.area),
        item.metro ? 'метро: ' + clean(item.metro) : '',
        item.distance == null ? '' : 'от центра ' + item.distance.toFixed(1) + ' км по прямой',
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


def _script(chosen: Mapping[str, str], found: geo_query.Selection) -> str:
    """Скрипт карты. Всё, что зависит от запроса, подставляется здесь, а не в JS."""
    center = found.center or CENTER
    base = _url(
        {key: value for key, value in chosen.items() if key not in ("clat", "clng", "radius")}
    )
    body = MAP_JS
    body = body.replace("__LAT__", str(center[0])).replace("__LNG__", str(center[1]))
    body = body.replace("__TILES__", tile_url()).replace("__ATTR__", ATTRIBUTION)
    body = body.replace("__COLORS__", json.dumps(LEVEL_COLORS, ensure_ascii=False))
    body = body.replace("__BASE__", base)
    body = body.replace("__RADIUS__", str(found.radius or DEFAULT_RADIUS))
    body = body.replace("__URL__", "/map/points.json" + _url(chosen)[len("/map"):])
    return "<script>{}</script>".format(body)


def _option(value: str, label: str, current: str) -> str:
    mark = " selected" if str(value) == str(current) else ""
    return '<option value="{}"{}>{}</option>'.format(esc(value), mark, esc(label))


def _select(name: str, options: list[tuple[str, str]], current: str) -> str:
    body = "".join(_option(value, label, current) for value, label in options)
    return '<select name="{}">{}</select>'.format(esc(name), body)


def _hidden(chosen: Mapping[str, str], skip: tuple[str, ...]) -> str:
    """Что не показано в форме, то теряется при отправке — если не перенести скрытым."""
    return "".join(
        '<input type=hidden name="{}" value="{}">'.format(esc(key), esc(str(value)))
        for key, value in chosen.items()
        if key not in skip and str(value or "").strip()
    )


def _quick(conn: sqlite3.Connection, chosen: Mapping[str, str], has_center: bool) -> str:
    """Быстрая строка: город, метро, радиус, скор, поиск.

    Полная форма со всеми полями лежит в `ui_filters.form` ниже и свёрнута. Здесь то,
    что спрашивают почти всегда: станция и радиус списком, потому что руками
    станцию набирают с опечатками.
    """
    cities = [("", "все города")] + [
        (name, "{} ({})".format(name, total)) for name, total in geo.areas(conn)
    ]
    metros = [("", "любое метро")] + [
        (name, "{} ({})".format(name, total))
        for name, total in geo_query.stations(conn)
    ]
    scores = [
        ("" if value == 0.0 else str(int(value)), label) for value, label in SCORE_STEPS
    ]
    current_score = str(chosen.get("min_score", "") or "")
    if current_score.endswith(".0"):
        current_score = current_score[:-2]
    radii = [("", "везде")] + [
        (str(step), "{} км".format(step)) for step in geo_query.RADIUS_STEPS
    ]
    radius_field = (
        '<label class=filt>Радиус от точки<br>{}</label>'.format(
            _select("radius", radii, str(chosen.get("radius", "") or ""))
        )
        if has_center
        else '<label class=filt>Радиус<br><span class=muted>кликните по карте</span></label>'
    )
    return (
        '<form method=get action="/map" class=filters>{hidden}'
        '<label class=filt>Город<br>{cities}</label>'
        '<label class=filt>Метро<br>{metros}</label>'
        "{radius}"
        '<label class=filt>Скор<br>{scores}</label>'
        '<label class=filt>Поиск<br>'
        '<input type=search name=q value="{q}" placeholder="должность или компания"></label>'
        "<label class=filt><br><button>Показать</button></label>"
        '<label class=filt><br><a class=chip href="/map">Сбросить</a></label>'
        "</form>"
    ).format(
        hidden=_hidden(chosen, QUICK_KEYS),
        cities=_select("area", cities, str(chosen.get("area", "") or "")),
        metros=_select("metro", metros, str(chosen.get("metro", "") or "")),
        radius=radius_field,
        scores=_select("min_score", scores, current_score),
        q=esc(str(chosen.get("q", "") or "")),
    )


def _legend() -> str:
    """Цвет без подписи — угадайка, поэтому легенда стоит рядом с картой."""
    items = [
        (CSR.LEVEL_GREEN, CSR.LEVEL_RU[CSR.LEVEL_GREEN]),
        (CSR.LEVEL_YELLOW, CSR.LEVEL_RU[CSR.LEVEL_YELLOW]),
        (CSR.LEVEL_RED, CSR.LEVEL_RU[CSR.LEVEL_RED]),
        (CSR.LEVEL_UNKNOWN, CSR.LEVEL_RU[CSR.LEVEL_UNKNOWN]),
        (NO_LEVEL, "оценки нет"),
    ]
    dots = "".join(
        '<span class=chip><span style="display:inline-block;width:10px;height:10px;'
        'border-radius:50%;background:{color};vertical-align:middle"></span> {label}</span>'.format(
            color=LEVEL_COLORS[level], label=esc(label)
        )
        for level, label in items
    )
    return (
        "<div class=chips>{}</div>"
        '<p class=muted>Цвет метки — оценка работодателя из досье, а не качество вакансии.</p>'
    ).format(dots)


def _backfill(blind: int) -> str:
    """Кнопка сбора адресов рядом с тем, ради чего она нужна.

    Берёт все недостающие адреса сразу, а не порцию: половинчатая карта хуже
    долгой задачи. Сколько это займёт, написано честно и до нажатия.
    """
    if not blind:
        return "<p class=muted>Адреса есть у всех вакансий, которые их сообщили.</p>"
    busy = jobs.runner.active()
    if busy is not None:
        return (
            '<p class=muted>Сейчас идёт задача «{}» — '
            '<a href="/?job={}">смотреть лог</a>. Адреса можно собрать после неё.</p>'
        ).format(esc(busy.title), busy.id)
    # Грубая, но честная оценка: пауза между страницами порядка двух секунд.
    minutes = max(1, int(round(blind * 2.5 / 60.0)))
    return (
        '<form method=post action="/map/geo" class=filters>'
        "<label class=filt><button>Собрать все адреса</button></label>"
        '<label class=filt><span class=muted>Осталось {blind} вакансий без точки. '
        "Задача откроет каждую страницу на hh.ru с паузой — примерно {minutes} мин. "
        "Можно остановить в любой момент, найденное останется.</span></label>"
        "</form>"
    ).format(blind=blind, minutes=minutes)


def _list(found: geo_query.Selection) -> str:
    """Те же точки текстом — работает без сети и читается с клавиатуры."""
    head = ["Вакансия", "Компания", "Адрес", "Метро", "Работодатель", "Скор"]
    if found.radius:
        head.append("От центра")
    rows = []
    for item in found.points[:200]:
        level = item["level"] or ""
        row = [
            '<a href="/vacancy?key={}">{}</a>'.format(esc(item["key"]), esc(item["title"])),
            esc(item["company"]),
            esc(item["address"] or item["area"]),
            esc(item["metro"]),
            '<span style="color:{}">&#9679;</span> {}'.format(
                LEVEL_COLORS.get(level, LEVEL_COLORS[NO_LEVEL]),
                esc(CSR.LEVEL_RU.get(level, "оценки нет")),
            ),
            '<span class=score>{}</span>'.format(int(round(item["score"]))),
        ]
        if found.radius:
            row.append("{:.1f} км".format(item["distance"] or 0.0))
        rows.append(row)
    if not rows:
        return "<p class=muted>Под эти условия точек нет.</p>"
    return table(head, rows)


def render_map(
    conn: sqlite3.Connection, params: dict[str, Any], note: str = ""
) -> str:
    chosen = view(params)
    found = geo_query.select(conn, chosen)
    mapped, total = geo.coverage(conn)
    blind = max(0, total - mapped)

    line = (
        "На карте {shown} точек из {mapped} вакансий с адресом; ещё {blind} без адреса сюда не попадают."
    ).format(shown=len(found.points), mapped=mapped, blind=blind)

    near = ""
    if found.radius:
        near = (
            "<p class=muted>Расстояние считается по прямой, не по дорогам.{extra}</p>"
        ).format(
            extra=" Рядом, но за кругом: ещё {} точек.".format(found.dropped)
            if found.dropped
            else ""
        )

    warn = ""
    if mapped == 0:
        warn = (
            "<div class=warn>Ни у одной вакансии нет координат. Они появляются при сборе"
            " или по кнопке «Собрать все адреса».</div>"
        )

    hidden = {
        key: value
        for key, value in chosen.items()
        if key in ("clat", "clng", "radius", "view", "sort", "key")
    }
    return (
        '<link rel=stylesheet href="{css}">'
        '<script src="{js}"></script>'
        "{note}{presets}{quick}{chips}{form}{sort}{warn}{backfill}"
        "<p class=muted>{line}</p>{near}{legend}"
        '<div id=map style="height:560px;border:1px solid var(--line);border-radius:4px"></div>'
        "{script}"
        "<details id=maplist><summary>Список адресов <span class=muted>то же самое без карты</span></summary>{rows}</details>"
    ).format(
        css=LEAFLET_CSS,
        js=LEAFLET_JS,
        note=note,
        presets=ui_filters.presets_line(filters.VACANCY_PRESETS, chosen, "/map"),
        quick=_quick(conn, chosen, found.center is not None),
        chips=ui_filters.chips(found.active, chosen, "/map"),
        form=ui_filters.form(
            geo_query.MAP_FILTERS,
            chosen,
            "/map",
            active_count=len(found.active),
            hidden=hidden,
        ),
        sort=""
        if found.radius
        else ui_filters.sort_line(filters.VACANCY_SORTS, chosen, "/map"),
        warn=warn,
        backfill=_backfill(blind),
        line=esc(line),
        near=near,
        legend=_legend(),
        script=_script(chosen, found),
        rows=_list(found),
    )


def start_backfill() -> tuple[int | None, str]:
    """Запускает сбор всех недостающих адресов. (id задачи, сообщение).

    Из браузера не приходит ни одного аргумента: команда целиком взята из
    jobs.TASKS [CORE-023].
    """
    try:
        job = jobs.runner.start(BACKFILL_TASK)
    except (KeyError, RuntimeError) as exc:
        return None, "<div class=warn>{}</div>".format(esc(exc))
    return job.id, ""


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
        # Второй вопрос к адресу всегда один: а что ещё есть рядом.
        parts.append(
            '<a href="/map?clat={lat}&clng={lng}&radius={radius}">что рядом</a>'.format(
                lat=item["lat"], lng=item["lng"], radius=DEFAULT_RADIUS
            )
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
    "BACKFILL_TASK",
    "CENTER",
    "DEFAULT_RADIUS",
    "DEFAULT_TILES",
    "LEVEL_COLORS",
    "NO_LEVEL",
    "QUICK_KEYS",
    "SCORE_STEPS",
    "hint",
    "link",
    "points_json",
    "render_map",
    "start_backfill",
    "tile_url",
    "view",
)
