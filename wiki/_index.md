# L1 — Index

Резервный уровень, если routing не дал результата. Формат: путь + одна строка.

## rules

- `wiki/rules/general.md` — конвенции кода и ведения документации.
- `wiki/rules/llm-usage.md` — как вызывать модели: квоты, каскад, кэш, запрет ротации ключей.
- `wiki/rules/legal.md` — 152-ФЗ/GDPR/ToS: что собираем и что нет.
- `wiki/rules/hr-signal-detection.md` — правила детектора HR-брехни и формат выводов.

## references

- `wiki/references/llm-providers.md` — роли провайдеров, лимиты, что не использовать.
- `wiki/references/data-sources.md` — источники вакансий и данных о компаниях.
- `wiki/references/prior-art.md` — проверенные аналоги и покрытие ТЗ.
- `wiki/references/domain-glossary.md` — термины и сущности домена.

## architecture

- `wiki/architecture/overview.md` — модули, границы, ADR.
- `wiki/architecture/pipeline.md` — этапы обработки вакансии от сбора до Telegram.
- `wiki/architecture/model-routing.md` — LiteLLM, цепочки фолбэков, маршрутизация по задачам.

## workflows

- `wiki/workflows/release.md` — порядок выпуска.
- `wiki/workflows/code-review.md` — ветвление, PR, ревью.
- `wiki/workflows/quota-audit.md` — регулярная сверка лимитов провайдеров.

## memory

- `memory/inbox.md` — черновые наблюдения агента (draft, лимит 20).
