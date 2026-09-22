"""Общее для адаптеров площадок: запросы, паузы, разбор зарплаты и регионов.

Каждая площадка живёт в своём файле (`src_*.py`) — иначе один модуль на восемь
сайтов упёрся бы в 25 КБ `[CORE-024]` и превратился в свалку частных случаев.
Здесь только то, что у них общее.

Сеть: один клиент на адаптер, браузерные заголовки, пауза между запросами и
единственная попытка. Площадка ответила не так — адаптер отдаёт пусто, а прогон
идёт дальше на остальных источниках `[CORE-017]`: падение одного сайта не имеет
права ронять сбор.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger("fuckhr")

TIMEOUT = 20.0
PAUSE = 1.0
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ru-RU,ru;q=0.9",
    # Как у браузера. Своя строка с application/json уже стоила нам площадки:
    # Zarplata.ru отвечала на неё 406 Not Acceptable, а в логе это выглядело
    # как «площадка не ответила». API-площадкам заголовок безразличен.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Регионы: профиль хранит коды hh.ru, у каждой площадки справочник свой.
# Сознательно поддержаны только Россия целиком, Москва и Питер: остальные
# коды означают «не сужать», и площадка отдаёт всё. Врать про регион хуже,
# чем не фильтровать по нему.
AREA_MAP: dict[str, dict[str, str]] = {
    "113": {"trudvsem": "", "superjob": "", "rabota": ""},
    "1": {"trudvsem": "7700000000000", "superjob": "4", "rabota": "moskva"},
    "2": {"trudvsem": "7800000000000", "superjob": "14", "rabota": "sankt-peterburg"},
}

_MONEY_RE = re.compile(r"(\d[\d\s ]{2,})")


def area_for(source: str, area: Any) -> str:
    """Код региона площадки по коду hh.ru. Неизвестный — пусто, то есть «везде»."""
    if isinstance(area, (list, tuple)):
        area = area[0] if area else None
    code = str(area or "").strip()
    return AREA_MAP.get(code, {}).get(source, "")


def money(value: Any) -> int | None:
    """Число из «от 400 000», «50000 руб.», 30000. Ноль — это отсутствие вилки."""
    if isinstance(value, (int, float)):
        return int(value) or None
    match = _MONEY_RE.search(str(value or ""))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) or None if digits else None


def currency(value: Any) -> str:
    """«руб.», «rub», «RUR» → RUR. Пусто — тоже RUR: площадки российские."""
    text = re.sub(r"[^A-Za-zА-Яа-я]", "", str(value or "")).upper()
    if not text or text.startswith(("РУБ", "RUB", "RUR")):
        return "RUR"
    return text[:3]


def client(pause: float = PAUSE, timeout: float = TIMEOUT) -> "Fetcher":
    return Fetcher(pause=pause, timeout=timeout)


class Fetcher:
    """Запросы к площадке с паузой. Ошибка — пустой ответ, не исключение."""

    def __init__(self, pause: float = PAUSE, timeout: float = TIMEOUT) -> None:
        self.pause = max(0.0, pause)
        self._last = 0.0
        self._client = httpx.Client(
            headers=HEADERS, timeout=timeout, follow_redirects=True
        )
        self.failures = 0

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if self._last and gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.monotonic()

    def text(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> str:
        self._wait()
        try:
            response = self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            self.failures += 1
            log.warning("%s не ответил: %s", url, exc)
            return ""
        if response.status_code != 200:
            self.failures += 1
            log.warning("%s ответил %s", url, response.status_code)
            return ""
        return response.text

    def json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        body = self.text(url, params=params, headers=headers)
        if not body:
            return None
        try:
            import json

            return json.loads(body)
        except ValueError as exc:
            self.failures += 1
            log.warning("%s ответил не JSON: %s", url, exc)
            return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ("AREA_MAP", "Fetcher", "area_for", "client", "currency", "money")
