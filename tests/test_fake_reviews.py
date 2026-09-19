"""Детекция накрученных отзывов: разбор страницы, сигналы, метка компании.

Ни сети, ни модели: всё, что здесь проверяется, обязано считаться на одном
Python [CORE-015]. Сигнал модели проверяется на подменённом шлюзе.
"""

from __future__ import annotations

from datetime import date, timedelta

import dossier
import fake_company
import fake_llm
import fake_reviews
import fake_rules
import fake_store
import reviewitems

КЛИШЕ = (
    "Динамично развивающаяся компания, дружный коллектив, современный офис, "
    "перспективы роста. Рекомендую всем!"
)
ЖИВОЙ = (
    "Работал два года в отделе биллинга, зарплата приходила 10 и 25 числа. "
    "Перед релизом были переработки, их оплачивали."
)


def отзыв(text: str, **kw) -> reviewitems.ReviewItem:
    params = {"url": "https://dreamjob.ru/c/1", "site": "dreamjob.ru", "body": text}
    params.update(kw)
    return reviewitems.ReviewItem(**params)


def страница(*блоки: str) -> str:
    return "".join('<div class="review-card">{}</div>'.format(b) for b in блоки)


def test_страница_разбирается_на_отдельные_отзывы():
    html = страница(
        '<time datetime="2026-03-12"></time><p>Плюсы: платят вовремя, '
        "интересные задачи</p><p>Минусы: не выявлено</p>",
        "<p>3 дня назад</p><p>Задерживают зарплату третий месяц, руководство хамит</p>",
    )
    items = reviewitems.split_page(html, url="https://dreamjob.ru/c/1")
    assert len(items) == 2
    assert items[0].dated_at == "2026-03-12"
    assert items[0].date_precision == "exact"
    assert items[0].cons == "не выявлено"
    assert items[1].date_precision == "approx"
    assert items[1].dated_by_day


def test_страница_без_разделителей_остаётся_одним_отзывом():
    # Fallback-разбор: хуже, но не пусто [CORE-017].
    text = "Работал в компании год, зарплату платят вовремя, коллектив нормальный"
    items = reviewitems.split_page("<p>{}</p>".format(text))
    assert len(items) == 1 and items[0].body == text
    assert items[0].date_precision == "none"


def test_месяц_без_дня_не_годится_для_всплеска():
    assert reviewitems.parse_date("отзыв за март 2026") == ("2026-03-01", "month")
    вчера = date.today() - timedelta(days=1)
    assert reviewitems.parse_date("вчера") == (вчера.isoformat(), "approx")


def test_клише_и_отсутствие_конкретики_дают_сомнительный_отзыв():
    (вердикт,) = fake_reviews.score_items([отзыв(КЛИШЕ)])
    assert "cliches" in вердикт.signals
    assert "no_specifics" in вердикт.signals
    assert вердикт.label == fake_rules.LABEL_SUSPECT


def test_живой_отзыв_остаётся_чистым():
    (вердикт,) = fake_reviews.score_items([отзыв(ЖИВОЙ)])
    assert вердикт.label == fake_rules.LABEL_CLEAN
    assert вердикт.score == 0.0


def test_совпадение_с_отзывом_о_другой_компании_помечает_сразу():
    чужие = {fake_reviews.text_hash(ЖИВОЙ): "ООО Одуванчик"}
    (вердикт,) = fake_reviews.score_items([отзыв(ЖИВОЙ)], known_hashes=чужие)
    assert "dup_other_company" in вердикт.signals
    assert вердикт.label == fake_rules.LABEL_FAKE


def test_всплеск_считается_только_по_известным_дням():
    дни = ["2026-03-01", "2026-03-02", "2026-03-03"]
    items = [
        отзыв("{} {}".format(ЖИВОЙ, i), index=i, dated_at=d, date_precision="exact")
        for i, d in enumerate(дни)
    ]
    assert all("burst" in v.signals for v in fake_reviews.score_items(items))

    расплывчатые = [
        отзыв("{} {}".format(ЖИВОЙ, i), index=i, dated_at=d, date_precision="month")
        for i, d in enumerate(дни)
    ]
    assert all("burst" not in v.signals for v in fake_reviews.score_items(расплывчатые))


def test_пятёрка_при_разгромном_тексте_это_рассинхрон():
    (вердикт,) = fake_reviews.score_items(
        [отзыв("Задерживают зарплату четвёртый месяц подряд", rating=5.0)]
    )
    assert "rating_mismatch" in вердикт.signals


def test_разгромные_отзывы_чистятся_так_же_как_хвалебные():
    # Правила симметричны: заказным бывает и отзыв от конкурента.
    (вердикт,) = fake_reviews.score_items(
        [отзыв("Лучшая компания, всё нравится, рекомендую", rating=1.0)]
    )
    assert "rating_mismatch" in вердикт.signals


def test_заказные_не_попадают_в_среднюю():
    items = [
        отзыв(ЖИВОЙ, index=0, rating=2.0),
        отзыв(КЛИШЕ, index=1, rating=5.0),
    ]
    вердикты = (
        fake_reviews.Verdict(index=0, label=fake_rules.LABEL_CLEAN),
        fake_reviews.Verdict(index=1, label=fake_rules.LABEL_FAKE),
    )
    assert fake_company.plain_average(items) == 3.5
    assert fake_company.weighted_average(items, вердикты) == 2.0


def test_сомнительный_отзыв_идёт_с_половинным_весом():
    items = [отзыв(ЖИВОЙ, index=0, rating=2.0), отзыв(КЛИШЕ, index=1, rating=5.0)]
    вердикты = (
        fake_reviews.Verdict(index=0, label=fake_rules.LABEL_CLEAN),
        fake_reviews.Verdict(index=1, label=fake_rules.LABEL_SUSPECT),
    )
    assert fake_company.weighted_average(items, вердикты) == 3.0


def test_метка_компании_растёт_от_числа_признаков():
    items = [
        отзыв(
            "{} {}".format(КЛИШЕ, i),
            index=i,
            rating=5.0,
            dated_at="2026-03-0{}".format(i + 1),
            date_precision="exact",
        )
        for i in range(6)
    ]
    вердикты = fake_reviews.score_items(items)
    метка = fake_company.evaluate(items, вердикты)
    assert метка.level == fake_rules.MARK_FAKE
    assert len(метка.signs) >= 2
    # Метка всегда раскрывается: у каждого признака есть формулировка с числами.
    assert all(sign.text for sign in метка.signs)


def test_накрутка_красит_риск_в_красный():
    отзывы = [dossier.Review(url="https://dreamjob.ru/c/1", body=КЛИШЕ)]
    items = [
        отзыв(
            "{} {}".format(КЛИШЕ, i),
            index=i,
            rating=5.0,
            dated_at="2026-03-0{}".format(i + 1),
            date_precision="exact",
        )
        for i in range(6)
    ]
    досье = dossier.analyze(
        "ООО Ромашка", отзывы, items=items, verdicts=fake_reviews.score_items(items)
    )
    assert досье.mark.flagged
    assert досье.risk == dossier.RISK_RED
    assert "заказные" in " ".join(dossier.format_lines(досье)).lower()


def test_после_чистки_осталось_мало_отзывов():
    отзывы = [dossier.Review(url="https://dreamjob.ru/c/1", body=КЛИШЕ)]
    items = [отзыв(КЛИШЕ, index=0, rating=5.0), отзыв(ЖИВОЙ, index=1, rating=4.0)]
    вердикты = (
        fake_reviews.Verdict(index=0, label=fake_rules.LABEL_FAKE),
        fake_reviews.Verdict(index=1, label=fake_rules.LABEL_CLEAN),
    )
    досье = dossier.analyze("ООО Ромашка", отзывы, items=items, verdicts=вердикты)
    assert досье.risk == dossier.RISK_THIN
    assert dossier.RISK_RU[dossier.RISK_THIN] == "данных мало"


def test_хэши_ловят_фабрику_между_компаниями(conn):
    items = [отзыв(ЖИВОЙ)]
    вердикты = fake_reviews.score_items(items)
    fake_store.store(conn, "ООО Одуванчик", items, вердикты)

    чужие = fake_store.known_hashes(conn, "ООО Ромашка")
    assert чужие[fake_reviews.text_hash(ЖИВОЙ)] == "ООО Одуванчик"
    assert fake_store.known_hashes(conn, "ООО Одуванчик") == {}

    (строка,) = fake_store.load_items(conn, "ООО Одуванчик")
    assert строка["label"] == fake_rules.LABEL_CLEAN
    assert строка["excerpt"]


def test_досье_сохраняет_метку_и_обе_средние(conn):
    items = [
        отзыв(
            "{} {}".format(КЛИШЕ, i),
            index=i,
            rating=5.0,
            dated_at="2026-03-0{}".format(i + 1),
            date_precision="exact",
        )
        for i in range(6)
    ]
    досье = dossier.analyze(
        "ООО Ромашка",
        [dossier.Review(url="https://dreamjob.ru/c/1", body=КЛИШЕ)],
        items=items,
        verdicts=fake_reviews.score_items(items),
    )
    dossier.store(conn, досье)
    строка = dossier.load(conn, "ООО Ромашка")
    assert строка["fake_level"] == fake_rules.MARK_FAKE
    assert строка["avg_rating_all"] == 5.0
    assert "заказные" in " ".join(dossier.row_to_lines(строка)).lower()
    assert len(fake_store.load_items(conn, "ООО Ромашка")) == 6


def test_сигнал_модели_выключен_по_умолчанию(monkeypatch):
    class Шлюз:
        enabled = True

        def complete(self, stage, messages):
            raise AssertionError("этап не должен вызываться")

    monkeypatch.delenv("FAKE_REVIEW_LLM", raising=False)
    assert fake_llm.ad_indexes(Шлюз(), [отзыв(КЛИШЕ)]) == set()


def test_модель_без_дословной_цитаты_игнорируется():
    class Шлюз:
        enabled = True

        def __init__(self, answer):
            self.answer = answer

        def complete(self, stage, messages):
            assert stage == "review_fake"
            return self.answer

    выдумка = '{"items": [{"id": 0, "verdict": "ad", "quote": "такого текста нет"}]}'
    правда = '{{"items": [{{"id": 0, "verdict": "ad", "quote": "{}"}}]}}'.format(
        "дружный коллектив"
    )
    item = отзыв(КЛИШЕ, index=0)
    assert fake_llm.ad_indexes(Шлюз(выдумка), [item], force=True) == set()
    assert fake_llm.ad_indexes(Шлюз(правда), [item], force=True) == {0}


def test_этап_review_fake_только_на_локальной_модели():
    import llm

    assert llm.profile_for("review_fake") == llm.LOCAL
    assert "review_fake" in llm.PERSONAL_STAGES


class ПоискСОтзывами:
    """Провайдер выдачи: одна ссылка на площадку отзывов."""

    enabled = True
    disabled_reason = ""

    def __init__(self, url: str):
        self.url = url

    def search_many(self, queries, limit=5):
        from dataclasses import make_dataclass

        Hit = make_dataclass("Hit", [("url", str), ("title", str), ("snippet", str)])
        return [Hit(self.url, "Отзывы сотрудников", "")]


def test_полный_прогон_от_страницы_до_карточки(conn):
    """build → разбор страницы → fake_score → метка → база → страница компании."""
    import reviewpage
    import ui_companies

    блоки = "".join(
        '<div class="review-card"><time datetime="2026-03-0{}"></time>'
        "<p>Отзыв об ООО Ромашка. {} Вариант {}</p>"
        "<p>Минусы: нет</p></div>".format(i + 1, КЛИШЕ, i)
        for i in range(6)
    )
    url = "https://dreamjob.ru/c/1"
    fetcher = reviewpage.PageFetcher(conn=conn, transport=lambda _u: блоки, pause=0)

    досье = dossier.build(
        "ООО Ромашка", ПоискСОтзывами(url), fetcher=fetcher, conn=conn
    )
    assert len(досье.items) == 6
    assert досье.mark.level != fake_rules.MARK_NONE
    assert досье.suspicious

    dossier.store(conn, досье)
    страница = ui_companies.render_company(conn, "ООО Ромашка")
    assert "накрутк" in страница.lower()
    # Сигналы раскрыты: без них метку нельзя перепроверить.
    assert "клише" in страница.lower()
