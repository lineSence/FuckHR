# L1 — Routing

Машиночитаемая таблица `keywords → files`. Агент читает её, если задача не покрыта ядром `AGENTS.md`.

```yaml
triggers:
  - id: llm-providers
    keywords: [лимит, квота, rpm, rpd, 429, groq, gemini, cerebras, openrouter, ollama, nvidia nim, провайдер, токены]
    load:
      - wiki/references/llm-providers.md
      - wiki/architecture/model-routing.md

  - id: llm-gateway
    keywords: [шлюз, gateway, фолбэк, fallback, ротация, прокси, circuit breaker, cooldown]
    load:
      - wiki/architecture/model-routing.md
      - wiki/rules/llm-usage.md

  - id: pipeline
    keywords: [пайплайн, предфильтр, дедуп, скоринг, карточка, этап, оркестратор, langgraph]
    load:
      - wiki/architecture/pipeline.md

  - id: outreach
    keywords: [контакт, письмо, аутрич, outreach, обойти hr, напрямую, руководитель отдела, тимлид, нанимающий менеджер, email, follow-up, шаблон письма]
    load:
      - wiki/rules/outreach.md
      - wiki/references/contact-discovery.md
      - wiki/architecture/sources-and-outreach.md
      - wiki/rules/legal.md

  - id: contact-discovery
    keywords: [поиск людей, кто нанимает, habr career, github org, корпоративная почта, валидация email, спикеры, команда компании]
    load:
      - wiki/references/contact-discovery.md
      - wiki/rules/outreach.md

  - id: tech-stack
    keywords: [стек, зависимости, библиотека, альтернатива, выбор технологии]
    load:
      - wiki/architecture/tech-stack.md

  - id: runtime-windows
    keywords: [расписание, планировщик, task scheduler, автозапуск, windows, служба]
    load:
      - wiki/architecture/runtime-windows.md

  - id: hh-html
    keywords: [html hh, разбор страницы, селектор, вёрстка hh, fallback разбора, 403]
    load:
      - wiki/architecture/hh-html-scraping.md

  - id: scraping-sources
    keywords: [hh.ru, hh, джобборд, карьерная страница, greenhouse, lever, ashby, скрейпинг, парсер, капча, open api]
    load:
      - wiki/references/data-sources.md
      - wiki/rules/legal.md

  - id: hr-bullshit-detector
    keywords: [hr-брехня, клише, дружная команда, правдивость, отзывы, dream job, досье, компания]
    load:
      - wiki/architecture/pipeline.md
      - wiki/rules/hr-signal-detection.md

  - id: company-score
    keywords: [оценка работодателя, общая оценка, скоринг компании, светофор компании, красные флаги, закономерности, улика, вето, покрытие данных]
    load:
      - wiki/architecture/company-score.md
      - docs/company-score.md

  - id: embeddings
    keywords: [эмбеддинг, вектор, косинус, похожие вакансии, перефраз, bge-m3, семантика, sqlite-vec]
    load:
      - wiki/architecture/embeddings.md
      - docs/embeddings.md

  - id: deep-research
    keywords: [глубокий ресёрч, ресерч компании, реестр, егрюл, инн, суд, арбитраж, банкротство, приставы, исполнительное производство, новости о компании]
    load:
      - wiki/architecture/deep-research.md
      - docs/deep-research.md

  - id: prompt-injection
    keywords: [инъекция, промпт-инъекция, prompt injection, скрытый текст, инструкция для ии, ignore previous instructions, невидимые символы, гомоглифы]
    load:
      - wiki/architecture/prompt-injection.md
      - docs/prompt-injection.md

  - id: fake-reviews
    keywords: [накрутка, заказные отзывы, фейковые отзывы, fake_score, шингл, дубли отзывов, всплеск отзывов, метка компании]
    load:
      - docs/fake-reviews.md
      - wiki/rules/hr-signal-detection.md

  - id: market-salary
    keywords: [рынок, средняя зарплата, медиана, перцентиль, вилка, ниже рынка, выше рынка, срез, market_stats]
    load:
      - docs/market-salary.md

  - id: ai-text
    keywords: [сгенерированный текст, ии-текст, нейросеть написала, шаблонный текст, ai_text, канцелярит]
    load:
      - docs/ai-text.md

  - id: review-quality
    keywords: [легитимность отзыва, мусор на странице, скрейпинг отзовиков, разбор страницы, шаблон площадки, фикстуры вёрстки]
    load:
      - docs/review-quality.md

  - id: legal
    keywords: [152-фз, gdpr, tos, персональные данные, телефон, linkedin, юридика, osint, спам, отказ от рассылки]
    load:
      - wiki/rules/legal.md
      - wiki/rules/outreach.md

  - id: prior-art
    keywords: [аналог, конкурент, career-ops, autopilot-jobhunt, jobspy, apollo, clay, hunter, готовое решение, рынок]
    load:
      - wiki/references/prior-art.md

  - id: telegram
    keywords: [telegram, тг, бот, уведомление, выдача, дайджест, подтверждение отправки, кнопки]
    load:
      - wiki/architecture/pipeline.md
      - wiki/references/contact-discovery.md

  - id: workflow-release
    keywords: [релиз, деплой, версия, changelog]
    load:
      - wiki/workflows/release.md

  - id: workflow-review
    keywords: [ревью, pr, pull request, коммит, ветка]
    load:
      - wiki/workflows/code-review.md

  - id: workflow-quota-audit
    keywords: [сверка лимитов, аудит квот, проверить провайдеров]
    load:
      - wiki/workflows/quota-audit.md

  - id: rules-general
    keywords: [правило, конвенция, стиль, запрет]
    load:
      - wiki/rules/general.md
```

## Правила ведения

- Один триггер = одна тема; keywords в нижнем регистре, на русском и английском.
- Большие файлы не грузятся «на всякий случай».
- Новый файл в `wiki/` → обновить и `_routing.md`, и `_index.md`.
