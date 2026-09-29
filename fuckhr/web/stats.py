"""Счётчики для страницы «Статистика»: только SQL по своей базе.

Отдельно от интерфейса, потому что здесь считается, а там рисуется: запрос
проверяется тестом на базе в памяти, а HTML — нет. Сеть и модель не трогаются.

Что здесь считается — это статистика того, что уже случилось, а не измерение
качества пайплайна [CORE-019]: «отсеялось 80%» не значит «80% были мусором».

Два разреза общие для всех счётчиков: профиль (у проекта их несколько, и
смешивать питониста с аналитиком нельзя) и окно в днях. Профиль пустой —
считаем по всей базе.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

TOP = 10                  # длина любого топа: дальше читать перестают
BINS = 12                 # столбиков в гистограмме зарплат
MANY_REPUBLISH = 3        # столько публикаций одной вакансии — уже вопрос


def since(days: int) -> str:
    """Граница окна в том же формате, в каком лежат отметки времени."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _scope(profile: str) -> tuple[str, str, list]:
    """(join, условие, параметры) для разреза по профилю.

    Скор берётся профильный: в `vacancies.score` лежит лучший по всем
    профилям, и на разрезе он завысил бы долю подходящих.
    """
    if profile:
        return (
            " JOIN vacancy_profiles p ON p.key = v.key AND p.profile_id = ?",
            "COALESCE(p.score, 0)",
            [profile],
        )
    return "", "COALESCE(v.score, 0)", []


def profiles_seen(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Профили, по которым в базе вообще есть вакансии."""
    try:
        rows = conn.execute(
            "SELECT profile_id, COUNT(*) AS n FROM vacancy_profiles"
            " GROUP BY profile_id ORDER BY n DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(str(r[0]), int(r[1])) for r in rows]


def funnel(conn: sqlite3.Connection, profile: str, days: int, threshold: float) -> list[tuple[str, int]]:
    """Путь вакансии от «встретили» до «я её посмотрел»."""
    join, score, params = _scope(profile)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS seen,
            SUM(COALESCE(v.description, '') <> '') AS described,
            SUM(EXISTS (SELECT 1 FROM vacancy_conditions c WHERE c.key = v.key)) AS parsed,
            SUM(EXISTS (SELECT 1 FROM vacancy_signals s WHERE s.key = v.key)) AS detected,
            SUM({score} >= ?) AS fit,
            SUM(EXISTS (SELECT 1 FROM company_dossier d WHERE d.company = v.company)) AS dossier,
            SUM(v.notified_at IS NOT NULL) AS notified,
            SUM(COALESCE(v.feedback, '') <> '') AS answered
        FROM vacancies v{join}
        WHERE v.first_seen_at >= ?
        """.format(score=score, join=join),
        [threshold] + params + [since(days)],
    ).fetchone()
    names = (
        ("встретили", "seen"),
        ("скачали описание", "described"),
        ("разобрали условия", "parsed"),
        ("прогнали детектор", "detected"),
        ("выше порога профиля", "fit"),
        ("есть досье компании", "dossier"),
        ("ушло в Telegram", "notified"),
        ("я отметил", "answered"),
    )
    return [(title, int(row[key] or 0)) for title, key in names]


def daily(conn: sqlite3.Connection, profile: str, days: int, threshold: float) -> list[tuple[str, int, int]]:
    """(день, встречено, из них выше порога). Провал в графике — обычно капча."""
    join, score, params = _scope(profile)
    rows = conn.execute(
        """
        SELECT substr(v.first_seen_at, 1, 10) AS day,
               COUNT(*) AS seen,
               SUM({score} >= ?) AS fit
        FROM vacancies v{join}
        WHERE v.first_seen_at >= ?
        GROUP BY day ORDER BY day
        """.format(score=score, join=join),
        [threshold] + params + [since(days)],
    ).fetchall()
    return [(str(r[0]), int(r[1] or 0), int(r[2] or 0)) for r in rows]


def salaries(conn: sqlite3.Connection, profile: str, days: int) -> dict:
    """Точки вилок в рублях, медиана и доля вакансий без вилки.

    Точка — середина вилки, как в market_store: сравнивать «от» с «до» нельзя.
    До вычета и на руки не приводятся друг к другу: коэффициент выдумывать
    нечестно, поэтому доля «до вычета» просто показана рядом [CORE-019].
    """
    join, _, params = _scope(profile)
    rows = conn.execute(
        """
        SELECT v.salary_from, v.salary_to, v.currency, v.gross
        FROM vacancies v{join}
        WHERE v.first_seen_at >= ?
        """.format(join=join),
        params + [since(days)],
    ).fetchall()
    points: list[float] = []
    gross = shown = 0
    for low, high, currency, is_gross in rows:
        if (currency or "RUR") not in ("RUR", "RUB", ""):
            continue
        pair = [float(x) for x in (low, high) if x]
        if not pair:
            continue
        shown += 1
        gross += 1 if is_gross else 0
        points.append(sum(pair) / len(pair))
    points.sort()
    return {
        "total": len(rows),
        "shown": shown,
        "gross": gross,
        "median": median(points),
        "points": points,
    }


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def histogram(points: list[float], bins: int = BINS) -> list[tuple[str, int]]:
    """Гистограмма вилок. Подписи — тысячи рублей: рубли не читаются."""
    if not points:
        return []
    low, high = points[0], points[-1]
    if high <= low:
        return [("{:.0f}к".format(low / 1000), len(points))]
    step = (high - low) / bins
    counts = Counter(min(bins - 1, int((p - low) / step)) for p in points)
    return [
        ("{:.0f}к".format((low + i * step) / 1000), counts.get(i, 0)) for i in range(bins)
    ]


def employers(conn: sqlite3.Connection, profile: str, days: int, limit: int = TOP) -> list[tuple[str, int]]:
    join, _, params = _scope(profile)
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(v.company, ''), 'без работодателя') AS name, COUNT(*) AS n
        FROM vacancies v{join}
        WHERE v.first_seen_at >= ?
        GROUP BY name ORDER BY n DESC, name LIMIT ?
        """.format(join=join),
        params + [since(days), limit],
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def company_levels(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Светофор работодателей. Считается по всей базе: досье не привязано к окну."""
    try:
        rows = conn.execute(
            "SELECT level, COUNT(*) AS n FROM company_score GROUP BY level ORDER BY n DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(str(r[0]), int(r[1])) for r in rows]


def text_quality(conn: sqlite3.Connection, profile: str, days: int) -> dict:
    """HR-клише, инъекции и ИИ-текст: из чего состоит брехня в описаниях."""
    join, _, params = _scope(profile)
    window = params + [since(days)]
    rows = conn.execute(
        """
        SELECT s.flags FROM vacancy_signals s
        JOIN vacancies v ON v.key = s.key{join}
        WHERE v.first_seen_at >= ?
        """.format(join=join),
        window,
    ).fetchall()
    flags: Counter[str] = Counter()
    flagged = 0
    for (raw,) in rows:
        names = [f for f in (raw or "").split(",") if f]
        flagged += 1 if names else 0
        flags.update(names)
    ai = conn.execute(
        """
        SELECT COALESCE(NULLIF(v.ai_label, ''), 'не проверяли') AS label, COUNT(*) AS n
        FROM vacancies v{join}
        WHERE v.first_seen_at >= ? GROUP BY label ORDER BY n DESC
        """.format(join=join),
        window,
    ).fetchall()
    try:
        injections = conn.execute(
            "SELECT level, COUNT(*) AS n FROM injection_hits GROUP BY level ORDER BY n DESC"
        ).fetchall()
    except sqlite3.Error:
        injections = []
    return {
        "reports": len(rows),
        "flagged": flagged,
        "top": flags.most_common(TOP),
        "ai": [(str(r[0]), int(r[1])) for r in ai],
        "injections": [(str(r[0]), int(r[1])) for r in injections],
    }


def lifetime(conn: sqlite3.Connection, profile: str, days: int) -> dict:
    """Сколько вакансия висит и сколько раз её перевывешивают.

    Перепубликации считаются так же, как в детекторе (`db.republish_count`):
    разные даты публикации или разные внешние идентификаторы у одной вакансии.
    """
    join, _, params = _scope(profile)
    rows = conn.execute(
        """
        SELECT v.key,
               julianday(v.last_seen_at) - julianday(v.first_seen_at) AS alive,
               (SELECT MAX(
                    COUNT(DISTINCT substr(sn.published_at, 1, 10)),
                    COUNT(DISTINCT sn.external_id))
                  FROM vacancy_snapshots sn WHERE sn.key = v.key) AS posts
        FROM vacancies v{join}
        WHERE v.first_seen_at >= ?
        """.format(join=join),
        params + [since(days)],
    ).fetchall()
    alive = sorted(float(r[1] or 0) for r in rows)
    posts = Counter(min(int(r[2] or 0), MANY_REPUBLISH + 1) for r in rows)
    return {
        "tracked": len(rows),
        "median_days": round(median(alive), 1),
        "republished": sum(n for times, n in posts.items() if times >= MANY_REPUBLISH),
        "posts": [
            ("{}{}".format(times, "+" if times > MANY_REPUBLISH else ""), count)
            for times, count in sorted(posts.items())
        ],
    }


def sources(conn: sqlite3.Connection, days: int) -> list[tuple[str, int]]:
    try:
        rows = conn.execute(
            "SELECT source, COUNT(DISTINCT key) AS n FROM vacancy_sources"
            " WHERE last_seen >= ? GROUP BY source ORDER BY n DESC",
            (since(days),),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(str(r[0]), int(r[1])) for r in rows]


def model_usage(conn: sqlite3.Connection, days: int) -> list[tuple[str, int]]:
    """Ответы модели в кеше по этапам.

    Это не полный учёт вызовов: в кеш попадает не всё, а повтор одного и того
    же текста считается один раз. Настоящий счётчик — отдельная задача.
    """
    try:
        rows = conn.execute(
            "SELECT stage, COUNT(*) AS n FROM llm_cache WHERE created_at >= ?"
            " GROUP BY stage ORDER BY n DESC",
            (since(days),),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(str(r[0]), int(r[1])) for r in rows]


def reviews(conn: sqlite3.Connection) -> dict:
    """Отзывы: сколько собрано, сколько помечено накруткой."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(label <> 'clean') AS marked,"
            " COUNT(DISTINCT company) AS companies FROM review_items"
        ).fetchone()
    except sqlite3.Error:
        return {"items": 0, "marked": 0, "companies": 0}
    return {
        "items": int(row[0] or 0),
        "marked": int(row[1] or 0),
        "companies": int(row[2] or 0),
    }


__all__ = (
    "BINS",
    "TOP",
    "company_levels",
    "daily",
    "employers",
    "funnel",
    "histogram",
    "lifetime",
    "median",
    "model_usage",
    "profiles_seen",
    "reviews",
    "salaries",
    "since",
    "sources",
    "text_quality",
)
