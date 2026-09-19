"""Общая оценка работодателя: уровень собирается из уже собранных данных.

Проверяется главное, ради чего писался ADR-018: итог — худшая ось, а не
среднее; вето работает поверх любых плюсов; накрутка отзывов обесценивает
чужие оценки; при тонком покрытии честный ответ — «недостаточно данных»
[HRD-004].
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import company_score
import company_score_rules as R
import company_score_store
import db
import dossier_store
import fake_store
import maintenance
import ui_companies


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    dossier_store.ensure_schema(conn)
    company_score_store.ensure_schema(conn)
    return conn


def _ts(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )


def _vacancy(conn, key: str, company: str, salary_from=100000, salary_to=150000):
    conn.execute(
        """
        INSERT INTO vacancies (
            key, source, external_id, url, title, company,
            salary_from, salary_to, first_seen_at, last_seen_at
        ) VALUES (?, 'hh', ?, 'https://hh.ru/v', 'Инженер', ?, ?, ?, ?, ?)
        """,
        (key, key, company, salary_from, salary_to, _ts(200), _ts(0)),
    )
    conn.commit()


def _snapshot(conn, key: str, days_ago: int, published_at=None, active=True):
    conn.execute(
        """
        INSERT INTO vacancy_snapshots (
            key, seen_at, external_id, published_at, company_id,
            salary_from, salary_to, is_active
        ) VALUES (?, ?, ?, ?, NULL, 100000, 150000, ?)
        """,
        (key, _ts(days_ago), key, published_at, int(active)),
    )
    conn.commit()


def _history(conn, key: str, company: str, republished: int = 0):
    """Вакансия с историей длиной в полгода и заданным числом перепубликаций."""
    _vacancy(conn, key, company)
    for index in range(max(1, republished)):
        _snapshot(conn, key, days_ago=180 - index * 30, published_at=f"2026-0{index + 1}-01")
    _snapshot(conn, key, days_ago=0, published_at="2026-09-01")


def _dossier(conn, company: str, patterns, fake_level="none", fake_signs=()):
    conn.execute(
        """
        INSERT INTO company_dossier (
            company, review_count, avg_rating, fake_level, fake_signs,
            risk, patterns, red_flags, green_flags, sources, updated_at
        ) VALUES (?, 5, 3.0, ?, ?, 'yellow', ?, '[]', '[]', '[]', ?)
        """,
        (
            company,
            fake_level,
            json.dumps([{"code": c, "text": t} for c, t in fake_signs], ensure_ascii=False),
            json.dumps(
                [
                    {"code": code, "label": code, "polarity": "red", "hits": hits, "quotes": []}
                    for code, hits in patterns
                ],
                ensure_ascii=False,
            ),
            _ts(1),
        ),
    )
    conn.commit()


def _item(conn, company, codes, site="dreamjob.ru", label="clean", days_ago=30):
    """Разобранный отзыв: у улики из него есть и площадка, и дата."""
    fake_store.ensure_schema(conn)
    idx = conn.execute(
        "SELECT COUNT(*) FROM review_items WHERE company = ?", (company,)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO review_items (
            company, url, idx, site, text_hash, excerpt, rating, dated_at,
            date_precision, has_reply, fake_score, signals, patterns, label, created_at
        ) VALUES (?, ?, ?, ?, ?, '', 4.0, ?, 'exact', 0, 0, '[]', ?, ?, ?)
        """,
        (
            company,
            "https://dreamjob.ru/r/{}".format(idx),
            idx,
            site,
            "hash{}".format(idx),
            _ts(days_ago)[:10],
            json.dumps(list(codes)),
            label,
            _ts(0),
        ),
    )
    conn.commit()


def test_улика_из_отзыва_несёт_площадку_и_дату():
    conn = _conn()
    _dossier(conn, "Ромашка", [("overtime", 1)])
    _item(conn, "Ромашка", ["overtime"], days_ago=40)

    улики = [e for e in company_score.evaluate(conn, "Ромашка").evidence if e.code == "overtime"]

    assert улики and "Dream Job" in улики[0].text
    assert улики[0].observed_at == _ts(40)[:10]
    # 0.8 «чужие слова» × площадка 1.0 × чистый отзыв 1.0 × нет накрутки
    assert улики[0].trust == 0.8


def test_заказной_отзыв_не_даёт_улики():
    conn = _conn()
    _dossier(conn, "Ромашка", [("overtime", 1)])
    _item(conn, "Ромашка", ["overtime"], label="fake")

    коды = {e.code for e in company_score.evaluate(conn, "Ромашка").evidence}

    assert "overtime" not in коды


def test_отзыв_на_слабой_площадке_весит_меньше():
    сильная = _conn()
    _dossier(сильная, "Ромашка", [])
    _item(сильная, "Ромашка", ["toxic"], site="dreamjob.ru")
    _item(сильная, "Ромашка", ["toxic"], site="dreamjob.ru")
    слабая = _conn()
    _dossier(слабая, "Ромашка", [])
    _item(слабая, "Ромашка", ["toxic"], site="antijob.net")
    _item(слабая, "Ромашка", ["toxic"], site="antijob.net")

    сильно = company_score.evaluate(сильная, "Ромашка").axes[R.CONDITIONS]
    слабо = company_score.evaluate(слабая, "Ромашка").axes[R.CONDITIONS]

    assert слабо < сильно


def test_старый_отзыв_весит_меньше_свежего():
    свежий = _conn()
    _dossier(свежий, "Ромашка", [])
    _item(свежий, "Ромашка", ["toxic"], days_ago=30)
    старый = _conn()
    _dossier(старый, "Ромашка", [])
    _item(старый, "Ромашка", ["toxic"], days_ago=30 * 40)

    assert (
        company_score.evaluate(старый, "Ромашка").axes[R.CONDITIONS]
        < company_score.evaluate(свежий, "Ромашка").axes[R.CONDITIONS]
    )


def test_одна_жалоба_с_трёх_площадок_даёт_одну_строку():
    conn = _conn()
    _dossier(conn, "Ромашка", [])
    for site in ("dreamjob.ru", "pravda-sotrudnikov.ru", "orabote.top"):
        _item(conn, "Ромашка", ["salary_delay"], site=site)
    _history(conn, "hh:1", "Ромашка", republished=3)
    _history(conn, "hh:2", "Ромашка", republished=3)

    строки = company_score.evaluate(conn, "Ромашка").lines()

    assert sum(1 for line in строки if "выплат" in line) == 1


def test_одна_ось_не_даёт_вывода():
    conn = _conn()
    _dossier(conn, "Ромашка", [("overtime", 2)])

    score = company_score.evaluate(conn, "Ромашка")

    assert score.level == R.LEVEL_UNKNOWN
    assert score.lines()[0].startswith("Оценка работодателя: недостаточно данных")


def test_вето_по_задержкам_зарплаты_сильнее_плюсов():
    conn = _conn()
    _dossier(
        conn,
        "Ромашка",
        [("salary_delay", 4), ("pays_on_time", 6), ("tech_culture", 5), ("remote_ok", 5)],
    )
    _history(conn, "hh:1", "Ромашка", republished=3)
    _history(conn, "hh:2", "Ромашка", republished=3)

    score = company_score.evaluate(conn, "Ромашка")

    assert score.level == R.LEVEL_RED
    assert score.veto == R.VETO_RED
    assert "задержк" in " ".join(score.lines()).lower()


def test_накрутка_обесценивает_отзывы():
    честный = _conn()
    _dossier(честный, "Ромашка", [("overtime", 3)])
    грязный = _conn()
    _dossier(
        грязный,
        "Ромашка",
        [("overtime", 3)],
        fake_level="fake",
        fake_signs=(("fake_share", "похожи на заказные 4 отзыва из 6"),),
    )

    чистая_ось = company_score.evaluate(честный, "Ромашка").axes[R.CONDITIONS]
    грязная_ось = company_score.evaluate(грязный, "Ромашка").axes[R.CONDITIONS]

    assert грязная_ось < чистая_ось


def test_накрутка_ставит_потолок_жёлтого():
    conn = _conn()
    _dossier(
        conn,
        "Ромашка",
        [("pays_on_time", 5), ("tech_culture", 4)],
        fake_level="fake",
        fake_signs=(("fake_share", "похожи на заказные 4 отзыва из 6"),),
    )
    _history(conn, "hh:1", "Ромашка", republished=1)
    _history(conn, "hh:2", "Ромашка", republished=1)

    score = company_score.evaluate(conn, "Ромашка")

    assert score.level == R.LEVEL_YELLOW
    assert score.veto == R.FAKE_CODE


def test_итог_по_худшей_оси_а_не_по_среднему():
    conn = _conn()
    _dossier(conn, "Ромашка", [("toxic", 3), ("micromanagement", 2), ("chaos", 2)])
    _history(conn, "hh:1", "Ромашка", republished=3)
    _history(conn, "hh:2", "Ромашка", republished=3)

    score = company_score.evaluate(conn, "Ромашка")

    assert score.axes[R.CONDITIONS] >= R.AXIS_RED
    assert score.level == R.LEVEL_RED


def test_строки_карточки_читаются_из_базы_без_пересчёта():
    conn = _conn()
    _dossier(conn, "Ромашка", [("salary_delay", 3), ("overtime", 2)])
    _history(conn, "hh:1", "Ромашка", republished=3)
    _history(conn, "hh:2", "Ромашка", republished=3)
    company_score_store.refresh(conn, ["Ромашка"])

    lines = company_score_store.row_lines(company_score_store.load(conn, "Ромашка"))

    assert lines and lines[0].startswith("Оценка работодателя:")
    assert len(lines) <= 4
    assert company_score_store.level_of(conn, "Ромашка") == R.LEVEL_RED


def test_свежесть_гасит_старую_улику():
    свежий = _conn()
    _dossier(свежий, "Ромашка", [("toxic", 2)])
    старый = _conn()
    _dossier(старый, "Ромашка", [("toxic", 2)])
    старый.execute(
        "UPDATE company_dossier SET updated_at = ?", (_ts(30 * 36),)
    )
    старый.commit()

    новая = company_score.evaluate(свежий, "Ромашка").axes[R.CONDITIONS]
    старая = company_score.evaluate(старый, "Ромашка").axes[R.CONDITIONS]

    assert старая < новая


def test_карточка_компании_показывает_уровень_и_улики():
    conn = _conn()
    _dossier(conn, "Ромашка", [("salary_delay", 3), ("overtime", 2)])
    _history(conn, "hh:1", "Ромашка", republished=3)
    _history(conn, "hh:2", "Ромашка", republished=3)
    company_score_store.refresh(conn, ["Ромашка"])

    html = ui_companies.render_score(conn, "Ромашка")

    assert "Оценка работодателя" in html
    assert R.LEVEL_RU[R.LEVEL_RED] in html
    assert "Доверие" in html and "вето" in html


def test_список_компаний_показывает_оценку_отдельной_колонкой():
    conn = _conn()
    _dossier(conn, "Ромашка", [("toxic", 2)])
    company_score_store.refresh(conn, ["Ромашка"])

    rows = ui_companies.company_rows(conn)

    assert rows and R.LEVEL_RU[R.LEVEL_UNKNOWN] in rows[0][1]
    assert len(rows[0]) == len(ui_companies.COMPANY_COLUMNS)


def test_очистка_убирает_только_оценку():
    conn = _conn()
    _dossier(conn, "Ромашка", [("toxic", 2)])
    company_score_store.refresh(conn, ["Ромашка"])

    maintenance.wipe(conn, ["score"])

    assert company_score_store.load(conn, "Ромашка") is None
    assert dossier_store.load(conn, "Ромашка") is not None


def test_одного_отзыва_про_задержки_мало_для_вето():
    conn = _conn()
    _dossier(conn, "Ромашка", [])
    _item(conn, "Ромашка", ["salary_delay"])
    _history(conn, "hh:1", "Ромашка", republished=3)
    _history(conn, "hh:2", "Ромашка", republished=3)

    один = company_score.evaluate(conn, "Ромашка")
    assert один.veto == ""

    _item(conn, "Ромашка", ["salary_delay"], site="pravda-sotrudnikov.ru")
    два = company_score.evaluate(conn, "Ромашка")

    assert два.veto == R.VETO_RED
    assert два.level == R.LEVEL_RED
