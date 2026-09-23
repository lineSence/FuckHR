"""Счётчики страницы «Статистика»: на базе в памяти, без сети и модели."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import conditions
import db
import detector
import dossier_store
import stats
import ui_chart
import ui_stats

СЕЙЧАС = datetime.now(timezone.utc)


def когда(дней: int) -> str:
    return (СЕЙЧАС - timedelta(days=дней)).isoformat(timespec="seconds")


def база() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    detector.ensure_schema(conn)
    conditions.ensure_schema(conn)
    dossier_store.ensure_schema(conn)
    return conn


def вакансия(conn, key, *, score=50.0, profile="python", дней=1, company="ООО Ромашка",
             salary=(100000, 200000), описание="описание", notified=False):
    conn.execute(
        "INSERT INTO vacancies (key,source,external_id,url,title,company,description,"
        "salary_from,salary_to,currency,score,first_seen_at,last_seen_at,notified_at)"
        " VALUES (?,'hh',?,?,?,?,?,?,?,'RUR',?,?,?,?)",
        (key, key, "https://x/" + key, "Python", company, описание, salary[0], salary[1],
         score, когда(дней), когда(0), когда(0) if notified else None),
    )
    conn.execute(
        "INSERT INTO vacancy_profiles (key, profile_id, score, matched_at) VALUES (?,?,?,?)",
        (key, profile, score, когда(дней)),
    )
    conn.commit()


def test_воронка_считает_шаги_и_не_путает_порог():
    conn = база()
    вакансия(conn, "k1", score=60, notified=True)
    вакансия(conn, "k2", score=10, описание="")
    шаги = dict(stats.funnel(conn, "", 30, 45))
    assert шаги["встретили"] == 2
    assert шаги["скачали описание"] == 1
    assert шаги["выше порога профиля"] == 1
    assert шаги["ушло в Telegram"] == 1
    assert шаги["есть досье компании"] == 0


def test_разрез_по_профилю_берёт_скор_этого_профиля():
    conn = база()
    вакансия(conn, "k1", score=60, profile="python")
    вакансия(conn, "k2", score=60, profile="analyst")
    assert dict(stats.funnel(conn, "python", 30, 45))["встретили"] == 1
    assert dict(stats.funnel(conn, "", 30, 45))["встретили"] == 2
    assert {имя for имя, _ in stats.profiles_seen(conn)} == {"python", "analyst"}


def test_окно_отсекает_старое():
    conn = база()
    вакансия(conn, "k1", дней=2)
    вакансия(conn, "k2", дней=40)
    assert dict(stats.funnel(conn, "", 7, 45))["встретили"] == 1
    assert dict(stats.funnel(conn, "", 90, 45))["встретили"] == 2


def test_вилки_считаются_серединой_а_молчащие_видны_отдельно():
    conn = база()
    вакансия(conn, "k1", salary=(100000, 200000))
    вакансия(conn, "k2", salary=(None, None))
    данные = stats.salaries(conn, "", 30)
    assert данные["total"] == 2 and данные["shown"] == 1
    assert данные["median"] == 150000


def test_гистограмма_на_одном_значении_не_делит_на_ноль():
    assert stats.histogram([]) == []
    assert stats.histogram([100000.0]) == [("100к", 1)]


def test_флаги_детектора_разбираются_в_топ():
    conn = база()
    вакансия(conn, "k1")
    вакансия(conn, "k2")
    conn.execute("INSERT INTO vacancy_signals (key,created_at,flags,payload)"
                 " VALUES ('k1',?,'печеньки,dream_team','{}')", (когда(1),))
    conn.execute("INSERT INTO vacancy_signals (key,created_at,flags,payload)"
                 " VALUES ('k2',?,'','{}')", (когда(1),))
    conn.commit()
    данные = stats.text_quality(conn, "", 30)
    assert данные["reports"] == 2 and данные["flagged"] == 1
    assert dict(данные["top"])["печеньки"] == 1


def test_перепубликации_считаются_по_слепкам():
    conn = база()
    вакансия(conn, "k1")
    for n in range(3):
        conn.execute(
            "INSERT INTO vacancy_snapshots (key,seen_at,external_id,published_at,is_active)"
            " VALUES ('k1',?,?,?,1)", (когда(n), "k1-%d" % n, "2026-09-0%d" % (n + 1)),
        )
    conn.commit()
    данные = stats.lifetime(conn, "", 30)
    assert данные["tracked"] == 1 and данные["republished"] == 1


def test_пустая_база_не_роняет_ни_один_счётчик():
    conn = база()
    assert stats.funnel(conn, "", 30, 45)[0][1] == 0
    assert stats.daily(conn, "", 30, 45) == []
    assert stats.salaries(conn, "", 30)["median"] == 0
    assert stats.lifetime(conn, "", 30)["median_days"] == 0
    # Таблиц может не быть вовсе: страница всё равно должна открыться.
    assert stats.model_usage(conn, 30) == [] and stats.sources(conn, 30) == []


def test_графики_на_пустых_данных_говорят_что_данных_нет():
    assert ui_chart.bars([]) == ui_chart.EMPTY
    assert ui_chart.columns([]) == ui_chart.EMPTY
    assert ui_chart.spark([("2026-09-01", 1, 1)]) == ui_chart.EMPTY


def test_график_экранирует_подписи_из_базы():
    svg = ui_chart.bars([("<script>alert(1)</script>", 5)])
    assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_чужой_период_и_профиль_из_адреса_заменяются_умолчанием():
    conn = база()
    вакансия(conn, "k1", profile="python")
    assert ui_stats.pick_days("9999") == ui_stats.DEFAULT_DAYS
    assert ui_stats.pick_days("7") == 7
    assert ui_stats.pick_profile(conn, "; DROP TABLE") == ""
    assert ui_stats.pick_profile(conn, "python") == "python"


def test_страница_рисует_все_разделы():
    conn = база()
    вакансия(conn, "k1", score=60, notified=True)
    html = ui_stats.render_stats(conn, {"days": "30"})
    for раздел in ("Воронка", "По дням", "Деньги", "Работодатели",
                   "Качество текста", "Жизнь вакансии", "Расход и источники"):
        assert раздел in html
