# FuckHR

**Personal Labour Market Intelligence Agent** — агентная система мониторинга рынка труда для соискателя.

## Зачем это

Фоновые агенты мониторят джобборды и карьерные страницы компаний, отсеивают HR-клише, обогащают данные о компании (отзывы, публичные контакты) и отправляют подходящие вакансии в Telegram. При исчерпании бесплатных лимитов система сама переключает LLM-провайдера или уходит на локальную модель.

## Ключевая ценность

Не поиск вакансий (там конкуренция с hh.ru проиграна заранее), а **проверка правдивости работодателя до отклика**: сопоставление обещаний вакансии с внешними данными.

> «Дружная команда» → 23 упоминания переработок в отзывах → вакансия публиковалась 5 раз за 8 месяцев → вывод: «утверждение о стабильной команде не подтверждается найденными данными».

Метрика успеха — не «500 откликов в день», а **«5 вакансий с полным досье»**.

## Статус

Инициализация: анализ аналогов и провайдеров завершён, кода пока нет. Готового решения на рынке под ТЗ нет — см. [`wiki/references/prior-art.md`](./wiki/references/prior-art.md).

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
LiteLLM                                     — оркестратор моделей, один endpoint
        ↓
Groq → Gemini Flash → Cerebras → OpenRouter :free → Ollama
```

Плюс: Telegram Bot API, hh.ru Open API, SQLite/Postgres, APScheduler.

## Документы

- [`docs/architecture.md`](./docs/architecture.md) — архитектура для людей
- [`docs/roadmap.md`](./docs/roadmap.md) — шаги до первого работающего контура
- [`docs/knowledge-lifecycle.md`](./docs/knowledge-lifecycle.md) — как растут знания агента
- [`docs/contributing.md`](./docs/contributing.md) — как вести код и документацию
