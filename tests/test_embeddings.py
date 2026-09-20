"""Векторы: арифметика, хранение и две задачи, ради которых всё затевалось.

Сеть и модель не трогаем: `llm_embed.embed` подменяется на функцию, которая
возвращает заранее заданные векторы. Проверяется не качество модели, а то, что
без эмбеддера всё работает, а с ним появляются ровно два эффекта — сигнал
`paraphrase` и список похожих вакансий.
"""

from __future__ import annotations

import sqlite3

import pytest

import embeddings
import embeddings_store as store
import embeddings_tasks
import fake_reviews
import llm_embed
import reviewitems


class FakeGateway:
    """Шлюз, у которого есть маршрут этапа embeddings и больше ничего."""

    timeout = 5.0

    class _Route:
        name = "local"
        base_url = "http://localhost/v1"
        api_key = ""
        model = "bge-m3"

    def route_for(self, stage: str):
        return self._Route() if stage == "embeddings" else None


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return connection


def test_косинус_нормируется_и_не_падает_на_разной_длине() -> None:
    same = embeddings.normalize([3, 0, 0])
    other = embeddings.normalize([30, 0, 0])
    assert embeddings.cosine(same, other) == pytest.approx(1.0, abs=1e-6)
    # Разная длина = разные модели. Ноль, а не исключение [LLM-011].
    assert embeddings.cosine(same, embeddings.normalize([1, 0])) == 0.0
    assert embeddings.cosine(same, embeddings.normalize([0, 0, 0])) == 0.0


def test_упаковка_переживает_круг_через_blob() -> None:
    blob = embeddings.pack([1.0, 2.0, 2.0])
    back = embeddings.unpack(blob)
    assert len(back) == 3
    assert embeddings.cosine(back, embeddings.normalize([1, 2, 2])) == pytest.approx(1.0, abs=1e-6)


def test_векторы_прежней_модели_удаляются(conn: sqlite3.Connection) -> None:
    """Смешать векторы двух моделей — значит тихо испортить весь поиск."""
    store.save(conn, store.KIND_VACANCY, "старая", [("a", [1, 0])])
    store.save(conn, store.KIND_VACANCY, "bge-m3", [("a", [1, 0, 0])])
    assert store.drop_other_models(conn, "bge-m3") == 1
    assert store.counts(conn) == [(store.KIND_VACANCY, "bge-m3", 1)]
    assert store.missing(conn, store.KIND_VACANCY, "bge-m3", ["a", "b"]) == ["b"]


def test_без_эмбеддера_всё_работает_без_него(conn: sqlite3.Connection) -> None:
    """Выключенная настройка и отсутствующий маршрут — штатные состояния [CORE-017]."""
    assert llm_embed.embed(None, ["текст"]) is None
    assert llm_embed.model_name(None) == ""
    assert embeddings_tasks.review_pairs(conn, None, []) == ()
    assert embeddings_tasks.similar_vacancies(conn, None, "k") == []


def test_перефразированный_отзыв_получает_сигнал(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Шинглы такой пары не видят: общих четвёрок слов у них нет."""
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "1")
    monkeypatch.setenv("EMBEDDINGS_REVIEW_DUP", "0.9")
    items = [
        reviewitems.ReviewItem(url="a", index=1, body="зарплату задерживают на две недели"),
        reviewitems.ReviewItem(url="b", index=2, body="выплаты приходят с опозданием в полмесяца"),
        reviewitems.ReviewItem(url="c", index=3, body="отличный офис рядом с метро"),
    ]
    assert fake_reviews.similarity(
        fake_reviews.shingles(items[0].text), fake_reviews.shingles(items[1].text)
    ) == 0.0

    vectors = {0: [1.0, 0.0, 0.0], 1: [0.97, 0.24, 0.0], 2: [0.0, 0.0, 1.0]}
    monkeypatch.setattr(
        llm_embed, "embed", lambda gateway, texts: [vectors[i] for i in range(len(texts))]
    )

    pairs = embeddings_tasks.review_pairs(conn, FakeGateway(), items)
    assert pairs == ((1, 2),)

    verdicts = fake_reviews.score_items(items, near_pairs=pairs)
    assert "paraphrase" in verdicts[0].signals
    assert "paraphrase" in verdicts[1].signals
    assert "paraphrase" not in verdicts[2].signals


def test_перефраз_не_добавляется_поверх_дословного_дубля(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Один и тот же текст — одно наблюдение, а не два сигнала подряд."""
    same = "зарплату задерживают каждый месяц на две недели подряд"
    items = [
        reviewitems.ReviewItem(url="a", index=1, body=same),
        reviewitems.ReviewItem(url="b", index=2, body=same),
    ]
    verdicts = fake_reviews.score_items(items, near_pairs=((1, 2),))
    for verdict in verdicts:
        assert "dup_same_company" in verdict.signals
        assert "paraphrase" not in verdict.signals


def test_похожие_вакансии_берутся_из_посчитанного(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Страница ничего не считает: нет вектора — нет блока."""
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "1")
    monkeypatch.setenv("EMBEDDINGS_VACANCY_SIM", "0.8")
    store.save(
        conn,
        store.KIND_VACANCY,
        "bge-m3",
        [("сама", [1.0, 0.0]), ("похожая", [0.95, 0.31]), ("чужая", [0.0, 1.0])],
    )
    found = embeddings_tasks.similar_vacancies(conn, FakeGateway(), "сама")
    assert [key for key, _score in found] == ["похожая"]
    assert embeddings_tasks.similar_vacancies(conn, FakeGateway(), "нет такой") == []
