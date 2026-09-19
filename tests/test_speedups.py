"""Узкие места прогона: кэш описаний, пулы и адаптивная пауза."""

from __future__ import annotations

import db
import dossier
import hh_html
import reviewpage
import websearch
from hh import Vacancy


def vacancy(number: int = 1, description: str = "Python, FastAPI") -> Vacancy:
    return Vacancy(
        external_id=str(number),
        title="Backend-разработчик",
        company="АКМЕ",
        url="https://hh.ru/vacancy/{}".format(number),
        description=description,
        published_at="2026-09-01T10:00:00+03:00",
    )


# --- 1. Описание не качается второй раз ---


def test_описание_берётся_из_базы(tmp_path) -> None:
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    item = vacancy()
    db.upsert_vacancy(conn, item, 70.0, ["скор"])
    cached = db.cached_details(conn, item.key)
    assert cached is not None
    assert cached[0] == "Python, FastAPI"
    assert cached[2] == item.published_at


def test_пустое_описание_не_считается_кэшем(tmp_path) -> None:
    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    item = vacancy(description="   ")
    db.upsert_vacancy(conn, item, 70.0, [])
    assert db.cached_details(conn, item.key) is None


# --- 3. Внешний поиск идёт параллельно, но кэш и потолок считаются один раз ---


def test_поиск_запрашивает_каждый_запрос_один_раз(tmp_path) -> None:
    conn = db.connect(tmp_path / "s.sqlite3")
    calls: list[str] = []

    def transport(provider: str, query: str, limit: int):
        calls.append(query)
        return [websearch.Hit(url="https://a.ru/" + query, title=query, snippet="")]

    provider = websearch.SearchProvider(api_key="k", transport=transport, conn=conn)
    hits = provider.search_many(["АКМЕ директор", "АКМЕ руководитель", "АКМЕ директор"])
    assert sorted(calls) == ["АКМЕ директор", "АКМЕ руководитель"]
    assert len(hits) == 2
    # Повтор целиком из кэша: в сеть больше не ходим.
    provider.search_many(["АКМЕ директор"])
    assert len(calls) == 2
    assert provider.usage.cached == 1


def test_потолок_запросов_соблюдается_при_пуле(tmp_path) -> None:
    conn = db.connect(tmp_path / "s.sqlite3")
    calls: list[str] = []

    def transport(provider: str, query: str, limit: int):
        calls.append(query)
        return []

    provider = websearch.SearchProvider(
        api_key="k", transport=transport, conn=conn, max_calls=2
    )
    provider.search_many(["а", "б", "в", "г"])
    assert len(calls) == 2
    assert provider.usage.skipped == 2


# --- 5. Страницы отзывов читаются пачкой ---


def test_страницы_отзывов_читаются_пачкой(tmp_path) -> None:
    conn = db.connect(tmp_path / "r.sqlite3")
    seen: list[str] = []

    def transport(url: str) -> str:
        seen.append(url)
        return "<html><body>Отличная компания, платят вовремя</body></html>"

    fetcher = reviewpage.PageFetcher(transport=transport, conn=conn, max_pages=8)
    out = fetcher.fetch_many(
        ["https://dreamjob.ru/a", "https://pravda-sotrudnikov.ru/b", "https://dreamjob.ru/a"]
    )
    assert len(seen) == 2
    assert set(out) == {"https://dreamjob.ru/a", "https://pravda-sotrudnikov.ru/b"}
    assert fetcher.usage.fetched == 2


def test_досье_читает_страницы_через_пачку(tmp_path) -> None:
    class Fetcher:
        enabled = True

        def __init__(self) -> None:
            self.batches = 0

        def fetch_many(self, urls):
            self.batches += 1
            return {url: "Платят вовремя, руководитель адекватный" for url in urls}

    class Hit:
        def __init__(self, url: str) -> None:
            self.url = url
            self.title = "Отзывы"
            self.snippet = ""

    fetcher = Fetcher()
    reviews = dossier.reviews_from_hits(
        [Hit("https://dreamjob.ru/a"), Hit("https://pravda-sotrudnikov.ru/b")],
        fetcher=fetcher,
    )
    assert fetcher.batches == 1  # один поход, а не по странице за раз
    assert len(reviews) == 2
    assert all(r.body for r in reviews)


# --- пауза hh.ru подстраивается ---


def test_пауза_снижается_на_чистых_ответах() -> None:
    client = hh_html.HHHtmlClient(pause=2.0, pause_min=0.8)
    assert client.pause == 2.0
    for _ in range(10):
        client._ease()
    assert client.pause == 0.8  # ниже нижней границы не опускается
    client._back_off()
    assert client.pause == 2.0
    client.close()


# --- 2. Контакты ищутся один раз на компанию ---


def test_контакты_ищутся_один_раз_на_компанию(conn) -> None:
    import contact_finds
    import contacts
    import outreach
    from tests.test_outreach import _row

    contacts.ensure_schema(conn)
    contact_finds.ensure_schema(conn)
    queries: list[str] = []

    def transport(provider: str, query: str, limit: int):
        queries.append(query)
        return []

    provider = websearch.SearchProvider(api_key="k", transport=transport, conn=conn)
    rows = [
        _row(key="hh:1", title="Backend-разработчик", score=80.0),
        _row(key="hh:2", title="Оператор 1С", score=40.0),
        _row(key="hh:3", title="Курьер", company="БЕТА", score=10.0),
    ]
    outreach.collect_contacts(conn, rows, provider)
    # Три запроса на АКМЕ (по лучшей вакансии) и три на БЕТУ — не девять.
    assert len(queries) == 6
    assert any("руководитель backend-разработчик" in q for q in queries)


# --- 4. Этапы модели идут пулом ---


def test_этапы_модели_идут_пулом(tmp_path, monkeypatch) -> None:
    import llm
    import llm_batch
    import llm_tasks

    path = tmp_path / "l.sqlite3"
    db.init_schema(db.connect(path))
    threads: set[str] = set()

    class FakeGateway:
        enabled = True

    monkeypatch.setattr(llm.Gateway, "from_env", classmethod(lambda cls, conn: FakeGateway()))

    def fake_extract(gateway, description):
        import threading

        threads.add(threading.current_thread().name)
        return [{"kind": "salary", "text": description}]

    monkeypatch.setattr(llm_tasks, "extract_conditions", fake_extract)
    items = [
        vacancy(n, "описание {}".format(n)).model_copy(
            update={"company": "АКМЕ {}".format(n)}
        )
        for n in range(8)
    ]
    out = llm_batch.extract_all(path, items, workers=4)
    assert len(out) == 8
    assert len(threads) > 1  # вызовы действительно разошлись по потокам
