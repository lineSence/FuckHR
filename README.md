# FuckHR

**Personal Labour Market Intelligence Agent** — агент мониторинга рынка труда для одного соискателя.

Фоновый прогон собирает вакансии с российских площадок, отсеивает HR-клише, проверяет работодателя
по отзывам и истории публикаций и присылает карточки в Telegram. Управление — локальный
веб-интерфейс.

Главное — не поиск вакансий, а **проверка правдивости работодателя до отклика**: обещания вакансии
сверяются с внешними данными.

> «Дружная команда» → 23 упоминания переработок в отзывах → вакансия публиковалась 5 раз за 8 месяцев →
> вывод: «утверждение о стабильной команде не подтверждается найденными данными».

Цель — полное досье на компанию и выход на нанимающего менеджера: рабочий канал связи и черновик
письма, который владелец отправляет сам (`[CORE-018]`). Массовых откликов и рассылок нет.

## Запуск

Установка на Windows — [`INSTALL.md`](./INSTALL.md). Дальше:

```powershell
.venv\Scripts\python webui.py            # интерфейс: http://127.0.0.1:8765
.venv\Scripts\python run.py --dry-run    # один прогон без записи и без Telegram
.venv\Scripts\python -m pytest -q        # тесты, без единого сетевого вызова
.venv\Scripts\ruff check .               # линтер, как в CI
```

Без модели прогон доходит до конца — беднее деталями, но полностью (`[CORE-015]`, `[CORE-017]`).
Остальные команды — `python -m fuckhr.<пакет>.<модуль>`, список в [`docs/architecture.md`](./docs/architecture.md).

## Где что

| Нужно | Где |
| --- | --- |
| Как устроено сейчас: пакеты, слои, прогон, хранилище, модели | [`docs/architecture.md`](./docs/architecture.md) |
| Правила для агента и Critical Rules `[CORE-*]` | [`AGENTS.md`](./AGENTS.md) |
| Как вести код и документацию | [`docs/contributing.md`](./docs/contributing.md) |
| Тесты | [`docs/testing.md`](./docs/testing.md) |
| Что сделано и что дальше | [`docs/roadmap.md`](./docs/roadmap.md) |
| Как прошёл рефакторинг структуры | [`docs/refactoring.md`](./docs/refactoring.md) |
| Замысел, ADR, правила и справочники | [`wiki/`](./wiki) — вход через [`wiki/_index.md`](./wiki/_index.md) |
| Черновые наблюдения агента | [`memory/inbox.md`](./memory/inbox.md), цикл знания — [`docs/knowledge-lifecycle.md`](./docs/knowledge-lifecycle.md) |

Функции — отдельными документами в `docs/`: детектор ([`detector.md`](./docs/detector.md)), накрутка
отзывов ([`fake-reviews.md`](./docs/fake-reviews.md)), рынок зарплат
([`market-salary.md`](./docs/market-salary.md)), контакты и письма ([`contacts.md`](./docs/contacts.md)),
цели ([`targets.md`](./docs/targets.md)), профили ([`profiles.md`](./docs/profiles.md)),
источники ([`sources.md`](./docs/sources.md)), бенч моделей ([`model-bench.md`](./docs/model-bench.md)).

Правило одного источника: факт о коде живёт в `docs/`, `wiki/architecture/` хранит замысел и
причины решений и ссылается на `docs/` за текущим состоянием.
