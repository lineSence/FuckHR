"""Страж интерфейса: отвечаем только своим страницам.

127.0.0.1 не граница для браузера: любая открытая вкладка может отправить форму
на localhost (CSRF), а через DNS rebinding — читать страницы под чужим именем
хоста. Поэтому Host сверяется у каждого запроса, источник — у каждого POST.
Запрос без этих заголовков пропускается: браузер их шлёт всегда, а curl и
тесты — нет.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping
from urllib.parse import urlsplit

from ui_core import esc, page

log = logging.getLogger("webui")

LOCAL_NAMES = ("127.0.0.1", "localhost")
OWN_FETCH = {"same-origin", "none"}  # same-site пропустил бы соседний порт localhost


def problem(method: str, headers: Mapping[str, str], port: int) -> str:
    """Причина отказа; пустая строка — запрос свой."""
    own = {"{}:{}".format(name, port) for name in LOCAL_NAMES}
    host = (headers.get("Host") or "").strip().lower()
    if host and host not in own:
        return "чужой Host: {}".format(host)
    if method != "POST":
        return ""
    fetch = (headers.get("Sec-Fetch-Site") or "").lower()
    if fetch and fetch not in OWN_FETCH:
        return "форма отправлена с другого сайта ({})".format(fetch)
    source = headers.get("Origin") or headers.get("Referer") or ""
    if source:
        parts = urlsplit(source)
        if parts.scheme != "http" or parts.netloc.lower() not in own:
            return "форма отправлена с чужой страницы: {}".format(source)
    return ""


def refuse(handler: Any) -> bool:
    """Отвечает 403 и возвращает True, если запрос не от наших страниц."""
    reason = problem(handler.command, handler.headers, handler.server.server_address[1])
    if reason:
        log.warning("отклонён %s %s: %s", handler.command, handler.path, reason)
        handler._send(page("Отказ", "<div class=warn>{}</div>".format(esc(reason))), 403)
    return bool(reason)


__all__ = ("LOCAL_NAMES", "problem", "refuse")
