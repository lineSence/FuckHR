# FuckHR

**Personal Labour Market Intelligence Agent** — агентная система мониторинга рынка труда для соискателя.

## Зачем это

Фоновые агенты мониторят джобборды и карьерные страницы компаний, отсеивают HR-клише, обогащают данные о компании (отзывы, публичные контакты) и отправляют подходящие вакансии в Telegram. При исчерпании бесплатных лимитов система сама переключает LLM-провайдера или уходит на локальную модель.

## Ключевая ценность

Не поиск вакансий (там конкуренция с hh.ru проиграна заранее) и даже не карточка «подходящей вакансии», а **выход на человека, который принимает решение о найме**: проверка правдивости работодателя до отклика плюс рабочий контакт нанимающего менеджера и черновик письма. Отправляет письмо владелец вручную.

> «Дружная команда» → 23 упоминания переработок в отзывах → вакансия публиковалась 5 раз за 8 месяцев → вывод: «утверждение о стабильной команде не подтверждается найденными данными».

Метрика успеха — не «500 откликов в день», а **«5 вакансий с полным досье и живым контактом»** (`[CORE-018]`).

## Статус (16.09.2026)

Работает «ходячий скелет» — шаг 1 из [`docs/mvp-windows.md`](./docs/mvp-windows.md):

```txt
hh.ru (HTML поиска) → SQLite + слепки → предфильтр → скоринг без LLM → карточка в Telegram
```

Файлы: `hh_html.py`, `hh.py`, `db.py`, `score.py`, `bot.py`, `run.py`, `probe_hh.py`, `profile.yaml`.

Ещё не сделано: запуск по расписанию, LLM-слой, детектор HR-брехни, contact discovery, черновики писем. Готового решения на рынке под ТЗ нет — см. [`wiki/references/prior-art.md`](./wiki/references/prior-art.md).

## Структура документации

Документация организована по концепции **Self-Evolving Knowledge (SEK)**: знания разделены на уровни контекста, грузится только то, что нужно для текущей задачи.

| Уровень | Что это | Где лежит |
| --- | --- | --- |
| L0 — Bootstrap | Сжатая инструкция агента и Critical Rules | [`AGENTS.md`](./AGENTS.md) |
| L1 — Routing & Index | `keywords → files` и оглавление | [`wiki/_routing.md`](./wiki/_routing.md), [`wiki/_index.md`](./wiki/_index.md) |
| L2 — Validated | Правила, справочники, архитектура, процессы | [`wiki/`](./wiki) |
| L3 — Ephemeral | Конвейер черновых наблюдений агента | [`memory/inbox.md`](./memory/inbox.md) |

Жизненный цикл знания: `draft → validated → core`, см. [`docs/knowledge-lifecycle.md`](./docs/knowledge-lifecycle.md).

## Стек

```txt
LangGraph (или своя Python state machine)   — оркестратор логики
        ↓
FreeLLMAPI                                  — единый LLM-endpoint ([CORE-010])
        ↓
Groq → Gemini Flash → Cerebras → OpenRouter :free → Ollama
```

LiteLLM — резервная замена шлюза, а не второй endpoint.

Плюс: Telegram Bot API, hh.ru через HTML-страницы (Open API закрыт, ADR-015), SQLite, Task Scheduler на Windows (ADR-014).

## Документы

- [`docs/architecture.md`](./docs/architecture.md) — архитектура для людей
- [`docs/mvp-windows.md`](./docs/mvp-windows.md) — короткий путь до работающего контура на Windows
- [`docs/roadmap.md`](./docs/roadmap.md) — шаги от скелета до цели
- [`docs/knowledge-lifecycle.md`](./docs/knowledge-lifecycle.md) — как растут знания агента
- [`docs/contributing.md`](./docs/contributing.md) — как вести код и документацию
