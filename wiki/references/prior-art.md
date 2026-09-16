# Справочник: аналоги и покрытие ТЗ

> Готового решения под ТЗ нет. Рынок распадается на три несвязанных кластера, ценность проекта — в шве между ними.
> Проверено поиском по GitHub 16.09.2026.

## Три кластера рынка

1. **Open-source job-скраперы + LLM-скоринг + Telegram** — 40–60% ТЗ, самая проработанная часть.
2. **SaaS auto-apply и AI-копилоты** (Jobright, Simplify, JobCopilot, Teal) — закрытые, США-центричные, без Telegram и локальных моделей.
3. **Sales-intelligence** (Apollo, Clay, RocketReach, Hunter, Lusha) — здесь контакты людей, но это платный B2B-стек вне job-мониторинга.

## Покрытие требований рынком

| Требование | Покрытие | Комментарий |
| --- | --- | --- |
| Фоновый мониторинг джоббордов | 80–90% | Решённая задача |
| Карьерные страницы компаний | 30–50% | Западные ATS да, российские почти нет |
| Фильтр по параметрам | 60–80% | Обычно «резюме ↔ вакансия» |
| **Детектор HR-брехни** | **~10%** | В пайплайны не встроено |
| **Досье на компанию + отзывы** | **20–30%** | Источники есть, автосбора нет |
| **Руководители, контакты** | **~15%** | Корп. email реалистичен, личный мобильный — нет и незаконно |
| Telegram-выдача | 80–90% | Стандарт |
| Автосмена LLM + локальный фолбэк | инфра 95% | LiteLLM решает, но ни в одном job-агенте не встроено |

## Что изучить в первую очередь

| Проект | ★ | Зачем |
| --- | --- | --- |
| [career-ops](https://github.com/career-ops-hq/career-ops) | 71 793 | Структурированный отчёт A–H со скором 1–5 — ближайший аналог детектора HR-брехни |
| [career-ops-ui](https://github.com/Fighter90/career-ops-ui) | 69 | Форк с адаптерами hh.ru и Habr Career + Greenhouse/Ashby/Lever |
| [autopilot-jobhunt](https://github.com/tarunlnmiit/autopilot-jobhunt) | 208 | 130+ карьерных страниц ночью, LLM-скоринг 0–100, алерты в Telegram |
| [CezaryChodun/FreeLLM](https://github.com/CezaryChodun/FreeLLM) | 6 | LiteLLM-прокси по free-tier — ровно наша схема |
| [litellm-local-config](https://github.com/gaiagent0/litellm-local-config) | 0 | Готовый конфиг Groq + Gemini + OpenRouter → Ollama |
| [awesome-freellm-apis](https://github.com/open-free-llm-api/awesome-freellm-apis) | 3 043 | Справочник по 134+ бесплатным API |

## Полезные примеры кода

| Проект | ★ | О чём |
| --- | --- | --- |
| [hh-ai-agent](https://github.com/fikstt2/hh-ai-agent) | 232 | Playwright + Ollama + llama3 по hh.ru (внимание: антикапча → `[LEG-003]`) |
| [JobSpy](https://github.com/speedyapply/JobSpy) | 4 289 | Скрапер LinkedIn/Indeed/Glassdoor + API-обёртка и MCP-сервер |
| [ai-job-scraper](https://github.com/BjornMelin/ai-job-scraper) | 47 | ScrapeGraph-AI + LangGraph, local-first, SQLite |
| [JobScoutPublic](https://github.com/hardenbr-personal/JobScoutPublic) | 19 | Ежедневный обход + LLM-триаж через GitHub Actions |
| [JobMonitor](https://github.com/spichkinevgeniy/JobMonitor) | 21 | Вакансии из Telegram-каналов, Telethon + PydanticAI |
| [brightdata/trendscan](https://github.com/brightdata/trendscan) | — | Company intelligence — аналог блока обогащения о компании |
| [hh-vacancy-monitor](https://github.com/Chivivivivi/hh-vacancy-monitor), [hh-bot](https://github.com/AgentShekel/hh-bot) | 0–2 | Минимальные прототипы той же идеи |

## Чего не повторять

- [llm-keypool](https://github.com/piyush-tyagi-13/llm-keypool) (65★), [freeflow-llm](https://github.com/TheSecondChance/freeflow-llm) (44★) — ротация ключей одного провайдера, запрещено `[LLM-003]`.
- [freellmapi](https://github.com/tashfeenahmed/freellmapi) (26 579★) — автор прямо пишет «personal experimentation only».
- Пустышки с 0–2★: `HomarUS`, `jobagent`, `JobScout-AI`, `pyhh`, `ParserHHvacancy`, `trinomen` — только как примеры кода.
- Не существуют: `ruslan-automation/hh-auto-apply`, `Ar1temiy/hh-parse`. Названия вроде `Mira`, `ultimate-ai`, `agent-orchestrator` слишком общие и бесполезны как рекомендации.
