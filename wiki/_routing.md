# L1 — Routing

Машиночитаемая таблица `keywords → files`. Агент читает её, если задача не покрыта ядром `AGENTS.md`.

```yaml
triggers:
  - id: llm-providers
    keywords: [лимит, квота, rpm, rpd, 429, groq, gemini, cerebras, openrouter, ollama, nvidia nim, провайдер, токены]
    load:
      - wiki/references/llm-providers.md
      - wiki/architecture/model-routing.md

  - id: litellm
    keywords: [litellm, фолбэк, fallback, ротация, прокси, circuit breaker, cooldown]
    load:
      - wiki/architecture/model-routing.md
      - wiki/rules/llm-usage.md

  - id: pipeline
    keywords: [пайплайн, предфильтр, дедуп, скоринг, карточка, этап, оркестратор, langgraph]
    load:
      - wiki/architecture/pipeline.md

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

  - id: legal
    keywords: [152-фз, gdpr, tos, персональные данные, телефон, linkedin, юридика, osint]
    load:
      - wiki/rules/legal.md

  - id: prior-art
    keywords: [аналог, конкурент, career-ops, autopilot-jobhunt, jobspy, готовое решение, рынок]
    load:
      - wiki/references/prior-art.md

  - id: telegram
    keywords: [telegram, тг, бот, уведомление, выдача, дайджест]
    load:
      - wiki/architecture/pipeline.md

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
