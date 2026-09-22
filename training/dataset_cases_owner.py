"""Материал для примеров про самого владельца: письмо, intake, резюме.

Отделено от `dataset_cases` по [CORE-024]: файл перешёл 25 КБ. Граница по
смыслу — здесь тексты, которые пишет или про себя рассказывает владелец, там
чужие тексты из внешних источников.
"""

from __future__ import annotations

from itertools import islice, product

from training.dataset_cases import COMPANIES, ROLES

# ——— draft ———

CLOSINGS = [
    ("Готов созвониться на двадцать минут и обсудить задачи.",
     "Буду признателен за обратную связь."),
    ("Если задача ещё открыта, напишите — покажу похожие проекты.",
     "Заранее благодарю за рассмотрение моей кандидатуры."),
]


def draft_cases(count):
    combos = product(COMPANIES, ROLES, CLOSINGS)
    out = []
    for company, (role, stack), (good_end, bad_end) in islice(combos, count):
        facts = ("7 лет на Python", "поднимал ClickHouse на 2 ТБ")
        body = (
            "Уважаемые представители компании {company}! В связи с открытой "
            "вакансией {role} хотел бы выразить свою заинтересованность. "
            "Обладаю опытом работы 7 лет на Python, а также осуществлял "
            "внедрение ClickHouse на 2 ТБ данных. Считаю, что мои компетенции "
            "позволят внести существенный вклад в развитие вашей динамично "
            "развивающейся команды. {bad_end}"
        ).format(company=company, role=role, bad_end=bad_end)
        gold = (
            "Здравствуйте! Пишу про вакансию «{role}» в {company}. "
            "Семь лет пишу на Python, из заметного — поднимал ClickHouse на 2 ТБ. "
            "Судя по описанию, вам близок {stack}, и это ровно то, чем я "
            "занимаюсь. {good_end}"
        ).format(role=role, company=company, stack=stack, good_end=good_end)
        out.append(({"body": body, "facts": facts}, gold))
    return out


# ——— intake ———

INTAKE_WORDS = [
    ("Пишу на Python шесть лет, хочу удалёнку и от 250 тысяч на руки.",
     ["Python-разработчик", "Backend-разработчик"], 250000, True,
     ["Готовы ли рассматривать гибрид?", "Какой стек не хотите брать?"]),
    ("Делал витрины на ClickHouse, ищу дата-инженера, город не важен.",
     ["Data-инженер", "Инженер данных"], None, True,
     ["Какая зарплата вам подходит?", "Сколько лет опыта считать основным?"]),
    ("Хочу в бэкенд на Go, в офис в Москве, деньги обсудим.",
     ["Go-разработчик", "Backend-разработчик Go"], None, False,
     ["Какая вилка вам интересна?", "Рассматриваете ли удалёнку?"]),
]


SALARY_WORDS = [
    (250000, "от 250 тысяч на руки"),
    (300000, "от 300 тысяч чистыми"),
    (None, "деньги обсудим"),
]
GEO_WORDS = [(True, "хочу удалёнку"), (False, "готов в офис")]


SAYING = [
    "Пишу на {stack} {years} лет, ищу работу как {role}. {geo}, {money}.",
    "Я {role}, {years} лет в основном {stack}. {money}. {geo}. "
    "Только не хочу снова в аутсорс.",
]


def intake_cases(count):
    combos = product(ROLES, SALARY_WORDS, GEO_WORDS, SAYING)
    out = []
    for i, ((role, stack), (salary, money), (remote, geo), saying) in enumerate(
        islice(combos, count)
    ):
        years = 3 + i % 6
        text = saying.format(
            stack=stack, years=years, role=role.lower(), geo=geo, money=money
        )
        profile = {"queries": [role], "remote_ok": remote}
        questions = ["Какой стек не хотите брать?"]
        if salary:
            profile["salary_min_net"] = salary
            questions.append("Рассматриваете ли вилку ниже при интересных задачах?")
        else:
            questions.append("Какая вилка вам подходит?")
        if "аутсорс" in text:
            profile["stop_words"] = ["аутсорс"]
            questions = ["Какой стек не хотите брать?", "Аутстафф тоже исключаем?"]
        gold = {
            "questions": questions,
            "summary": "Ищет работу: {}.".format(role),
            "profile": profile,
            "facts": [],
        }
        out.append(({"text": text}, gold))
    return out


# ——— resume ———

RESUME_ANSWERS = [
    ("experience", "Делал бэкенд на Django, сам поднимал очереди на Celery, "
     "чинил медленные запросы к Postgres.",
     {"text": "- Разрабатывал бэкенд на Django\n"
              "- Поднимал очереди задач на Celery\n"
              "- Ускорял медленные запросы к PostgreSQL",
      "added": []}),
    ("summary", "Пишу на Python, беру задачу от постановки до прода, люблю базы.",
     {"text": "Python-разработчик. Беру задачу от постановки до прода, "
              "отдельно сильна работа с базами данных.",
      "added": []}),
    ("skills", "Python, Django, Postgres, немного Docker и Kafka.",
     {"text": "Python, Django; PostgreSQL; Docker, Kafka", "added": []}),
]


RESUME_ANSWERS += [
    ("projects", "Сделал внутренний сервис отчётов: собирал данные из трёх баз, "
     "отдавал витрину аналитикам.",
     {"text": "Внутренний сервис отчётов: собирал данные из трёх баз и отдавал "
              "витрину аналитикам.", "added": []}),
    ("education", "Закончил физфак в 2014, потом курсы по бэкенду.",
     {"text": "Физфак, 2014. Дополнительно — курсы по бэкенд-разработке.",
      "added": []}),
    ("experience", "Тянул интеграции с внешними API, писал ретраи и складывал "
     "ошибки в отдельную очередь.",
     {"text": "- Разрабатывал интеграции с внешними API\n"
              "- Реализовал повторные попытки запросов\n"
              "- Складывал ошибки интеграций в отдельную очередь",
      "added": []}),
    ("summary", "Работаю с данными: забираю из источников, чищу, отдаю "
     "аналитикам витрины.",
     {"text": "Инженер данных. Забираю данные из источников, чищу и отдаю "
              "аналитикам готовые витрины.", "added": []}),
    ("skills", "SQL каждый день, Python для ETL, Airflow, немного dbt.",
     {"text": "SQL, Python (ETL); Airflow, dbt", "added": []}),
]


def resume_section_cases(count):
    combos = product(RESUME_ANSWERS, ROLES)
    out = []
    for (section, answer, gold), (role, stack) in islice(combos, count):
        text = "{} Основной стек — {}.".format(answer, stack)
        out.append(
            ({"section": section, "answer": text, "role_hint": role}, gold)
        )
    return out


RESUME_POOLS = [
    [
        ("experience", "Разрабатывал бэкенд на Django и Celery"),
        ("experience", "Собирал витрины в ClickHouse"),
        ("skills", "Python, PostgreSQL, Docker"),
        ("education", "Физфак, 2014"),
    ],
    [
        ("experience", "Писал сервисы на Go и держал очереди Kafka"),
        ("experience", "Поддерживал монолит на Django"),
        ("skills", "Go, Kafka, Kubernetes"),
        ("projects", "Внутренний сервис отчётов"),
        ("education", "Мехмат, 2016"),
    ],
    [
        ("experience", "Собирал ETL на Airflow и считал витрины"),
        ("skills", "SQL, Python, Airflow, dbt"),
        ("projects", "Витрина маркетинга на ClickHouse"),
        ("education", "Прикладная математика, 2018"),
    ],
    [
        ("experience", "Делал фронт на React и API на Node.js"),
        ("experience", "Чинил медленные запросы к PostgreSQL"),
        ("skills", "TypeScript, React, Node.js"),
        ("projects", "Личный кабинет для клиник"),
    ],
]

# Ключевые слова вакансии → какие блоки идут первыми. Порядок эталона считается
# правилами, а не на глаз: иначе датасет учит случайной перестановке.
TAILOR_HINTS = {
    "Data": ("etl", "витрин", "sql"),
    "Go": ("go", "kafka"),
    "Fullstack": ("react", "node"),
    "ML": ("python", "витрин"),
}


def _tailor_order(pool, role):
    """Сначала блоки под стек вакансии, потом опыт, потом навыки.

    Порядок считается правилами, а не на глаз: иначе датасет учит случайной
    перестановке [CORE-015]. Образование и проекты не по теме отбрасываются —
    в версии под вакансию им места нет.
    """
    keys = TAILOR_HINTS.get(role.split("-")[0], ("python", "django", "postgres"))
    weight = {"experience": 0, "skills": 1, "projects": 2, "education": 3}
    scored = []
    for number, (section, body) in enumerate(pool, start=1):
        hit = any(key in body.lower() for key in keys)
        if not hit and section in ("education", "projects"):
            continue
        scored.append(((0 if hit else 1, weight.get(section, 4), number), number))
    return [number for _, number in sorted(scored)][:3]


def resume_tailor_cases(count):
    # Блоков должно быть больше трёх: на коротком резюме этап не зовёт модель.
    combos = product(RESUME_POOLS, ROLES, ["Москва", "удалённо", "Санкт-Петербург"])
    out = []
    for pool, (role, stack), where in islice(combos, count):
        vacancy = "Ищем {}. Стек: {}. Формат: {}.".format(role, stack, where)
        out.append(
            ({"blocks": pool, "vacancy": vacancy},
             {"order": _tailor_order(pool, role),
              "reason": "сверху опыт и навыки под стек вакансии"})
        )
    return out


