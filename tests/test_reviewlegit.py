"""Тесты проверки легитимности отзывов.

Главное свойство — живой отзыв должен проходить. Ошибочно выброшенный отзыв
не оставляет следа в досье, поэтому все проверки парные: мусор отбрасывается,
соседний живой текст — нет.
"""

from __future__ import annotations

import reviewitems
import reviewlegit
import reviewlegit_rules as R
import reviewlegit_store

ЖИВОЙ = (
    "Работал в отделе поддержки почти два года. Зарплата приходила 10 и 25 "
    "числа, переработки бывали перед релизом, их оплачивали. Начальник "
    "адекватный, ушёл из компании из-за потолка по деньгам."
)
МЕНЮ = "Главная | Компании | Отзывы | Вакансии | Контакты | Все города"
КЛИЕНТ = (
    "Заказывал доставку товара, курьер приехал вовремя, но упаковка помята. "
    "Магазин вернул деньги за возврат денег без споров."
)
ОТВЕТ = (
    "Ответ компании: спасибо за ваш отзыв! Мы передали информацию "
    "руководителю отдела и обязательно разберёмся в ситуации."
)
РЕКЛАМА = (
    "Оставьте отзыв о работодателе на нашем сайте! Читайте также отзывы о "
    "других компаниях, звоните +7 495 123 45 67 или перейти на сайт."
)


def _item(text: str, **kw) -> reviewitems.ReviewItem:
    return reviewitems.ReviewItem(url="https://dreamjob.ru/c/1", body=text, **kw)


def test_живой_отзыв_проходит():
    check = reviewlegit.assess(_item(ЖИВОЙ, rating=4.0, dated_at="2026-03-01"))
    assert check.ok and check.score > R.ACCEPT_AT and check.reasons == ()


def test_мусор_отбрасывается():
    for text, reason in (
        (МЕНЮ, "navigation"),
        (КЛИЕНТ, "customer"),
        (ОТВЕТ, "employer_reply"),
    ):
        check = reviewlegit.assess(_item(text))
        assert not check.ok, text
        assert reason in check.reasons
        assert check.why


def test_рекламная_врезка_не_проходит():
    check = reviewlegit.assess(_item(РЕКЛАМА))
    assert not check.ok
    assert "reader_address" in check.reasons and "promo" in check.reasons


def test_страница_чужой_компании_видна():
    assert reviewlegit.company_on_page("Отзывы сотрудников ООО «Ромашка»", "Ромашка")
    assert reviewlegit.company_on_page("Работа в Ромашке, отзывы", "ООО Ромашка")
    assert not reviewlegit.company_on_page(
        "Отзывы о работодателях Москвы: Вектор, Альфа, Гамма", "Ромашка"
    )
    # Слишком короткое название не проверяем: сравнение дало бы шум.
    assert reviewlegit.company_on_page("что угодно", "БК")


def test_шаблон_площадки_находится_сам(conn):
    """Строка, встреченная у трёх компаний, — вёрстка сайта, а не отзыв."""
    digest = reviewlegit.line_hash(ЖИВОЙ)
    for i in range(R.BOILERPLATE_COMPANIES):
        reviewlegit_store.remember(conn, "dreamjob.ru", "Компания {}".format(i), [digest])
    marks = reviewlegit_store.boilerplate(conn, "dreamjob.ru")
    assert digest in marks
    # Тот же текст у другой площадки шаблоном не считается.
    assert digest not in reviewlegit_store.boilerplate(conn, "orabote.top")

    check = reviewlegit.assess(_item(ЖИВОЙ), marks)
    assert not check.ok and "boilerplate" in check.reasons


def test_счётчики_площадки_копятся_и_складываются_в_строку(conn):
    reviewlegit_store.note(conn, "dreamjob.ru", pages=1, items=4, dropped=2, no_date=1)
    reviewlegit_store.note(conn, "dreamjob.ru", pages=1, items=2, dropped=0)
    rows = reviewlegit_store.health(conn)
    assert rows[0]["items"] == 6 and rows[0]["dropped"] == 2
    line = reviewlegit_store.health_line(rows)
    assert "dreamjob.ru" in line and "отброшено 2" in line


def test_фильтр_делит_поток(conn):
    items = [_item(ЖИВОЙ, index=0), _item(МЕНЮ, index=1), _item(КЛИЕНТ, index=2)]
    kept, dropped = reviewlegit.filter_items(items)
    assert len(kept) == 1 and len(dropped) == 2
    assert all(check.why for _item_, check in dropped)


def test_страница_не_про_нас_не_попадает_в_досье(conn):
    """Сквозной прогон: поиск привёл на подборку про другие компании."""
    import dossier
    import reviewpage

    страница = (
        '<div class="review-card"><p>{}</p></div>'.format(ЖИВОЙ)
        + '<div class="review-card"><p>{}</p></div>'.format(МЕНЮ)
    )

    class Поиск:
        enabled = True
        disabled_reason = ""

        def __init__(self, url: str):
            self.url = url

        def search_many(self, queries, limit=5):
            from dataclasses import make_dataclass

            Hit = make_dataclass("Hit", [("url", str), ("title", str), ("snippet", str)])
            return [Hit(self.url, "Отзывы сотрудников", "")]

    fetcher = reviewpage.PageFetcher(conn=conn, transport=lambda _u: страница, pause=0)
    чужое = dossier.build(
        "ООО Вектор", Поиск("https://dreamjob.ru/c/9"), fetcher=fetcher, conn=conn
    )
    assert чужое.items == ()

    fetcher2 = reviewpage.PageFetcher(
        conn=conn,
        transport=lambda _u: страница.replace("Работал", "Работал в Ромашке"),
        pause=0,
    )
    своё = dossier.build(
        "ООО Ромашка", Поиск("https://dreamjob.ru/c/10"), fetcher=fetcher2, conn=conn
    )
    # Живой отзыв остался, меню выброшено.
    assert len(своё.items) == 1
