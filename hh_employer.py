"""Работодатель на hh.ru: разбор ввода, кандидаты, вакансии компании (ADR-025).

Три вида ввода, как решил владелец: название, ссылка на страницу компании и
ИНН. Ссылка даёт id сразу, название — только список кандидатов (одноимённых
контор много, и угадывать за владельца мы не будем), ИНН на hh.ru не ищется
вовсе — он нужен глубокому ресёрчу по реестрам.

Open API закрыт для неавторизованных с апреля 2026 (ADR-015), поэтому всё идёт
тем же путём, что и сбор: HTML страниц hh.ru через `hh_html.HHHtmlClient`.

Поиск кандидатов идёт тремя попытками подряд, от точной к грубой:

1. JSON состояния страницы — тот же путь, что и у сбора вакансий. Разметка
   у hh.ru давно рисуется скриптом, и ссылок `<a href="/employer/...">`
   в исходном HTML может не быть вовсе: именно поэтому раньше поиск по
   названию молча отдавал пустой список.
2. Ссылки в разметке — если состояние не разобралось.
3. Поиск вакансий по названию компании: у каждой вакансии есть работодатель
   с id, и это тот же запрос, которым живёт весь сбор. Медленнее, зато
   работает, пока работает сбор.

Капча наверх не проглатывается: «не нашли» и «hh.ru нас не пускает» — разные
ответы, и владельцу нужно видеть второй, а не гадать над первым.

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

import hh_html
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
NAME_RE = re.compile(r'"(?:companyName|employerName)"\s*:\s*"([^"]{2,120})"')
HEADER_RE = re.compile(
    r'data-qa="(?:company-header-title-name|company-name)"[^>]*>(.*?)<', re.DOTALL
)
OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]{2,200})"')
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Признаки того, что узел состояния — работодатель, а не что-то ещё с именем.
EMPLOYER_MARKS = (
    "logoUrls",
    "vacanciesCount",
    "openVacancies",
    "areaName",
    "employerId",
    "companyId",
    "badges",
    "trusted",
)


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


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "title", "text", "@name"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def _looks_like_employer(node: dict[str, Any]) -> bool:
    name = node.get("name") or node.get("companyName") or node.get("employerName")
    if not isinstance(name, str) or not name.strip():
        return False
    has_id = any(k in node for k in ("employerId", "companyId", "id"))
    return has_id and any(k in node for k in EMPLOYER_MARKS)


def _from_state(body: str, limit: int) -> list[Employer]:
    """Работодатели из JSON состояния страницы: основной путь после редизайнов."""
    try:
        state = hh_html.extract_state(body)
    except hh_html.ExtractionError:
        return []
    out: dict[str, Employer] = {}
    queue: list[Any] = [state]
    seen = 0
    while queue and len(out) < limit:
        node = queue.pop(0)
        seen += 1
        if seen > 200_000:
            break
        if isinstance(node, dict):
            if _looks_like_employer(node):
                eid = str(
                    node.get("employerId") or node.get("companyId") or node.get("id") or ""
                )
                name = _clean(
                    str(
                        node.get("name")
                        or node.get("companyName")
                        or node.get("employerName")
                    )
                )
                if eid.isdigit() and name:
                    out.setdefault(
                        eid,
                        Employer(
                            id=eid,
                            name=name,
                            url=EMPLOYER_URL + eid,
                            area=_clean(_text(node.get("area") or node.get("areaName"))),
                        ),
                    )
                continue
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return list(out.values())


def _from_links(body: str, limit: int) -> list[Employer]:
    """Резерв: обычные ссылки на страницы работодателей в разметке."""
    out: dict[str, Employer] = {}
    for eid, label in CARD_RE.findall(body):
        title = _clean(label)
        if not title or eid in out:
            continue
        out[eid] = Employer(id=eid, name=title, url=EMPLOYER_URL + eid)
        if len(out) >= limit:
            break
    return list(out.values())


def _from_vacancies(client: Any, name: str, limit: int) -> list[Employer]:
    """Последняя попытка: работодатели из выдачи вакансий по названию компании.

    Это тот же запрос, которым живёт сбор. Пока работает он — работает и поиск
    компании, даже если страница `/search/employer` поменялась до неузнаваемости.
    """
    out: dict[str, Employer] = {}
    try:
        found = client.search(
            name,
            period=0,
            max_pages=1,
            extra={"search_field": "company_name"},
        )
        for vacancy in found:
            eid = str(getattr(vacancy, "company_id", "") or "")
            title = _clean(str(getattr(vacancy, "company", "") or ""))
            if not eid.isdigit() or not title or eid in out:
                continue
            out[eid] = Employer(
                id=eid,
                name=title,
                url=EMPLOYER_URL + eid,
                area=_clean(str(getattr(vacancy, "area", "") or "")),
            )
            if len(out) >= limit:
                break
    except hh_html.BlockedError:
        raise
    except Exception as exc:  # noqa: BLE001 — резерв не должен ронять страницу
        log.warning("поиск компании «%s» по вакансиям не удался: %s", name, exc)
    return list(out.values())


def candidates(client: Any, name: str, limit: int = 10) -> list[Employer]:
    """Работодатели hh.ru по названию. Пустой список — не ошибка, а «не нашли».

    Кандидаты показываются владельцу: «ООО Ромашка» в выдаче десяток, и
    ресёрч не по той конторе хуже отсутствия ресёрча.

    Капча (`BlockedError`) пробрасывается наверх: молчаливый пустой список на
    блокировке — худший из возможных ответов, владелец правит не то.
    """
    name = (name or "").strip()
    if not name:
        return []
    body = ""
    try:
        body = client.fetch(EMPLOYER_SEARCH_URL, {"text": name, "area": 113})
    except hh_html.BlockedError:
        raise
    except Exception as exc:  # noqa: BLE001 — сеть не должна ронять страницу
        log.warning("поиск работодателя «%s» не удался: %s", name, exc)
    out = _from_state(body, limit) if body else []
    if not out and body:
        out = _from_links(body, limit)
        if out:
            log.info("состояние страницы не разобралось, кандидаты взяты из разметки")
    if not out:
        # Страница поиска работодателей могла поменяться: сохраняем её для
        # разбора и идём обходным путём через выдачу вакансий.
        if body:
            _dump(client, body, "employer-search-empty")
        out = _from_vacancies(client, name, limit)
        if out:
            log.info("кандидаты по «%s» найдены через выдачу вакансий", name)
    log.info("кандидатов по «%s»: %s", name, len(out))
    return out


def _dump(client: Any, body: str, reason: str) -> None:
    """Сырая страница на диск: без неё починка разбора — гадание."""
    directory = getattr(client, "failure_dir", None)
    if not directory:
        return
    try:
        path = hh_html.dump_failure(body, reason, directory)
    except OSError as exc:
        log.warning("не смог сохранить страницу поиска работодателя: %s", exc)
        return
    log.warning("страница поиска работодателя сохранена: %s", path)


def employer(client: Any, employer_id: str) -> Employer | None:
    """Название компании по её id. Нужно, когда владелец дал ссылку."""
    try:
        body = client.fetch(EMPLOYER_URL + str(employer_id))
    except hh_html.BlockedError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("страница работодателя %s недоступна: %s", employer_id, exc)
        return None
    name = ""
    for pattern in (NAME_RE, HEADER_RE, OG_TITLE_RE):
        match = pattern.search(body)
        if match:
            name = _clean(match.group(1))
            if name:
                break
    if not name:
        title = TITLE_RE.search(body)
        # «Работа в компании X | Вакансии …» — берём то, что до первой черты.
        name = _clean(title.group(1)).split("|")[0].strip() if title else ""
    name = re.sub(r"^(?:Работа в компании|Вакансии компании)\s+", "", name, flags=re.I)
    name = name.strip(" ·—-")
    if not name:
        log.warning("имя работодателя %s со страницы не вынулось", employer_id)
        _dump(client, body, "employer-page-no-name")
        return None
    return Employer(id=str(employer_id), name=name, url=EMPLOYER_URL + str(employer_id))


def employer_of_vacancy(client: Any, vacancy_id: str) -> Employer | None:
    """Работодатель по ссылке на его вакансию: частый способ дать компанию."""
    try:
        detail = client.vacancy(str(vacancy_id))
    except hh_html.BlockedError:
        raise
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
