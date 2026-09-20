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
- `wiki/architecture/tech-stack.md` — замысел стека и рассмотренные альтернативы.
- `wiki/architecture/sources-and-outreach.md` — источники контактов и поисковый API этапа писем.
- `wiki/architecture/runtime-windows.md` — запуск по расписанию на Windows (ADR-014).
- `wiki/architecture/hh-html-scraping.md` — разбор HTML hh.ru и его fallback-стратегии (ADR-015).
- `wiki/architecture/company-score.md` — общая оценка работодателя: оси, улики, вето (ADR-018, замысел).
- `wiki/architecture/prompt-injection.md` — инъекции в чужих текстах: чистка входа, улика, скрытый HTML (ADR-020, замысел).
- `wiki/architecture/embeddings.md` — локальные эмбеддинги: модель, хранение, две задачи (ADR-021).
- `wiki/architecture/deep-research.md` — глубокий ресёрч по компании: источники, капча, потолок по времени (ADR-019, замысел).

## workflows

- `wiki/workflows/release.md` — порядок выпуска.
- `wiki/workflows/code-review.md` — ветвление, PR, ревью.
- `wiki/workflows/quota-audit.md` — регулярная сверка лимитов провайдеров.

## memory

- `memory/inbox.md` — черновые наблюдения агента (draft, лимит 20).
