"""Глубокий ресёрч: что проверяется (ADR-019).

Сеть не трогаем: и поиск, и загрузка страниц идут через подменённый транспорт.
Проверяются обещания, которые дороже всего нарушить:

1. страница засчитывается компании, только если она про эту компанию;
2. капча не обходится, источник пропускается, обход идёт дальше [CORE-017];
3. второй круг запускается только от найденного ИНН;
4. время — единственный потолок: по дедлайну ресёрч заканчивается сам;
5. находки не попадают в оценку работодателя без DEEP_IN_SCORE.
"""

from __future__ import annotations

import pytest

import company_score
import deepresearch
import deepresearch_store as store
import websearch


def provider(pages: dict[str, list[str]]) -> websearch.SearchProvider:
    """Поиск по словарю: подстрока запроса → ссылки."""

    def transport(name: str, query: str, limit: int) -> list[websearch.Hit]:
        for needle, urls in pages.items():
            if needle in query:
                return [websearch.Hit(title=url, url=url, snippet="") for url in urls]
        return []

    return websearch.SearchProvider(provider="searxng", base_url="http://x", transport=transport)


def researcher(conn, pages, texts, seconds=60.0):
    return deepresearch.Researcher(
        conn,
        provider(pages),
        seconds,
        transport=lambda url: texts.get(url, ""),
        cache_days=0,
    )


def test_страница_про_другую_контору_не_становится_находкой(conn) -> None:
    worker = researcher(
        conn,
        {"суд невыплата": ["https://sudact.ru/1"]},
        {"https://sudact.ru/1": "<p>ООО «Василёк»: задолженность по заработной плате</p>"},
    )
    report = worker.run("ООО «Ромашка»")
    assert [item.code for item in report.findings] == []


def test_находка_сохраняется_с_источником_и_цитатой(conn) -> None:
    worker = researcher(
        conn,
        {"суд невыплата": ["https://sudact.ru/2"]},
        {
            "https://sudact.ru/2": "<p>Иск к ООО «Ромашка»: взыскании заработной платы "
            "за три месяца</p>"
        },
    )
    report = worker.run("ООО «Ромашка»")
    found = [item for item in report.findings if item.code == "salary_delay"]
    assert len(found) == 1
    assert found[0].domain == "sudact.ru"
    assert "заработной платы" in found[0].quote
    assert found[0].trust == 1.0
    # Отчёт читается обратно из базы целиком.
    saved = store.load(conn, "ООО «Ромашка»")
    assert saved is not None and len(saved.findings) == 1


def test_капча_пропускает_источник_и_не_роняет_ресёрч(conn) -> None:
    worker = researcher(
        conn,
        {
            "суд невыплата": ["https://kad.arbitr.ru/1"],
            "банкротство": ["https://checko.ru/1"],
        },
        {
            "https://kad.arbitr.ru/1": "<html>Подтвердите, что вы не робот</html>",
            "https://checko.ru/1": "<p>ООО «Ромашка» — конкурсное производство</p>",
        },
    )
    report = worker.run("ООО «Ромашка»")
    assert "kad.arbitr.ru" in report.blocked
    assert any(item.code == "bankruptcy" for item in report.findings)


def test_второй_круг_идёт_только_от_найденного_инн(conn) -> None:
    registry = "<p>ООО «Ромашка» действующая, ИНН 7701234567, дата регистрации 12.03.2015</p>"
    worker = researcher(
        conn,
        {
            "ИНН ОГРН реквизиты": ["https://rusprofile.ru/1"],
            "ИНН 7701234567 взыскание": ["https://sudact.ru/3"],
        },
        {
            "https://rusprofile.ru/1": registry,
            "https://sudact.ru/3": "<p>ИНН 7701234567: задолженность по заработной плате</p>",
        },
    )
    report = worker.run("ООО «Ромашка»")
    assert report.inn == "7701234567"
    assert report.registered_at == "12.03.2015"
    assert any(item.code == "salary_delay_inn" for item in report.findings)


def test_время_единственный_потолок(conn) -> None:
    worker = researcher(
        conn,
        {"": ["https://example.com/1"]},
        {"https://example.com/1": "<p>ООО «Ромашка»</p>"},
        seconds=1.0,
    )
    worker.deadline = 0.0  # время уже вышло
    report = worker.run("ООО «Ромашка»")
    assert report.status == "время вышло"
    assert report.queries == 0


def test_находки_входят_в_оценку_только_по_настройке(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.ensure_schema(conn)
    store.save(
        conn,
        store.Report(
            company="ООО «Ромашка»",
            status="готово",
            findings=(
                store.Finding(
                    code="salary_delay",
                    url="https://sudact.ru/4",
                    domain="sudact.ru",
                    quote="взыскании заработной платы",
                    trust=1.0,
                    observed_at="2026-09-19T00:00:00+00:00",
                ),
            ),
        ),
    )
    monkeypatch.setenv("DEEP_IN_SCORE", "0")
    codes = [e.code for e in company_score.evaluate(conn, "ООО «Ромашка»").evidence]
    assert "salary_delay" not in codes

    monkeypatch.setenv("DEEP_IN_SCORE", "1")
    score = company_score.evaluate(conn, "ООО «Ромашка»")
    assert "salary_delay" in [e.code for e in score.evidence]
    # Улика с доверием 1.0 по задержкам зарплаты — это вето [ADR-018].
    assert score.level == "red"
