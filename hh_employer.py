"""Работодатель на hh.ru: разбор ввода, кандидаты, вакансии компании (ADR-025).

Три вида ввода, как решил владелец: название, ссылка на страницу компании и
ИНН. Ссылка даёт id сразу, название — только список кандидатов (одноимённых
контор много, и угадывать за владельца мы не будем), ИНН на hh.ru не ищется
вовсе — он нужен глубокому ресёрчу по реестрам.

Open API закрыт для неавторизованных с апреля 2026 (ADR-015), поэтому всё идёт
тем же путём, что и сбор: HTML страниц hh.ru через `hh_html.HHHtmlClient`.
Разбор нарочно грубый — ссылки вида `/employer/<id>` и текст ссылки. Он
переживает редизайн лучше, чем путь внутрь JSON состояния, а когда не
переживёт, список кандидатов просто окажется пустым: это видно сразу и не
портит данные.

Вакансии работодателя берутся тем же поиском, что и весь сбор, только вместо
текста запроса подставляется `employer_id`. Отдельного обхода писать не нужно,
и дедуп по ключу вакансии работает как обычно.
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterator

from hh import Vacancy

log = logging.getLogger(__name__)

EMPLOYER_SEARCH_URL = "https://hh.ru/search/employer"
EMPLOYER_URL = "https://hh.ru/employer/"

# Ссылка на компанию: и «чистая», и с хвостом параметров, и с поддоменом города.
LINK_RE = re.compile(r"hh\.ru/employer/(\d+)", re.IGNORECASE)
# Ссылка на вакансию тоже годится: из неё id работодателя достаётся страницей.
VACANCY_LINK_RE = re.compile(r"hh\.ru/vacancy/(\d+)", re.IGNORECASE)
INN_RE = re.compile(r"^\d{10}(\d{2})?$")
CARD_RE = re.compile(
    r'<a[^>]+href="[^"]*?/employer/(\d+)[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
TAGS_RE = re.compile(r"<[^>]+>")
NAME_RE = re.compile(r'"companyName"\s*:\s*"([^"]{2,120})"')
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Employer:
    id: str
    name: str
    url: str = ""
    area: str = ""

    @property
    def link(self) -> str:
        return self.url or (EMPLOYER_URL + self.id)


@dataclass(frozen=True)
class Ask:
    """Что именно ввёл владелец."""

    kind: str  # link | inn | name
    value: str


def parse_input(text: str) -> Ask:
    """Строка из формы → вид ввода. Ничего не запрашивает."""
    raw = (text or "").strip()
    match = LINK_RE.search(raw)
    if match:
        return Ask("link", match.group(1))
    match = VACANCY_LINK_RE.search(raw)
    if match:
        return Ask("vacancy", match.group(1))
    digits = re.sub(r"[\s-]+", "", raw)
    if INN_RE.match(digits):
        return Ask("inn", digits)
    return Ask("name", raw)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(TAGS_RE.sub(" ", value))).strip()


def candidates(client: Any, name: str, limit: int = 10) -> list[Employer]:
    """Работодатели hh.ru по названию. Пустой список — не ошибка, а «не нашли».

    Кандидаты показываются владельцу: «ООО Ромашка» в выдаче десяток, и
    ресёрч не по той конторе хуже отсутствия ресёрча.
    """
    name = (name or "").strip()
    if not name:
        return []
    try:
        body = client.fetch(EMPLOYER_SEARCH_URL, {"text": name, "area": 113})
    except Exception as exc:  # noqa: BLE001 — сеть и капча не должны ронять страницу
        log.warning("поиск работодателя «%s» не удался: %s", name, exc)
        return []
    out: dict[str, Employer] = {}
    for eid, label in CARD_RE.findall(body):
        title = _clean(label)
        if not title or eid in out:
            continue
        out[eid] = Employer(id=eid, name=title, url=EMPLOYER_URL + eid)
        if len(out) >= limit:
            break
    log.info("кандидатов по «%s»: %s", name, len(out))
    return list(out.values())


def employer(client: Any, employer_id: str) -> Employer | None:
    """Название компании по её id. Нужно, когда владелец дал ссылку."""
    try:
        body = client.fetch(EMPLOYER_URL + str(employer_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("страница работодателя %s недоступна: %s", employer_id, exc)
        return None
    match = NAME_RE.search(body)
    name = _clean(match.group(1)) if match else ""
    if not name:
        title = TITLE_RE.search(body)
        # «Работа в компании X | Вакансии …» — берём то, что до первой черты.
        name = _clean(title.group(1)).split("|")[0].strip() if title else ""
        name = re.sub(r"^Работа в компании\s+", "", name, flags=re.IGNORECASE)
    if not name:
        log.warning("имя работодателя %s со страницы не вынулось", employer_id)
        return None
    return Employer(id=str(employer_id), name=name, url=EMPLOYER_URL + str(employer_id))


def employer_of_vacancy(client: Any, vacancy_id: str) -> Employer | None:
    """Работодатель по ссылке на его вакансию: частый способ дать компанию."""
    try:
        detail = client.vacancy(str(vacancy_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("вакансия %s недоступна: %s", vacancy_id, exc)
        return None
    block = (detail or {}).get("employer") or {}
    name = _clean(str(block.get("name") or ""))
    eid = str(block.get("id") or "")
    if not name:
        return None
    return Employer(id=eid, name=name, url=EMPLOYER_URL + eid if eid else "")


def vacancies(
    client: Any, employer_id: str, max_pages: int = 0, period: int = 0
) -> Iterator[Vacancy]:
    """Все вакансии работодателя: тот же поиск, но по employer_id.

    `period=0` — без ограничения по дате: цель интересна целиком, а не только
    свежими вакансиями. `max_pages=0` — до конца выдачи.
    """
    yield from client.search(
        "", period=period, max_pages=max_pages, extra={"employer_id": str(employer_id)}
    )


__all__ = (
    "Ask",
    "EMPLOYER_SEARCH_URL",
    "EMPLOYER_URL",
    "Employer",
    "candidates",
    "employer",
    "employer_of_vacancy",
    "parse_input",
    "vacancies",
)
