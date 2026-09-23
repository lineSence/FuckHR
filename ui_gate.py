"""Страница «Гейт отзывов»: разметка, поправки владельца, обучение гейта.

Отдельный раздел, а не блок на странице «Модель»: там речь про генеративные
модели и шлюз, а здесь свой цикл, который идёт неделями. Кнопка «обучить» без
ответа на вопрос «а сколько разметки уже есть» бесполезна, поэтому счётчики,
поправки и кнопка живут на одном экране.

Главное, чего нет в консоли: поправка владельца одной кнопкой. Учитель
ошибается, а его ошибку видно только в тексте отзыва — значит текст надо
показать и рядом поставить «верно» и «ошибка». Поправка весит больше вердикта
учителя и целиком уходит в отложенную часть.

Обучение отсюда запускается: линейная модель на готовых векторах учится
секунды, видеокарта не нужна (`review_gate_train.py`).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import jobs
import judge_labels
import review_gate
import review_gate_store
import review_gate_train
import settings
from ui_core import details, esc, table

ROOT = Path(__file__).resolve().parent
REPORT_PATH = ROOT / "data" / "bench" / "review_gate.json"

STAGE_RU = {
    "review_fake": "Заказные отзывы",
    "ai_text": "Текст написан нейросетью",
}
# Настройка этапа → подпись. Оба этапа выключены по умолчанию: они стоят
# вызова модели на отзыв, поэтому платить за них решает владелец.
STAGE_ENV = {
    "review_fake": ("FAKE_REVIEW_LLM", "Спрашивать модель про заказные отзывы"),
    "ai_text": ("AI_TEXT_LLM", "Спрашивать модель про ИИ-текст"),
}
SHOW = 10                 # сколько текстов показывать на проверку за раз
CUT = 400                 # столько символов отзыва видно без разворачивания


def counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Сколько меток накопилось: учитель, поправки, компании."""
    judge_labels.ensure_schema(conn)
    out: dict[str, dict[str, int]] = {}
    for stage in review_gate.STAGES:
        row = conn.execute(
            """
            SELECT
                SUM(source = 'llm'   AND verdict = 1) AS llm_yes,
                SUM(source = 'llm'   AND verdict = 0) AS llm_no,
                SUM(source = 'owner' AND verdict = 1) AS own_yes,
                SUM(source = 'owner' AND verdict = 0) AS own_no,
                COUNT(DISTINCT company)               AS companies
            FROM judge_labels WHERE stage = ?
            """,
            (stage,),
        ).fetchone()
        keys = ("llm_yes", "llm_no", "own_yes", "own_no", "companies")
        out[stage] = {key: int(value or 0) for key, value in zip(keys, row)}
    return out


def unchecked(conn: sqlite3.Connection, limit: int = SHOW) -> list[sqlite3.Row]:
    """Вердикты учителя, которых владелец ещё не подтверждал.

    Сначала «да»: их меньше, и именно на них учитель ошибается заметнее всего.
    """
    judge_labels.ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            """
            SELECT t.* FROM judge_labels t
            WHERE t.source = 'llm' AND NOT EXISTS (
                SELECT 1 FROM judge_labels o
                WHERE o.source = 'owner' AND o.stage = t.stage
                  AND o.text_hash = t.text_hash
            )
            ORDER BY t.verdict DESC, t.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    )


def load_report(path: Path = REPORT_PATH) -> list[dict] | None:
    """Отчёт последнего обучения: то же, что печатает review_gate_train."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def render_counts(data: dict[str, dict[str, int]]) -> str:
    rows = []
    for stage, c in data.items():
        rows.append([
            esc(STAGE_RU.get(stage, stage)),
            "{} да / {} нет".format(c["llm_yes"], c["llm_no"]),
            "{} да / {} нет".format(c["own_yes"], c["own_no"]),
            str(c["companies"]),
            enough(c),
        ])
    return table(["Этап", "Учитель", "Поправки владельца", "Компаний", "Хватит ли"], rows)


def enough(c: dict[str, int]) -> str:
    """Дообучение упирается в положительные примеры: их всегда меньше."""
    yes = c["llm_yes"] + c["own_yes"]
    if yes >= review_gate_train.FEW:
        return '<span class=ok>да, {} положительных</span>'.format(yes)
    return '<span class=warn>ещё {} положительных</span>'.format(review_gate_train.FEW - yes)


def render_stages_form() -> str:
    boxes = []
    for stage, (key, label) in STAGE_ENV.items():
        boxes.append(
            '<label><input type=checkbox name="{key}" value="1"{on}> {label}</label>'.format(
                key=esc(key), label=esc(label), on=" checked" if settings.flag(key) else ""
            )
        )
    return (
        '<form method=post action="/gate/stages">{boxes} <button>Сохранить</button></form>'
        "<p class=muted>Метки копятся сами: этапы спрашивают модель при сборе "
        "вакансий и на шаге по цели, а ответ вместе с полным текстом отзыва "
        "ложится в базу. Каждый отзыв — один вызов модели, поэтому этапы "
        "выключены по умолчанию.</p>"
    ).format(boxes=" ".join(boxes))


def render_check(rows: list[sqlite3.Row]) -> str:
    """Поправки: текст, вердикт учителя и две кнопки."""
    if not rows:
        return (
            "<p class=muted>Непроверенных вердиктов нет. Они появятся после "
            "прогона с включёнными этапами выше.</p>"
        )
    cards = []
    for row in rows:
        text = row["text"] or ""
        body = esc(text[:CUT]) + ("…" if len(text) > CUT else "")
        verdict = "да" if row["verdict"] else "нет"
        cards.append(
            '<div class=panel><p><b>{stage}</b> · учитель сказал «{verdict}»'
            ' · <span class=muted>{company}</span></p><p>{body}</p>'
            '<form method=post action="/gate/label">'
            '<input type=hidden name=stage value="{stage_id}">'
            '<input type=hidden name=hash value="{hash}">'
            '<button name=verdict value="same">Верно</button> '
            '<button name=verdict value="flip">Ошибка</button></form></div>'.format(
                stage=esc(STAGE_RU.get(row["stage"], row["stage"])),
                stage_id=esc(row["stage"]),
                verdict=verdict,
                company=esc(row["company"] or "без компании"),
                body=body,
                hash=esc(row["text_hash"]),
            )
        )
    return "".join(cards)


def render_report(report: list[dict]) -> str:
    """Замер последнего обучения: по нему и решается, включать ли гейт."""
    rows = []
    for part in report:
        rows.append([
            esc(STAGE_RU.get(part.get("этап", ""), part.get("этап", ""))),
            str(part.get("обучающих", 0)),
            "{} из {}".format(part.get("положительных", 0), part.get("отложенных", 0)),
            score_cell(part.get("auroc")),
            str(part.get("точность", "—")),
            "{} / {}".format(part.get("high", "—"), part.get("low", "—")),
        ])
    notes = [
        "<p class=warn>{}: {}</p>".format(
            esc(STAGE_RU.get(part.get("этап", ""), part.get("этап", ""))), esc(part["итог"])
        )
        for part in report
        if part.get("итог")
    ]
    return (
        table(
            ["Этап", "Обучающих", "Положительных", "AUROC", "Точность", "Порог да / нет"],
            rows,
        )
        + "".join(notes)
        + "<p class=muted>Пороги подобраны по отложенной части: «да» — самый "
        "низкий порог без ложных срабатываний, «нет» — самый высокий порог, "
        "ниже которого не осталось ни одного положительного. Их и стоит "
        "поставить в настройках.</p>"
    )


def score_cell(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "<span class=muted>—</span>"
    cls = "ok" if value >= 0.7 else "danger"
    return '<span class="{}">{:.3f}</span>'.format(cls, value)


def render_models(conn: sqlite3.Connection) -> str:
    """Что лежит в базе сейчас — гейт берёт веса именно оттуда."""
    models = review_gate_store.all_models(conn)
    if not models:
        return (
            "<p class=muted>Весов в базе нет: гейт молчит, оба этапа работают "
            "как раньше.</p>"
        )
    rows = [
        [
            esc(STAGE_RU.get(model.stage, model.stage)),
            esc(model.model),
            str(model.rows),
            score_cell(model.auroc),
            "{} / {}".format(model.high, model.low),
            esc(model.trained_at[:16].replace("T", " ")),
        ]
        for model in models
    ]
    return table(
        ["Этап", "Модель векторов", "Обучающих", "AUROC", "Порог да / нет", "Обучен"], rows
    )


HOWTO = (
    "<ol>"
    "<li>Векторы считает эмбеддер (настройка «Модель эмбеддингов», обычно "
    "<code>bge-m3</code>): без него обучать не на чем.</li>"
    "<li>Кнопка выше или <code>python review_gate_train.py</code> — минуты на "
    "тысяче отзывов, видеокарта не нужна. Веса ложатся в базу, рядом с "
    "векторами, из которых посчитаны.</li>"
    "<li>Пороги из замера поставить в настройках, затем включить «Гейт этапов "
    "отзывов». Веса считаются отдельно для каждой модели векторов: сменили "
    "эмбеддер — обучите заново.</li>"
    "</ol>"
    "<p class=muted>Подробно — <code>docs/review-gate.md</code>. Там же "
    "замер, по которому от дообученной Laya отказались: те же цифры ценой "
    "второго энкодера в памяти.</p>"
)


def render_gate(conn: sqlite3.Connection, note: str = "") -> str:
    data = counts(conn)
    parts = [
        "<p class=muted>Два этапа отзывов стоят вызова модели на каждый текст. "
        "Гейт — логистическая регрессия на векторах, которые и так считаются: "
        "уверенное «да» и уверенное «нет» принимаются без модели, середина "
        "уходит в шлюз, как раньше. Путь один: накопить разметку, обучить, "
        "поставить пороги из замера и только потом включать.</p>",
        note,
        "<h2>1. Разметка</h2>",
        render_counts(data),
        render_stages_form(),
        "<h2>2. Поправки владельца</h2>",
        "<p class=muted>Вердикт учителя — это ответ обычной модели, он бывает "
        "неверным. Проверенные тексты весят больше и идут в отложенную "
        "выборку: мерить надо на правде.</p>",
        render_check(unchecked(conn)),
        "<h2>3. Обучение гейта</h2>",
        '<form method=post action="/gate/train"><button>Обучить гейт на '
        "разметке</button></form>",
        "<p class=muted>Учится на разметке из базы: деление отложенной части "
        "по компаниям, векторы из общего кэша. Ни одной генерации; вызовы "
        "эмбеддера — только на тексты без вектора.</p>",
        render_models(conn),
    ]
    report = load_report()
    if report:
        parts.append("<h2>4. Последний замер</h2>")
        parts.append(render_report(report))
    parts.append(details("Как включить", "три шага", HOWTO))
    return "".join(parts)


def save_stages(form: dict) -> str:
    keys = [key for key, _ in STAGE_ENV.values()]
    saved = settings.save({key: "1" if form.get(key) else "0" for key in keys})
    return "<div class=ok>Сохранено: {}</div>".format(esc(", ".join(saved) or "без изменений"))


def save_label(conn: sqlite3.Connection, form: dict) -> str:
    """Поправка владельца по одному тексту. Текст берётся из базы, не из формы."""
    stage = (form.get("stage") or [""])[0]
    digest = (form.get("hash") or [""])[0]
    flip = (form.get("verdict") or [""])[0] == "flip"
    judge_labels.ensure_schema(conn)
    row = conn.execute(
        "SELECT text, verdict, company FROM judge_labels"
        " WHERE stage = ? AND text_hash = ? AND source = 'llm'",
        (stage, digest),
    ).fetchone()
    if row is None:
        return "<div class=warn>Такой метки уже нет.</div>"
    text, verdict, company = row[0], bool(row[1]), row[2]
    judge_labels.record(conn, stage, {text: (not verdict) if flip else verdict}, company, "owner")
    return "<div class=ok>Записано: «{}».</div>".format(
        "да" if ((not verdict) if flip else verdict) else "нет"
    )


def post(path: str, form: dict) -> tuple[int | None, str]:
    """Обработка форм раздела: (номер задачи, страница).

    Номер задачи — значит уходим на страницу запуска с логом, как сборка
    датасета на странице «Модель». Иначе показываем свою страницу с отметкой
    о записи: настройка и поправка выполняются мгновенно, уводить некуда.
    """
    note = ""
    if path == "/gate/train":
        job_id, note = start_train()
        if job_id is not None:
            return job_id, ""
    from ui_core import open_db  # noqa: PLC0415 — соединение на запрос, как у остальных форм

    conn = open_db()
    try:
        if path == "/gate/stages":
            note = save_stages(form)
        elif path == "/gate/label":
            note = save_label(conn, form)
        return None, render_gate(conn, note)
    finally:
        conn.close()


def start(task: str, extra: tuple[str, ...] = ()) -> tuple[int | None, str]:
    try:
        job = jobs.runner.start(task, list(extra))
    except (KeyError, RuntimeError) as exc:
        return None, "<div class=warn>{}</div>".format(esc(exc))
    return job.id, ""


def start_train() -> tuple[int | None, str]:
    """Аргументов из браузера нет: команда целиком из jobs.TASKS [CORE-023]."""
    return start("gate-train")


__all__ = (
    "counts",
    "load_report",
    "post",
    "render_gate",
    "render_models",
    "save_label",
    "save_stages",
    "start_train",
    "unchecked",
)
