"""Словарь маршрутизации: профили, этапы и имена переменных окружения.

Отдельный файл, потому что это правила, а не логика: llm.py перешёл 25 КБ
[CORE-024]. Публичные имена остаются доступными через `llm`, вызовы и тесты
переписывать не нужно.
"""

from __future__ import annotations

# Профили шлюза вместо model="auto" (wiki/architecture/model-routing.md).
FAST = "auto:fast"
SMART = "auto:smart"
LONG = "auto:long"
LOCAL = "local-only"
EMBEDDINGS = "embeddings"

# Этап пайплайна -> профиль. Новый этап обязан объявить свой профиль здесь:
# молчаливого дефолта нет специально, иначе данные о людях когда-нибудь
# утекут в облако через «забыли добавить этап».
#
# resume_section и resume_tailor работают с резюме владельца — это тоже
# персональные данные, но его собственные, а не третьих лиц. Своё резюме
# владелец и так отдаёт работодателям, поэтому в PERSONAL_STAGES эти этапы не
# внесены: запрет облака здесь защищал бы его от самого себя. FAST — потому что
# задачи узкие (перефразировать ответ, выбрать номера блоков), а вызовов много:
# версия собирается на каждую прошедшую скоринг вакансию [CORE-016].
STAGE_PROFILES: dict[str, str] = {
    # intake — разговор о поиске работы. Данные владельца о себе, как и в
    # resume_*, поэтому в PERSONAL_STAGES этап не внесён: запрет облака здесь
    # защищал бы владельца от него самого. SMART — нужно понять свободный текст
    # и не выдумать лишнего, вызов один на реплику.
    "intake": SMART,
    "extract": FAST,
    "hr_filter": SMART,
    "company": LONG,
    "score": SMART,
    "contacts": LOCAL,
    "dossier": LOCAL,
    # review_fake — тексты чужих отзывов, где встречаются имена сотрудников.
    "review_fake": LOCAL,
    # ai_text — «текст похож на сгенерированный». Решением владельца этап не
    # local-only и не в PERSONAL_STAGES: описания вакансий публичны, а для
    # отзывов это осознанное отступление (docs/ai-text.md).
    "ai_text": FAST,
    "draft": LOCAL,
    "resume_section": FAST,
    "resume_tailor": FAST,
    "embeddings": EMBEDDINGS,
}

# Этапы, где в промпте есть данные о конкретных людях.
PERSONAL_STAGES = frozenset({"contacts", "dossier", "draft", "review_fake"})

# Этапы, которые предпочитают локальный адрес, даже когда прокси настроен.
# Пока такой один — эмбеддинги. Причины две. Первая: модель векторов пиннится
# на всю жизнь базы [LLM-011], а состав моделей на чужом прокси владелец не
# контролирует; локальный Ollama меняется только его руками. Вторая: на прокси
# уходит имя вроде `bge-m3:latest`, и если такого алиаса в config.yaml нет,
# приходит 400. Прокси остаётся запасным вариантом: если локального адреса нет,
# маршрут всё равно будет найден.
LOCAL_FIRST_STAGES = frozenset({"embeddings"})

# Имена маршрутов — то, что видно в логах и в интерфейсе.
ROUTE_LOCAL = "local"
ROUTE_PROXY = "proxy"

# Сколько кандидатов держит каскад этапа. Три — решение владельца
# (20.09.2026): в худшем дне это три реальных похода в сеть на вакансию,
# четвёртый уже дороже пропуска этапа [CORE-016], [CORE-017].
MAX_CANDIDATES = 3

# Переменная окружения с именем модели на прокси для каждого профиля.
PROXY_MODEL_ENV = {
    FAST: "LLM_PROXY_MODEL_FAST",
    SMART: "LLM_PROXY_MODEL_SMART",
    LONG: "LLM_PROXY_MODEL_LONG",
    LOCAL: "LLM_PROXY_MODEL_LOCAL",
    EMBEDDINGS: "LLM_PROXY_MODEL_EMBEDDINGS",
}

# То же самое, но для локального адреса. Пустая переменная — в запрос уйдёт
# само название профиля, как было раньше: когда на локальном сервере одна
# модель, настраивать нечего. Но у Ollama владельца их несколько (qwen3:8b,
# qwen3:4b, bge-m3), и на model="auto:fast" она отвечает «model not found».
# Профиль здесь — тот же вопрос «какого класса модель нужна», только ответ
# ищется среди установленных локально: список живых имён показывает страница
# «Модель» кнопкой «Спросить список моделей» (она же `ollama list`).
LOCAL_MODEL_ENV = {
    FAST: "LLM_LOCAL_MODEL_FAST",
    SMART: "LLM_LOCAL_MODEL_SMART",
    LONG: "LLM_LOCAL_MODEL_LONG",
    LOCAL: "LLM_LOCAL_MODEL_LOCAL",
    EMBEDDINGS: "LLM_LOCAL_MODEL_EMBEDDINGS",
}

# Переменная окружения с именем модели на прокси для отдельного этапа. Пустая
# — этап берёт модель своего профиля. Профиль отвечает на вопрос «какого класса
# модель нужна», но у одинаковых по классу этапов модели разъезжаются: бенчмарк
# регулярно показывает, что на extract выигрывает одна модель, а на draft — другая.
STAGE_MODEL_ENV = {
    stage: "LLM_STAGE_MODEL_{}".format(stage.upper()) for stage in STAGE_PROFILES
}

# То же для локального адреса и сильнее имени профиля. Разъезд здесь даже
# заметнее, чем на прокси: на 8 ГБ VRAM извлечению полей хватает qwen3:4b, а
# черновику письма нужна qwen3:8b, и держать оба этапа на одной модели значит
# либо ждать лишнее, либо получать мусор (wiki/references/local-models.md).
LOCAL_STAGE_MODEL_ENV = {
    stage: "LLM_LOCAL_STAGE_MODEL_{}".format(stage.upper()) for stage in STAGE_PROFILES
}

# Переменная окружения с каскадом кандидатов на этап: до трёх имён через
# запятую в порядке бенча (`bench.cascades`). Сильнее одиночного
# STAGE_MODEL_ENV, пустая — этап работает одной моделью, как раньше.
STAGE_MODELS_ENV = {
    stage: "LLM_STAGE_MODELS_{}".format(stage.upper()) for stage in STAGE_PROFILES
}

class ProfileError(RuntimeError):
    """Неизвестный этап или попытка увести ПД из local-only."""


def profile_for(stage: str) -> str:
    try:
        profile = STAGE_PROFILES[stage]
    except KeyError as exc:
        raise ProfileError(
            f"этап {stage!r} не объявил профиль в STAGE_PROFILES"
        ) from exc
    if stage in PERSONAL_STAGES and profile != LOCAL:
        raise ProfileError(
            f"этап {stage!r} работает с персональными данными и требует {LOCAL}"
        )
    return profile
