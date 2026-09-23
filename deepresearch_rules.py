"""Темы глубокого ресёрча, шаблоны запросов, доверие доменам. Только данные.

Отделено от логики по [CORE-024] и [CORE-025]. Здесь правятся формулировки и
веса, а не разбор страниц.

Что важно понимать про этот файл.

Список источников открытый. Владелец решил: тянемся до всего, до чего можем
дотянуться, а закрытый список — только у стоп-доменов (агрегаторы вакансий и
сборщики отзывов, где про контору пишет она сама). Доверие домену задаётся
таблицей: реестр и суд весят как собственное наблюдение, новость — меньше,
неизвестный сайт — половину.

Маркеры ищутся подстрокой по нижнему регистру. Морфологию не подключаем ради
одной таблицы [CORE-015]: вместо «сокращение» пишем «сокращ».

Круг второй запускается только от факта: нашли ИНН — спрашиваем по ИНН. Ни
одного запроса «из головы» модели здесь нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import company_score_rules as R

# Сколько ссылок берём с одного запроса. Не настройка: больше пяти — это уже
# вторая страница выдачи, где начинается мусор.
HITS_PER_QUERY = 5
# Пауза между загрузками страниц, секунды [CORE-014].
PAUSE = 1.0
# Сколько символов текста страницы разбираем.
MAX_PAGE_CHARS = 20000
# Длина цитаты в находке.
QUOTE_CHARS = 220

# Доверие домену. Ключ — суффикс хоста.
DOMAIN_TRUST: tuple[tuple[str, float], ...] = (
    ("kad.arbitr.ru", 1.0),
    ("sudact.ru", 1.0),
    ("fssp.gov.ru", 1.0),
    ("fedresurs.ru", 1.0),
    ("bankrot.fedresurs.ru", 1.0),
    ("egrul.nalog.ru", 1.0),
    ("pb.nalog.ru", 1.0),
    ("rusprofile.ru", 0.9),
    ("checko.ru", 0.9),
    ("list-org.com", 0.9),
    ("zachestnyibiznes.ru", 0.8),
    ("sbis.ru", 0.8),
    ("audit-it.ru", 0.8),
    ("rbc.ru", 0.7),
    ("kommersant.ru", 0.7),
    ("vedomosti.ru", 0.7),
    ("forbes.ru", 0.7),
    ("interfax.ru", 0.7),
    ("tadviser.ru", 0.6),
    ("vc.ru", 0.5),
    ("habr.com", 0.5),
)
DEFAULT_TRUST = 0.5

# Куда не ходим. Агрегаторы вакансий пересказывают саму вакансию, отзовики уже
# разобраны в досье, соцсети работодателя — его же реклама.
STOP_DOMAINS: tuple[str, ...] = (
    "hh.ru",
    "superjob.ru",
    "rabota.ru",
    "avito.ru",
    "zarplata.ru",
    "trudvsem.ru",
    "career.habr.com",
    "dreamjob.ru",
    "pravda-sotrudnikov.ru",
    "orabote.top",
    "otzyvy-sotrudnikov.ru",
    "antijob.net",
    "facebook.com",
    "instagram.com",
    "vk.com",
    "t.me",
)

# Признаки капчи и защиты. Встретили — источник пропускаем и идём к следующему
# [CORE-017]: капчу не обходим и не разгадываем.
BLOCK_MARKERS: tuple[str, ...] = (
    "captcha",
    "recaptcha",
    "подтвердите, что вы не робот",
    "подтвердите что вы не робот",
    "я не робот",
    "проверка браузера",
    "checking your browser",
    "access denied",
    "доступ ограничен",
    "cf-browser-verification",
    "attention required",
)


@dataclass(frozen=True)
class Topic:
    """Одна тема ресёрча: что спрашиваем и что считаем находкой."""

    code: str
    title: str
    axis: str
    polarity: str
    weight: int
    queries: tuple[str, ...]
    markers: tuple[str, ...]
    round: int = 1
    # Тема нужна только чтобы вытащить реквизиты для второго круга.
    lookup: bool = False
    extras: tuple[str, ...] = field(default_factory=tuple)


TOPICS: tuple[Topic, ...] = (
    Topic(
        code="registry",
        title="Юрлицо в реестре",
        axis=R.TRUTH,
        polarity="green",
        weight=2,
        queries=("{company} ИНН ОГРН реквизиты юридическое лицо",),
        markers=("действующ", "дата регистрации", "огрн"),
        lookup=True,
    ),
    Topic(
        code="salary_delay",
        title="Суды по невыплате зарплаты",
        axis=R.PAY,
        polarity="red",
        weight=5,
        queries=(
            "{company} суд невыплата заработной платы",
            "{company} иск о взыскании заработной платы",
        ),
        markers=(
            "невыплат",
            "задолженность по заработной плате",
            "взыскании заработной платы",
            "взыскании невыплаченной",
            "трудовой спор",
            "задержк",
        ),
    ),
    Topic(
        code="bankruptcy",
        title="Банкротство и ликвидация",
        axis=R.PAY,
        polarity="red",
        weight=5,
        queries=(
            "{company} банкротство",
            "{company} ликвидация исключение из ЕГРЮЛ",
        ),
        markers=(
            "банкрот",
            "конкурсное производство",
            "наблюдение введено",
            "в стадии ликвидации",
            "исключено из егрюл",
            "недействующ",
        ),
    ),
    Topic(
        code="debts",
        title="Долги и исполнительные производства",
        axis=R.PAY,
        polarity="red",
        weight=3,
        queries=(
            "{company} исполнительное производство задолженность",
            "{company} задолженность по налогам приостановление операций",
        ),
        markers=(
            "исполнительное производство",
            "задолженность по налогам",
            "приостановление операций по счет",
            "недоимк",
        ),
    ),
    Topic(
        code="finance_loss",
        title="Убытки и падение выручки",
        axis=R.PAY,
        polarity="red",
        weight=2,
        queries=("{company} выручка чистый убыток бухгалтерская отчётность",),
        markers=("чистый убыток", "убыток за", "выручка снизилась", "отрицательные чистые активы"),
    ),
    Topic(
        code="layoffs",
        title="Сокращения и массовые увольнения",
        axis=R.CHURN,
        polarity="red",
        weight=3,
        queries=(
            "{company} сокращение сотрудников новости",
            "{company} массовые увольнения",
        ),
        markers=("сокращ", "массовые увольнения", "уволил", "оптимизация штата"),
    ),
    Topic(
        code="court_general",
        title="Прочие судебные споры",
        axis=R.TRUTH,
        polarity="red",
        weight=2,
        queries=("{company} арбитражное дело ответчик",),
        markers=("ответчик", "арбитражн", "исковое заявление"),
    ),
    # —— второй круг: только по найденным реквизитам ——
    Topic(
        code="salary_delay_inn",
        title="Суды по зарплате по ИНН",
        axis=R.PAY,
        polarity="red",
        weight=5,
        queries=("ИНН {inn} взыскание заработной платы суд",),
        markers=(
            "невыплат",
            "задолженность по заработной плате",
            "взыскании заработной платы",
        ),
        round=2,
    ),
    Topic(
        code="debts_inn",
        title="Долги по ИНН",
        axis=R.PAY,
        polarity="red",
        weight=3,
        queries=("ИНН {inn} исполнительное производство задолженность",),
        markers=("исполнительное производство", "задолженность", "недоимк"),
        round=2,
    ),
    Topic(
        code="bankruptcy_inn",
        title="Банкротство по ИНН",
        axis=R.PAY,
        polarity="red",
        weight=5,
        queries=("ИНН {inn} банкротство федресурс",),
        markers=("банкрот", "конкурсное производство", "наблюдение введено"),
        round=2,
    ),
)

TOPIC_BY_CODE = {topic.code: topic for topic in TOPICS}

# Реквизиты вытаскиваются регулярками со страницы: числа из модели запрещены
# [CORE-019].
INN_RE = re.compile(r"ИНН[^\d]{0,12}(\d{10}|\d{12})", re.IGNORECASE)
OGRN_RE = re.compile(r"ОГРН[^\d]{0,12}(\d{13}|\d{15})", re.IGNORECASE)
REG_DATE_RE = re.compile(
    r"(?:дата регистрации|зарегистрирован[аоы]?)[^\d]{0,20}(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
    re.IGNORECASE,
)


__all__ = (
    "BLOCK_MARKERS",
    "DEFAULT_TRUST",
    "DOMAIN_TRUST",
    "HITS_PER_QUERY",
    "INN_RE",
    "MAX_PAGE_CHARS",
    "OGRN_RE",
    "PAUSE",
    "QUOTE_CHARS",
    "REG_DATE_RE",
    "STOP_DOMAINS",
    "TOPICS",
    "TOPIC_BY_CODE",
    "Topic",
)
