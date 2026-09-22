"""SuperJob: официальное API по ключу приложения.

Ключ бесплатный, но обязателен: без заголовка `X-Api-App-Id` площадка отвечает
403 и прямо говорит, где регистрироваться. Поэтому источник считается
недоступным, пока `SUPERJOB_KEY` не заполнен, — и это видно на главной
странице, а не в логе после пустого прогона `[CORE-017]`.

Ключ — секрет: на странице настроек он под маской, как токен Telegram.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import src_common as C
from hh import Vacancy, normalize_published_at, strip_html

log = logging.getLogger("fuckhr")

CODE = "superjob"
LABEL = "SuperJob"
API = "https://api.superjob.ru/2.0/vacancies/"
PER_PAGE = 50
MAX_PAGES = 4


def key() -> str:
    """Ключ приложения из настроек. Имя переменной живёт здесь, а не в реестре."""
    import settings

    return (settings.get("SUPERJOB_KEY", "") or "").strip()


def to_vacancy(node: dict[str, Any]) -> Vacancy | None:
    ident = str(node.get("id") or "").strip()
    title = str(node.get("profession") or "").strip()
    if not ident or not title:
        return None
    town = node.get("town") if isinstance(node.get("town"), dict) else {}
    client = node.get("client") if isinstance(node.get("client"), dict) else {}
    body = "\n".join(
        strip_html(str(node.get(key) or ""))
        for key in ("candidat", "work", "vacancyRichText")
        if node.get(key)
    )
    return Vacancy(
        source=CODE,
        external_id=ident,
        url=str(node.get("link") or ""),
        title=title,
        company=str(node.get("firm_name") or "").strip() or None,
        company_id=str(client.get("id") or "").strip() or None,
        area=str(town.get("title") or "").strip() or None,
        salary_from=C.money(node.get("payment_from")),
        salary_to=C.money(node.get("payment_to")),
        currency=C.currency(node.get("currency")),
        # is_gross у SuperJob означает «до вычета налога».
        gross=bool(node["is_gross"]) if node.get("is_gross") is not None else None,
        schedule=str((node.get("type_of_work") or {}).get("title") or "").strip() or None
        if isinstance(node.get("type_of_work"), dict)
        else None,
        experience=str((node.get("experience") or {}).get("title") or "").strip() or None
        if isinstance(node.get("experience"), dict)
        else None,
        description=body,
        published_at=normalize_published_at(node.get("date_published")),
    )


def ready(key: str) -> bool:
    return bool((key or "").strip())


def search(
    text: str,
    area: Any = None,
    period: int = 7,
    limit: int = 0,
    fetcher: Any = None,
    key: str = "",
    max_pages: int = MAX_PAGES,
) -> Iterator[Vacancy]:
    """Вакансии по запросу. Без ключа не ходим вовсе: ответ всё равно 403."""
    if not ready(key):
        log.info("SuperJob пропущен: не задан SUPERJOB_KEY")
        return
    own = fetcher is None
    fetcher = fetcher or C.client()
    params: dict[str, Any] = {"keyword": text, "count": PER_PAGE, "period": period}
    town = C.area_for(CODE, area)
    if town:
        params["town"] = town
    found = 0
    try:
        for page in range(max_pages):
            params["page"] = page
            data = fetcher.json(API, params=params, headers={"X-Api-App-Id": key})
            nodes = (data or {}).get("objects") or []
            if not nodes:
                return
            for node in nodes:
                vacancy = to_vacancy(node) if isinstance(node, dict) else None
                if vacancy is None:
                    continue
                yield vacancy
                found += 1
                if limit and found >= limit:
                    return
            if not (data or {}).get("more"):
                return
    finally:
        if own:
            fetcher.close()


__all__ = ("CODE", "LABEL", "key", "ready", "search", "to_vacancy")
