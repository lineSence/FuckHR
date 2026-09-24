"""Каскад кандидатов на этап: порядок из бенча, последний слот — локальный.

Отдельный файл, потому что `llm.py` упёрся в 25 КБ [CORE-024], и потому что
это два разных вопроса: *куда и в каком порядке* пробовать (здесь) и *как
сходить и что положить в кэш* (там).

Зачем каскад вообще. Раньше у этапа была одна модель и три повтора к ней
подряд. На 429 это худшее из возможного: ждём того, что за пять секунд не
изменится, вместо того чтобы спросить следующего кандидата [CORE-016].
Теперь у этапа список: до трёх имён с прокси в порядке бенча плюс локальный
адрес последним слотом [LLM-010] — пайплайн доходит до конца даже при
полностью мёртвом облаке [CORE-017].

Порядок берётся строго из бенча (`bench.cascades`, решение владельца
20.09.2026) и живёт в `LLM_STAGE_MODELS_<ЭТАП>`. Рейтинг в рантайме не
пересчитывается: качество ответа мы на живых данных не меряем, а гадать по
длине текста хуже, чем не гадать [CORE-019].

Два этапа из каскада исключены:

- `embeddings` — модель пиннится на всю жизнь базы [LLM-011]; «фолбэк» здесь
  означает молча испорченные векторы, поэтому у LOCAL_FIRST_STAGES слот один;
- персональные этапы — кандидаты остаются внутри локального адреса, наружу
  каскад не спускается без явного `LLM_PERSONAL_VIA_PROXY` [CORE-012].
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from llm_profiles import (
    LOCAL_FIRST_STAGES,
    MAX_CANDIDATES,
    PERSONAL_STAGES,
    ROUTE_LOCAL,
    ROUTE_PROXY,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    """Куда физически уходит запрос и под каким именем модели."""

    name: str
    base_url: str
    api_key: str | None
    model: str

    @property
    def is_proxy(self) -> bool:
        return self.name == ROUTE_PROXY


def parse_models(value: str | None) -> list[str]:
    """«a, b, b, c, d» → ['a', 'b', 'c']: дубли выброшены, длина ограничена."""
    out: list[str] = []
    for chunk in (value or "").replace(";", ",").split(","):
        name = chunk.strip()
        if name and name not in out:
            out.append(name)
    return out[:MAX_CANDIDATES]


class Dropped:
    """Пары (маршрут, модель), выбывшие до конца прогона.

    Решением владельца (20.09.2026) 400/404 и 429 обрабатываются одинаково —
    кандидат выбывает до конца прогона. Для 400 это очевидно: конфиг прокси
    внутри прогона не меняется. Для 429 это выбор в пользу простоты: квота
    восстанавливается по часам провайдера, а прогон идёт минуты, поэтому
    кулдаун короче прогона всё равно ничего не вернёт, а таймеры стоят строк
    [CORE-025].
    """

    def __init__(self) -> None:
        self._reasons: dict[tuple[str, str], str] = {}

    def add(self, route: str, model: str, reason: str) -> None:
        self._reasons[(route, model)] = reason

    def __contains__(self, pair: object) -> bool:
        return tuple(pair) in self._reasons if isinstance(pair, tuple) else False

    def __len__(self) -> int:
        return len(self._reasons)

    def reason(self, route: str, model: str) -> str:
        return self._reasons.get((route, model), "")

    def items(self) -> list[tuple[tuple[str, str], str]]:
        return sorted(self._reasons.items())


# Этап -> о каком наборе моделей уже предупреждали. Меняется набор — говорим
# заново: это другое решение.
_personal_said: dict[str, str] = {}


def build_chain(
    stage: str,
    local: Route | None,
    proxy_names: Sequence[str],
    proxy_base_url: str,
    proxy_api_key: str | None,
    dropped: Dropped,
    personal_via_proxy: bool,
) -> list[Route]:
    """Кандидаты по порядку. Пустой список — считать этап негде.

    Правила те же, что были у одиночного маршрута, плюс перебор:

    1. этапы с ПД идут на локальный адрес, если владелец явно не разрешил
       обратное через LLM_PERSONAL_VIA_PROXY;
    2. остальные предпочитают прокси: модели там сильнее;
    3. этапы из LOCAL_FIRST_STAGES получают ровно один слот;
    4. локальный адрес всегда замыкает каскад [LLM-010];
    5. выбывшие в этом прогоне пары пропускаются.
    """
    personal = stage in PERSONAL_STAGES

    proxies = [
        Route(ROUTE_PROXY, proxy_base_url, proxy_api_key, name)
        for name in proxy_names
        if proxy_base_url and (ROUTE_PROXY, name) not in dropped
    ]
    if local is not None and (ROUTE_LOCAL, local.model) in dropped:
        local = None

    if stage in LOCAL_FIRST_STAGES:
        # Модель векторов пиннится [LLM-011]: перебирать тут нечего.
        return [local] if local is not None else proxies[:1]

    if personal and not personal_via_proxy:
        if proxies and local is None:
            log.warning(
                "этап %s работает с ПД и пропущен: локальной модели нет, "
                "а на прокси его пускать не разрешено (LLM_PERSONAL_VIA_PROXY)",
                stage,
            )
        proxies = []
    elif personal and proxies:
        # Решение должно быть видно в логе, но один раз на этап за процесс:
        # на каждый вызов это 130 строк предупреждений за прогон, и за ними
        # перестают быть видны настоящие ошибки.
        names = ", ".join(route.model for route in proxies)
        if _personal_said.get(stage) != names:
            _personal_said[stage] = names
            log.warning(
                "этап %s с персональными данными уходит на внешний прокси (%s)",
                stage,
                names,
            )

    chain = list(proxies)
    if local is not None:
        chain.append(local)
    return chain


__all__ = (
    "Dropped",
    "MAX_CANDIDATES",
    "Route",
    "build_chain",
    "parse_models",
)
