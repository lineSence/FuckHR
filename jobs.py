"""Фоновые задачи для веб-интерфейса.

Зачем подпроцесс, а не вызов run.main() в потоке. Три причины, и все три
проявляются на первом же реальном прогоне:

1. Сбор живёт минутами и падает от сети. Исключение в потоке сервера убивает
   интерфейс, из которого ты смотришь, почему всё упало.
2. Задачу надо уметь остановить. Поток в Python прервать нельзя, процесс — можно.
3. Модули читают настройки на старте. Новый процесс берёт свежий .env без
   перезапуска интерфейса и без плясок вокруг перезагрузки модулей.

Одновременно идёт ровно одна задача. Два параллельных сбора бьются за блокировку
sqlite и вдвое быстрее приводят к капче на hh.ru.

Весь вывод копится в памяти (последние MAX_LINES строк), пишется в файл под
каталогом задач и дублируется в терминал, из которого запущен интерфейс:
браузер удобен, но живой поток строк в консоли отвечает на вопрос «оно вообще живое?»
быстрее любой страницы.

Прогресс не сообщается задачей явно — он вычитывается из её же логов: строки вида
«[3/30]» или «запрос 3/30». Сознательный компромисс: никакого протокола между
процессами придумывать не надо, а если счётчика в логах нет, интерфейс просто
покажет неопределённую полоску вместо вранья про проценты.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

log = logging.getLogger("jobs")

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "data" / "jobs"
MAX_LINES = 4000
HISTORY = 20

# Счётчики в логах. Сначала ищется явная форма в скобках, потом любое n/m:
# второй вариант чаще ловит лишнее, поэтому он запасной.
PROGRESS_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")
PROGRESS_FALLBACK_RE = re.compile(r"(?<![\d.])(\d+)\s*/\s*(\d+)(?![\d.])")

# Что разрешено запускать. Список закрытый: интерфейс никогда не собирает
# командную строку из того, что пришло из браузера.
TASKS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "collect": (
        "Сбор вакансий",
        ("run.py",),
        "Собирает вакансии, считает скоринг, условия, HR-флаги, досье и рабочие контакты, шлёт карточки в Telegram.",
    ),
    "collect-dry": (
        "Сбор без записи",
        ("run.py", "--dry-run"),
        "То же самое, но без записи в базу и без отправки в Telegram. Запускается из командной строки: python run.py --dry-run",
    ),
    "outreach": (
        "Подготовка писем",
        ("outreach.py",),
        "Собирает черновики по контактам, найденным при сборе. Запускается из командной строки: python outreach.py",
    ),
    "outreach-dry": (
        "Письма без записи",
        ("outreach.py", "--dry-run"),
        "Показывает карточки и черновики, ничего не записывая. Запускается из командной строки: python outreach.py --dry-run",
    ),
    # Компанию задаёт кнопка на странице досье: без неё команда бессмысленна,
    # поэтому на странице запуска кнопки у задачи нет.
    "research-deep": (
        "Глубокий ресёрч",
        ("research_deep.py",),
        "Собирает по одной компании реестр, суды, долги и новости. Запускается кнопкой на странице досье.",
    ),
    # Цель и шаг задаёт кнопка на странице цели: в argv уходит только id из
    # своей базы, название компании берётся оттуда же.
    "target-scan": (
        "Шаг по цели",
        ("target_scan.py",),
        "Собирает всё по цели: вакансии, адреса для карты, отзывы с оценкой и глубокий ресёрч. Запускается кнопкой в разделе «Цели».",
    ),
    # Старые записи собирались до карты и координат не имеют. Кнопка живёт на
    # странице карты: там же видно, скольких точек не хватает.
    "geo-backfill": (
        "Адреса для карты",
        ("geo_backfill.py", "--all"),
        "Добирает адреса и координаты у всех вакансий без точки. Ходит на hh.ru по одной странице с паузами, поэтому на тысяче адресов идёт часами. Запускается кнопкой на странице «Карта».",
    ),
    "rebuild": (
        "Пересчёт базы",
        ("rebuild.py",),
        "Применяет сегодняшние правила к уже собранным вакансиям: скор по текущему профилю, инъекции, утверждения, оценка компаний, векторы. Сеть не трогается, hh.ru не опрашивается.",
    ),
    "check-llm": (
        "Проверка модели",
        ("check_llm.py", "--live"),
        "Проверяет адреса, ключи и маршруты, делает один живой вызов на выдуманном тексте.",
    ),
    # Модели задаёт форма на странице «Модель»: без них команда не имеет смысла,
    # поэтому кнопки на странице запуска у задачи нет.
    "bench": (
        "Сравнение моделей",
        ("bench.py",),
        "Гоняет одинаковые задачи пайплайна на нескольких моделях и считает баллы.",
    ),
    # Потолок примеров задаёт форма на странице «Модель»: кнопка живёт там же,
    # где видно, что в базе вообще есть.
    "dataset": (
        "Датасет для дообучения",
        ("dataset_export.py",),
        "Собирает примеры для файнтюна из своей базы: запросы как в проде, ответы уже принятые пайплайном. Сеть и модель не трогаются. Запускается кнопкой на странице «Модель».",
    ),
    "gate-report": (
        "Цена ворот на карточки",
        ("gate_report.py",),
        "Считает по своей базе, сколько карточек не качалось бы при PREFILTER_DETAILS_DELTA 5, 10 и 15 и что при этом потерял бы детектор. Сеть не трогается.",
    ),
    "tests": (
        "Тесты",
        ("-m", "pytest", "-q"),
        "Прогон всех тестов. Сеть не трогается, вызовов модели нет.",
    ),
}

_counter = itertools.count(1)


@dataclass
class Job:
    """Один запуск. Состояние меняется из потока-читателя под замком."""

    id: int
    task: str
    title: str
    argv: tuple[str, ...]
    started_at: float
    lines: list[str] = field(default_factory=list)
    finished_at: float | None = None
    code: int | None = None
    error: str | None = None
    log_path: Path | None = None
    process: object | None = None
    stopped: bool = False
    done: int | None = None
    total: int | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def duration(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    @property
    def status(self) -> str:
        if self.running:
            return "идёт"
        if self.stopped:
            return "остановлена"
        if self.error:
            return "ошибка запуска"
        if self.code == 0:
            return "готово"
        return "завершилась с кодом {}".format(self.code)

    @property
    def progress(self) -> tuple[int, int] | None:
        """(сделано, всего) или None, если задача не сообщала счёта."""
        if self.done is None or not self.total:
            return None
        return min(self.done, self.total), self.total

    @property
    def percent(self) -> int | None:
        pair = self.progress
        if pair is None:
            return None
        done, total = pair
        return int(round(100.0 * done / total))

    def tail(self, count: int = 200) -> list[str]:
        return self.lines[-count:]


def parse_progress(line: str) -> tuple[int, int] | None:
    """Извлекает счётчик из строки лога. Ноль и обратный счёт игнорируются."""
    for pattern in (PROGRESS_RE, PROGRESS_FALLBACK_RE):
        match = pattern.search(line)
        if match is None:
            continue
        done, total = int(match.group(1)), int(match.group(2))
        if total > 0 and done <= total:
            return done, total
    return None


class Runner:
    """Одна активная задача плюс короткая история запусков."""

    def __init__(
        self,
        root: Path | None = None,
        log_dir: Path | None = None,
        echo: bool = True,
    ) -> None:
        self.root = Path(root or ROOT)
        self.log_dir = Path(log_dir or LOG_DIR)
        self.echo = echo
        self._lock = threading.Lock()
        self._jobs: list[Job] = []

    # —— чтение состояния ——

    def active(self) -> Job | None:
        with self._lock:
            for job in reversed(self._jobs):
                if job.running:
                    return job
        return None

    def history(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            for job in self._jobs:
                if job.id == job_id:
                    return job
        return None

    def last(self, task: str | None = None) -> Job | None:
        with self._lock:
            for job in reversed(self._jobs):
                if task is None or job.task == task:
                    return job
        return None

    # —— управление ——

    def start(self, task: str, extra: Sequence[str] = ()) -> Job:
        """Запускает задачу из TASKS. Готовая строка из браузера не принимается.

        extra — уже проверенные вызывающим аргументы (сейчас это только имена
        моделей для bench.py). Сама команда всё равно берётся из TASKS: из
        браузера не должно приходить ничего, что попадёт в argv[0].
        """
        if task not in TASKS:
            raise KeyError("неизвестная задача: {}".format(task))
        busy = self.active()
        if busy is not None:
            raise RuntimeError(
                "уже идёт задача «{}», дождись её или останови".format(busy.title)
            )

        title, args, _ = TASKS[task]
        args = tuple(args) + tuple(extra)
        job = Job(
            id=next(_counter),
            task=task,
            title=title,
            argv=tuple(args),
            started_at=time.time(),
        )
        self.log_dir.mkdir(parents=True, exist_ok=True)
        job.log_path = self.log_dir / "{}-{}.log".format(job.id, task)

        with self._lock:
            self._jobs.append(job)
            del self._jobs[:-HISTORY]

        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def stop(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None or not job.running or job.process is None:
            return False
        job.stopped = True
        try:
            job.process.terminate()
        except OSError:
            return False
        return True

    # —— внутреннее ——

    def _echo(self, job: Job, line: str) -> None:
        """Дублирует строку в терминал интерфейса.

        Падение печати не должно ронять задачу: на Windows консоль может не взять
        символ, а если интерфейс запущен службой, stdout вообще может быть закрыт.
        """
        if not self.echo:
            return
        try:
            sys.stdout.write("[{}] {}\n".format(job.task, line))
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 — терминал — удобство, а не обязательство
            pass

    def _append(self, job: Job, line: str, handle: object | None) -> None:
        counter = parse_progress(line)
        with self._lock:
            job.lines.append(line)
            if len(job.lines) > MAX_LINES:
                del job.lines[:-MAX_LINES]
            if counter is not None:
                job.done, job.total = counter
        if handle is not None:
            handle.write(line + "\n")
            handle.flush()
        self._echo(job, line)

    def _run(self, job: Job) -> None:
        command = [sys.executable, "-u", *job.argv]
        env = dict(os.environ)
        # Вывод в UTF-8 независимо от кодовой страницы Windows: иначе русские
        # логи приезжают в браузер кракозябрами.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        handle = None
        try:
            if job.log_path is not None:
                handle = job.log_path.open("w", encoding="utf-8")
            self._append(job, "$ {}".format(" ".join(job.argv)), handle)
            process = subprocess.Popen(
                command,
                cwd=str(self.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            job.process = process
            if process.stdout is not None:
                for raw in process.stdout:
                    self._append(job, raw.rstrip("\n"), handle)
            job.code = process.wait()
        except Exception as exc:  # noqa: BLE001 — ошибка запуска — тоже результат
            log.exception("задача %s не запустилась", job.task)
            job.error = str(exc)
            self._append(job, "ошибка запуска: {}".format(exc), handle)
        finally:
            job.finished_at = time.time()
            if handle is not None:
                handle.close()
            summary = "задача {} — {} за {:.1f} с".format(
                job.task, job.status, job.duration
            )
            log.info("%s", summary)
            self._echo(job, summary)


# Задачи, у которых есть своё место или своя форма: на странице запуска кнопки
# им не нужны. Главная — это то, что запускаешь регулярно; всё остальное живёт
# рядом с данными, к которым относится:
#
# - шаг по цели — кнопка в разделе «Цели» (без цели он бессмыслен);
# - адреса для карты — кнопка на странице «Карта», где видно, скольких точек нет;
# - прогоны без записи и подготовка писем — редкие отладочные сценарии,
#   они остаются в командной строке и не занимают место на главной.
#
# Из TASKS они не удалены: запуск из своего раздела и через CLI должен работать.
HIDDEN_TASKS = frozenset(
    {
        "bench",
        "research-deep",
        "collect-dry",
        "outreach",
        "outreach-dry",
        "target-scan",
        "geo-backfill",
        "dataset",
    }
)

# Старое имя того же списка: на него ссылаются тесты и старый код.
FORM_TASKS = HIDDEN_TASKS


def task_list() -> list[tuple[str, str, str]]:
    """Задачи для кнопок на странице запуска: ключ, название, пояснение."""
    return [
        (key, title, help_text)
        for key, (title, _, help_text) in TASKS.items()
        if key not in HIDDEN_TASKS
    ]


# Единый экземпляр на процесс интерфейса.
runner = Runner()


__all__ = (
    "HIDDEN_TASKS",
    "HISTORY",
    "Job",
    "MAX_LINES",
    "Runner",
    "TASKS",
    "parse_progress",
    "runner",
    "FORM_TASKS",
    "task_list",
)
