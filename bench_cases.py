"""Задачи для сравнения моделей: входы, ожидания и проверки.

Почему не «спросить модель, какая модель лучше». Единственная полезная оценка
здесь — прошла ли задача те же проверки, что стоят в пайплайне: цитата дословно
из текста, никаких новых чисел, адресат из списка. Это считается детерминированно
и одинаково для любой модели [CORE-015].

Набор намеренно маленький: половина кейсов — ловушки. Модель, которая красиво
пишет, но дорисовывает вилку, для этого проекта хуже, чем скучная и точная.

Все тексты выдуманы: бенчмарк гоняют и на внешнем прокси, персональных данных
в кейсах нет [CORE-012].
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Case:
    """Одна задача: что подаём на вход и что считаем успехом."""

    name: str
    stage: str
    payload: dict[str, Any]
    expect: dict[str, Any] = field(default_factory=dict)
    trap: bool = False


@dataclass(frozen=True)
class Hit:
    """Сниппет выдачи — ровно то, что видит company_brief."""

    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class Person:
    """Кандидат в адресаты. label — то, что уходит в промпт."""

    label: str


@dataclass
class Draft:
    """Черновик письма. dataclass, потому что polish_draft делает replace()."""

    body: str


HYBRID = (
    "Ищем python-разработчика в команду биллинга. Формат гибридный: два дня в "
    "офисе на Тульской, остальное из дома. Вилка 250 000 — 320 000 на руки. "
    "Стек: FastAPI, PostgreSQL, Kafka. Оформление по ТК РФ, график 5/2."
)

NO_SALARY = (
    "Нужен backend-разработчик на Python. Работа полностью удалённая, команда "
    "из семи человек. Обсуждаем зарплату на собеседовании по итогам вилки "
    "кандидата. Стек: Django, PostgreSQL, Redis."
)

HITS = (
    Hit(
        url="https://acme.dev/about",
        title="ACME — платформа логистики",
        snippet="ACME делает платформу для планирования доставки. Команда 40 человек.",
    ),
    Hit(
        url="https://acme.dev/blog/stack",
        title="Наш стек",
        snippet="Бэкенд на Python и Go, очереди на Kafka, данные в PostgreSQL.",
    ),
    Hit(
        url="https://habr.com/acme",
        title="Как мы считаем маршруты",
        snippet="Разбор алгоритма маршрутизации и нагрузочного тестирования.",
    ),
)

PEOPLE = (
    Person("Ирина Соколова — менеджер по подбору персонала"),
    Person("Павел Дьяченко — CTO, отвечает за платформу"),
    Person("info@acme.dev — общий ящик компании"),
)

PEOPLE_TRAP = (
    Person("Анна Крылова — HR business partner, ведёт наём в IT"),
    Person("recruiting@acme.dev — почта отдела подбора"),
    Person("Сергей Мах — тимлид бэкенда, собеседует сам"),
)

INTAKE_FULL = (
    "Ищу backend на Python, 8 лет опыта, последние 3 года highload в финтехе. "
    "Хочу удалёнку, от 250 тысяч на руки. Не рассматриваю 1С и поддержку легаси."
)

INTAKE_THIN = "Хочу работать с Python. Надоело то, чем занимаюсь сейчас."

VACANCY_CLAIMS = (
    "У нас дружная команда и нулевая бюрократия. Зарплата выплачивается два "
    "раза в месяц без задержек. Переработки бывают редко и всегда "
    "компенсируются отгулами. Оформление по ТК РФ с первого дня."
)

VACANCY_DRY = (
    "Требуется инженер по данным. Стек: Python, Airflow, ClickHouse. "
    "Офис на Тульской, график 5/2."
)

SECTION_ANSWER = (
    "Работал в Тинькофф 4 года, делал биллинг на FastAPI и PostgreSQL, "
    "отвечал за очереди на Kafka и дежурства."
)

RESUME_POOL = (
    ("experience", "Биллинг на FastAPI и PostgreSQL, очереди на Kafka."),
    ("experience", "Вёрстка лендингов на jQuery в студии."),
    ("skills", "Python, FastAPI, PostgreSQL, Kafka, Docker."),
    ("education", "Радиофизика, МГУ."),
    ("projects", "Пет-проект: телеграм-бот для учёта расходов."),
)

TAILOR_VACANCY = (
    "Нужен backend-разработчик: Python, FastAPI, PostgreSQL, очереди Kafka, "
    "биллинг и высокие нагрузки."
)

REVIEWS = (
    ("отзовик", "Зарплату задерживали три месяца подряд, пришлось уйти."),
    ("отзовик", "Задержки выплат подтверждаю, руководство обещает и молчит."),
    ("отзовик", "Технически интересно, стек современный, коллеги сильные."),
    ("отзовик", "Переработки постоянные, отгулы не дают."),
)

LETTER = (
    "Здравствуйте! Меня зовут Алексей, я backend-разработчик с 8 годами опыта. "
    "Увидел вашу вакансию по биллингу. Делал похожий сервис: FastAPI, PostgreSQL, "
    "очереди на Kafka, нагрузка около 300 запросов в секунду. Готов за 20 минут "
    "разобрать ваш узкий участок или прислать код. Если это не ваша зона — "
    "подскажите, кто ведёт направление."
)

# Отзывы для этапа review_fake. Первый и третий — живой опыт, второй написан
# как реклама: ни одной проверяемой детали, зато весь набор клише.
FAKE_REVIEWS = (
    "Работал два года в отделе биллинга. Зарплата приходила 10 и 25 числа, "
    "переработки бывали перед релизом, но их оплачивали.",
    "Динамично развивающаяся компания, дружный коллектив, современный офис и "
    "перспективы роста. Рекомендую всем, кто ищет стабильную работу!",
    "Ушёл после испытательного: обещали python, посадили на поддержку 1С. "
    "Руководитель отдела сменился дважды за три месяца.",
)
HONEST_REVIEWS = (
    "Три месяца на испытательном, оффер совпал с тем, что говорили на "
    "собеседовании. Из минусов — тесты пишет один человек на всю команду.",
    "Задерживали зарплату в декабре на неделю, извинились и выплатили. "
    "Отпуск дают без споров, график 5/2.",
)

# Тексты для этапа ai_text. Первый собран из генеративного канцелярита, второй
# написан человеком: числа, имена систем, сбивчивый порядок мыслей.
AI_TEXTS = (
    "В современном мире динамично развивающаяся компания открывает широкие "
    "возможности для профессионального роста и развития. Мы ценим каждого "
    "сотрудника и предлагаем комплексный подход к решению задач любой "
    "сложности. Важно отметить, что наша команда профессионалов работает над "
    "инновационными продуктами, а эффективное взаимодействие внутри "
    "коллектива является ключевым аспектом успешного развития бизнеса.",
)
HUMAN_TEXTS = (
    "Ищем человека в команду биллинга: 4 сервиса на FastAPI, PostgreSQL 14, "
    "Kafka, около 300 rps в пике. Половина времени уйдёт на разбор чужого кода "
    "2019 года, предупреждаем сразу. Тесты есть, но покрытие 40%. Релизы по "
    "вторникам, дежурства раз в шесть недель, оплачиваются отдельно. Офис у "
    "Павелецкой, два дня в неделю обязательны, остальное из дома.",
)

CASES: tuple[Case, ...] = (
    Case(
        "extract: гибрид и вилка",
        "extract",
        {"description": HYBRID},
        {"fields": ("format", "salary"), "min_count": 2},
    ),
    Case(
        "extract: вилки нет",
        "extract",
        {"description": NO_SALARY},
        {"no_new_numbers": True},
        trap=True,
    ),
    Case(
        "company: справка по сниппетам",
        "company",
        {"company": "ACME", "hits": HITS},
        {"min_lines": 3, "forbidden_words": ("лидер", "динамично", "инвестиц")},
    ),
    Case(
        "contacts: выбрать CTO",
        "contacts",
        {"candidates": PEOPLE, "role_hint": "Python-разработчик"},
        {"choice": 2},
    ),
    Case(
        "contacts: кадровик первым",
        "contacts",
        {"candidates": PEOPLE_TRAP, "role_hint": "Python-разработчик"},
        {"choice": 3},
        trap=True,
    ),
    Case(
        "intake: слова о себе",
        "intake",
        {"text": INTAKE_FULL},
        {"needs": ("salary_min_net", "remote_ok", "queries"), "salary": 250000},
    ),
    Case(
        "intake: ничего не сказано (ловушка)",
        "intake",
        {"text": INTAKE_THIN},
        {"forbidden": ("salary_min_net", "experience_ok"), "min_questions": 1},
        trap=True,
    ),
    Case(
        "hr_filter: обещания вакансии",
        "hr_filter",
        {"text": VACANCY_CLAIMS},
        {"min_claims": 2},
    ),
    Case(
        "hr_filter: обещать нечего (ловушка)",
        "hr_filter",
        {"text": VACANCY_DRY},
        {"max_claims": 0},
        trap=True,
    ),
    Case(
        "resume_section: оформить ответ",
        "resume_section",
        {"section": "experience", "answer": SECTION_ANSWER, "role_hint": "backend"},
        {},
    ),
    Case(
        "resume_tailor: отобрать блоки",
        "resume_tailor",
        {"blocks": RESUME_POOL, "vacancy": TAILOR_VACANCY},
        {"first": 1},
    ),
    Case(
        "dossier: сводка по отзывам",
        "dossier",
        {"company": "ACME", "reviews": REVIEWS},
        {"min_lines": 3, "max_lines": 6},
    ),
    Case(
        "review_fake: рекламный текст среди живых",
        "review_fake",
        {"reviews": FAKE_REVIEWS},
        {"ad": (1,)},
    ),
    Case(
        "review_fake: живые отзывы не трогать",
        "review_fake",
        {"reviews": HONEST_REVIEWS},
        {"ad": ()},
        trap=True,
    ),
    Case(
        "ai_text: генеративный канцелярит",
        "ai_text",
        {"texts": AI_TEXTS},
        {"ad": (0,)},
    ),
    Case(
        "ai_text: живой текст не трогать",
        "ai_text",
        {"texts": HUMAN_TEXTS},
        {"ad": ()},
        trap=True,
    ),
    Case(
        "draft: переписать письмо",
        "draft",
        {"body": LETTER, "facts": ("8 лет опыта",)},
        {"changed": True},
    ),
)


def check(case: Case, result: Any) -> tuple[float, str]:
    """Оценка от 0 до 1 и короткая причина. Проверки те же, что в пайплайне."""
    if case.stage == "extract":
        return _check_extract(case, result)
    if case.stage == "company":
        return _check_company(case, result)
    if case.stage == "contacts":
        return _check_contacts(case, result)
    if case.stage == "draft":
        return _check_draft(case, result)
    if case.stage in ("review_fake", "ai_text"):
        return _check_review_fake(case, result)
    if case.stage == "intake":
        return _check_intake(case, result)
    if case.stage == "hr_filter":
        return _check_claims(case, result)
    if case.stage == "resume_section":
        return _check_section(case, result)
    if case.stage == "resume_tailor":
        return _check_tailor(case, result)
    if case.stage == "dossier":
        return _check_dossier(case, result)
    return 0.0, "неизвестный этап"


def _check_intake(case: Case, result: Any) -> tuple[float, str]:
    """Разговор о поиске: поля заполнены из слов человека и только из них."""
    if result is None:
        return 0.0, "ответа нет"
    patch = dict(result.profile)
    if result.dropped:
        return 0.0, "дорисовала: {}".format(result.dropped[0][:60])

    forbidden = [key for key in case.expect.get("forbidden", ()) if key in patch]
    if forbidden:
        return 0.0, "заполнила наугад: {}".format(", ".join(forbidden))

    least = int(case.expect.get("min_questions", 0))
    if least and len(result.questions) < least:
        # Ловушка: из «хочу что-то на Python» критерии не выводятся, их спрашивают.
        return 0.0, "ничего не спросила"

    wanted = tuple(case.expect.get("needs", ()))
    if not wanted:
        return 1.0, "вопросов: {}".format(len(result.questions))
    hit = sum(1 for key in wanted if patch.get(key) not in (None, "", [], {}))
    salary = case.expect.get("salary")
    if salary is not None and patch.get("salary_min_net") not in (None, salary):
        return 0.0, "зарплата мимо: {}".format(patch.get("salary_min_net"))
    return hit / len(wanted), "поля: {}".format(", ".join(sorted(patch)) or "—")


def _check_claims(case: Case, result: Any) -> tuple[float, str]:
    """HR-фильтр: цитаты уже сверены с текстом, вопрос — сколько их и есть ли лишние."""
    claims = tuple(result or ())
    ceiling = case.expect.get("max_claims")
    if ceiling is not None:
        # Ловушка: в сухом тексте обещаний нет, и выдумывать их не надо.
        return (1.0, "пусто, как и надо") if len(claims) <= int(ceiling) else (
            0.0,
            "нашла обещания на пустом месте: {}".format(len(claims)),
        )
    need = int(case.expect.get("min_claims", 1))
    return min(len(claims), need) / need, "цитат: {}".format(len(claims))


def _check_section(case: Case, result: Any) -> tuple[float, str]:
    """Секция резюме: draft_section сам отбрасывает дописанные числа и воду."""
    if result is None:
        return 0.0, "черновик отброшен (числа или раздув)"
    text, added = result
    if not text.strip():
        return 0.0, "пусто"
    if text.strip() == case.payload["answer"].strip():
        return 0.5, "вернула ответ как есть"
    return 1.0, "{} символов, дописано строк: {}".format(len(text), len(added))


def _check_tailor(case: Case, result: Any) -> tuple[float, str]:
    """Отбор блоков под вакансию: первым должен идти релевантный опыт."""
    if result is None:
        return 0.0, "порядок не вернулся"
    order, _reason = result
    if not order:
        return 0.0, "пустой порядок"
    want = int(case.expect["first"])
    if order[0] == want:
        return 1.0, "первым {}".format(order[0])
    return (0.5 if want in order else 0.0), "порядок: {}".format(
        ", ".join(str(i) for i in order[:5])
    )


def _check_dossier(case: Case, result: Any) -> tuple[float, str]:
    """Сводка по отзывам: своими словами, без новых чисел и без имён."""
    text = str(result or "").strip()
    if not text:
        return 0.0, "сводки нет"
    source = " ".join(body for _, body in case.payload["reviews"])
    invented = sorted(_numbers(text) - _numbers(source))
    if invented:
        return 0.0, "дорисовала числа: {}".format(", ".join(invented))
    lines = [line for line in text.splitlines() if line.strip()]
    least = int(case.expect.get("min_lines", 3))
    most = int(case.expect.get("max_lines", 6))
    if len(lines) < least:
        return len(lines) / least, "строк: {}".format(len(lines))
    if len(lines) > most:
        return 0.5, "растеклась: строк {}".format(len(lines))
    return 1.0, "строк: {}".format(len(lines))


NUMBER_RE = re.compile(r"\d+")


def _numbers(text: str) -> set[str]:
    """Числа из текста. Пробелы внутри числа снимаются: «250 000» — одно число."""
    glued = re.sub(r"(?<=\d)[\s\u00a0](?=\d)", "", text or "")
    return set(NUMBER_RE.findall(glued))


def _check_extract(case: Case, result: Any) -> tuple[float, str]:
    items = tuple(result or ())
    got = {item.field for item in items}

    if case.expect.get("no_new_numbers"):
        # Ловушка про вилку. Ловится не поле salary, а выдуманная сумма: про
        # зарплату в тексте сказано, и условие «обсуждается на собеседовании» с
        # дословной цитатой — правильный разбор, а не ошибка. Цитаты пайплайн
        # уже сверил с текстом, поэтому смотрим на value, и только у salary:
        # «команда 7 человек» из «команда из семи человек» — не выдумка.
        known = _numbers(case.payload.get("description", ""))
        invented = sorted(
            {
                n
                for item in items
                if item.field == "salary"
                for n in _numbers(item.value)
            }
            - known
        )
        if invented:
            return 0.0, "дорисовала числа: {}".format(", ".join(invented))
        # Молчание тоже провал, иначе «ничего не ответила» выглядело бы победой.
        return (1.0, "чисто: {}".format(len(items))) if items else (0.0, "пусто")

    wanted = tuple(case.expect.get("fields", ()))
    if not wanted:
        return (1.0, "чисто: {}".format(len(items))) if items else (0.0, "пусто")
    if len(items) < int(case.expect.get("min_count", 1)):
        return 0.0, "условий мало: {}".format(len(items))
    hit = len(got & set(wanted))
    return hit / len(wanted), "поля: {}".format(", ".join(sorted(got)) or "—")


def _check_company(case: Case, result: Any) -> tuple[float, str]:
    if result is None:
        return 0.0, "справки нет (или все строки отброшены)"
    lines = tuple(result.lines)
    low = " ".join(lines).lower()
    dirty = [w for w in case.expect.get("forbidden_words", ()) if w in low]
    if dirty:
        return 0.0, "маркетинг: {}".format(", ".join(dirty))
    need = int(case.expect.get("min_lines", 3))
    if len(lines) < need:
        return len(lines) / need, "строк: {}".format(len(lines))
    return 1.0, "строк: {}".format(len(lines))


def _check_contacts(case: Case, result: Any) -> tuple[float, str]:
    people = list(case.payload["candidates"])
    want = people[int(case.expect["choice"]) - 1]
    if result is None:
        return 0.0, "адресат не выбран"
    ok = getattr(result, "label", "") == want.label
    return (1.0 if ok else 0.0), getattr(result, "label", "")[:48]


def _check_review_fake(case: Case, result: Any) -> tuple[float, str]:
    """Сигнал по индексам (review_fake, ai_text): молчание на живом тексте важнее.

    Ложное срабатывание здесь дороже, поэтому лишний индекс обнуляет кейс, а
    пропущенный — только половинит [CORE-019].
    """
    got = set(result or ())
    want = set(case.expect.get("ad", ()))
    extra = sorted(got - want)
    if extra:
        return 0.0, "лишние: {}".format(", ".join(str(i) for i in extra))
    if got == want:
        return 1.0, "совпало"
    return 0.5, "пропустила: {}".format(", ".join(str(i) for i in sorted(want - got)))


def _check_draft(case: Case, result: Any) -> tuple[float, str]:
    text = str(getattr(result, "body", "") or "")
    if not text:
        return 0.0, "пусто"
    if text.strip() == case.payload["body"].strip():
        # polish_draft откатывается сам: выдуманные числа, огрызок или перебор
        # по длине. Для модели это провал задачи, а не безобидный ноль.
        return 0.0, "откат к черновику"
    return 1.0, "{} символов".format(len(text))


__all__ = ("CASES", "Case", "Draft", "Hit", "Person", "check")
