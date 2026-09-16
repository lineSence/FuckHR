# L1 — Routing

Машиночитаемая таблица `keywords → files`. Агент читает этот файл, если задача не покрыта ядром `AGENTS.md`.

```yaml
triggers:
  - id: architecture
    keywords: [архитектура, модуль, схема, поток данных, adr, architecture]
    load:
      - wiki/architecture/overview.md

  - id: domain
    keywords: [вакансия, резюме, кандидат, отклик, hr, парсинг]
    load:
      - wiki/references/domain-glossary.md

  - id: workflow-release
    keywords: [релиз, деплой, версия, changelog, release]
    load:
      - wiki/workflows/release.md

  - id: workflow-review
    keywords: [ревью, pr, pull request, коммит, ветка]
    load:
      - wiki/workflows/code-review.md

  - id: rules-general
    keywords: [правило, конвенция, стиль, запрет, лимит]
    load:
      - wiki/rules/general.md

  # TODO: добавить триггеры по мере роста проекта (frontend, backend, integrations, ...)
```

## Правила ведения

- Один триггер = одна тема. Keywords пишем в нижнем регистре, на русском и английском.
- Большие файлы не грузятся «на всякий случай» — только по совпавшему триггеру.
- При добавлении файла в `wiki/` обновляем и `_routing.md`, и `_index.md`.
