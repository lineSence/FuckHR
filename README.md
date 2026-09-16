# FuckHR

> Каркас документации проекта. Разделы с пометкой `TODO` нужно заполнить деталями проекта.

## О проекте

TODO: краткое описание продукта — какую проблему решает, для кого, чем отличается.

## Статус

Этап: инициализация репозитория и документации.

## Структура документации

Документация организована по концепции **Self-Evolving Knowledge (SEK)** — знания разделены на уровни контекста и грузятся «только то, что нужно прямо сейчас».

| Уровень | Что это | Где лежит |
| --- | --- | --- |
| L0 — Bootstrap | Сжатая главная инструкция агента: кто он, какие есть категории знаний, по каким триггерам что грузить | [`AGENTS.md`](./AGENTS.md) |
| L1 — Routing & Index | Машиночитаемая таблица `keywords → files` и оглавление | [`wiki/_routing.md`](./wiki/_routing.md), [`wiki/_index.md`](./wiki/_index.md) |
| L2 — Validated knowledge | Проверенная экспертиза проекта | [`wiki/rules/`](./wiki/rules), [`wiki/references/`](./wiki/references), [`wiki/architecture/`](./wiki/architecture), [`wiki/workflows/`](./wiki/workflows) |
| L3 — Ephemeral state | Конвейер черновых наблюдений агента | [`memory/inbox.md`](./memory/inbox.md) |

Жизненный цикл знания: `draft` → `validated` → `core`. Подробнее — [`docs/knowledge-lifecycle.md`](./docs/knowledge-lifecycle.md).

## Быстрый старт

TODO: требования к окружению, установка зависимостей, запуск, тесты.

## Документы

- [`docs/architecture.md`](./docs/architecture.md) — архитектура (TODO)
- [`docs/knowledge-lifecycle.md`](./docs/knowledge-lifecycle.md) — как растут знания агента
- [`docs/contributing.md`](./docs/contributing.md) — как вести документацию и код
