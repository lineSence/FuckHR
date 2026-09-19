"""Веса, пороги и формулировки общей оценки работодателя. Только данные.

Отделено от логики по [CORE-024] и [CORE-025]: правятся здесь веса, а не
разбор. Числа — первое приближение, на размеченной выборке не проверялись, и
менять их можно только вместе с прогоном тестов [CORE-019].

Смысл весов: 5 — вопрос закрыт (не платят), 1 — вкусовщина. Ось падает не от
числа упоминаний, а от тяжести худшего признака: насыщение в company_score
намеренно душит десятый отзыв про переработки.
"""

from __future__ import annotations

import dossier_rules
import fake_rules
import market_rules

# --- оси ---
PAY = "pay"
TRUTH = "truth"
CHURN = "churn"
CONDITIONS = "conditions"

AXES: tuple[str, ...] = (PAY, TRUTH, CHURN, CONDITIONS)

AXIS_RU = {
    PAY: "деньги",
    TRUTH: "правдивость вакансии",
    CHURN: "текучка",
    CONDITIONS: "условия работы",
}

# --- уровни ---
LEVEL_UNKNOWN = "unknown"
LEVEL_GREEN = "green"
LEVEL_YELLOW = "yellow"
LEVEL_RED = "red"

LEVEL_RU = {
    LEVEL_UNKNOWN: "недостаточно данных",
    LEVEL_GREEN: "претензий не видно",
    LEVEL_YELLOW: "есть к чему придраться",
    LEVEL_RED: "красные флаги",
}

LEVEL_ORDER = (LEVEL_GREEN, LEVEL_YELLOW, LEVEL_RED)

# --- счёт ---
AXIS_CAP = 100.0      # потолок оси
PLUS_CAP = 40.0       # сколько максимум снимают зелёные улики
SATURATION = 6.0      # делитель насыщения: десять отзывов — одна проблема
DECAY_MONTHS = 18.0   # горизонт затухания улики
AXIS_YELLOW = 20.0    # с этого значения ось жёлтая
AXIS_RED = 50.0       # с этого — красная

# --- доверие к источнику ---
TRUST_OWN = 1.0       # слепки, рынок, детектор, поля вакансии
TRUST_REVIEW = 0.8    # отзывы: чужие слова даже на хорошей площадке

# Накрутка не делает компанию плохой напрямую, но обнуляет ценность чужих оценок.
INTEGRITY_TRUST = {
    fake_rules.MARK_NONE: 1.0,
    fake_rules.MARK_SUSPECT: 0.6,
    fake_rules.MARK_FAKE: 0.25,
}

# --- покрытие [HRD-004] ---
MIN_AXES_COVERED = 2  # меньше — «недостаточно данных», а не осторожное «вроде норм»
COVER_TRUST = 0.8     # одна улика такого доверия покрывает ось, иначе нужны две

# --- улики из досье (отзывы) ---
# Ось берётся здесь, вес и полярность — из dossier_rules.PATTERN_RULES, чтобы
# у флага в карточке и у оси был один источник истины.
PATTERN_AXIS = {
    "salary_delay": PAY,
    "grey_salary": PAY,
    "pays_on_time": PAY,
    "overtime": CONDITIONS,
    "micromanagement": CONDITIONS,
    "toxic": CONDITIONS,
    "chaos": CONDITIONS,
    "hr_pressure": CONDITIONS,
    "sane_management": CONDITIONS,
    "tech_culture": CONDITIONS,
    "remote_ok": CONDITIONS,
    "churn": CHURN,
    "fake_vacancy": TRUTH,
}

PATTERN_META = {
    code: (label, polarity, weight)
    for code, label, polarity, weight, _needles in dossier_rules.PATTERN_RULES
}

# --- улики из наших наблюдений ---
# код → (ось, вес, шаблон формулировки)
OWN_SIGNALS: dict[str, tuple[str, int, str]] = {
    "republished": (CHURN, 3, "вакансий публиковали заново: {count} из {total}"),
    "long_running": (CHURN, 3, "вакансий висят в выдаче {days}+ дн.: {count}"),
    "no_salary": (PAY, 2, "без вилки: {count} из {total} вакансий"),
    "salary_moved": (TRUTH, 2, "вилка менялась между слепками: {count}"),
    "ai_text": (TRUTH, 1, "описаний похожи на шаблон или генерацию: {count}"),
}

# Метка по деньгам: коды признаков market_company. Вес — оттуда же.
MARKET_AXIS = {"below": PAY, "hidden": PAY, "above": TRUTH}
MARKET_WEIGHT = market_rules.SIGN_WEIGHT

# Флаги детектора: не подтверждённое историей утверждение вакансии.
# long_running детектора здесь нет сознательно: его же считает company_signals,
# и улика ушла бы в ось дважды.
DETECTOR_AXIS: dict[str, tuple[str, int, str]] = {
    "stable_team": (CHURN, 4, "«стабильная команда» не подтверждается историей"),
    "no_overtime": (CONDITIONS, 3, "«без переработок» противоречит тексту вакансии"),
    "white_salary": (PAY, 4, "«белая зарплата» не подтверждается вилкой"),
    "fast_growth": (CONDITIONS, 1, "«быстрый рост» ничем не подтверждён"),
    "salary_band_wide": (PAY, 2, "вилка настолько широкая, что её фактически нет"),
    "grade_mismatch": (TRUTH, 2, "грейд не совпадает с требованиями"),
}

# Накрутка отзывов — флаг поведения компании, а не условий труда.
FAKE_CODE = "fake_reviews"
FAKE_WEIGHT = fake_rules.MARK_FLAG_WEIGHT

# --- закономерности ---
# Комбинации видны только вместе: по отдельности каждый код уже учтён, поэтому
# вес комбинации — надбавка за совпадение, а не полная стоимость признаков.
COMBOS: tuple[tuple[tuple[str, ...], str, int, str], ...] = (
    (
        ("long_running", "republished", "churn"),
        CHURN,
        5,
        "дыра в команде: вакансия не закрывается, перевыкладывается и в отзывах текучка",
    ),
    (
        ("hidden", "below"),
        PAY,
        4,
        "цифру называют на финале, и она ниже рынка",
    ),
    (
        (FAKE_CODE, "site_gap"),
        TRUTH,
        4,
        "репутацией управляют: накрутка и расхождение площадок",
    ),
    (
        ("stable_team", "republished"),
        TRUTH,
        4,
        "вакансия обещает стабильную команду и сама же перевыкладывается",
    ),
)

# --- вето ---
# Плюсы не компенсируют невыплату зарплаты: потолок ставится независимо от осей.
VETO_RED = "salary_delay"
VETO_RED_HITS = 2
VETO_YELLOW = ("grey_salary", FAKE_CODE)

VETO_RU = {
    VETO_RED: "подтверждённые задержки зарплаты",
    "grey_salary": "серая зарплата или оформление",
    FAKE_CODE: "часть отзывов похожа на заказные",
}

__all__ = (
    "AXES",
    "AXIS_CAP",
    "AXIS_RED",
    "AXIS_RU",
    "AXIS_YELLOW",
    "CHURN",
    "COMBOS",
    "CONDITIONS",
    "COVER_TRUST",
    "DECAY_MONTHS",
    "DETECTOR_AXIS",
    "FAKE_CODE",
    "FAKE_WEIGHT",
    "INTEGRITY_TRUST",
    "LEVEL_GREEN",
    "LEVEL_ORDER",
    "LEVEL_RED",
    "LEVEL_RU",
    "LEVEL_UNKNOWN",
    "LEVEL_YELLOW",
    "MARKET_AXIS",
    "MARKET_WEIGHT",
    "MIN_AXES_COVERED",
    "OWN_SIGNALS",
    "PATTERN_AXIS",
    "PATTERN_META",
    "PAY",
    "PLUS_CAP",
    "SATURATION",
    "TRUST_OWN",
    "TRUST_REVIEW",
    "TRUTH",
    "VETO_RED",
    "VETO_RED_HITS",
    "VETO_RU",
    "VETO_YELLOW",
)
