"""Реестр площадок: выбор, готовность, общий обход и дедуп между сайтами."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import db
import settings
import source_store
import sources
from hh import Vacancy
from score import Profile


@dataclass
class Loaded:
    id: str
    profile: Profile


def bundle() -> list[Loaded]:
    return [
        Loaded(
            "main",
            Profile(
                title="main",
                queries=[{"text": "python", "area": 113, "period": 7}],
                skills=["python"],
                min_score=0.0,
            ),
        )
    ]


def base() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    source_store.ensure_schema(conn)
    return conn


def vacancy(title: str, company: str, source: str = "trudvsem", ident: str = "1") -> Vacancy:
    return Vacancy(
        source=source,
        external_id=ident,
        url="https://example.test/{}".format(ident),
        title=title,
        company=company,
        description="python, asyncio",
        salary_from=300000,
        currency="RUR",
    )


def test_selected_defaults_to_hh(monkeypatch):
    monkeypatch.delenv("SOURCE_SITES", raising=False)
    assert sources.selected() == (sources.CODE_HH,)


def test_selected_ignores_unknown_codes(monkeypatch):
    monkeypatch.setenv("SOURCE_SITES", "hh, trudvsem ; авито,")
    assert sources.selected() == ("hh", "trudvsem")


def test_superjob_not_ready_without_key(monkeypatch):
    monkeypatch.setenv("SUPERJOB_KEY", "")
    site = sources.BY_CODE["superjob"]
    ok, why = sources.ready(site)
    assert not ok and "SUPERJOB_KEY" in why


def test_main_source_prefers_hh():
    assert sources.main_source("trudvsem", "hh.ru") == "hh.ru"
    assert sources.main_source("rabota", "trudvsem") == "trudvsem"


def test_collect_external_passes_prefilter(monkeypatch):
    conn = base()
    monkeypatch.setattr(
        sources.src_trudvsem,
        "search",
        lambda text, **kw: iter([vacancy("Python разработчик", "Ромашка")]),
    )
    seen, passed, owners = sources.collect_external(
        bundle(),
        prefilter=settings.PrefilterOptions(enabled=True, min_score=0.0, fuzzy=88),
        conn=conn,
        codes=("trudvsem",),
    )
    assert len(seen) == 1 and len(passed) == 1
    assert list(owners.values()) == [["main"]]
    assert source_store.counts(conn) == {"trudvsem": 1}


def test_known_vacancy_is_not_collected_twice(monkeypatch):
    """Вакансия уже собрана с hh.ru: площадка запоминается, черновик — нет."""
    conn = base()
    draft = vacancy("Python разработчик", "Ромашка")
    monkeypatch.setattr(sources.src_trudvsem, "search", lambda text, **kw: iter([draft]))
    seen, passed, _owners = sources.collect_external(
        bundle(), conn=conn, known=(draft.key,), codes=("trudvsem",)
    )
    assert len(seen) == 1 and passed == {}
    assert source_store.sources_of(conn, draft.key) == ["trudvsem"]


def test_broken_site_does_not_stop_collection(monkeypatch):
    """Падение площадки — это пустой результат, а не сорванный прогон [CORE-017]."""
    conn = base()

    def boom(text, **kw):
        raise RuntimeError("сайт лёг")

    monkeypatch.setattr(sources.src_trudvsem, "search", boom)
    monkeypatch.setattr(
        sources.src_rabota,
        "search",
        lambda text, **kw: iter([vacancy("Python разработчик", "Ромашка", "rabota", "7")]),
    )
    seen, passed, _ = sources.collect_external(
        bundle(), conn=conn, codes=("trudvsem", "rabota")
    )
    assert len(seen) == 1 and len(passed) == 1


def test_site_without_key_is_skipped(monkeypatch):
    conn = base()
    monkeypatch.setenv("SUPERJOB_KEY", "")
    called = []
    monkeypatch.setattr(
        sources.src_superjob,
        "search",
        lambda text, **kw: called.append(text) or iter(()),
    )
    seen, passed, _ = sources.collect_external(bundle(), conn=conn, codes=("superjob",))
    assert called == [] and seen == {} and passed == {}


def test_hh_off_means_do_not_go_there():
    """Снятая галочка hh.ru — запрет ходить туда, а не украшение списка."""
    assert sources.hh_enabled(["rabota"]) is False
    assert sources.hh_enabled(["rabota", "hh"]) is True


def test_queue_note_показывает_вклад_каждой_площадки() -> None:
    from hh import Vacancy

    seen = {
        "a": Vacancy(source="hh.ru", external_id="1", url="u1", title="t1"),
        "b": Vacancy(source="zarplata", external_id="2", url="u2", title="t2"),
        "c": Vacancy(source="zarplata", external_id="3", url="u3", title="t3"),
    }
    note = sources.queue_note(seen, {"a": seen["a"]})
    assert "Zarplata.ru: увидели 2, в прогон 0" in note
    assert "hh.ru: увидели 1, в прогон 1" in note


def test_площадки_обходятся_параллельно(monkeypatch) -> None:
    """Две медленные площадки должны укладываться в время одной, а не двух."""
    import threading
    import time

    live = []
    peak = [0]
    lock = threading.Lock()

    def slow_search(text, **kwargs):
        with lock:
            live.append(1)
            peak[0] = max(peak[0], len(live))
        time.sleep(0.2)
        with lock:
            live.pop()
        return iter(())

    for code in ("zarplata", "rabota"):
        monkeypatch.setattr(sources.BY_CODE[code].module, "search", slow_search)
    started = time.monotonic()
    sources.collect_external(bundle(), codes=("zarplata", "rabota"))
    spent = time.monotonic() - started
    assert peak[0] == 2
    assert spent < 0.35


def test_фоновый_обход_отдаёт_находки_и_отсекает_известные(monkeypatch) -> None:
    from hh import Vacancy

    draft = Vacancy(source="zarplata", external_id="9", url="u", title="Оператор 1С")

    def fake_search(text, **kwargs):
        return iter([draft])

    monkeypatch.setattr(sources.BY_CODE["zarplata"].module, "search", fake_search)
    job = sources.start_external(bundle(), codes=("zarplata",))
    seen, passed, owners = job.result(None, known=())
    assert list(seen) == [draft.key]
    assert list(passed) == [draft.key]

    job = sources.start_external(bundle(), codes=("zarplata",))
    seen, passed, owners = job.result(None, known=(draft.key,))
    # Вакансию уже принёс hh.ru: площадку запомним, второй раз в прогон не берём.
    assert list(seen) == [draft.key]
    assert passed == {} and owners == {}
    assert job.marks and job.marks[0][1] == "zarplata"
