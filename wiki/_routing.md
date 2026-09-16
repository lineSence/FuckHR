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
    keywords: [freellmapi, шлюз, gateway, base_url, litellm, фолбэк, fallback, ротация ключей, прокси, circuit breaker, cooldown, профиль local-only]
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
      - wiki/rules/legal.md

  - id: contact-discovery
    keywords: [поиск людей, кто нанимает, habr career, github org, корпоративная почта, валидация email, спикеры, команда компании]
    load:
      - wiki/references/contact-discovery.md
      - wiki/rules/outreach.md

  - id: scraping-sources
    keywords: [hh.ru, hh, джобборд, карьерная страница, greenhouse, lever, ashby, скрейпинг, парсер, капча, open api, 403, cookie, initialstate, probe_hh]
    load:
      - wiki/architecture/hh-html-scraping.md
      - wiki/references/data-sources.md
      - wiki/rules/legal.md

  - id: runtime-windows
    keywords: [windows, расписание, task scheduler, автозапуск, pythonw, systemd, wsl, docker, utf-8, sqlite-vec, блокировка базы]
    load:
      - wiki/architecture/runtime-windows.md

  - id: hr-bullshit-detector
    keywords: [hr-брехня, клише, дружная команда, правдивость, отзывы, dream job, досье, компания, слепки, снапшот, история публикаций, перепубликация, текучка]
    load:
      - wiki/architecture/pipeline.md
      - wiki/rules/hr-signal-detection.md

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
