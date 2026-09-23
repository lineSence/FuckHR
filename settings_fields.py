"""Каталог настроек: какие поля бывают, как подписаны и в какой группе.

Вынесено из settings.py по [CORE-024]: каталог не знает ни про .env, ни про
формы — это данные, а не логика. Публичные имена доступны через settings.
"""

from __future__ import annotations

import llm

from dataclasses import dataclass, replace

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
GROUP_SCORE = "Оценка работодателя"
GROUP_DEEP = "Глубокий ресёрч"
GROUP_SOURCE = "Источник вакансий"
GROUP_TELEGRAM = "Telegram"
GROUP_LLM = "Модель"
GROUP_LLM_STAGES = "Модель по этапам"
GROUP_EMBED = "Векторы и похожесть"
GROUP_SEARCH = "Внешний поиск"
GROUP_PATHS = "Файлы и логи"

# Подпись группы в свёрнутом виде: по названию не всегда понятно, что внутри.
GROUP_HINTS: dict[str, str] = {
    GROUP_RUN: "сколько собираем за прогон, цикл, письма",
    GROUP_PREFILTER: "что отсеиваем до загрузки описания",
    GROUP_DETECTOR: "пороги проверки утверждений вакансии",
    GROUP_SCORE: "светофор работодателя и его влияние на скоринг",
    GROUP_DEEP: "реестры, суды, долги, новости по кнопке",
    GROUP_SOURCE: "темп запросов к hh.ru, cookie и прокси",
    GROUP_TELEGRAM: "куда и как часто уходят карточки",
    GROUP_LLM: "адреса шлюза, имена моделей, лимиты",
    GROUP_LLM_STAGES: "имя модели для отдельного этапа, если профиля мало",
    GROUP_EMBED: "семантические дубли отзывов и похожие вакансии",
    GROUP_SEARCH: "провайдер поиска и потолки запросов",
}

# Порядок важен: в таком виде поля рисуются на странице настроек.
FIELDS: tuple[Field, ...] = (
    Field(
        "RUN_LIMIT",
        "Сколько вакансий собирать за прогон",
        GROUP_RUN,
        INT,
        "30",
        "Верхняя граница на один запуск сбора. Цель — не охват, а пять хороших "
        "входов. Ноль — без ограничения: сбор идёт, пока не кончатся страницы выдачи.",
    ),
    Field(
        "RUN_LOOP_ENABLED",
        "Повторять сбор по кругу",
        GROUP_RUN,
        BOOL,
        "0",
        "Режим цикла: прогон повторяется, пока не кончатся циклы или не остановишь сам. "
        "Включается и галочкой на странице запуска.",
    ),
    Field(
        "RUN_LOOP_CYCLES",
        "Сколько циклов",
        GROUP_RUN,
        INT,
        "0",
        "Ноль — пока не остановишь вручную.",
    ),
    Field(
        "RUN_LOOP_PAUSE",
        "Пауза между циклами, секунды",
        GROUP_RUN,
        INT,
        "300",
        "Круг без паузы — это ддос собственными руками и быстрый путь к капче [CORE-014].",
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
        "Файл или каталог профилей",
        GROUP_RUN,
        TEXT,
        "profile.yaml",
        "Запросы, навыки, порог скоринга и факты о себе. Каталог (например "
        "profiles) — это несколько профилей сразу, по файлу на профиль [ADR-023].",
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
        "PREFILTER_DETAILS_DELTA",
        "На сколько баллов ниже порога ещё качаем карточку",
        GROUP_PREFILTER,
        FLOAT,
        "0",
        "Карточка вакансии — самый дорогой запрос прогона. Значение больше нуля "
        "отсекает тех, кто не дотянет до порога профиля даже с идеальным описанием. "
        "Цена: у таких вакансий не будет ни описания, ни HR-флагов. 0 — выключено, "
        "качаем всем, кто прошёл предфильтр (B-15).",
    ),
    Field(
        "COMPANY_SCORE_ENABLED",
        "Считать общую оценку работодателя",
        GROUP_SCORE,
        BOOL,
        "1",
        "Сводит досье, метку накрутки, метку по деньгам и историю публикаций в один "
        "светофор с уликами (ADR-018). Новых запросов в сеть не делает.",
    ),
    Field(
        "COMPANY_SCORE_IN_SCORE",
        "Учитывать оценку в скоринге вакансий",
        GROUP_SCORE,
        BOOL,
        "0",
        "По умолчанию оценка только показывается в карточке. Включённая — снимает баллы "
        "у вакансий красных работодателей, по уровню прошлого прогона.",
    ),
    Field(
        "COMPANY_SCORE_PENALTY",
        "Сколько баллов снимает красный уровень",
        GROUP_SCORE,
        FLOAT,
        "15",
        "Работает только при включённом учёте в скоринге.",
    ),
    Field(
        "EMBEDDINGS_ENABLED",
        "Считать векторы текстов",
        GROUP_EMBED,
        BOOL,
        "0",
        "Локальный эмбеддер ловит перефразированные отзывы и показывает похожие "
        "вакансии (ADR-021). Модель нельзя менять на живой базе [LLM-011].",
    ),
    Field(
        "EMBEDDINGS_REVIEW_DUP",
        "Отзывы: с какой близости это один текст",
        GROUP_EMBED,
        FLOAT,
        "0.93",
        "Шинглы ловят только дословный повтор. Ниже 0.9 в дубли попадают просто "
        "похожие жалобы, и сигнал начинает врать.",
    ),
    Field(
        "EMBEDDINGS_VACANCY_SIM",
        "Вакансии: с какой близости считать похожими",
        GROUP_EMBED,
        FLOAT,
        "0.80",
        "Порог блока «Похожие вакансии» на странице вакансии. На оценку и отбор "
        "не влияет.",
    ),
    Field(
        "DEEP_ENABLED",
        "Разрешить глубокий ресёрч",
        GROUP_DEEP,
        BOOL,
        "1",
        "Кнопка на странице компании: реестр, суды, долги, банкротство, новости.",
    ),
    Field(
        "DEEP_TIME_BUDGET",
        "Бюджет времени на компанию, секунды",
        GROUP_DEEP,
        INT,
        "300",
        "Единственный потолок: запросы и страницы не ограничены, ресёрч идёт, пока есть время.",
    ),
    Field(
        "DEEP_TTL_DAYS",
        "Сколько дней отчёт считается свежим",
        GROUP_DEEP,
        INT,
        "30",
        "Повторный запуск внутри срока показывает сохранённый отчёт, если не нажать «Собрать заново».",
    ),
    Field(
        "DEEP_IN_SCORE",
        "Учитывать находки в оценке работодателя",
        GROUP_DEEP,
        BOOL,
        "0",
        "По умолчанию находки справочные: привязка страницы к конторе идёт по названию, "
        "и ошибка покрасила бы невиновного.",
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
        "HH_COOKIE",
        "Cookie hh.ru",
        GROUP_SOURCE,
        SECRET,
        "",
        "Заголовок Cookie из браузера. Нужен, когда hh.ru начинает показывать капчу.",
    ),
    Field(
        "HH_PROXY",
        "Прокси для hh.ru",
        GROUP_SOURCE,
        TEXT,
        "",
        "Вида http://user:pass@host:port. Сбор идёт только с RU-IP.",
    ),
    Field(
        "HH_PAUSE",
        "Пауза между запросами, сек",
        GROUP_SOURCE,
        FLOAT,
        "2.0",
        "Верхняя граница и точка возврата: сюда пауза прыгает после капчи.",
    ),
    Field(
        "HH_PAUSE_MIN",
        "Минимальная пауза, сек",
        GROUP_SOURCE,
        FLOAT,
        "0.8",
        "На чистых ответах пауза плавно снижается до этого значения. "
        "Ниже 0.5 — быстрый путь к бану.",
    ),
    Field(
        "HH_STOP_ON_KNOWN",
        "Останавливаться на полностью знакомой странице",
        GROUP_SOURCE,
        BOOL,
        "1",
        "Выдача отсортирована по дате публикации: если вся страница уже в базе и с той "
        "же датой, дальше лежит только более старое. Выключи, если нужен полный обход "
        "выдачи каждый раз (B-15).",
    ),
    Field(
        "HH_SEARCH_CACHE",
        "Помнить страницы выдачи внутри прогона",
        GROUP_SOURCE,
        BOOL,
        "1",
        "Одинаковый запрос у разных профилей скачивается один раз. Кэш живёт только "
        "в памяти и только на время прогона (B-15).",
    ),
    Field(
        "TELEGRAM_BOT_TOKEN",
        "Токен бота",
        GROUP_TELEGRAM,
        SECRET,
        "",
        "От @BotFather. Без него карточки остаются только в интерфейсе.",
    ),
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
    Field(
        "LLM_PROXY_MODEL_FAST",
        "Модель профиля auto:fast",
        GROUP_LLM,
        TEXT,
        "",
        "Этапы extract, ai_text, resume_*. Пусто — на прокси уходит сам алиас auto:fast.",
    ),
    Field(
        "LLM_PROXY_MODEL_SMART",
        "Модель профиля auto:smart",
        GROUP_LLM,
        TEXT,
        "",
        "Этапы hr_filter, score, intake.",
    ),
    Field(
        "LLM_PROXY_MODEL_LONG",
        "Модель профиля auto:long",
        GROUP_LLM,
        TEXT,
        "",
        "Этап company: справка о компании по длинным страницам.",
    ),
    Field(
        "LLM_PROXY_MODEL_LOCAL",
        "Модель профиля local-only",
        GROUP_LLM,
        TEXT,
        "",
        "Этапы с данными о людях: contacts, dossier, draft, review_fake [CORE-012].",
    ),
    Field(
        "LLM_PROXY_MODEL_EMBEDDINGS",
        "Модель векторов",
        GROUP_LLM,
        TEXT,
        "",
        "Например bge-m3.",
    ),
    *(
        Field(
            env_key,
            "Этап {}".format(stage),
            GROUP_LLM_STAGES,
            TEXT,
            "",
            "Пусто — берётся модель профиля {}.".format(llm.STAGE_PROFILES[stage]),
        )
        for stage, env_key in llm.STAGE_MODEL_ENV.items()
    ),
    *(
        Field(
            env_key,
            "Каскад этапа {}".format(stage),
            GROUP_LLM_STAGES,
            TEXT,
            "",
            "До трёх моделей через запятую по порядку бенча. Сильнее одиночной "
            "модели этапа; последним кандидатом всегда идёт локальная модель "
            "[ADR-022].",
        )
        for stage, env_key in llm.STAGE_MODELS_ENV.items()
    ),
    Field(
        "LLM_PERSONAL_VIA_PROXY",
        "Пускать персональные этапы на прокси",
        GROUP_LLM,
        BOOL,
        "0",
        "ВНИМАНИЕ: включённый флаг отправляет ФИО и адреса живых людей за пределы машины [CORE-012].",
    ),
    Field(
        "LLM_TIMEOUT",
        "Таймаут модели, сек",
        GROUP_LLM,
        FLOAT,
        "60",
        "Ответ дольше — вызов неудачный: этап деградирует, прогон идёт дальше [CORE-017].",
    ),
    Field(
        "LLM_MAX_CALLS",
        "Лимит вызовов на прогон",
        GROUP_LLM,
        INT,
        "300",
        "Считается шлюзом: на потолке этапы идут без модели. Страховка от бесконечного цикла.",
    ),
    Field(
        "SEARCH_PROVIDER",
        "Провайдер поиска",
        GROUP_SEARCH,
        TEXT,
        "searxng",
        "searxng работает без ключа, tavily и brave требуют SEARCH_API_KEY.",
    ),
    Field(
        "SEARCH_BASE_URL",
        "Адрес SearXNG",
        GROUP_SEARCH,
        TEXT,
        "",
        "Свой инстанс, например http://127.0.0.1:8888 через туннель. Движки, язык и "
        "период инстанса — на странице «Поиск».",
    ),
    Field("SEARCH_API_KEY", "Ключ провайдера", GROUP_SEARCH, SECRET, "", "Только для tavily и brave."),
    Field("SEARCH_BASIC_AUTH", "Логин:пароль для SearXNG", GROUP_SEARCH, SECRET, "", "Если инстанс закрыт basic-авторизацией."),
    Field(
        "SEARCH_TIMEOUT",
        "Таймаут поиска, сек",
        GROUP_SEARCH,
        FLOAT,
        "20",
        "Медленный инстанс не должен растягивать прогон: запрос считается неудачным.",
    ),
    Field(
        "SEARCH_MAX_CALLS",
        "Лимит запросов на прогон",
        GROUP_SEARCH,
        INT,
        "60",
        "Досье на одну компанию — около десятка запросов. Потолок общий на прогон [CORE-016].",
    ),
    Field(
        "SEARCH_WORKERS",
        "Запросов к поиску одновременно",
        GROUP_SEARCH,
        INT,
        "4",
        "Досье на компанию — это десяток запросов подряд. Максимум 8.",
    ),
    Field(
        "RESEARCH_WORKERS",
        "Компаний изучаем одновременно",
        GROUP_SEARCH,
        INT,
        "4",
        "Досье собираются потоками: сеть ждёт дольше, чем считает процессор. Максимум 8.",
    ),
    Field(
        "REVIEW_FETCH_WORKERS",
        "Страниц отзывов одновременно",
        GROUP_SEARCH,
        INT,
        "4",
        "Площадки разные, ждать их по очереди незачем. Максимум 8.",
    ),
    Field(
        "LLM_WORKERS",
        "Вызовов модели одновременно",
        GROUP_LLM,
        INT,
        "4",
        "Этапы extract и hr_filter идут пулом после сбора. Максимум 8.",
    ),
    Field(
        "DB_PATH",
        "Файл базы",
        GROUP_PATHS,
        TEXT,
        "data/fuckhr.sqlite3",
        "SQLite со всем: вакансии, слепки, досье, оценки, контакты.",
    ),
    Field(
        "LOG_PATH",
        "Файл лога",
        GROUP_PATHS,
        TEXT,
        "data/fuckhr.log",
        "Его же показывает страница запуска.",
    ),
    Field(
        "FAILURE_DIR",
        "Куда класть сырой HTML при сбоях",
        GROUP_PATHS,
        TEXT,
        "data/failures",
        "Хранятся 5 последних: по ним видно, вёрстка hh.ru или капча.",
    ),
    Field(
        "ALERT_COOLDOWN_HOURS",
        "Пауза между тревогами, часов",
        GROUP_PATHS,
        INT,
        "24",
        "Канарейка не повторяет одну тревогу чаще этого срока.",
    ),
)

# Импорт снизу: дополнительные каталоги берут отсюда Field [CORE-024].
from settings_fields_extra import EXTRA_FIELDS, EXTRA_HINTS  # noqa: E402

FIELDS = FIELDS + EXTRA_FIELDS
GROUP_HINTS.update(EXTRA_HINTS)

from settings_fields_install import GROUP_INSTALL, INSTALL_HINT, is_install  # noqa: E402

GROUP_HINTS[GROUP_INSTALL] = INSTALL_HINT
# Пути, потолки и темп запросов ставят один раз при установке: на странице
# настроек они только удлиняют список (docs/ui-map.md).
FIELDS = tuple(f for f in FIELDS if not is_install(f.key)) + tuple(
    replace(f, group=GROUP_INSTALL) for f in FIELDS if is_install(f.key)
)

FIELD_BY_KEY: dict[str, Field] = {field.key: field for field in FIELDS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(field.group for field in FIELDS))
