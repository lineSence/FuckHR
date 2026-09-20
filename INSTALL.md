# Установка (шаг 1, Windows 10/11)

Шаг 1 — тонкий срез без LLM: `hh.ru -> SQLite -> скоринг -> карточка в Telegram`.
Ollama, FreeLLMAPI и Node здесь не нужны — они на шаге 2 (см. `docs/mvp-windows.md`).

Все команды — для **PowerShell** (не cmd). Путь без кириллицы и пробелов обязателен.

## 0. Python и Git

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Закрой и открой PowerShell заново (PATH подхватывается только в новой сессии), потом проверь:

```powershell
py -3.12 --version
git --version
```

## 1. Клон и окружение

```powershell
mkdir C:\dev -Force
cd C:\dev
git clone https://github.com/lineSence/FuckHR.git fuckhr
cd C:\dev\fuckhr

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Если `Activate.ps1` отказывается запускаться из-за политики исполнения:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Установка зависимостей (в активном venv, в промпте видно `(.venv)`):

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Разово включи UTF-8 для Python, иначе кириллица в логах и карточках превратится в мойме:

```powershell
setx PYTHONUTF8 1
```

После `setx` нужна новая сессия PowerShell и повторный `.\.venv\Scripts\Activate.ps1`.

## 2. Telegram-бот

1. Напиши [@BotFather](https://t.me/BotFather) команду `/newbot`, получи токен вида `123456:AA...`.
2. Открой своего бота и отправь ему любое сообщение — без этого бот не имеет права писать тебе первым.
3. Узнай `chat_id`:

```powershell
$token = "123456:AA..."
(Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates").result.message.chat.id
```

## 3. Настройки

```powershell
Copy-Item .env.example .env
notepad .env
```

Заполни две строки:

```
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=123456789
```

Остальное по Telegram настраивается на странице «Настройки»: `TELEGRAM_ENABLED` — отправка
целиком (при 0 прогон работает, карточки ждут в интерфейсе), `TELEGRAM_DELAY` — пауза между
сообщениями (0.6 с; меньше 0.5 — Telegram отвечает 429), `TELEGRAM_QUIET_FROM` /
`TELEGRAM_QUIET_TO` — тихие часы в формате ЧЧ:ММ. В тихие часы карточки не уходят и
не помечаются отправленными: они уйдут следующим прогоном. Тревоги канарейки тихие часы
не глушат — сломанный сбор тем и важен, что о нём узнают сразу.

Затем под себя — запросы, стек, вилку, стоп-слова:

```powershell
notepad profile.yaml
```

## 4. Первый запуск

Сначала всухую — ничего не уйдёт в Telegram:

```powershell
python run.py --dry-run --verbose
```

Если список пуст — ослабь `min_score`, расширь `period` или убери часть стоп-слов в `profile.yaml`.

Затем боевой прогон:

```powershell
python run.py --verbose --limit 10
```

Кнопки «Интересно / Мимо» записываются в базу только пока живёт приёмник. В отдельном окне:

```powershell
cd C:\dev\fuckhr
.\.venv\Scripts\Activate.ps1
python bot.py
```

Нажатия без запущенного `bot.py` не теряются навсегда: Telegram держит их в очереди сутки.

## 5. Ежедневный запуск (после того, как карточки устраивают)

```powershell
schtasks /create /tn "FuckHR daily" /tr "C:\dev\fuckhr\.venv\Scripts\pythonw.exe C:\dev\fuckhr\run.py" /sc daily /st 08:00
```

В планировщике задач у задачи важно поставить: «Выполнять независимо от регистрации пользователя», «Запустить задачу как можно скорее после пропуска запуска» и рабочую папку `C:\dev\fuckhr` — иначе относительные пути из `.env` уедут в `C:\Windows\System32`.

Проверка, удаление и ручной запуск задачи:

```powershell
schtasks /query /tn "FuckHR daily"
schtasks /run   /tn "FuckHR daily"
schtasks /delete /tn "FuckHR daily" /f
```

## 6. Диагностика

Лог:

```powershell
Get-Content .\data\fuckhr.log -Tail 40
```

Топ вакансий в базе без внешних инструментов:

```powershell
python -c "import db; c=db.connect('data/fuckhr.sqlite3'); [print(round(r['score'],1), r['title'], '|', r['company']) for r in c.execute('SELECT score,title,company FROM vacancies ORDER BY score DESC LIMIT 10')]"
```

Статистика:

```powershell
python -c "import db; print(db.stats(db.connect('data/fuckhr.sqlite3')))"
```

Сбросить отметки об отправке, чтобы карточки пришли заново:

```powershell
python -c "import db; c=db.connect('data/fuckhr.sqlite3'); c.execute('UPDATE vacancies SET notified_at=NULL'); c.commit()"
```

Полный сброс базы (история публикаций тоже умрёт, а она нужна детектору HR-брехни):

```powershell
Remove-Item .\data\fuckhr.sqlite3*
```

### Типовые ошибки

| Симптом | Причина и лечение |
|---|---|
| `KeyError: 'TELEGRAM_BOT_TOKEN'` | запуск не из папки проекта или нет `.env` |
| `Bad Request: chat not found` | не написал боту первым или неверный `TELEGRAM_CHAT_ID` |
| `sqlite3.OperationalError: database is locked` | открыт DB Browser или два прогона сразу; закрыть лишнее |
| HTTP 403 от hh.ru | слишком частые запросы; поднять `pause` в `HHClient` или урезать `max_pages` |
| кракозябры в консоли | не выставлен `PYTHONUTF8=1` или не перезапущен PowerShell |

## 7. Обновление

```powershell
cd C:\dev\fuckhr
git pull
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`profile.yaml`, `.env` и `data/` при обновлении не трогаются: `.env` и `data/` вне git,
а `profile.yaml` при конфликте придётся слить руками.
