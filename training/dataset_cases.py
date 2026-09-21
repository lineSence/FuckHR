"""Материал для синтетических примеров: тексты, наборы и эталонные ответы.

Отдельный файл от сборщика по [CORE-024] и по смыслу: здесь данные, там
механика. Всё выдумано целиком — ни одной живой компании, вакансии или
человека [CORE-013]: датасет уезжает в Colab, то есть в облако.

Каждый билдер возвращает список пар (payload, gold): payload идёт в ту же
функцию пайплайна, что работает в проде, gold — ответ, которому мы учим.

Здесь этапы про чужие тексты: вакансии, компании, отзывы, кандидаты. Этапы
про самого владельца (письмо, intake, резюме) — в `dataset_cases_owner`
[CORE-024].
"""

from __future__ import annotations

import json
from itertools import cycle, islice, product

# ——— общее ———

ROLES = [
    ("Python-разработчик", "FastAPI и PostgreSQL"),
    ("Backend-разработчик", "Django и Celery"),
    ("Data-инженер", "Airflow и ClickHouse"),
    ("Go-разработчик", "Go и Kafka"),
    ("Fullstack-разработчик", "React и Node.js"),
    ("ML-инженер", "PyTorch и MLflow"),
]

COMPANIES = [
    "Ромашка Софт", "Кедр Диджитал", "Вектор Лабс", "Северный Код",
    "Ирбис Технологии", "Полюс Данных", "Аметист Системы", "Тайга Аналитика",
]

CLICHE = [
    "Мы — молодая динамично развивающаяся компания.",
    "У нас дружная команда единомышленников.",
    "Работа в атмосфере драйва и постоянного роста.",
    "Ищем человека, который горит своим делом.",
]


def _take(items, count):
    """Ровно count элементов, по кругу и без случайности."""
    return list(islice(cycle(items), count))


# ——— extract ———

FORMATS = [
    ("Гибрид: два дня в офисе, остальное из дома.", "format", "гибрид 2/3"),
    ("Работа полностью удалённая, офиса нет.", "format", "удалённо"),
    ("Офис в Москве, метро Павелецкая.", "office", "Москва, Павелецкая"),
    ("График с 10 до 19, пятница короткая.", "schedule", "с 10 до 19"),
]

SALARIES = [
    ("Вилка 250 000 — 320 000 ₽ на руки.", "salary", "250000-320000 на руки"),
    ("Оклад от 180 000 ₽ до вычета НДФЛ.", "salary", "от 180000 gross"),
    ("Зарплату обсуждаем на собеседовании.", "salary", "обсуждается"),
    ("Доход до 400 000 ₽ вместе с премией.", "salary", "до 400000 с премией"),
]

EXTRAS = [
    ("Оформление по ТК РФ с первого дня.", "process", "по ТК РФ"),
    ("Грейд middle или senior.", "grade", "middle/senior"),
    ("Код-ревью обязателен для всех задач.", "process", "обязательное код-ревью"),
]


def extract_cases(count):
    out = []
    for (role, stack), fmt, salary, extra in islice(
        product(ROLES, FORMATS, SALARIES, EXTRAS), count
    ):
        stack_line = "Стек: {}.".format(stack)
        text = " ".join(
            ["Ищем {}.".format(role), CLICHE[len(out) % len(CLICHE)],
             fmt[0], salary[0], stack_line, extra[0]]
        )
        conditions = [
            {"field": fmt[1], "value": fmt[2], "quote": fmt[0]},
            {"field": salary[1], "value": salary[2], "quote": salary[0]},
            {"field": "stack", "value": stack.lower(), "quote": stack_line},
            {"field": extra[1], "value": extra[2], "quote": extra[0]},
        ]
        out.append(({"description": text}, {"conditions": conditions}))
    return out


def extract_traps(count):
    """Условий нет вовсе: правильный ответ — пустой список, а не догадки."""
    out = []
    for role, _ in _take(ROLES, count):
        text = " ".join(
            ["Ищем {}.".format(role), CLICHE[len(out) % len(CLICHE)],
             "Подробности расскажем на созвоне."]
        )
        out.append(({"description": text}, {"conditions": []}))
    return out


# ——— hr_filter ———

CLAIMS = [
    "Команда разработки — двенадцать человек.",
    "Релизы выкатываем раз в неделю.",
    "Тестовое задание занимает не больше трёх часов.",
    "Есть оплачиваемое обучение и конференции.",
    "Дежурств по ночам нет, on-call добровольный.",
]


def hr_filter_cases(count):
    pairs = [(a, b) for a, b in product(CLAIMS, CLAIMS) if a != b]
    out = []
    for i, (company, (first, second)) in enumerate(
        islice(product(COMPANIES, pairs), count)
    ):
        text = " ".join([
            "{} ищет разработчика.".format(company),
            CLICHE[i % len(CLICHE)], first, second,
        ])
        claims = [
            {"label": "размер команды" if "человек" in q else "процесс", "quote": q}
            for q in (first, second)
        ]
        out.append(({"text": text}, {"claims": claims}))
    return out


def hr_filter_traps(count):
    """Сухой текст без проверяемых утверждений: пустой список."""
    out = []
    for i, company in enumerate(_take(COMPANIES, count)):
        text = " ".join([
            "{} ищет разработчика.".format(company),
            CLICHE[i % len(CLICHE)], CLICHE[(i + 1) % len(CLICHE)],
        ])
        out.append(({"text": text}, {"claims": []}))
    return out


# ——— company ———

FACTS = [
    ("складской учёт для небольших магазинов", "продукт"),
    ("сервис расписаний для автошкол", "продукт"),
    ("платёжный шлюз для маркетплейсов", "продукт"),
    ("система документооборота для клиник", "продукт"),
]

TECHS = ["Python и PostgreSQL", "Go и Kafka", "Kotlin и ClickHouse", "Java и RabbitMQ"]


def company_cases(count):
    out = []
    for i, (company, (what, _), tech) in enumerate(
        islice(product(COMPANIES, FACTS, TECHS), count)
    ):
        hits = [
            {"title": "{} — о компании".format(company),
             "url": "https://example.test/{}/about".format(i),
             "snippet": "{} делает {}.".format(company, what)},
            {"title": "Вакансии {}".format(company),
             "url": "https://example.test/{}/jobs".format(i),
             "snippet": "Основной стек: {}.".format(tech)},
            {"title": "Блог {}".format(company),
             "url": "https://example.test/{}/blog".format(i),
             "snippet": "Команда пишет о переезде с монолита на сервисы."},
        ]
        lines = [
            "Делает {}.".format(what),
            "Основной стек — {}.".format(tech),
            "Переезжает с монолита на сервисы.",
        ]
        out.append(({"company": company, "hits": hits}, {"lines": lines}))
    return out


# ——— contacts ———

TECH_LEADS = [
    "Руководитель разработки", "CTO", "Тимлид бэкенда", "Технический директор",
]
NOISE = ["Менеджер по персоналу", "Рекрутер", "Офис-менеджер", "Бухгалтер"]
NAMES = ["Петров", "Иванова", "Сидоров", "Кузнецова", "Орлов", "Белова"]


def contacts_cases(count):
    out = []
    combos = product(TECH_LEADS, NOISE, NAMES, ROLES)
    for i, (lead, noise, name, (role, _)) in enumerate(islice(combos, count)):
        # Нужный человек стоит не первым: выбор по роли, а не по порядку.
        labels = [
            "{} А. — {}".format(name, noise),
            "{} С. — {}".format(NAMES[(i + 1) % len(NAMES)], lead),
            "{} М. — Менеджер проектов".format(NAMES[(i + 2) % len(NAMES)]),
        ]
        out.append(
            ({"candidates": labels, "role_hint": role},
             {"choice": 2, "reason": "решает по найму в разработке"})
        )
    return out


def contacts_traps(count):
    """Технического руководителя нет: кадровик — последний выбор, но выбор."""
    out = []
    for i, (noise, name) in enumerate(islice(product(NOISE, NAMES), count)):
        labels = [
            "{} А. — Бухгалтер".format(name),
            "{} С. — {}".format(NAMES[(i + 1) % len(NAMES)], noise),
        ]
        out.append(
            ({"candidates": labels, "role_hint": ROLES[i % len(ROLES)][0]},
             {"choice": 2, "reason": "технического руководителя в списке нет"})
        )
    return out


# ——— dossier ———

PATTERNS = [
    ("переработки в конце квартала", "Перед закрытием квартала задерживаются все."),
    ("текучка в отделе разработки", "За год сменилось полкоманды."),
    ("зарплату платят вовремя", "Деньги приходят день в день."),
    ("нет процессов код-ревью", "Код уезжает в прод без чужих глаз."),
]


def dossier_cases(count):
    pairs = [(a, b) for a, b in product(PATTERNS, PATTERNS) if a != b]
    out = []
    for company, ((name_a, text_a), (name_b, text_b)) in islice(
        product(COMPANIES, pairs), count
    ):
        reviews = [
            ("dreamjob", text_a),
            ("pravda-sotrudnikov", text_b),
            ("dreamjob", "{} Это повторяется не первый год.".format(text_a)),
        ]
        gold = "\n".join([
            "- {}: упоминают в нескольких отзывах".format(name_a),
            "- {}: упоминают отдельно".format(name_b),
            "- отзывов мало, выводы предварительные",
        ])
        out.append(({"company": company, "reviews": reviews}, gold))
    return out


# ——— review_fake ———

AD_TEMPLATES = [
    "{company} — лучший работодатель города, всем советую! Отличные условия, "
    "белая зарплата, дружный коллектив и настоящие перспективы роста.",
    "Работа в {company} — это компания мечты: заботливое руководство, "
    "современный офис, бесплатные обеды и карьерный рост для каждого!",
]
EXPERIENCE_TEMPLATES = [
    "Проработал {years} года {role}, задачи однотипные, начальник менялся дважды.",
    "Зарплату платили вовремя, но перед релизом сидели допоздна. {role} тут "
    "закрывает и тесты, и деплой.",
]



TECH_LEAD_TEMPLATES = [
    "{company} — лучший работодатель города, всем советую! Отличные условия, "
    "белая зарплата, дружный коллектив и настоящие перспективы роста.",
    "Работа в {company} — это компания мечты: заботливое руководство, "
    "современный офис, бесплатные обеды и карьерный рост для каждого!",
]
EXPERIENCE_TEMPLATES = [
    "Проработал {years} года {role}, задачи однотипные, начальник менялся дважды.",
    "Зарплату платили вовремя, но перед релизом сидели допоздна. {role} тут "
    "закрывает и тесты, и деплой.",
]


def review_fake_cases(count):
    combos = product(AD_TEMPLATES, EXPERIENCE_TEMPLATES, COMPANIES, ROLES)
    out = []
    for i, (ad_t, exp_t, company, (role, _)) in enumerate(islice(combos, count)):
        ad = ad_t.format(company=company)
        exp = exp_t.format(years=2 + i % 4, role=role.lower())
        items = [
            {"id": 0, "verdict": "ad", "quote": ad},
            {"id": 1, "verdict": "experience", "quote": exp},
        ]
        out.append(({"reviews": [ad, exp]}, {"items": items}))
    return out


def review_fake_traps(count):
    """Короткий нейтральный отзыв: сомневаешься — experience."""
    short = ["Нормально. Работать можно.", "Ничего особенного, обычная контора.",
             "Год отработал, ушёл спокойно.", "Средне: бывает лучше, бывает хуже."]
    out = []
    for i, (text, company) in enumerate(islice(product(short, COMPANIES), count)):
        body = "{} {}".format(company, text)
        out.append(
            ({"reviews": [body]},
             {"items": [{"id": 0, "verdict": "experience", "quote": body}]})
        )
    return out


# ——— ai_text ———

GENERATED_TEMPLATE = (
    "В современном динамичном мире компания {company} предоставляет уникальную "
    "возможность реализовать свой потенциал в команде настоящих профессионалов. "
    "Мы ценим каждого сотрудника и создаём комфортную среду, в которой "
    "инновации сочетаются с заботой о людях. Присоединяйтесь к нам, чтобы "
    "вместе двигать индустрию вперёд и раскрывать свои сильные стороны "
    "каждый день."
)
HUMAN_TEMPLATE = (
    "Нужен {role} на легаси: {stack}, тестов почти нет, миграцию тянем сами. "
    "Задачи приходят от двух команд сразу, приоритеты меняются в середине "
    "недели. Отпуск летом не обещаю, зато ночных дежурств нет и релизы днём. "
    "Если любите чистый код с нуля — это точно не к нам, тут сначала надо "
    "разгрести то, что уже написано."
)


HUMAN_TEMPLATE_2 = (
    "Команда маленькая, {role} тут один на два сервиса: {stack}. Документации "
    "мало, половина знаний в головах, поэтому первые недели будете много "
    "спрашивать. Зато решения принимаются за день, без трёх согласований. "
    "Ищем того, кто спокойно относится к чужому коду и умеет чинить, а не "
    "переписывать с нуля."
)


def ai_text_cases(count):
    humans = (HUMAN_TEMPLATE, HUMAN_TEMPLATE_2)
    combos = product(COMPANIES, ROLES, humans)
    out = []
    for company, (role, stack), human_t in islice(combos, count):
        gen = GENERATED_TEMPLATE.format(company=company)
        human = human_t.format(role=role.lower(), stack=stack)
        items = [
            {"id": 0, "verdict": "generated", "quote": gen},
            {"id": 1, "verdict": "human", "quote": human},
        ]
        out.append(({"texts": [gen, human]}, {"items": items}))
    return out


def gold_json(value):
    return json.dumps(value, ensure_ascii=False)
