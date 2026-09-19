"""Каталог настроек: какие поля бывают, как подписаны и в какой группе.

Вынесено из settings.py по [CORE-024]: сам каталог не знает ни про .env, ни про
формы — это данные, а не логика. Публичные имена остаются доступны через
settings, чтобы вызывающие не переучивались.
"""

from __future__ import annotations

from dataclasses import dataclass

TEXT = "text"
SECRET = "secret"
INT = "int"
FLOAT = "float"
BOOL = "bool"

TRUE_VALUES = {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Field:
    """Одна настройка: ключ в .env плюс всё, что нужно для формы."""

    key: str
    label: str
    group: str
    kind: str = TEXT
    default: str = ""
    help: str = ""

    @property
    def is_secret(self) -> bool:
        return self.kind == SECRET


GROUP_RUN = "Запуск"
GROUP_PREFILTER = "Предфильтр"
GROUP_DETECTOR = "Детектор брехни"
GROUP_SOURCE = "Источник вакансий"
GROUP_TELEGRAM = "Telegram"
GROUP_LLM = "Модель"
GROUP_SEARCH = "Внешний поиск"
GROUP_PATHS = "Файлы и логи"

# Порядок важен: в таком виде поля рисуются на странице настроек.
FIELDS: tuple[Field, ...] = (
    Field(
        "RUN_LIMIT",
        "Сколько вакансий собирать за прогон",
        GROUP_RUN,
        INT,
        "30",
        "Верхняя граница на один запуск сбора. Цель — не охват, а пять хороших входов.",
    ),
    Field(
        "RUN_DETAILS",
        "Загружать полные описания",
        GROUP_RUN,
        BOOL,
        "1",
        "Без описаний не работают ни детектор HR-сигналов, ни разбор условий.",
    ),
    Field(
        "OUTREACH_LIMIT",
        "Сколько писем готовить за прогон",
        GROUP_RUN,
        INT,
        "5",
        "Пять осмысленных писем работают лучше сотни откликов.",
    ),
    Field(
        "OUTREACH_MIN_SCORE",
        "Порог скоринга для письма",
        GROUP_RUN,
        FLOAT,
        "60",
        "Вакансии ниже порога в письма не попадают.",
    ),
    Field(
        "OUTREACH_ALLOW_GENERIC",
        "Готовить сопроводительное без прямого контакта",
        GROUP_RUN,
        BOOL,
        "1",
        "hh.ru вырезает контакты из текста: без этого флага почти всё отбрасывается.",
    ),
    Field(
        "OUTREACH_CHECK_MX",
        "Проверять MX домена",
        GROUP_RUN,
        BOOL,
        "0",
        "Нужен dnspython. Отсекает угаданные адреса на мёртвых доменах.",
    ),
    Field(
        "RUN_PROFILE",
        "Файл профиля",
        GROUP_RUN,
        TEXT,
        "profile.yaml",
        "Запросы, навыки, порог скоринга и факты о себе.",
    ),
    Field(
        "PREFILTER_ENABLED",
        "Отсеивать вакансии до загрузки описания",
        GROUP_PREFILTER,
        BOOL,
        "1",
        "Предфильтр считает черновой скор по выдаче и экономит запросы к hh.ru. "
        "Выключенный — собираем всё подряд, включая явный мусор.",
    ),
    Field(
        "PREFILTER_MIN_SCORE",
        "Черновой скор, ниже которого не открываем вакансию",
        GROUP_PREFILTER,
        FLOAT,
        "0",
        "0 — отсекать только по стоп-словам и вилке. Скор на выдаче занижен: "
        "описания ещё нет, и стек виден не весь.",
    ),
    Field(
        "PREFILTER_FUZZY",
        "Порог нечёткого совпадения навыка, %",
        GROUP_PREFILTER,
        INT,
        "88",
        "Ниже — «постгрес» и «postgresql» считаются одним навыком чаще, но растёт "
        "число ложных совпадений.",
    ),
    Field(
        "DETECTOR_ENABLED",
        "Проверять утверждения вакансии",
        GROUP_DETECTOR,
        BOOL,
        "1",
        "Сверяет обещания вакансии с историей публикаций. Выключенный — карточки "
        "без HR-флагов, слепки истории всё равно пишутся.",
    ),
    Field(
        "DETECTOR_MIN_DAYS",
        "Сколько дней истории нужно для вывода",
        GROUP_DETECTOR,
        INT,
        "30",
        "Короче — вердикт «недостаточно данных» [HRD-004]. Занижать значит выдавать "
        "догадки за наблюдения.",
    ),
    Field(
        "DETECTOR_REPUBLISH_ALARM",
        "Перепубликаций, после которых «стабильная команда» не верится",
        GROUP_DETECTOR,
        INT,
        "3",
        "Сколько раз вакансия возвращалась в выдачу за период наблюдения.",
    ),
    Field(
        "DETECTOR_WIDE_BAND",
        "Во сколько раз вилка считается фиктивной",
        GROUP_DETECTOR,
        FLOAT,
        "2.0",
        "Верхняя граница больше нижней во столько раз — вилки фактически нет.",
    ),
    Field(
        "DETECTOR_LLM_CLAIMS",
        "Пускать модель на подсказку утверждений",
        GROUP_DETECTOR,
        BOOL,
        "1",
        "Модель только показывает цитату, вердикт у таких пунктов всегда "
        "«недостаточно данных» [CORE-019]. Работает, только если модель включена.",
    ),
    Field(
        "HH_TOKEN",
        "Токен hh.ru",
        GROUP_SOURCE,
        SECRET,
        "",
        "Необязателен: сбор работает по HTML-страницам (ADR-015).",
    ),
    Field(
        "HH_COOKIE",
        "Cookie hh.ru",
        GROUP_SOURCE,
        SECRET,
        "",
        "Помогает, когда площадка начинает показывать капчу.",
    ),
    Field("HH_USER_AGENT", "User-Agent", GROUP_SOURCE, TEXT, "", "Подпись клиента при запросах."),
    Field("HH_PROXY", "Прокси для hh.ru", GROUP_SOURCE, TEXT, "", "Например http://127.0.0.1:8080"),
    Field(
        "HH_PAUSE",
        "Пауза между запросами, сек",
        GROUP_SOURCE,
        FLOAT,
        "2.0",
        "Меньше двух секунд — быстрый путь к бану.",
    ),
    Field("TELEGRAM_BOT_TOKEN", "Токен бота", GROUP_TELEGRAM, SECRET, "", "Без него карточки только в интерфейсе."),
    Field("TELEGRAM_CHAT_ID", "Чат для карточек", GROUP_TELEGRAM, TEXT, "", "Свой id можно узнать у @userinfobot."),
    Field(
        "TELEGRAM_ENABLED",
        "Отправлять карточки в Telegram",
        GROUP_TELEGRAM,
        BOOL,
        "1",
        "Выключено — прогон работает целиком, карточки просто ждут в интерфейсе.",
    ),
    Field(
        "TELEGRAM_DELAY",
        "Задержка между сообщениями, сек",
        GROUP_TELEGRAM,
        FLOAT,
        "0.6",
        "Меньше 0.5 — Telegram начнёт отвечать 429. Больше — прогон просто дольше.",
    ),
    Field(
        "TELEGRAM_QUIET_FROM",
        "Тихие часы: с",
        GROUP_TELEGRAM,
        TEXT,
        "",
        "Время в формате ЧЧ:ММ по местному времени. Пусто — тихих часов нет.",
    ),
    Field(
        "TELEGRAM_QUIET_TO",
        "Тихие часы: до",
        GROUP_TELEGRAM,
        TEXT,
        "",
        "В тихие часы карточки не уходят и ждут следующего прогона. Тревоги идут всегда.",
    ),
    Field(
        "LLM_ENABLED",
        "Использовать модель",
        GROUP_LLM,
        BOOL,
        "1",
        "Выключенная модель — штатный режим: пайплайн работает без неё целиком.",
    ),
    Field("LLM_BASE_URL", "Локальный адрес модели", GROUP_LLM, TEXT, "", "Например http://localhost:3001/v1"),
    Field("LLM_API_KEY", "Ключ локальной модели", GROUP_LLM, SECRET, "", "Обычно не нужен."),
    Field(
        "LLM_PROXY_BASE_URL",
        "Адрес внешнего прокси",
        GROUP_LLM,
        TEXT,
        "",
        "LiteLLM через туннель, например http://127.0.0.1:4000/v1",
    ),
    Field("LLM_PROXY_API_KEY", "Ключ прокси", GROUP_LLM, SECRET, "", "Мастер-ключ LiteLLM. Без него будет 401."),
    Field("LLM_PROXY_MODEL_FAST", "Модель для быстрых этапов", GROUP_LLM, TEXT, "", "Разбор условий, черновая работа."),
    Field("LLM_PROXY_MODEL_SMART", "Модель для разбора", GROUP_LLM, TEXT, "", "HR-фильтр и скоринг."),
    Field("LLM_PROXY_MODEL_LONG", "Модель для длинных текстов", GROUP_LLM, TEXT, "", "Справка о компании."),
    Field("LLM_PROXY_MODEL_EMBEDDINGS", "Модель векторов", GROUP_LLM, TEXT, "", "Например bge-m3."),
    Field(
        "LLM_PERSONAL_VIA_PROXY",
        "Пускать персональные этапы на прокси",
        GROUP_LLM,
        BOOL,
        "0",
        "ВНИМАНИЕ: включённый флаг отправляет ФИО и адреса живых людей за пределы машины [CORE-012].",
    ),
    Field("LLM_TIMEOUT", "Таймаут модели, сек", GROUP_LLM, FLOAT, "60", ""),
    Field("LLM_MAX_CALLS", "Лимит вызовов на прогон", GROUP_LLM, INT, "300", "Страховка от бесконечного цикла."),
    Field(
        "SEARCH_PROVIDER",
        "Провайдер поиска",
        GROUP_SEARCH,
        TEXT,
        "searxng",
        "searxng, tavily или brave.",
    ),
    Field(
        "SEARCH_BASE_URL",
        "Адрес SearXNG",
        GROUP_SEARCH,
        TEXT,
        "",
        "Свой инстанс, например http://127.0.0.1:8888 через туннель.",
    ),
    Field("SEARCH_API_KEY", "Ключ провайдера", GROUP_SEARCH, SECRET, "", "Только для tavily и brave."),
    Field("SEARCH_BASIC_AUTH", "Логин:пароль для SearXNG", GROUP_SEARCH, SECRET, "", "Если инстанс закрыт basic-авторизацией."),
    Field("SEARCH_TIMEOUT", "Таймаут поиска, сек", GROUP_SEARCH, FLOAT, "20", ""),
    Field("SEARCH_MAX_CALLS", "Лимит запросов на прогон", GROUP_SEARCH, INT, "60", ""),
    Field("DB_PATH", "Файл базы", GROUP_PATHS, TEXT, "data/fuckhr.sqlite3", ""),
    Field("LOG_PATH", "Файл лога", GROUP_PATHS, TEXT, "data/fuckhr.log", ""),
    Field("FAILURE_DIR", "Куда класть сырой HTML при сбоях", GROUP_PATHS, TEXT, "data/failures", ""),
    Field("ALERT_COOLDOWN_HOURS", "Пауза между тревогами, часов", GROUP_PATHS, INT, "24", ""),
)

FIELD_BY_KEY: dict[str, Field] = {field.key: field for field in FIELDS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(field.group for field in FIELDS))
