"""Тесты детектора сгенерированного текста.

Главное проверяемое свойство — молчание там, где текст написан человеком:
ложное срабатывание стоит дороже пропуска, целевая доля — не выше 5%.
"""

from __future__ import annotations

import aitext
import aitext_rules as R
import fake_reviews
import reviewitems

GENERATED = (
    "В современном мире динамично развивающаяся компания открывает широкие "
    "возможности для профессионального роста и развития. Мы ценим каждого "
    "сотрудника и предлагаем комплексный подход к решению задач любой "
    "сложности. Важно отметить, что наша команда профессионалов работает над "
    "инновационными продуктами, а эффективное взаимодействие внутри "
    "коллектива является ключевым аспектом успешного развития бизнеса."
)

HUMAN = (
    "Ищем человека в команду биллинга: 4 сервиса на FastAPI, PostgreSQL 14, "
    "Kafka, около 300 rps в пике. Половина времени уйдёт на разбор чужого кода "
    "2019 года, предупреждаем сразу. Тесты есть, но покрытие 40%. Релизы по "
    "вторникам, дежурства раз в шесть недель, оплачиваются отдельно. Офис у "
    "Павелецкой, два дня в неделю обязательны, остальное из дома."
)

HUMAN_REVIEW = (
    "Работал в поддержке 1С почти два года, зарплата 120к на руки, приходила "
    "10 и 25 числа без задержек... Начальник нормальный, но в декабре завал: "
    "закрытие года, сидели до девяти. Обучение обещали и правда оплатили, "
    "курсы по SQL прошёл за счёт компании. Ушёл, потому что дальше расти "
    "некуда, а зарплату подняли всего на 8 тысяч за два года."
)


def test_сгенерированный_текст_помечается():
    verdict = aitext.assess(GENERATED, R.VACANCY)
    assert verdict.flagged and verdict.label == R.LABEL_LIKELY
    assert "markers" in verdict.signals and "no_specifics" in verdict.signals
    assert "Текст:" in verdict.line()


def test_человеческий_текст_молчит():
    for text, kind in ((HUMAN, R.VACANCY), (HUMAN_REVIEW, R.REVIEW)):
        verdict = aitext.assess(text, kind)
        assert not verdict.flagged, verdict.signals


def test_короткий_текст_не_оценивается():
    verdict = aitext.assess("Отличная компания, всем советую!", R.REVIEW)
    assert verdict.label == R.LABEL_SKIP
    assert verdict.signals == () and verdict.line() == ""


def test_старый_текст_не_оценивается():
    # Дата известна и раньше 2022 — генерация тогда была редкостью.
    assert aitext.assess(GENERATED, R.REVIEW, "2019-05-01").label == R.LABEL_SKIP
    # Даты нет — предохранитель не применяется.
    assert aitext.assess(GENERATED, R.REVIEW, None).flagged


def test_сигнал_модели_только_добавляет_вес():
    plain = aitext.assess(HUMAN, R.VACANCY)
    judged = aitext.assess(HUMAN, R.VACANCY, llm=True)
    assert "llm_generated" in judged.signals
    assert judged.score > plain.score
    # Одного сигнала модели не хватает, чтобы пометить текст.
    assert not judged.flagged


def test_однородные_пункты_ловятся():
    text = (
        "Обязанности:\n"
        "- Разработка новых сервисов на языке программирования\n"
        "- Внедрение практик код-ревью в команде разработки\n"
        "- Оптимизация существующих процессов внутри компании\n"
        "- Поддержка коллег и развитие культуры качества\n"
        "Условия: интересные задачи и профессиональный рост."
    )
    assert "uniform_bullets" in aitext.signals_of(text, R.VACANCY)


def test_сигнал_попадает_в_счёт_накрутки():
    items = [
        reviewitems.ReviewItem(url="https://e/1", index=0, body=GENERATED),
        reviewitems.ReviewItem(url="https://e/2", index=1, body=HUMAN_REVIEW),
    ]
    verdicts = fake_reviews.score_items(items)
    assert "ai_text" in verdicts[0].signals
    assert "ai_text" not in verdicts[1].signals
    assert verdicts[0].score > verdicts[1].score


def test_строка_карточки_из_базы(conn, make_vacancy):
    import db

    vacancy = make_vacancy(description=GENERATED)
    db.upsert_vacancy(
        conn, vacancy, 80.0, ["вилка"], ai_verdict=aitext.assess(GENERATED, R.VACANCY)
    )
    row = conn.execute("SELECT * FROM vacancies WHERE key = ?", (vacancy.key,)).fetchone()
    assert "Текст:" in aitext.row_line(row)
