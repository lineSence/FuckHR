# L1 — Index

Резервный уровень, если routing не дал результата. Формат: путь + одна строка.

## rules

- `wiki/rules/general.md` — конвенции кода и ведения документации.
- `wiki/rules/llm-usage.md` — как вызывать модели: шлюз, профили, квоты, кэш.
- `wiki/rules/legal.md` — 152-ФЗ/GDPR: что собираем и что нет.
- `wiki/rules/hr-signal-detection.md` — правила детектора HR-брехни и формат выводов.
- `wiki/rules/outreach.md` — прямой контакт в обход HR: кому, куда и как писать.

## references

- `wiki/references/llm-providers.md` — роли провайдеров, лимиты, доступность.
- `wiki/references/local-models.md` — железо владельца, локальные модели, настройки Ollama.
- `wiki/references/data-sources.md` — источники вакансий и данных о компаниях.
- `wiki/references/contact-discovery.md` — где искать нанимающего менеджера и рабочий канал.
- `wiki/references/prior-art.md` — проверенные аналоги и покрытие ТЗ.
- `wiki/references/domain-glossary.md` — термины и сущности домена.

## architecture

- `wiki/architecture/overview.md` — модули, границы, ADR.
- `wiki/architecture/pipeline.md` — этапы обработки вакансии от сбора до Telegram.
- `wiki/architecture/model-routing.md` — FreeLLMAPI, профили, маршрутизация по этапам.

## workflows

- `wiki/workflows/release.md` — порядок выпуска.
- `wiki/workflows/code-review.md` — ветвление, PR, ревью.
- `wiki/workflows/quota-audit.md` — регулярная сверка лимитов провайдеров.

## memory

- `memory/inbox.md` — черновые наблюдения агента (draft, лимит 20).
