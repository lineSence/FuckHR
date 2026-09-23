"""Диагностика сбора с hh.ru: что именно отдаёт сайт и что из этого удалось разобрать.

Запуск:  python probe_hh.py "python разработчик"

Скрипт кладёт сырую страницу в data/probe.html и печатает отчёт: какой стратегией
взят JSON, сколько найдено вакансий, какие ключи у первой. Если структура страницы
поменялась, этот отчёт — всё, что нужно, чтобы починить маппинг.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import hh_html


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
    text = sys.argv[1] if len(sys.argv) > 1 else "python разработчик"

    client = hh_html.HHHtmlClient(
        pause=float(os.getenv("HH_PAUSE", "2.0")),
        cookie=os.getenv("HH_COOKIE") or None,
        proxy=os.getenv("HH_PROXY") or None,
    )
    try:
        body = client.fetch(
            hh_html.SEARCH_URL,
            {"text": text, "area": 113, "items_on_page": 20, "page": 0},
        )
    except hh_html.BlockedError as exc:
        print("ЗАБЛОКИРОВАНО:", exc)
        return 2
    finally:
        client.close()

    out = Path("data/probe.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"страница сохранена: {out} ({len(body)} символов)")

    for index, pattern in enumerate(hh_html.STATE_PATTERNS):
        print(f"стратегия {index}: {'есть совпадение' if pattern.search(body) else 'нет'}")

    try:
        state = hh_html.extract_state(body)
    except hh_html.ExtractionError as exc:
        print("JSON состояния не найден:", exc)
        cards = hh_html.parse_cards_fallback(body)
        print(f"резервный разбор разметки: {len(cards)} карточек")
        for card in cards[:5]:
            print("  ", card.external_id, card.title)
        return 1

    print("корневые ключи состояния:", sorted(state)[:30])
    nodes = hh_html.find_vacancy_nodes(state)
    print(f"найдено узлов, похожих на вакансию: {len(nodes)}")
    if nodes:
        # Без среза: в прошлый раз обрезанный список ключей спрятал ответ —
        # график лежал под именем «@workSchedule» в самом начале сортировки.
        print("ключи первого узла:", sorted(nodes[0]))
        print("поля условий в узле:")
        for key in ("workFormat", "workSchedule", "schedule", "workScheduleByDays",
                    "workingHours", "employment", "employmentForm", "workExperience",
                    "experience", "keySkills"):
            raw = hh_html.first_of(nodes[0], key)
            if raw is not None:
                print("  {:<20} {}".format(key, json.dumps(raw, ensure_ascii=False)[:120]))
        Path("data/probe_node.json").write_text(
            json.dumps(nodes[0], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("первый узел целиком: data/probe_node.json")
        for node in nodes[:5]:
            vacancy = hh_html.node_to_vacancy(node)
            print(
                f"  {vacancy.external_id} | {vacancy.title} | {vacancy.company} | "
                f"{vacancy.salary_from}-{vacancy.salary_to} {vacancy.currency} | "
                f"график: {vacancy.schedule} | занятость: {vacancy.employment} | "
                f"опыт: {vacancy.experience}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
