"""Графики на странице: инлайн-SVG, который рисует сервер.

Почему не библиотека. Машина работает офлайн, CDN недоступен, а тянуть в
репозиторий Chart.js ради четырёх картинок — лишние сотни килобайт и чужой
код в странице [CORE-025]. Здесь три примитива на чистых функциях: на вход
числа, на выход строка SVG, и её можно сравнить в тесте.

Ни одного скрипта в разметке: страница остаётся читаемой и без JavaScript.
"""

from __future__ import annotations

from typing import Sequence

from ui_core import esc

BRAND = "#0b5cd5"
SECOND = "#79a9f0"
LINE = "#dfe1e6"
MUTED = "#6b778c"
EMPTY = '<p class=muted>Данных пока нет.</p>'


def _short(name: str, limit: int = 26) -> str:
    """Подпись столбика. Длинная подпись налезала на полосу, поэтому режется."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _fmt(value: float) -> str:
    """Подпись числа: 1200 → «1,2к». Длинные числа ломают верстку столбиков."""
    if value >= 10000:
        return "{:.0f}к".format(value / 1000)
    if value >= 1000:
        return "{:.1f}к".format(value / 1000).replace(".", ",")
    return "{:.0f}".format(value)


def bars(items: Sequence[tuple[str, float]], width: int = 560, label_width: int = 200) -> str:
    """Горизонтальные столбики: подпись слева, полоса справа.

    Горизонтальные, потому что подписи здесь — названия компаний и этапов, а
    они не помещаются под вертикальными столбиками.
    """
    rows = [(str(name), float(value)) for name, value in items]
    if not rows:
        return EMPTY
    top = max((v for _, v in rows), default=0) or 1
    step, pad = 24, 6
    height = step * len(rows) + pad
    bar_area = max(60, width - label_width - 60)
    parts = [
        '<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" '
        'style="max-width:{w}px">'.format(w=width, h=height)
    ]
    for index, (name, value) in enumerate(rows):
        y = index * step + pad
        length = max(1.0, bar_area * value / top) if value else 0
        parts.append(
            '<text x="0" y="{ty}" font-size="12" fill="{muted}">{name}</text>'
            '<rect x="{bx}" y="{y}" width="{w:.1f}" height="14" rx="2" fill="{brand}"/>'
            '<text x="{tx:.1f}" y="{ty}" font-size="12" fill="{muted}">{value}</text>'.format(
                ty=y + 11, name=esc(_short(name)), muted=MUTED, brand=BRAND,
                bx=label_width, y=y, w=length, tx=label_width + length + 6,
                value=esc(_fmt(value)),
            )
        )
    parts.append("</svg>")
    return "".join(parts)


def columns(items: Sequence[tuple[str, float]], width: int = 520, height: int = 150) -> str:
    """Вертикальные столбики для гистограммы: подписи короткие, порядок важен."""
    rows = [(str(name), float(value)) for name, value in items]
    if not rows:
        return EMPTY
    top = max((v for _, v in rows), default=0) or 1
    inner = height - 24
    slot = width / len(rows)
    parts = [
        '<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" '
        'style="max-width:{w}px">'.format(w=width, h=height)
    ]
    for index, (name, value) in enumerate(rows):
        bar = inner * value / top
        x = index * slot
        parts.append(
            '<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="2" fill="{c}"/>'
            '<text x="{tx:.1f}" y="{ty}" font-size="10" fill="{muted}" '
            'text-anchor="middle">{name}</text>'.format(
                x=x + slot * 0.15, y=inner - bar, w=slot * 0.7, h=bar, c=BRAND,
                tx=x + slot / 2, ty=height - 8, muted=MUTED, name=esc(name),
            )
        )
    parts.append("</svg>")
    return "".join(parts)


def spark(points: Sequence[tuple[str, float, float]], width: int = 520, height: int = 140) -> str:
    """Две линии по дням: всего и «из них подходящих».

    Провал на графике почти всегда означает, что площадка отдала капчу, а не
    что рынок встал, — поэтому вторая линия обязательна: она падает иначе.
    """
    rows = [(str(day), float(a), float(b)) for day, a, b in points]
    if len(rows) < 2:
        return EMPTY
    top = max(max(a, b) for _, a, b in rows) or 1
    inner = height - 20
    stepx = width / (len(rows) - 1)

    def path(index: int) -> str:
        dots = [
            "{:.1f},{:.1f}".format(i * stepx, inner - inner * row[index] / top)
            for i, row in enumerate(rows)
        ]
        return " ".join(dots)

    return (
        '<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" '
        'style="max-width:{w}px">'
        '<polyline fill="none" stroke="{second}" stroke-width="2" points="{all}"/>'
        '<polyline fill="none" stroke="{brand}" stroke-width="2" points="{fit}"/>'
        '<line x1="0" y1="{inner}" x2="{w}" y2="{inner}" stroke="{line}"/>'
        '<text x="0" y="{ty}" font-size="11" fill="{muted}">{first}</text>'
        '<text x="{w}" y="{ty}" font-size="11" fill="{muted}" text-anchor="end">{last}</text>'
        "</svg>"
    ).format(
        w=width, h=height, inner=inner, ty=height - 4, line=LINE, muted=MUTED,
        second=SECOND, brand=BRAND, all=path(1), fit=path(2),
        first=esc(rows[0][0]), last=esc(rows[-1][0]),
    )


def legend(pairs: Sequence[tuple[str, str]]) -> str:
    """Подпись к линиям: без неё две полоски одного оттенка ничего не значат."""
    items = [
        '<span style="color:{}">■</span> {}'.format(color, esc(name)) for name, color in pairs
    ]
    return '<p class=muted>{}</p>'.format(" · ".join(items))


__all__ = ("BRAND", "EMPTY", "SECOND", "bars", "columns", "legend", "spark")
