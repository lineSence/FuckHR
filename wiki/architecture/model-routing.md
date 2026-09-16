# Архитектура: маршрутизация моделей

## Стек

```txt
LangGraph (или своя Python state machine)  — оркестратор логики
        ↓
LiteLLM                                    — оркестратор моделей, один endpoint
        ↓
Groq → Gemini Flash → Cerebras → OpenRouter :free → Ollama
```

## Маршрутизация по этапам

Общий порядок фолбэков для всех шагов — ошибка `[LLM-002]`.

| Этап пайплайна | Основной | Фолбэк |
| --- | --- | --- |
| Предфильтр и дедуп | Python, без LLM | — |
| Извлечение полей из HTML | Groq | Cerebras → Ollama |
| Фильтрация HR-брехни | Groq | Gemini Flash |
| Семантический матч | Gemini Flash | OpenRouter |
| Исследование компании, отзывы | Gemini Flash (длинный контекст) | Cerebras → Ollama |
| Эмбеддинги и похожие вакансии | Cloudflare / локальный bge | — |
| Финальный скоринг и карточка | Gemini Flash | OpenRouter → Ollama |

Если Gemini AI Studio недоступен из сети пользователя — Groq становится основным «мозгом», а длинный контекст режется чанками.

## Четыре обязательные меры

1. **Каскад по сложности** — дешёвые модели отсеивают 80–90%.
2. **Детерминированный код вместо LLM**, где возможно.
3. **Кэш на диске по хэшу промпта** — 60–80% запросов повторные.
4. **Свой счётчик квот + circuit breaker + cooldown**, очередь с backoff, параллелизм 1–2.

## Ориентиры для конфига

Примеры готовых конфигов LiteLLM — `CezaryChodun/FreeLLM`, `gaiagent0/litellm-local-config` (см. `references/prior-art.md`).
Лимиты и роли провайдеров — `references/llm-providers.md`.
