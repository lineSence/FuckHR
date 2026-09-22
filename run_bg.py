"""Фоновые этапы прогона: досье и этапы модели, пока hh.ru отсиживает паузы.

Сбор с hh.ru медленный намеренно: пауза между запросами — единственная защита
от капчи, и всё это время процесс просто ждёт. Досье ходит на другие домены,
этапы модели — в локальный шлюз, и ни то ни другое очереди hh.ru не касается.
Раньше оба этапа начинались после всего сбора, то есть ровно тогда, когда
ждать было уже нечего.

Правило одно: соединение sqlite не делится между потоками. Фоновая работа
открывает своё соединение только для чтения и кэшей, а всё, что попадает в
таблицы прогона, пишет главный поток при `collect()`.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

import db
import dossier
import extract_spans
import llm
import llm_batch
import research
import stage_gates
import websearch

log = logging.getLogger("fuckhr")

BATCH = 20  # вакансий в порции: меньше — чаще мелкие вызовы, больше — позже старт


def _gateway(conn: Any, use_llm: bool) -> Any:
    if not use_llm:
        return None
    candidate = llm.Gateway.from_env(conn)
    return candidate if candidate.enabled else None


def _extract_batch(db_path: Path, use_llm: bool, vacancies: Sequence[Any]) -> dict[str, Any]:
    """Условия по порции вакансий: сначала спаны, модель — только остатку."""
    conn = db.connect(db_path)
    try:
        gateway = _gateway(conn, use_llm)
        out: dict[str, Any] = {}
        for vacancy in vacancies:
            items = extract_spans.conditions(vacancy.description)
            if items:
                out[vacancy.key] = items
        rest = stage_gates.keep_for_stage(
            conn, gateway, "extract", [v for v in vacancies if v.key not in out]
        )
        out.update(llm_batch.extract_all(db_path, rest))
        return out
    finally:
        conn.close()


def _claims_batch(
    db_path: Path, use_llm: bool, pairs: Sequence[tuple[Any, Any]]
) -> dict[str, Any]:
    """Утверждения по порции. Отчёт детектора возвращается и для отсеянных.

    Гейт экономит вызов модели, а не выкидывает детерминированные находки
    [CORE-015].
    """
    conn = db.connect(db_path)
    try:
        gateway = _gateway(conn, use_llm)
        kept = {
            vacancy.key
            for vacancy in stage_gates.keep_for_stage(
                conn, gateway, "hr_filter", [pair[0] for pair in pairs]
            )
        }
        out: dict[str, Any] = {vacancy.key: report for vacancy, report in pairs}
        out.update(llm_batch.claims_all(db_path, [p for p in pairs if p[0].key in kept]))
        return out
    finally:
        conn.close()


class Stages:
    """Этапы модели порциями в фоне. Пишет в базу главный поток при `collect`."""

    def __init__(
        self, db_path: str | Path, use_llm: bool, size: int = BATCH, workers: int = 2
    ) -> None:
        self.db_path = Path(db_path)
        self.use_llm = use_llm
        self.size = max(1, size)
        self.pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="stage")
        self._extract: list[Any] = []
        self._claims: list[tuple[Any, Any]] = []
        self._jobs: list[tuple[str, Any]] = []
        self.batches = 0

    def extract(self, vacancy: Any) -> None:
        self._extract.append(vacancy)
        if len(self._extract) >= self.size:
            self._send_extract()

    def claim(self, vacancy: Any, report: Any) -> None:
        self._claims.append((vacancy, report))
        if len(self._claims) >= self.size:
            self._send_claims()

    def _send_extract(self) -> None:
        batch, self._extract = self._extract, []
        if not batch:
            return
        self.batches += 1
        self._jobs.append(
            ("extract", self.pool.submit(_extract_batch, self.db_path, self.use_llm, batch))
        )

    def _send_claims(self) -> None:
        batch, self._claims = self._claims, []
        if not batch:
            return
        self.batches += 1
        self._jobs.append(
            ("claims", self.pool.submit(_claims_batch, self.db_path, self.use_llm, batch))
        )

    def collect(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Дожидается всех порций. Возвращает условия и отчёты детектора."""
        self._send_extract()
        self._send_claims()
        conditions: dict[str, Any] = {}
        claims: dict[str, Any] = {}
        for kind, future in self._jobs:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — этап не стоит прогона [CORE-017]
                log.warning("этап %s не досчитался: %s", kind, exc)
                continue
            (conditions if kind == "extract" else claims).update(result)
        self.pool.shutdown(wait=False)
        self._jobs = []
        return conditions, claims


class Research:
    """Досье на компании по мере их появления, а не одной пачкой в конце."""

    def __init__(
        self, conn: Any, db_path: str | Path, use_llm: bool, force: bool = False
    ) -> None:
        dossier.ensure_schema(conn)
        self.conn = conn
        self.db_path = Path(db_path)
        self.use_llm = use_llm
        self.force = force
        self.pool = ThreadPoolExecutor(
            max_workers=research.research_workers(), thread_name_prefix="dossier"
        )
        self.futures: dict[str, Any] = {}
        self.fresh = 0
        self._provider_ok: bool | None = None

    def _enabled(self) -> bool:
        """Поиск проверяется один раз: без него досье не собрать ни одной."""
        if self._provider_ok is None:
            probe = websearch.SearchProvider.from_env(None)
            self._provider_ok = bool(probe.enabled)
            if not probe.enabled:
                log.warning("досье собирать нечем: %s", probe.disabled_reason)
        return self._provider_ok

    def submit(self, company: str, site_url: str | None = None) -> bool:
        """Ставит компанию в очередь. Свежее досье не пересобирается."""
        company = (company or "").strip()
        if not company or company in self.futures or not self._enabled():
            return False
        if not self.force and dossier.is_fresh(dossier.load(self.conn, company)):
            self.fresh += 1
            log.debug("досье на %s свежее, пропускаем", company)
            return False
        self.futures[company] = self.pool.submit(
            research._research_one, self.db_path, company, site_url, self.use_llm, 5
        )
        return True

    def collect(self) -> dict[str, dossier.Dossier]:
        """Дожидается досье и сохраняет их. Сохраняет главный поток."""
        out: dict[str, dossier.Dossier] = {}
        if not self.futures:
            self.pool.shutdown(wait=False)
            return out
        total = len(self.futures)
        done = 0
        by_future = {future: company for company, future in self.futures.items()}
        for future in as_completed(by_future):
            company = by_future[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — одна компания не роняет прогон
                log.warning("досье на %s не собралось: %s", company, exc)
                continue
            # Счётчик в квадратных скобках — по нему интерфейс рисует полоску.
            log.info(
                "[%s/%s] досье: %s — %s",
                done,
                total,
                company,
                dossier.RISK_RU.get(result.risk, result.risk),
            )
            dossier.store(self.conn, result)
            out[company] = result
        self.pool.shutdown(wait=False)
        self.futures = {}
        return out


__all__ = ("BATCH", "Research", "Stages")
