"""Несколько профилей поиска: загрузка, общий сбор и скоринг по каждому.

Решения владельца 20.09.2026 (B-08, ADR-023):

- профиль — это **весь набор критериев** (запросы, гео, вилка, стек, стоп-слова,
  веса, порог), а не одна роль;
- профилей около десятка;
- вакансия, подошедшая двум профилям, — **одна запись** плюс связи в
  `vacancy_profiles`; дедуп и история остаются глобальными;
- досье, оценки компаний и контакты **общие**: компания одна на всех, и правило
  «одно письмо на компанию» [OUT-004] осталось бы дырявым при делении по профилям;
- Telegram — один чат, профиль подписывается в карточке.

Почему отдельный файл, а не правка `run.py`: тот упёрся в 25 КБ [CORE-024].
Здесь только обход профилей; сам сбор по-прежнему в `collector.collect`,
скоринг — в `score.evaluate`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import settings
from hh import Vacancy
from score import Profile, Verdict, evaluate

log = logging.getLogger(__name__)

# Около десятка — оценка владельца. Больше не запрещено, но каждый профиль
# это отдельный обход hh.ru, и об этом стоит сказать вслух.
EXPECTED_MAX = 10


@dataclass(frozen=True)
class Loaded:
    """Профиль вместе с его идентификатором: имя файла без расширения."""

    id: str
    profile: Profile

    @property
    def name(self) -> str:
        """Человеческое имя: из файла, а если его там нет — из имени файла."""
        return self.profile.title or self.id.replace("_", " ").replace("-", " ")


def load_all(path: str | Path) -> list[Loaded]:
    """Все профили из каталога или один профиль из файла.

    Каталог — обычный `profiles/`, где каждый YAML это профиль, а имя файла —
    его идентификатор. Одиночный `profile.yaml` продолжает работать: это тот же
    список из одного элемента, поэтому старые запуски не ломаются.
    """
    target = Path(path)
    if target.is_dir():
        files = sorted(p for p in target.glob("*.yaml") if p.is_file())
        if not files:
            raise FileNotFoundError(f"в каталоге {target} нет ни одного *.yaml")
    else:
        files = [target]
    every = [Loaded(id=f.stem, profile=Profile.load(f)) for f in files]
    out = [item for item in every if item.profile.enabled]
    off = [item.id for item in every if not item.profile.enabled]
    if off:
        # Выключенный профиль остаётся файлом: владелец гасит направление на
        # время, а не удаляет критерии. В логе это должно быть видно, иначе
        # «почему не собралось» ищется часами.
        log.info("профили выключены и пропущены: %s", ", ".join(off))
    if not out:
        raise ValueError(
            "все профили выключены: включи хотя бы один в разделе «Профили»"
        )
    if len(out) > EXPECTED_MAX:
        log.warning(
            "профилей %s: каждый это отдельный обход hh.ru, прогон станет длиннее",
            len(out),
        )
    log.info("профилей загружено: %s (%s)", len(out), ", ".join(o.id for o in out))
    return out


def collect_all(
    client: Any,
    bundle: Sequence[Loaded],
    limit: int = 0,
    prefilter: "settings.PrefilterOptions | None" = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Vacancy], dict[str, Vacancy], dict[str, list[str]]]:
    """Сбор по всем профилям. Третья карта — кто из профилей забрал вакансию.

    Лимит действует на каждый профиль отдельно: он ограничивает обход hh.ru, а
    обходов теперь столько же, сколько профилей. Одна и та же вакансия у двух
    профилей остаётся одной записью — здесь же и склеивается.
    """
    from collector import collect

    seen: dict[str, Vacancy] = {}
    drafts: dict[str, Vacancy] = {}
    owners: dict[str, list[str]] = {}
    for loaded in bundle:
        found, passed = collect(client, loaded.profile, limit, prefilter, conn=conn)
        log.info(
            "профиль %s: увидел %s, прошло предфильтр %s",
            loaded.id,
            len(found),
            len(passed),
        )
        seen.update(found)
        drafts.update(passed)
        for key in passed:
            owners.setdefault(key, []).append(loaded.id)
    return seen, drafts, owners


def score_all(
    vacancy: Vacancy,
    bundle: Sequence[Loaded],
    owner_ids: Iterable[str] | None,
    fuzzy: int,
    market_marker: Any | None = None,
) -> list[tuple[str, Verdict]]:
    """Вердикт каждого профиля, который забрал вакансию. Отклонённые отброшены.

    Скоринг идёт по всем «своим» профилям, а не по первому: критерии разные, и
    вакансия, проходная для одного, для другого может быть отклонена стоп-словом.
    """
    wanted = set(owner_ids) if owner_ids is not None else {o.id for o in bundle}
    out = []
    for loaded in bundle:
        if loaded.id not in wanted:
            continue
        verdict = evaluate(vacancy, loaded.profile, fuzzy, market_marker=market_marker)
        if verdict.rejected:
            log.debug("профиль %s отклонил %s: %s", loaded.id, vacancy.key, verdict.reject_reason)
            continue
        out.append((loaded.id, verdict))
    return out


def best(matches: Sequence[tuple[str, Verdict]]) -> tuple[str, Verdict] | None:
    """Лучший вердикт: его скор и причины попадают в саму запись вакансии."""
    if not matches:
        return None
    return max(matches, key=lambda m: m[1].score)


def passed(
    bundle: Sequence[Loaded], matches: Sequence[tuple[str, Verdict]]
) -> list[str]:
    """Профили, где вакансия перешла **свой** порог.

    Порог у каждого профиля свой, поэтому сравнивать общий лучший скор с чужим
    `min_score` нельзя: так вакансия попадала бы в досье по порогу соседа.
    """
    thresholds = {loaded.id: loaded.profile.min_score for loaded in bundle}
    return [pid for pid, verdict in matches if verdict.score >= thresholds.get(pid, 0.0)]


def min_threshold(bundle: Sequence[Loaded]) -> float:
    """Самый низкий порог из всех профилей: ниже него карточка не нужна никому."""
    return min((loaded.profile.min_score for loaded in bundle), default=0.0)


def facts(bundle: Sequence[Loaded]) -> list[str]:
    """Факты о владельце общие: человек один, профили — его разные интересы."""
    out: list[str] = []
    for loaded in bundle:
        for fact in loaded.profile.facts:
            if fact not in out:
                out.append(fact)
    return out


__all__ = (
    "EXPECTED_MAX",
    "Loaded",
    "best",
    "collect_all",
    "facts",
    "load_all",
    "min_threshold",
    "passed",
    "score_all",
)
