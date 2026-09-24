"""Линейный гейт этапов отзывов: пороги, веса в базе, решение без модели."""

from __future__ import annotations

import sqlite3

import judge_labels
import linear_model
import review_gate
import review_gate_store as store
import review_gate_train as train


def база() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    judge_labels.ensure_schema(conn)
    store.ensure_schema(conn)
    return conn


def векторы(monkeypatch, таблица: dict[str, list[float]]) -> None:
    """Подменяет и эмбеддер, и кэш: в тестах ни сети, ни модели."""
    monkeypatch.setattr(review_gate.llm_embed, "model_name", lambda gateway: "bge-m3")
    monkeypatch.setattr(
        review_gate,
        "vectors_for",
        lambda conn, gateway, texts: ("bge-m3", {k: таблица[k] for k in texts if k in таблица}),
    )


def test_предсказание_на_чужой_длине_не_падает():
    assert review_gate.predict([1.0, 1.0], 0.0, [1.0]) == 0.5
    assert review_gate.predict([], 0.0, [1.0]) == 0.5


def test_auroc_упорядочивает_и_молчит_без_класса():
    assert review_gate.auroc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert review_gate.auroc([0.1], [0.9]) == 0.0
    assert review_gate.auroc([], [0.5]) is None


def test_перевёрнутые_пороги_считаются_выключенным_гейтом(monkeypatch):
    monkeypatch.setenv("REVIEW_GATE_ENABLED", "1")
    monkeypatch.setenv("REVIEW_GATE_LOW", "0.8")
    monkeypatch.setenv("REVIEW_GATE_HIGH", "0.2")
    assert review_gate.options().usable is False


def test_веса_переживают_запись_и_чтение():
    conn = база()
    store.save(conn, "review_fake", "bge-m3", [0.5, -0.25], 0.125, rows=10, auroc=0.8)
    модель = store.load(conn, "review_fake", "bge-m3")
    assert модель is not None
    assert [round(v, 3) for v in модель.weights] == [0.5, -0.25]
    assert модель.bias == 0.125 and модель.rows == 10
    # Веса другой модели векторов несравнимы, поэтому и не находятся [LLM-011].
    assert store.load(conn, "review_fake", "e5") is None
    assert len(store.all_models(conn)) == 1


def test_выключенный_гейт_ничего_не_считает(monkeypatch):
    monkeypatch.delenv("REVIEW_GATE_ENABLED", raising=False)
    conn = база()
    store.save(conn, "review_fake", "bge-m3", [1.0], 0.0)
    assert review_gate.decide(conn, None, "review_fake", {0: "текст"}) == (set(), set(), {})


def test_без_весов_гейт_молчит(monkeypatch):
    monkeypatch.setenv("REVIEW_GATE_ENABLED", "1")
    векторы(monkeypatch, {})
    conn = база()
    да, нет, оценки = review_gate.decide(conn, None, "review_fake", {0: "текст отзыва"})
    assert (да, нет, оценки) == (set(), set(), {})


def test_гейт_делит_на_да_нет_и_середину(monkeypatch):
    monkeypatch.setenv("REVIEW_GATE_ENABLED", "1")
    monkeypatch.setenv("REVIEW_GATE_LOW", "0.2")
    monkeypatch.setenv("REVIEW_GATE_HIGH", "0.8")
    from fake_reviews import text_hash

    тексты = {0: "реклама", 1: "живой отзыв", 2: "серединка"}
    векторы(monkeypatch, {
        text_hash("реклама"): [10.0],
        text_hash("живой отзыв"): [-10.0],
        text_hash("серединка"): [0.0],
    })
    conn = база()
    store.save(conn, "review_fake", "bge-m3", [1.0], 0.0)
    да, нет, оценки = review_gate.decide(conn, None, "review_fake", тексты)
    assert да == {0} and нет == {1}
    assert 0.2 < оценки[2] < 0.8


def test_неизвестный_этап_не_решается(monkeypatch):
    monkeypatch.setenv("REVIEW_GATE_ENABLED", "1")
    векторы(monkeypatch, {})
    assert review_gate.scores(база(), None, "hr_filter", {0: "текст"}) == {}


def test_обучение_разделяет_и_подбирает_пороги():
    строки = [([1.0, 0.0], 0.85)] * 20 + [([0.0, 1.0], 0.15)] * 20
    веса, смещение = linear_model.train(строки, epochs=200)
    assert review_gate.predict(веса, смещение, [1.0, 0.0]) > 0.6
    assert review_gate.predict(веса, смещение, [0.0, 1.0]) < 0.4
    low, high = linear_model.choose([0.9, 0.85, 0.1, 0.2], [1, 1, 0, 0])
    # Разделимая выборка: середины нет, порог «нет» прижат к порогу «да»,
    # иначе гейт получился бы с перевёрнутыми порогами.
    assert high is not None and low is not None and low <= high
    assert review_gate.GateOptions(True, low, high).usable


def test_датасет_делит_по_компаниям_и_слушает_владельца():
    conn = база()
    рекламный = "Лучшая компания в мире, всё прекрасно и замечательно тут" * 2
    живой = "Зарплата вовремя, но переработки каждую неделю и текучка в отделе"
    judge_labels.record(conn, "review_fake", {рекламный: True}, "Ромашка")
    judge_labels.record(conn, "review_fake", {живой: True}, "Ромашка")
    # Поправка владельца перебивает учителя и уходит в отложенную часть.
    judge_labels.record(conn, "review_fake", {живой: False}, "Ромашка", "owner")
    обучающая, отложенная = train.dataset(conn, "review_fake")
    отложенные = {s.text: s.yes for s in отложенная}
    assert отложенные[живой] == 0.0
    assert [s.yes for s in обучающая] == [0.85]
    assert живой not in {s.text for s in обучающая}


def test_короткие_тексты_в_датасет_не_попадают():
    conn = база()
    judge_labels.record(conn, "ai_text", {"коротко": True}, "Ромашка")
    обучающая, отложенная = train.dataset(conn, "ai_text")
    assert not обучающая and not отложенная


def test_поправки_владельца_читаются_из_файла(tmp_path):
    conn = база()
    файл = tmp_path / "labels.json"
    файл.write_text(
        '[{"stage": "ai_text", "company": "Ромашка", '
        '"texts": ["первый текст", "второй текст"], "ad": [1]}]',
        encoding="utf-8",
    )
    assert train.import_owner(conn, файл, "review_fake") == 2
    метки = {row["text"]: bool(row["verdict"]) for row in judge_labels.rows(conn, "ai_text")}
    assert метки == {"первый текст": False, "второй текст": True}


def test_гейт_снимает_тексты_с_модели(monkeypatch):
    """Решённое гейтом до модели не доходит, а спрошенное — доходит."""
    import review_scoring
    from fake_reviews import text_hash

    class Отзыв:
        def __init__(self, index, text):
            self.index, self.text = index, text

    отзывы = [Отзыв(0, "реклама"), Отзыв(1, "живой отзыв"), Отзыв(2, "серединка")]
    monkeypatch.setenv("REVIEW_GATE_ENABLED", "1")
    monkeypatch.setenv("REVIEW_GATE_LOW", "0.2")
    monkeypatch.setenv("REVIEW_GATE_HIGH", "0.8")
    векторы(monkeypatch, {
        text_hash("реклама"): [10.0],
        text_hash("живой отзыв"): [-10.0],
        text_hash("серединка"): [0.0],
    })
    conn = база()
    store.save(conn, "review_fake", "bge-m3", [1.0], 0.0)
    спрошено: list[list[int]] = []
    monkeypatch.setattr(review_scoring.fake_llm, "enabled", lambda: True)
    monkeypatch.setattr(
        review_scoring.fake_llm,
        "ad_indexes",
        lambda gateway, items, seen=None: (
            спрошено.append([item.index for item in items]),
            set(),
        )[1],
    )
    assert review_scoring.gated_ads(conn, None, отзывы, {}) == {0}
    assert спрошено == [[2]]


def test_выключенный_этап_не_зовёт_даже_гейт(monkeypatch):
    import review_scoring

    monkeypatch.setattr(review_scoring.fake_llm, "enabled", lambda: False)
    monkeypatch.setattr(review_scoring.aitext_llm, "enabled", lambda: False)
    упало = lambda *a, **k: (_ for _ in ()).throw(AssertionError("гейт не должен считаться"))
    monkeypatch.setattr(review_scoring.review_gate, "decide", упало)
    assert review_scoring.gated_ads(None, None, [], {}) == set()
    assert review_scoring.gated_ai(None, None, {0: "текст"}, {}) == set()


def test_экономия_считает_решённое_и_ошибки():
    оценки = [0.95, 0.9, 0.5, 0.05, 0.1, 0.9]
    метки = [1, 1, 1, 0, 0, 0]
    итог = linear_model.yield_of(оценки, метки, low=0.2, high=0.8)
    # Решено: три «да» (одно зря) и два «нет», середина 0.5 ушла бы в шлюз.
    assert итог["решено_да"] == 3 and итог["решено_нет"] == 2
    assert итог["решено_без_модели"] == 5 and итог["неверно"] == 1
    assert итог["доля"] == round(5 / 6, 3)


def test_без_порогов_экономию_не_обещают():
    assert linear_model.yield_of([0.5], [1], None, 0.8) == {}


def test_в_вызов_попадают_самые_спорные_отзывы() -> None:
    """Вызов один и на дюжину текстов: важно, какие двенадцать в него попадут."""
    import review_scoring

    scores = {1: 0.05, 2: 0.49, 3: 0.55, 4: 0.3}
    assert review_scoring.by_doubt([1, 2, 3, 4], scores) == [2, 3, 4, 1]


def test_без_оценок_гейта_порядок_отзывов_прежний() -> None:
    import review_scoring

    assert review_scoring.by_doubt([3, 1, 2], {}) == [3, 1, 2]


def test_у_ai_text_к_вектору_добавляются_приметы() -> None:
    """Связка учится на векторе плюс стилометрия, и рантайм считает то же самое."""
    import ai_text_rules
    import review_gate

    text = "Компания обеспечивает комфортную атмосферу. " * 6
    base = [0.1, 0.2, 0.3]
    grown = review_gate.augment("ai_text", base, text)
    assert len(grown) == len(base) + len(ai_text_rules.FEATURE_NAMES)
    assert tuple(grown[3:]) == ai_text_rules.features(text)
    # Второй этап не трогается: там вектор как был.
    assert review_gate.augment("review_fake", base, text) == base


def test_веса_связки_лежат_под_своим_именем() -> None:
    """Размерность другая, и старые веса к ней неприменимы [LLM-011]."""
    import review_gate

    assert review_gate.model_key("ai_text", "bge-m3") == "bge-m3+rules1"
    assert review_gate.model_key("review_fake", "bge-m3") == "bge-m3"
