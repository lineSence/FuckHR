"""Парсеры площадок: что именно они обязаны достать из разметки.

Разметка в тестах — урезанные куски живых страниц (проверка 24.09.2026,
docs/review-sites.md). Полные страницы не хранятся: в них чужие тексты и
полмегабайта служебного HTML.

Главное, что проверяется, — не вёрстка, а три вещи, которых у общего разбора
не было: должность, оценка и дата. И то, что сломавшийся парсер не ломает
досье: не нашёл отзывов — разбирает общий путь.
"""

from __future__ import annotations

import reviewpage
import reviewsites

DREAMJOB = """
<div class="review review-fl" id="review1">
  <div class="review__header">
    <span class="review__location"> Екатеринбург, Июнь&nbsp;2026 </span>
    <h2 class="review__header-title"> Директор магазина </h2>
  </div>
  <div class="tags__item tags__item_grey">Работал 5-10 лет</div>
  <div class="dj-rating dj-rating--40" data-partly-switch="rating|3,3,5,4,4,5"> 4,0 </div>
  <div class="review__title review__title_border">Что нравится?</div>
  <div class="review__text"> Официальное трудоустройство, белая зарплата </div>
  <div class="review__title">Что можно улучшить?</div>
  <div class="review__text"> Нагрузка на линейный персонал, мало людей в смене </div>
</div>
<div class="review review-fl" id="review2">
  <div class="review__header">
    <span class="review__location"> Москва, Сентябрь&nbsp;2026 </span>
    <h2 class="review__header-title"> Программист </h2>
  </div>
  <div class="dj-rating dj-rating--20" data-partly-switch="rating|2,1,3,2,2,2"> 2,0 </div>
  <div class="review__title">Что нравится?</div>
  <div class="review__text"> Спринты короткие, ревью по делу </div>
  <div class="review__title">Что можно улучшить?</div>
  <div class="review__text"> Задерживают зарплату второй месяц </div>
</div>
"""

PRAVDA = """
<div class="company-reviews-list-item " id="div_review_1">
  <div class="company-reviews-list-item-name"> Роман (Бывший сотрудник) </div>
  <div class="company-reviews-list-item-city"> Город: Московская Область </div>
  <div class="company-reviews-list-item-date"> 04:20 05.09.2026 </div>
  <div class="col-sm-6 company-reviews-list-item-text pos">
    <div class="company-reviews-list-item-text-message"> Рядом с домом </div>
  </div>
  <div class="col-sm-6 company-reviews-list-item-text con">
    <div class="company-reviews-list-item-text-message"> За десять смен не заплатили </div>
  </div>
  <div class="company-reviews-list-item-ratings2 clearfix">
    <div class="company-reviews-list-item-ratings2-item">
      <span class="company-reviews-list-item-ratings2-item-label">Зарплата:</span>
      <span class="company-reviews-list-item-ratings2-item-value">серая</span>
    </div>
  </div>
  <div class="company-reviews-list-item-ratings">
    <span class="company-reviews-list-item-ratings-item-stars rating-autostars" data-rating="4"></span>
    <span class="company-reviews-list-item-ratings-item-stars rating-autostars" data-rating="1"></span>
    <span class="company-reviews-list-item-ratings-item-stars rating-autostars" data-rating="1"></span>
  </div>
</div>
"""

LDJSON = """
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"Review","author":{"@type":"Person","name":"Аноним"},
  "datePublished":"2026-04-15",
  "reviewRating":{"@type":"Rating","ratingValue":"6.0","bestRating":"10","worstRating":"0"},
  "reviewBody":"Склад не справляется с объемами, смены по тринадцать часов"},
 {"@type":"Review","author":{"@type":"Person","name":"Бывший сотрудник"},
  "datePublished":"2025-02-14T14:27:44+03:00",
  "reviewRating":{"@type":"Rating","ratingValue":"2.7","bestRating":5},
  "reviewBody":"Задержки в зарплате, не рассчитались после увольнения"}]}
</script>
"""


def test_dreamjob_даёт_должность_оценку_и_дату() -> None:
    items = reviewsites.parse(DREAMJOB, "https://dreamjob.ru/employers/25996")
    assert len(items) == 2
    first = items[0]
    assert first.role == "Директор магазина"
    assert first.rating == 4.0
    assert (first.dated_at, first.date_precision) == ("2026-06-01", "month")
    assert "Официальное трудоустройство" in first.pros
    assert "линейный персонал" in first.cons
    # Город и стаж — про условия, а не про человека, и остаются в тексте.
    assert "Екатеринбург" in first.body and "5-10 лет" in first.body
    assert items[1].role == "Программист" and items[1].rating == 2.0


def test_дожность_из_разметки_даёт_сферу() -> None:
    """Ради этого всё и делалось: должность — самый сильный признак сферы."""
    import review_area

    items = reviewsites.parse(DREAMJOB, "https://dreamjob.ru/employers/25996")
    codes = [review_area.classify(item.text, item.role).code for item in items]
    assert codes == ["retail", "it"]


def test_правда_сотрудников_считает_оценку_и_не_берёт_автора() -> None:
    items = reviewsites.parse(
        PRAVDA, "https://pravda-sotrudnikov.ru/company/vernyy-set-magazinov"
    )
    assert len(items) == 1
    item = items[0]
    # Оценка — средняя по звёздам критериев: строки «4,0» на этой площадке нет.
    assert item.rating == 2.0
    assert (item.dated_at, item.date_precision) == ("2026-09-05", "exact")
    # Имя автора в базу не идёт, статус — идёт [CORE-013].
    assert "Роман" not in item.text
    assert "Бывший сотрудник" in item.body
    # Словесная метка развёрнута так, чтобы её увидели правила закономерностей.
    assert "серая зарплата" in item.body


def test_schema_org_приводит_шкалу_к_пяти_баллам() -> None:
    items = reviewsites.parse(LDJSON, "https://hrlike.ru/wildberries/")
    assert len(items) == 2
    # 6 из 10 — это 3.0 из 5, а не «шесть баллов».
    assert items[0].rating == 3.0
    assert items[0].dated_at == "2026-04-15"
    assert items[1].rating == 2.7 and items[1].dated_at == "2025-02-14"


def test_чужая_площадка_и_мусор_отдают_пусто() -> None:
    assert reviewsites.parse(DREAMJOB, "https://example.com/reviews") == ()
    assert reviewsites.parse("", "https://dreamjob.ru/employers/1") == ()
    assert reviewsites.parse("<html><body>меню</body></html>",
                             "https://dreamjob.ru/employers/1") == ()


def test_страницы_считаются_только_от_первой() -> None:
    url = "https://dreamjob.ru/employers/25996"
    assert reviewsites.next_pages(url, 2) == [url + "?page=2", url + "?page=3"]
    # Мы уже на второй странице — листает тот, кто её запросил.
    assert reviewsites.next_pages(url + "?page=2", 2) == []
    # Площадка без листания и нулевой запас страниц.
    assert reviewsites.next_pages("https://hrlike.ru/wildberries/", 2) == []
    assert reviewsites.next_pages(url, 0) == []


def test_поломанный_парсер_не_ломает_разбор(monkeypatch) -> None:
    """Редизайн площадки не должен стоить досье [CORE-017]."""

    def boom(*_args, **_kwargs):
        raise ValueError("разметка поменялась")

    monkeypatch.setitem(reviewsites.PARSERS, "dreamjob.ru", boom)
    assert reviewsites.parse(DREAMJOB, "https://dreamjob.ru/employers/1") == ()
    # Общий путь при этом работает как раньше.
    items = reviewpage.split_items(
        DREAMJOB, "https://dreamjob.ru/employers/1", reviewpage.extract_reviews(DREAMJOB)
    )
    assert items


def test_листание_складывает_отзывы_под_адресом_первой_страницы() -> None:
    url = "https://dreamjob.ru/employers/25996"
    second = DREAMJOB.replace("Официальное трудоустройство, белая зарплата", "Другой отзыв про смены")
    second = second.replace("Спринты короткие, ревью по делу", "И ещё один про кассу")
    pages = {url: DREAMJOB, url + "?page=2": second}
    fetcher = reviewpage.PageFetcher(
        enabled=True, site_pages=2, pause=0.0, transport=lambda u: pages[u]
    )
    fetcher.fetch(url)
    items = fetcher.items[url]
    assert len(items) == 4
    # Номера сквозные: по ним потом сходятся вердикты и строки базы.
    assert [item.index for item in items] == [0, 1, 2, 3]


def test_повторы_страницы_не_копятся() -> None:
    """Последняя страница у многих площадок отдаёт то же, что предыдущая."""
    url = "https://pravda-sotrudnikov.ru/company/x"
    fetcher = reviewpage.PageFetcher(
        enabled=True, site_pages=4, pause=0.0, transport=lambda _u: PRAVDA
    )
    fetcher.fetch(url)
    assert len(fetcher.items[url]) == 1
    # Одинаковая страница читается один раз: дальше листать незачем.
    assert fetcher.usage.fetched == 2


def test_один_отзыв_с_двух_площадок_считается_один_раз() -> None:
    """Часть площадок пересобирает чужие отзывы: копия портит и среднюю
    оценку, и детекцию накрутки — «группа похожих» ловит как раз копии."""
    import dossier

    один = "https://dreamjob.ru/employers/1"
    другой = "https://jobtrue.ru/company/x/"
    reviews = (
        dossier.Review(url=один, site="dreamjob.ru", title="Отзывы", body="текст"),
        dossier.Review(url=другой, site="jobtrue.ru", title="Отзывы", body="текст"),
    )

    class Fetcher:
        items = {
            один: reviewsites.parse(DREAMJOB, один),
            другой: reviewsites.parse(DREAMJOB, другой.replace("jobtrue.ru", "dreamjob.ru")),
        }

    out = dossier.items_from_reviews(reviews, Fetcher())
    assert len(out) == 2
    assert {item.site for item in out} == {"dreamjob.ru"}
