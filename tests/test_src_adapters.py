"""Адаптеры площадок: разбор ответов без единого сетевого вызова."""

from __future__ import annotations

import json

import src_common as C
import src_hhlike
import src_rabota
import src_superjob
import src_trudvsem


class Fake:
    """Подставной загрузчик: отдаёт заранее заданный ответ."""

    def __init__(self, payload: object, text: str = "") -> None:
        self.payload = payload
        self.body = text
        self.calls: list[tuple[str, dict]] = []

    def json(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        payload, self.payload = self.payload, None
        return payload

    def text(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        body, self.body = self.body, ""
        return body

    def close(self) -> None:
        pass


def test_money_and_currency():
    assert C.money("от 400 000") == 400000
    assert C.money("50000 руб.") == 50000
    assert C.money(0) is None and C.money("") is None
    assert C.currency("«руб.»") == "RUR"
    assert C.currency("") == "RUR"
    assert C.currency("USD") == "USD"


def test_area_map_is_honest():
    """Неизвестный регион — пусто, то есть «не сужать». Врать про город нельзя."""
    assert C.area_for("trudvsem", 1) == "7700000000000"
    assert C.area_for("trudvsem", 113) == ""
    assert C.area_for("superjob", 66) == ""


def test_trudvsem_keeps_inn_as_company_id():
    node = {
        "id": "abc",
        "job-name": "Программист Python",
        "company": {"name": "ООО Ромашка", "inn": "7709969870"},
        "region": {"name": "Город Москва"},
        "salary_min": "30000",
        "salary_max": "50000",
        "currency": "«руб.»",
        "duty": "Писать код",
        "requirement": {"education": "Высшее", "experience": 3},
        "creation-date": "2026-08-25",
        "vac_url": "https://trudvsem.ru/x",
    }
    vacancy = src_trudvsem.to_vacancy(node)
    assert vacancy is not None
    assert vacancy.company_id == "7709969870"
    assert vacancy.salary_from == 30000 and vacancy.currency == "RUR"
    assert "Образование: Высшее" in vacancy.description
    assert vacancy.published_at == "2026-08-25"


def test_trudvsem_search_walks_pages():
    payload = {"results": {"vacancies": [{"vacancy": {"id": "1", "job-name": "Python"}}]}}
    fake = Fake(payload)
    found = list(src_trudvsem.search("python", area=1, fetcher=fake, max_pages=2))
    assert [v.external_id for v in found] == ["1"]
    assert "7700000000000" in fake.calls[0][0]


def test_superjob_needs_key():
    assert list(src_superjob.search("python", key="")) == []


def test_superjob_parses_objects():
    payload = {
        "objects": [
            {
                "id": 42,
                "profession": "Python разработчик",
                "firm_name": "Ромашка",
                "link": "https://superjob.ru/42",
                "payment_from": 250000,
                "payment_to": 0,
                "currency": "rub",
                "is_gross": True,
                "town": {"title": "Москва"},
                "candidat": "<p>Опыт</p>",
            }
        ],
        "more": False,
    }
    fake = Fake(payload)
    found = list(src_superjob.search("python", key="k", fetcher=fake))
    assert len(found) == 1
    assert found[0].gross is True and found[0].salary_to is None
    assert found[0].description == "Опыт"
    assert fake.calls[0][1]["keyword"] == "python"


def test_rabota_reads_json_ld():
    node = {
        "@type": "JobPosting",
        "title": "Senior AI developer (Python)",
        "url": "https://www.rabota.ru/vacancy/54422703/",
        "datePosted": "2026-09-21T10:10:42.000Z",
        "description": "<p>Текст</p>",
        "hiringOrganization": {"name": "Ромашка"},
        "jobLocation": {"address": {"addressLocality": "Москва"}},
        "baseSalary": {"currency": "RUB", "value": {"minValue": 300000, "maxValue": 450000}},
    }
    html = '<script type="application/ld+json">{}</script>'.format(json.dumps([node]))
    fake = Fake(None, text=html)
    found = list(src_rabota.search("python", fetcher=fake, max_pages=1))
    assert len(found) == 1
    assert found[0].external_id == "54422703"
    assert (found[0].salary_from, found[0].salary_to) == (300000, 450000)
    assert found[0].description == "Текст"


def test_rabota_survives_broken_block():
    """Битый JSON-LD пропускается, остальные вакансии со страницы остаются."""
    html = (
        '<script type="application/ld+json">{не json}</script>'
        '<script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Python", "url": "/vacancy/5/"}</script>'
    )
    assert len(src_rabota._postings(html)) == 1


def test_zarplata_uses_hh_engine():
    """Площадка на движке hh.ru: тот же разбор состояния, другой домен."""
    state = {
        "vacancySearchResult": {
            "vacancies": [
                {
                    "vacancyId": "9",
                    "name": "Python разработчик",
                    "company": {"name": "Ромашка"},
                    "compensation": {"from": 200000, "currencyCode": "RUR"},
                    "links": {"desktop": "/vacancy/9"},
                }
            ]
        }
    }
    html = '<template id="HH-Lux-InitialState">{}</template>'.format(json.dumps(state))
    fake = Fake(None, text=html)
    found = list(src_hhlike.search("python", fetcher=fake, max_pages=1))
    assert len(found) == 1
    assert found[0].source == "zarplata"
    assert found[0].url.startswith("https://zarplata.ru/")
    assert found[0].salary_from == 200000


def test_rabota_region_is_a_subdomain():
    """Регион у Работы.ру задаётся поддоменом: параметры она игнорирует, а
    молчание понимает как Москву (проверено на живой выдаче 22.09.2026)."""
    fetcher = Fake(None, text="<html></html>")
    list(src_rabota.search("python", area=2, fetcher=fetcher, max_pages=1))
    url, params = fetcher.calls[0]
    assert url.startswith("https://spb.rabota.ru/")
    assert "all_regions" not in params

    fetcher = Fake(None, text="<html></html>")
    list(src_rabota.search("python", area=113, fetcher=fetcher, max_pages=1))
    url, params = fetcher.calls[0]
    assert url.startswith("https://www.rabota.ru/")
    assert params["all_regions"] == 1


def test_accept_header_looks_like_a_browser():
    """Zarplata.ru отвечала 406 на наш Accept с application/json, и в логе это
    выглядело как «площадка не ответила». Заголовок должен быть браузерным."""
    assert "application/json" not in C.HEADERS["Accept"]
    assert C.HEADERS["Accept"].startswith("text/html")
