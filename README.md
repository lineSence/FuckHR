# FuckHR

**Personal Labour Market Intelligence Agent** — агентная система мониторинга рынка труда для соискателя.

## Зачем это

Фоновый прогон собирает вакансии, отсеивает HR-клише, проверяет работодателя по отзывам и
истории публикаций и присылает карточки в Telegram. Управление — локальный веб-интерфейс.

## Ключевая ценность

Не поиск вакансий (там конкуренция с hh.ru проиграна заранее), а **проверка правдивости работодателя до отклика**:
сопоставление обещаний вакансии с внешними данными.

> «Дружная команда» → 23 упоминания переработок в отзывах → вакансия публиковалась 5 раз за 8 месяцев →
> вывод: «утверждение о стабильной команде не подтверждается найденными данными».

Отзывы, похожие на заказные, в оценку не идут, а сама накрутка — отдельный сигнал о компании
(`docs/fake-reviews.md`).

Зарплата вакансии сравнивается с медианой похожих вакансий из собственных наблюдений за полгода:
«ниже рынка» и «вилки нет почти нигде» — такие же факты о работодателе, как переработки
(`docs/market-salary.md`).

Метрика успеха — не «500 откликов в день», а **«5 вакансий с полным досье»**.

## Статус

Работает. Прогон `run.py` доходит от сбора до карточек, интерфейс `webui.py` запускает задачи и правит настройки,
тесты идут без единого сетевого вызова (`docs/testing.md`).

```powershell
.venv\Scripts\python webui.py            # http://127.0.0.1:8765
.venv\Scripts\python run.py --dry-run    # один прогон без записи и без Telegram
.venv\Scripts\python -m pytest -q        # тесты
```

## Стек — фактический

```txt
Task Scheduler (pythonw.exe)
        ↓
run.py — один прогон, последовательный, без оркестратора
        ↓
hh.ru (HTML поиска, ADR-015) → score.py → db.py (SQLite)
        ↓                                      ↓
  detector.py (история публикаций)      dossier.py + websearch.py + reviewpage.py
                                       + fake_reviews.py (накрученные отзывы)
        ↓                                      ↓
                     bot.py → Telegram
```

Зависимости: `httpx`, `pydantic`, `PyYAML`, `rapidfuzz`, `aiogram`, `python-dotenv`, `dnspython`.
Всё остальное — стандартная библиотека, включая веб-интерфейс на `http.server`.

Модели идут через собственный шлюз `llm.py`: локальный адрес (FreeLLMAPI / Ollama) и
необязательный внешний прокси LiteLLM (ADR-005, ADR-017). Этапы `contacts`, `dossier`, `draft`
остаются локальными, пока владелец явно не разрешит обратное (`[CORE-012]`).

Без модели прогон доходит до конца — беднее деталями, но полностью (`[CORE-015]`, `[CORE-017]`).

## Чего в коде нет, хотя оно есть в ранних ADR

| Что | Статус |
|---|---|
| LangGraph + checkpoint (ADR-007) | не внедрено: паузы на подтверждение в пайплайне нет, письма готовит `outreach.py` по явной команде |
| hh.ru Open API (ADR-003) | заменено на разбор HTML: публичный `GET /vacancies` с апреля 2026 отдаёт 403 (ADR-015) |
| `sqlite-vec`, эмбеддинги, семантический дедуп (ADR-008) | не внедрено: дедуп по ключу `(company, external_id)`, векторов нет |
| `instructor`, structured output | не внедрено: ответы модели разбираются вручную в `llm_tasks.py` |
| Playwright, карьерные страницы | не внедрено: страницы отзывов читаются обычным HTTP (`reviewpage.py`) |
| systemd timers | заменено на Task Scheduler: система живёт на Windows (ADR-014) |

## Структура документации

Документация организована по концепции **Self-Evolving Knowledge (SEK)**: знания разделены по уровням контекста,
грузится только то, что нужно для текущей задачи.

| Уровень | Что это | Где лежит |
| --- | --- | --- |
| L0 — Bootstrap | Сжатая инструкция агента и Critical Rules | [`AGENTS.md`](./AGENTS.md) |
| L1 — Routing & Index | `keywords → files` и оглавление | [`wiki/_routing.md`](./wiki/_routing.md), [`wiki/_index.md`](./wiki/_index.md) |
| L2 — Validated | Правила, справочники, архитектура, процессы | [`wiki/`](./wiki) |
| L3 — Ephemeral | Конвейер черновых наблюдений агента | [`memory/inbox.md`](./memory/inbox.md) |

Жизненный цикл знания: `draft → validated → core`, см. [`docs/knowledge-lifecycle.md`](./docs/knowledge-lifecycle.md).

## Документы

- [`INSTALL.md`](./INSTALL.md) — установка и первый запуск
- [`docs/architecture.md`](./docs/architecture.md) — как устроено на самом деле
- [`docs/roadmap.md`](./docs/roadmap.md) — что сделано и что дальше
- [`docs/testing.md`](./docs/testing.md) — тесты
- [`docs/detector.md`](./docs/detector.md) — детектор HR-брехни
- [`docs/market-salary.md`](./docs/market-salary.md) — рынок зарплат и метки отклонений
- [`docs/ai-text.md`](./docs/ai-text.md) — признаки сгенерированного текста
- [`docs/contacts.md`](./docs/contacts.md) — поиск контактов и письма
- [`docs/knowledge-lifecycle.md`](./docs/knowledge-lifecycle.md) — как растут знания агента
- [`docs/contributing.md`](./docs/contributing.md) — как вести код и документацию
