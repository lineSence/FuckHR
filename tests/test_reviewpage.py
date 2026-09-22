"""Тесты чтения страниц с отзывами. Транспорт подменён, сети нет.

Смысл: проверить ровно то, из-за чего досье было пустым — что в анализ
попадает текст отзыва, а не подпись сайта-отзовика.
"""

from __future__ import annotations

from dataclasses import dataclass

import dossier
import reviewpage

СТРАНИЦА = """
<html>
<head><title>Отзывы сотрудников о компании ООО Ромашка</title></head>
<body>
<nav><a href="/">Главная</a> <a href="/companies">Компании</a> <a href="/add">Добавить компанию</a></nav>
<h1>ООО Ромашка</h1>
<script>var ads = "Задерживают зарплату третий месяц подряд, руководство молчит";</script>
<div class="review">
  <p>Минусы: зарплату задерживают третий месяц подряд, руководство кормит обещаниями и молчит.</p>
  <p>Плюсы: коллектив нормальный, но почти все уже написали заявления и ищут работу.</p>
</div>
<div class="review">
  <p>Переработки каждую неделю, задачи прилетают в пятницу вечером и делаются в выходные.</p>
</div>
<footer>Мы используем cookie. Все права защищены. Оставить отзыв о компании можно после регистрации.</footer>
</body>
</html>
"""


@dataclass(frozen=True)
class FakeHit:
    url: str
    title: str = ""
    snippet: str = ""


class Транспорт:
    """Считает вызовы: кэш должен избавлять от повторной загрузки."""

    def __init__(self, page: str = СТРАНИЦА):
        self.page = page
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        return self.page


def фетчер(transport=None, **kwargs) -> reviewpage.PageFetcher:
    kwargs.setdefault("pause", 0.0)
    return reviewpage.PageFetcher(transport=transport or Транспорт(), **kwargs)


def test_из_страницы_берётся_текст_отзывов_без_обвязки():
    text = reviewpage.extract_reviews(СТРАНИЦА)
    assert "зарплату задерживают третий месяц" in text
    assert "Переработки каждую неделю" in text
    # Меню, футер и содержимое <script> — не отзывы.
    assert "Добавить компанию" not in text
    assert "cookie" not in text.lower()
    assert "var ads" not in text


def test_короткие_и_служебные_строки_не_считаются_отзывом():
    assert reviewpage.looks_like_review("Отзывы") is False
    assert reviewpage.looks_like_review("Мы используем cookie на этом сайте, подробнее") is False
    assert (
        reviewpage.looks_like_review(
            "Зарплату платят вовремя, руководство адекватное, задачи интересные"
        )
        is True
    )


def test_страница_читается_один_раз_и_кладётся_в_кэш(conn):
    transport = Транспорт()
    f = фетчер(transport, conn=conn)
    первый = f.fetch("https://dreamjob.ru/c/1")
    второй = f.fetch("https://dreamjob.ru/c/1")
    assert первый == второй != ""
    assert transport.calls == ["https://dreamjob.ru/c/1"]
    assert f.usage.fetched == 1
    assert f.usage.cached == 1


def test_refresh_читает_страницу_заново(conn):
    """Разобранные отзывы лежат в кэше вместе с текстом: без обхода кэша
    починенный разбор к старым страницам не применяется."""
    transport = Транспорт()
    фетчер(transport, conn=conn).fetch("https://dreamjob.ru/c/1")
    свежий = фетчер(transport, conn=conn, refresh=True)
    свежий.fetch("https://dreamjob.ru/c/1")
    assert len(transport.calls) == 2
    assert свежий.usage.cached == 0


def test_потолок_страниц_соблюдается():
    transport = Транспорт()
    f = фетчер(transport, max_pages=1)
    f.fetch("https://dreamjob.ru/c/1")
    f.fetch("https://orabote.top/c/2")
    assert len(transport.calls) == 1
    assert f.usage.skipped == 1


def test_недоступная_страница_не_роняет_сбор():
    def падает(url: str) -> str:
        raise RuntimeError("таймаут")

    f = фетчер(падает)
    assert f.fetch("https://dreamjob.ru/c/1") == ""
    assert f.usage.failures == 1


def test_выключенный_загрузчик_ничего_не_качает():
    transport = Транспорт()
    f = фетчер(transport, enabled=False)
    assert f.fetch("https://dreamjob.ru/c/1") == ""
    assert transport.calls == []


def test_досье_считается_по_тексту_а_не_по_заголовку():
    # Ровно тот случай из жизни: в выдаче рекламная подпись отзовика.
    hits = [
        FakeHit(
            "https://dreamjob.ru/c/1",
            "Отзывы сотрудников о компании ООО Ромашка",
            "Читайте отзывы сотрудников на Dream Job",
        )
    ]
    без_чтения = dossier.reviews_from_hits(hits)
    assert без_чтения[0].polarity == "unknown"
    assert без_чтения[0].has_body is False

    с_чтением = dossier.reviews_from_hits(hits, fetcher=фетчер())
    отзыв = с_чтением[0]
    assert отзыв.has_body is True
    assert отзыв.polarity in ("negative", "mixed")

    досье = dossier.analyze("ООО Ромашка", с_чтением)
    assert досье.read_count == 1
    assert {p.code for p in досье.patterns} >= {"salary_delay", "overtime"}
    assert досье.risk == dossier.RISK_RED
    assert "прочитано страниц: 1 из 1" in dossier.format_summary(досье)


def test_текст_страницы_сохраняется_в_базу(conn):
    досье = dossier.analyze(
        "ООО Ромашка",
        dossier.reviews_from_hits(
            [FakeHit("https://dreamjob.ru/c/1", "Отзывы", "подпись сайта")],
            fetcher=фетчер(),
        ),
    )
    dossier.store(conn, досье)
    dossier.store(conn, досье)  # повтор не плодит дублей
    rows = dossier.load_reviews(conn, "ООО Ромашка")
    assert len(rows) == 1
    assert "задерживают" in (rows[0]["body"] or "").lower()
