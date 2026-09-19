"""Словари, классы и пороги рыночной статистики. Только данные.

Отделено от логики по [CORE-024] и [CORE-025]: словарь ролей будет расти
быстрее всего остального, а пороги меняются после каждой сверки с публичными
обзорами.

Числа — первое приближение из проектного документа, на живой выборке не
проверялись [CORE-019]. Сверять руками и ставить дату сверки.
"""

from __future__ import annotations

# Ставка НДФЛ. Огрубление сознательное: с 2025 года шкала прогрессивная
# (13/15/18/20/22%), и на вилках от 300 000 эта единая ставка завышает net.
# Поэтому в market_observations лежат сырые числа и флаг gross: когда дойдут
# руки до шкалы, пересчёт пойдёт по ним, без пересбора выдачи.
NDFL = 0.13
NET_RATE = 1.0 - NDFL

RUB = ("RUR", "RUB")

# --- роли ---
# Семейство → слова в заголовке. Порядок важен: сначала узкие роли, потом
# широкие, иначе «аналитик данных» съест «системный аналитик».
ROLE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ml", ("ml ", "machine learning", "машинн", "нейросет", "computer vision", "nlp")),
    ("data_scientist", ("data scientist", "дата саентист", "исследователь данных")),
    ("data_engineer", ("data engineer", "дата инженер", "инженер данных", "etl")),
    ("data_analyst", ("аналитик данных", "data analyst", "bi аналитик", "продуктовый аналитик")),
    ("system_analyst", ("системный аналитик", "бизнес аналитик", "system analyst")),
    ("devops", ("devops", "sre", "инфраструктур", "платформенный инженер")),
    ("qa", ("qa", "тестировщик", "автотест", "test engineer")),
    ("android", ("android", "андроид", "kotlin разработчик")),
    ("ios", ("ios", "swift разработчик")),
    ("frontend", ("frontend", "фронтенд", "react разработчик", "vue разработчик")),
    ("fullstack", ("fullstack", "фулстек", "full stack")),
    ("python", ("python", "питон", "django", "fastapi")),
    ("golang", ("golang", " go ", "го разработчик")),
    ("java", ("java", "джава", "kotlin backend")),
    ("php", ("php", "laravel", "symfony")),
    ("dotnet", (".net", "c#", "шарп")),
    ("backend", ("backend", "бэкенд", "бекенд", "серверн")),
    ("security", ("информационной безопасност", "пентест", "appsec", "security")),
    ("product", ("продакт", "product manager", "продуктовый менеджер")),
    ("project", ("проджект", "project manager", "руководитель проект")),
)

ROLE_RU = {
    "ml": "ML-инженер",
    "data_scientist": "data scientist",
    "data_engineer": "дата-инженер",
    "data_analyst": "аналитик данных",
    "system_analyst": "системный аналитик",
    "devops": "devops",
    "qa": "тестировщик",
    "android": "android",
    "ios": "ios",
    "frontend": "frontend",
    "fullstack": "fullstack",
    "python": "python",
    "golang": "golang",
    "java": "java",
    "php": "php",
    "dotnet": ".net",
    "backend": "backend",
    "security": "безопасность",
    "product": "продакт",
    "project": "проджект",
}

# --- грейды ---
# Слово в заголовке сильнее поля experience: «senior» в названии написал
# работодатель, а «between3And6» — это фильтр hh.
GRADE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lead", ("lead", "лид", "тимлид", "teamlead", "head", "руководитель группы", "cto")),
    ("senior", ("senior", "сеньор", "ведущий", "старший")),
    ("middle", ("middle", "миддл", "мидл")),
    ("junior", ("junior", "джун", "стажёр", "стажер", "начинающий")),
)

EXPERIENCE_GRADE = {
    "noExperience": "junior",
    "between1And3": "junior",
    "between3And6": "middle",
    "moreThan6": "senior",
}

GRADE_RU = {
    "junior": "junior",
    "middle": "middle",
    "senior": "senior",
    "lead": "lead",
    "": "грейд не определён",
}

# --- гео ---
MOSCOW = "msk"
SPB = "spb"
MILLION = "million"
OTHER = "other"

GEO_CITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MOSCOW, ("москва", "московская область", "зеленоград")),
    (SPB, ("санкт-петербург", "петербург", "ленинградская область")),
    (
        MILLION,
        (
            "новосибирск", "екатеринбург", "казань", "нижний новгород", "челябинск",
            "самара", "уфа", "ростов-на-дону", "омск", "красноярск", "воронеж",
            "пермь", "волгоград", "краснодар",
        ),
    ),
)

GEO_RU = {
    MOSCOW: "Москва",
    SPB: "Санкт-Петербург",
    MILLION: "город-миллионник",
    OTHER: "прочие регионы",
    "": "гео любое",
}

REMOTE = "remote"
ONSITE = "onsite"
FORMAT_RU = {REMOTE: "удалёнка", ONSITE: "офис или гибрид", "": "формат любой"}

# --- каскад срезов ---
# Точный срез на наших объёмах часто пуст, поэтому при нехватке наблюдений
# снимается по одному измерению. Уровень хранится и показывается: медиана по
# роли без учёта региона — вывод другого качества.
CASCADE: tuple[tuple[str, ...], ...] = (
    ("role", "grade", "geo", "format"),
    ("role", "grade", "geo"),
    ("role", "grade"),
    ("role",),
)

LEVEL_RU = (
    "роль, грейд, гео, формат",
    "роль, грейд, гео",
    "роль и грейд",
    "только роль",
)

# --- пороги ---
WINDOW_DAYS = 180          # окно наблюдений
MIN_OBSERVATIONS = 8       # меньше — «рынок неизвестен», меток нет
COMPANY_CAP = 0.2          # потолок доли одной компании в срезе
MIN_COMPANY_VACANCIES = 3  # меньше — о работодателе выводов не делаем
DEVIATION = 0.15           # |медиана отклонений| для метки работодателя
SHARE = 0.5                # доля вакансий компании с отклонением
HIDDEN_SHARE = 0.7         # доля вакансий без вилки

# --- метки вакансии ---
BELOW = "below"
IN_MARKET = "in_market"
ABOVE = "above"
NO_SALARY = "no_salary"
UNKNOWN = "unknown"

LABEL_RU = {
    BELOW: "ниже рынка",
    IN_MARKET: "в рынке",
    ABOVE: "выше рынка",
    NO_SALARY: "зарплата не указана",
    UNKNOWN: "рынок неизвестен",
}

# Доля веса `market` в скоринге. Неизвестность не штрафует: она получает
# столько же, сколько «в рынке», иначе вакансии без вилки уезжали бы вниз
# дважды — и по весу salary, и здесь.
LABEL_POINTS = {
    ABOVE: 1.0,
    IN_MARKET: 0.7,
    UNKNOWN: 0.7,
    NO_SALARY: 0.7,
    BELOW: 0.2,
}

# --- метка работодателя ---
MARK_NONE = "none"
MARK_WATCH = "watch"
MARK_SET = "mark"

MARK_RU = {
    MARK_NONE: "нет данных о зарплатах",
    MARK_WATCH: "наблюдение по зарплатам",
    MARK_SET: "метка по зарплатам",
}

SIGN_RU = {
    "below": "платит ниже рынка",
    "above": "платит выше рынка",
    "hidden": "прячет деньги: вилки нет",
}

# Вес жёлтого флага. Красный за зарплату не ставится: низкая зарплата — не
# обман, обман — это расхождение обещаний с фактами.
SIGN_WEIGHT = {"below": 3, "hidden": 3, "above": 2}
