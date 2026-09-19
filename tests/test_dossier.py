"""Тесты досье: всё без сети и без модели.

Смысл таких тестов простой: проверять анализ отзывов на живом прогоне — это
десятки запросов к поиску и минуты ожидания ради одной строки вывода.
"""

from __future__ import annotations

from dataclasses import dataclass

import dossier


@dataclass(frozen=True)
class FakeHit:
    """Тот же набор полей, что у websearch.Hit."""

    url: str
    title: str = ""
    snippet: str = ""


class DisabledProvider:
    enabled = False
    disabled_reason = "не задан SEARCH_BASE_URL"


class StubProvider:
    enabled = True
    disabled_reason = ""

    def __init__(self, hits):
        self.hits = list(hits)
        self.queries: list[str] = []

    def search_many(self, queries, limit=5):
        self.queries += list(queries)
        return list(self.hits)


class FakeGateway:
    enabled = True

    def __init__(self, answer="- переработки постоянные"):
        self.answer = answer
        self.stages: list[str] = []

    def complete(self, stage, messages):
        self.stages.append(stage)
        return self.answer


class BrokenGateway(FakeGateway):
    def complete(self, stage, messages):
        raise RuntimeError("proxy ответил 400")


def отзыв(text: str, url: str = "https://dreamjob.ru/c/1") -> dossier.Review:
    return dossier.Review(
        url=url,
        title="Отзыв",
        snippet=text,
        site=dossier.site_of(url),
        rating=dossier.extract_rating(text),
        polarity=dossier.polarity_of(text),
    )


def test_оценка_берётся_из_текста_выдачи():
    assert dossier.extract_rating("Оценка 3,2 из 5 по 48 отзывам") == 3.2
    assert dossier.extract_rating("Рейтинг 4.5") == 4.5
    # Стобалльная шкала приводится к пятибалльной, иначе среднее бессмысленно.
    assert dossier.extract_rating("8 из 10") == 4.0
    assert dossier.extract_rating("отзывы сотрудников") is None


def test_тональность_по_маркерам():
    assert dossier.polarity_of("Задерживают зарплату, не рекомендую") == "negative"
    assert dossier.polarity_of("Платят вовремя, интересные задачи") == "positive"
    assert dossier.polarity_of("Платят вовремя, но переработки") == "mixed"
    assert dossier.polarity_of("Офис в центре города") == "unknown"


def test_закономерность_это_повтор_а_не_один_голос():
    один = dossier.find_patterns([отзыв("Полный хаос, нет процессов")])
    assert [p.code for p in один] == ["chaos"]
    # Вес 2 и одно упоминание — ещё не закономерность.
    assert один[0].confirmed is False

    двое = dossier.find_patterns(
        [
            отзыв("Полный хаос, нет процессов", "https://dreamjob.ru/c/1"),
            отзыв("Нет процессов совсем", "https://orabote.top/c/2"),
        ]
    )
    хаос = next(p for p in двое if p.code == "chaos")
    assert хаос.hits == 2
    assert хаос.confirmed is True
    assert хаос.quotes  # без цитаты вывод нельзя проверить


def test_тяжёлый_признак_срабатывает_с_одного_упоминания():
    patterns = dossier.find_patterns([отзыв("Два месяца задерживают зарплату")])
    деньги = next(p for p in patterns if p.code == "salary_delay")
    assert деньги.confirmed is True


def test_без_отзывов_риск_неизвестен():
    # Нет данных — это не «всю хорошо» [CORE-019].
    assert dossier.risk_level([], [], None) == dossier.RISK_UNKNOWN
    пустое = dossier.analyze("ООО Ромашка", [])
    assert пустое.risk == dossier.RISK_UNKNOWN
    assert "Отзывов не найдено" in dossier.format_summary(пустое)


def test_задержки_зарплаты_дают_красный_статус():
    досье = dossier.analyze(
        "ООО Ромашка",
        [
            отзыв("Задерживают зарплату третий месяц", "https://dreamjob.ru/c/1"),
            отзыв("Не платят премии, не рекомендую", "https://antijob.net/c/2"),
        ],
    )
    assert досье.risk == dossier.RISK_RED
    assert any(p.code == "salary_delay" for p in досье.red_flags)
    строки = dossier.format_lines(досье)
    assert строки[0].startswith("Работодатель:")
    assert any("выплат" in строка.lower() for строка in строки)


def test_чужие_сайты_в_отзывы_не_попадают():
    hits = [
        FakeHit("https://dreamjob.ru/companies/1/", "Отзывы", "Оценка 2,1 из 5"),
        FakeHit("https://hh.ru/employer/1", "Вакансии", "Наша дружная команда"),
        FakeHit("https://reklama.example/top", "Лучшие работодатели", "реклама"),
        FakeHit("https://dreamjob.ru/companies/1/", "Дубль", "тот же url"),
    ]
    reviews = dossier.reviews_from_hits(hits)
    assert [r.url for r in reviews] == ["https://dreamjob.ru/companies/1/"]
    assert reviews[0].site == "dreamjob.ru"
    assert reviews[0].rating == 2.1


def test_досье_сохраняется_и_читается(conn):
    досье = dossier.analyze(
        "ООО Ромашка",
        [
            отзыв("Переработки каждую неделю", "https://dreamjob.ru/c/1"),
            отзыв("Переработки и текучка", "https://orabote.top/c/2"),
        ],
    )
    досье.summary = "Сводка"
    досье.summary_by = "правила"
    dossier.store(conn, досье)
    # Повторная запись не должна плодить ни дубли досье, ни дубли отзывов.
    dossier.store(conn, досье)

    row = dossier.load(conn, "ООО Ромашка")
    assert row is not None
    assert row["review_count"] == 2
    assert len(dossier.load_reviews(conn, "ООО Ромашка")) == 2
    assert dossier.is_fresh(row) is True
    assert dossier.is_fresh(None) is False
    assert dossier.row_to_lines(row)[0].startswith("Работодатель:")

    total, red, empty = dossier.coverage(conn)
    assert total == 1
    assert empty == 0
    assert red in (0, 1)
    assert [r["company"] for r in dossier.list_dossiers(conn)] == ["ООО Ромашка"]


def test_без_поиска_досье_не_падает():
    досье = dossier.build("ООО Ромашка", DisabledProvider())
    assert досье.risk == dossier.RISK_UNKNOWN
    assert досье.review_count == 0
    assert досье.summary_by == "правила"


def test_запросы_идут_по_площадкам_отзывов():
    queries = dossier.review_queries("ООО Ромашка")
    assert any("site:dreamjob.ru" in q for q in queries)
    assert all("ООО Ромашка" in q for q in queries)
    assert dossier.review_queries("   ") == []


def test_модель_меняет_только_сводку():
    hits = [
        FakeHit("https://dreamjob.ru/c/1", "Отзыв", "Задерживают зарплату, бегите"),
        FakeHit("https://orabote.top/c/2", "Отзыв", "Не платят вовремя, переработки"),
    ]
    gateway = FakeGateway()
    с_моделью = dossier.build("ООО Ромашка", StubProvider(hits), gateway=gateway)
    без_модели = dossier.build("ООО Ромашка", StubProvider(hits))

    assert gateway.stages == ["company"]
    assert с_моделью.summary_by == "модель"
    assert без_модели.summary_by == "правила"
    # Риск и флаги считаются правилами — модель на них не влияет [CORE-015].
    assert с_моделью.risk == без_модели.risk
    assert [p.code for p in с_моделью.red_flags] == [
        p.code for p in без_модели.red_flags
    ]


def test_ошибка_модели_не_роняет_досье():
    hits = [FakeHit("https://dreamjob.ru/c/1", "Отзыв", "Переработки")]
    досье = dossier.build(
        "ООО Ромашка", StubProvider(hits), gateway=BrokenGateway()
    )
    assert досье.summary_by == "правила"
    assert досье.review_count == 1
