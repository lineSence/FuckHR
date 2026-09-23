"""Страница «Laya»: разметка, поправки владельца, датасет и дообучение.

Отдельный раздел, а не блок на странице «Модель»: там речь про генеративные
модели и шлюз, а здесь — свой цикл из четырёх шагов, который идёт неделями.
Кнопка «собрать датасет» без ответа на вопрос «а сколько разметки уже есть»
бесполезна, поэтому счётчики и кнопки живут на одном экране.

Главное, чего нет в консоли: поправка владельца одной кнопкой. Учитель
ошибается, а его ошибку видно только в тексте отзыва — значит текст надо
показать и рядом поставить «верно» и «ошибка». Поправка весит больше вердикта
учителя и уходит в отложенную выборку (laya_dataset.py).

Обучение отсюда не запускается: без видеокарты оно идёт сутками, а в
песочнице падает по памяти. Здесь только готовая команда и разбор eval.json.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import jobs
import judge_labels
import laya_dataset
import settings
from ui_core import details, esc, table

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "train" / "laya"

STAGE_RU = {
    "review_fake": "Заказные отзывы",
    "ai_text": "Текст написан нейросетью",
}
# Настройка этапа → подпись. Оба этапа выключены по умолчанию: они стоят
# вызова модели на отзыв, а до дообучения Laya платить за это незачем.
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
    for stage in laya_dataset.STAGES:
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


def load_report(path: Path = OUT_DIR / "report.json") -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def model_eval() -> tuple[str, dict | None]:
    """Папка дообученной модели из LAYA_MODEL и её eval.json, если есть."""
    name = settings.get("LAYA_MODEL", "") or ""
    path = Path(name)
    if not path.is_absolute():
        path = ROOT / name
    if not name or not path.is_dir():
        return name, None
    try:
        return name, json.loads((path / "eval.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return name, None


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
    if yes >= laya_dataset.FEW:
        return '<span class=ok>да, {} положительных</span>'.format(yes)
    return '<span class=warn>ещё {} положительных</span>'.format(laya_dataset.FEW - yes)


def render_stages_form() -> str:
    boxes = []
    for stage, (key, label) in STAGE_ENV.items():
        boxes.append(
            '<label><input type=checkbox name="{key}" value="1"{on}> {label}</label>'.format(
                key=esc(key), label=esc(label), on=" checked" if settings.flag(key) else ""
            )
        )
    return (
        '<form method=post action="/laya/stages">{boxes} <button>Сохранить</button></form>'
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
            '<form method=post action="/laya/label">'
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


def render_report(report: dict) -> str:
    rows = []
    for stage, parts in sorted(report.items()):
        for split in ("train", "test"):
            part = parts.get(split) or {}
            if not part:
                continue
            rows.append([
                esc(STAGE_RU.get(stage, stage)),
                "обучающая" if split == "train" else "отложенная",
                str(part.get("да", 0)),
                str(part.get("нет", 0)),
                str(part.get("owner", 0)),
            ])
    return table(["Этап", "Часть", "Да", "Нет", "От владельца"], rows)


def render_eval(name: str, data: dict) -> str:
    rows = []
    for stage, part in sorted(data.items()):
        rows.append([
            esc(STAGE_RU.get(stage, stage)),
            str(part.get("вопросов", 0)),
            "{:.2f}".format(part.get("точность", 0.0)),
            order_cell(part.get("auroc_прямой")),
            order_cell(part.get("auroc_перевёрнутый")),
        ])
    return (
        "<p>Замер модели <code>{}</code>:</p>".format(esc(name))
        + table(["Этап", "Вопросов", "Точность", "AUROC прямой", "AUROC перевёрнутый"], rows)
        + "<p class=muted>Включать можно, только если оба AUROC не ниже 0.7. "
        "Высокий прямой при низком перевёрнутом — модель отвечает по позиции "
        "варианта, а не по смыслу.</p>"
    )


def order_cell(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "<span class=muted>—</span>"
    cls = "ok" if value >= 0.7 else "danger"
    return '<span class="{}">{:.2f}</span>'.format(cls, value)


HOWTO = (
    "<ol>"
    "<li>Файлы из <code>{out}</code> — приватным датасетом на Kaggle или Colab "
    "(в отзывах бывают имена сотрудников: публиковать их нельзя).</li>"
    "<li>Одна карта T4: в Colab открыть <code>training/laya_colab.ipynb</code> "
    "и пройти его сверху вниз, либо вручную <code>pip install laya</code> и "
    "<code>python training/laya_finetune.py --data data/train/laya "
    "--out models/laya-fuckhr</code> — около часа.</li>"
    "<li>Папку модели положить рядом с проектом и указать её в настройке "
    "«Чекпойнт решателя» — замер появится здесь.</li>"
    "</ol>"
    "<p class=muted>Подробно — <code>docs/laya-finetune.md</code>. Обучение "
    "отсюда не запускается: без видеокарты оно не проходит.</p>"
)


def render_laya(conn: sqlite3.Connection, note: str = "") -> str:
    data = counts(conn)
    parts = [
        "<p class=muted>Laya отвечает «да/нет» одним проходом энкодера вместо "
        "генерации. Без дообучения она отвечает по позиции варианта, поэтому "
        "путь один: накопить разметку на своих отзывах, собрать датасет, "
        "дообучить и только потом включать.</p>",
        note,
        "<h2>1. Разметка</h2>",
        render_counts(data),
        render_stages_form(),
        "<h2>2. Поправки владельца</h2>",
        "<p class=muted>Вердикт учителя — это ответ обычной модели, он бывает "
        "неверным. Проверенные тексты весят больше и идут в отложенную "
        "выборку: мерить надо на правде.</p>",
        render_check(unchecked(conn)),
        "<h2>3. Датасет</h2>",
        '<form method=post action="/laya/dataset"><button>Собрать датасет для '
        "Laya</button></form>",
        "<p class=muted>Собирается из базы: два порядка вариантов в каждой "
        "строке, деление отложенной части по компаниям, контакты под масками. "
        "Ни одного вызова модели и ни одного запроса в сеть.</p>",
    ]
    report = load_report()
    if report:
        parts.append(render_report(report))
        parts.append(
            "<p class=muted>Файлы лежат в <code>{}</code>.</p>".format(esc(str(OUT_DIR)))
        )
    parts.append("<h2>4. Дообучение и проверка</h2>")
    name, evaluated = model_eval()
    if evaluated:
        parts.append(render_eval(name, evaluated))
    parts.append(
        '<form method=post action="/laya/bench"><button>Сравнить с текущей '
        "моделью</button></form>"
    )
    parts.append(
        "<p class=muted>Прогон берёт отложенную выборку, если датасет уже "
        "собран, иначе кейсы бенчмарка. Требует установленной Laya и качает "
        "веса при первом запуске.</p>"
    )
    parts.append(details("Как дообучить", "три шага", HOWTO.format(out=esc(str(OUT_DIR)))))
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
    if path in ("/laya/dataset", "/laya/bench"):
        job_id, note = start_dataset() if path.endswith("dataset") else start_bench()
        if job_id is not None:
            return job_id, ""
    from ui_core import open_db  # noqa: PLC0415 — соединение на запрос, как у остальных форм

    conn = open_db()
    try:
        if path == "/laya/stages":
            note = save_stages(form)
        elif path == "/laya/label":
            note = save_label(conn, form)
        return None, render_laya(conn, note)
    finally:
        conn.close()


def start(task: str, extra: tuple[str, ...] = ()) -> tuple[int | None, str]:
    try:
        job = jobs.runner.start(task, list(extra))
    except (KeyError, RuntimeError) as exc:
        return None, "<div class=warn>{}</div>".format(esc(exc))
    return job.id, ""


def start_dataset() -> tuple[int | None, str]:
    """Аргументов из браузера нет: команда целиком из jobs.TASKS [CORE-023]."""
    return start("laya-dataset")


def start_bench() -> tuple[int | None, str]:
    cases = OUT_DIR / "bench_cases.json"
    return start("laya-bench", ("--cases", str(cases)) if cases.exists() else ())


__all__ = (
    "counts",
    "post",
    "load_report",
    "model_eval",
    "render_laya",
    "save_label",
    "save_stages",
    "start_bench",
    "start_dataset",
    "unchecked",
)
