"""Диагностика шлюза и внешнего прокси: где считается каждый этап.

Зачем отдельная команда, а не флаг у run.py: полный прогон стоит минуты и
сотни запросов к hh.ru, а проверить нужно две вещи: подхватился ли адрес и
куда уйдут этапы с персональными данными.

    python check_llm.py            # таблица маршрутов + список моделей
    python check_llm.py --live      # плюс один реальный вызов на этапе extract

Ни одного персонального вызова здесь не делается даже с --live: текст для пробного
запроса выдуманный и не содержит ни ФИО, ни контактов.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Sequence

import llm
import llm_tasks

PROBE = (
    "Ищем python-разработчика. Гибрид: два дня в офисе, остальное из дома. "
    "Стек: FastAPI и PostgreSQL. Оформление по ТК РФ."
)


def _mask(value: str | None) -> str:
    if not value:
        return "не задано"
    return f"задан ({len(value)} символов), оканчивается на …{value[-4:]}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка маршрутов к моделям")
    parser.add_argument(
        "--live",
        action="store_true",
        help="сделать один реальный вызов на этапе extract",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        from dotenv import find_dotenv, load_dotenv

        path = find_dotenv(usecwd=True)
        load_dotenv(path)
        print(f".env: {path or 'не найден — запускай из каталога проекта'}")
    except ImportError:
        print(".env: python-dotenv не установлен, беру переменные окружения как есть")

    gateway = llm.Gateway.from_env()

    print()
    print("Адреса")
    print(f"  локальный: {gateway.base_url or 'не задан (LLM_BASE_URL)'}")
    print(f"  прокси:    {gateway.proxy_base_url or 'не задан (LLM_PROXY_BASE_URL)'}")
    print(f"  ключ прокси: {_mask(gateway.proxy_api_key)}")
    print(
        "  персональные этапы на прокси: "
        + ("РАЗРЕШЕНЫ" if gateway.personal_via_proxy else "запрещены")
    )

    if not gateway.enabled:
        print()
        print(f"Шлюз выключен: {gateway.disabled_reason}")
        return 1

    print()
    print("Маршруты этапов")
    print(f"  {'этап':<12}{'профиль':<14}{'куда':<10}модель")
    for stage, profile, route, model in gateway.describe_routes():
        mark = " ← ПД" if stage in llm.PERSONAL_STAGES else ""
        print(f"  {stage:<12}{profile:<14}{route:<10}{model}{mark}")

    if gateway.proxy_base_url:
        print()
        names = gateway.models(llm.ROUTE_PROXY)
        if names:
            print(f"Модели на прокси ({len(names)}): {', '.join(names[:20])}")
        else:
            print("Прокси не отдал список моделей.")
            print("  Проверь туннель и адрес: в LLM_PROXY_BASE_URL нужен суффикс /v1")

    if not args.live:
        print()
        print("Для живого вызова: python check_llm.py --live")
        return 0

    print()
    print("Живой вызов (этап extract, выдуманный текст без ПД)")
    conditions = llm_tasks.extract_conditions(gateway, PROBE)
    if not conditions:
        print("  Условий не вернулось.")
        print("  Это либо ошибка сети (см. WARNING выше), либо модель не дала цитат")
        print("  из текста — такие ответы отбрасываются намеренно.")
    else:
        for condition in conditions:
            print(f"  [{condition.field}] {condition.value}")
            print(f"      цитата: {condition.quote}")

    usage = gateway.usage
    print()
    print(
        "Итого: вызовов {0}, из кэша {1}, ошибок {2}, пропущено {3}".format(
            usage.calls, usage.cached, usage.failures, usage.skipped
        )
    )
    if usage.failures and not conditions:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(os.sys.argv[1:] if False else None))
