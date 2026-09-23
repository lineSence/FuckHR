"""Дамп прочитанных отзывов: пишется только когда его попросили."""

import reviewpage

PAGE = """
<html><body>
<nav>Главная | Компании | Отзывы</nav>
<p>Зарплату задерживают на две недели каждый месяц, руководство кормит обещаниями.</p>
<footer>Все права защищены</footer>
</body></html>
"""


def _fetcher(tmp_path, dump: bool):
    return reviewpage.PageFetcher(
        transport=lambda url: PAGE,
        pause=0.0,
        dump_path=str(tmp_path / "reviews.txt") if dump else None,
    )


def test_дамп_пишет_адрес_и_текст(tmp_path):
    fetcher = _fetcher(tmp_path, dump=True)
    text = fetcher.fetch("https://dreamjob.ru/c/1")

    dumped = (tmp_path / "reviews.txt").read_text(encoding="utf-8")
    assert "https://dreamjob.ru/c/1" in dumped
    assert "Зарплату задерживают" in dumped
    # В дамп идёт то же, что уходит в анализ, иначе правила по нему не сверить.
    assert text in dumped
    assert "Все права защищены" not in dumped


def test_без_настройки_файл_не_создаётся(tmp_path):
    fetcher = _fetcher(tmp_path, dump=False)
    assert fetcher.fetch("https://dreamjob.ru/c/1")
    assert not list(tmp_path.iterdir())


def test_повторное_чтение_дублей_не_плодит(tmp_path):
    """Из кэша в дамп ничего не дописывается."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    fetcher = reviewpage.PageFetcher(
        conn=conn,
        transport=lambda url: PAGE,
        pause=0.0,
        dump_path=str(tmp_path / "reviews.txt"),
    )
    fetcher.fetch("https://dreamjob.ru/c/1")
    fetcher.fetch("https://dreamjob.ru/c/1")
    conn.close()

    dumped = (tmp_path / "reviews.txt").read_text(encoding="utf-8")
    assert dumped.count("https://dreamjob.ru/c/1") == 1
