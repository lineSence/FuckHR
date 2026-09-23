"""Чистка базы и кэшей.

Отдельный модуль, а не функция в db.py, по одной причине: удаление данных —
единственная необратимая операция в системе. Пусть она лежит в одном месте,
где видно всё, что можно потерять.

Главное предупреждение. История публикаций (vacancy_snapshots) — единственные данные,
которые нельзя восстановить повторным сбором: hh.ru не покажет, что вакансия
висела в апреле и исчезла в мае. Детектор HR-брехни (ADR-009) без неё слепой,
и накапливается она месяцами. Поэтому цели разделены: почистить кэши или черновики
можно спокойно, а историю — только отдельным осознанным выбором.

Удаляются строки, а не таблицы: схема остаётся на месте, и следующий запуск не
падает на отсутствующей таблице.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

# Цель: код, подпись, таблицы, пояснение последствий, опасная ли.
TARGETS: tuple[tuple[str, str, tuple[str, ...], str, bool], ...] = (
    (
        "search_cache",
        "Кэш внешнего поиска",
        ("search_cache", "page_cache"),
        "Следующий поиск пойдёт в интернет заново, страницы отзывов тоже "
        "перечитаются. Полезно, если менял движки или подозреваешь устаревшую "
        "выдачу.",
        False,
    ),
    (
        "llm_cache",
        "Кэш ответов модели",
        ("llm_cache",),
        "Повторные вызовы снова пойдут к модели и будут стоить времени. "
        "Нужно после смены модели или промптов.",
        False,
    ),
    (
        "embeddings",
        "Векторы текстов",
        ("embeddings",),
        "Своих данных у таблицы нет: векторы считаются заново следующим прогоном. "
        "Нужно после смены модели эмбеддингов [LLM-011].",
        False,
    ),
    (
        "dossier",
        "Досье и отзывы о компаниях",
        (
            "review_items",
            "review_hashes",
            "company_reviews",
            "company_dossier",
            "site_lines",
            "site_health",
        ),
        "Собирается заново при следующем сканировании, но потратит запросы поиска. "
        "Вместе с досье уходят хэши отзывов и шаблонные строки площадок: пока они "
        "не накопятся заново, ни совпадения текстов между компаниями, ни мусор "
        "со страниц не отсекаются.",
        False,
    ),
    (
        "contacts",
        "Найденные контакты и черновики",
        ("contact_finds", "contacts"),
        "Стирает и историю переписки: кому уже писал и кто просил не писать. "
        "После очистки тот же человек может получить второе письмо раньше трёх месяцев.",
        True,
    ),
    (
        "vacancies",
        "Вакансии и разобранные условия",
        (
            "vacancy_conditions",
            "vacancy_signals",
            "vacancy_profiles",
            "vacancy_sources",
            "vacancies",
        ),
        "Собирается заново за один прогон. История публикаций остаётся нетронутой. "
        "Вместе с вакансиями уходит разметка площадок (метрика «только здесь» "
        "начнёт набираться снова), сигналы детектора и привязка к профилям поиска.",
        False,
    ),
    (
        "score",
        "Оценка работодателей",
        ("company_score",),
        "Пересчитывается целиком за один прогон из досье, меток по деньгам и "
        "слепков: своих данных у таблицы нет.",
        False,
    ),
    (
        "injection",
        "Находки промпт-инъекций",
        ("injection_hits",),
        "Находится заново при следующем разборе тех же текстов.",
        False,
    ),
    (
        "deep",
        "Глубокий ресёрч",
        ("deep_research", "deep_findings", "deep_pages"),
        "Отчёты по компаниям и кэш прочитанных страниц. Восстанавливается "
        "новым ресёрчем, но это время и запросы к поиску.",
        False,
    ),
    (
        "market",
        "Наблюдения по зарплатам",
        ("market_observations", "market_stats", "company_market"),
        "Необратимо. Вилки копятся полгода: после очистки срезы рынка станут "
        "пустыми, метки «ниже/выше рынка» пропадут из карточек и из скоринга, "
        "пока не наберётся минимум наблюдений заново.",
        True,
    ),
    (
        "history",
        "История публикаций",
        ("vacancy_snapshots",),
        "Необратимо. Слепки накапливаются месяцами и повторным сбором не "
        "восстанавливаются: детектор перепубликаций начнёт с нуля.",
        True,
    ),
)

TARGET_CODES = tuple(code for code, _l, _t, _w, _d in TARGETS)
DANGEROUS = tuple(code for code, _l, _t, _w, danger in TARGETS if danger)

# Порядок важен: сначала зависимое, потом основное.
EVERYTHING = (
    "search_cache",
    "llm_cache",
    "embeddings",
    "dossier",
    "contacts",
    "vacancies",
    "score",
    "injection",
    "deep",
    "market",
    "history",
)


def target_label(code: str) -> str:
    for target, label, _tables, _warning, _danger in TARGETS:
        if target == code:
            return label
    return code


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько строк стоит за каждой целью — чтобы кнопка не была вслепую."""
    out: dict[str, int] = {}
    for code, _label, tables, _warning, _danger in TARGETS:
        total = 0
        for table in tables:
            if not _table_exists(conn, table):
                continue
            total += int(
                conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0] or 0
            )
        out[code] = total
    return out


def wipe(conn: sqlite3.Connection, targets: tuple[str, ...] | list[str]) -> dict[str, int]:
    """Удаляет строки выбранных целей и возвращает сколько чего удалено.

    Неизвестная цель — ошибка, а не тихое ничегонеделание: опечатка в кнопке
    не должна выглядеть как успешная очистка.
    """
    unknown = [code for code in targets if code not in TARGET_CODES]
    if unknown:
        raise ValueError("неизвестная цель очистки: {}".format(", ".join(unknown)))

    removed: dict[str, int] = {}
    for code, _label, tables, _warning, _danger in TARGETS:
        if code not in targets:
            continue
        total = 0
        for table in tables:
            if not _table_exists(conn, table):
                continue
            cur = conn.execute("DELETE FROM {}".format(table))
            total += int(cur.rowcount or 0)
        removed[code] = total
        log.info("очистка %s: удалено строк %s", code, total)
    conn.commit()
    return removed


def vacuum(conn: sqlite3.Connection) -> None:
    """Сжатие файла после большой очистки. Без него sqlite не отдаёт место диску."""
    conn.execute("VACUUM")
    conn.commit()


def describe() -> list[tuple[str, str, str, bool]]:
    """(код, подпись, последствия, опасная ли) — для страницы настроек."""
    return [
        (code, label, warning, danger)
        for code, label, _tables, warning, danger in TARGETS
    ]
