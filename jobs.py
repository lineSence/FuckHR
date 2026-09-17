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

Весь вывод копится в памяти (последние MAX_LINES строк) и пишется в файл под
каталогом задач: лог нужен и после перезагрузки страницы.
"""

from __future__ import annotations

import itertools
import logging
import os
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

# Что разрешено запускать. Список закрытый: интерфейс никогда не собирает
# командную строку из того, что пришло из браузера.
TASKS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "collect": (
        "Сбор вакансий",
        ("run.py",),
        "Собирает вакансии, считает скоринг, разбирает условия и HR-флаги, шлёт карточки в Telegram.",
    ),
    "collect-dry": (
        "Сбор без записи",
        ("run.py", "--dry-run"),
        "То же самое, но без записи в базу и без отправки в Telegram.",
    ),
    "outreach": (
        "Подготовка писем",
        ("outreach.py",),
        "Ищет контакты, собирает черновики и пишет их в лог контактов. Отправляешь письма всё равно ты сам.",
    ),
    "outreach-dry": (
        "Письма без записи",
        ("outreach.py", "--dry-run"),
        "Показывает карточки и черновики, ничего не записывая.",
    ),
    "check-llm": (
        "Проверка модели",
        ("check_llm.py", "--live"),
        "Проверяет адреса, ключи и маршруты, делает один живой вызов на выдуманном тексте.",
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

    def tail(self, count: int = 200) -> list[str]:
        return self.lines[-count:]


class Runner:
    """Одна активная задача плюс короткая история запусков."""

    def __init__(self, root: Path | None = None, log_dir: Path | None = None) -> None:
        self.root = Path(root or ROOT)
        self.log_dir = Path(log_dir or LOG_DIR)
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

    def start(self, task: str) -> Job:
        """Запускает задачу из TASKS. Готовая строка из браузера не принимается."""
        if task not in TASKS:
            raise KeyError("неизвестная задача: {}".format(task))
        busy = self.active()
        if busy is not None:
            raise RuntimeError(
                "уже идёт задача «{}», дождись её или останови".format(busy.title)
            )

        title, args, _ = TASKS[task]
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

    def _append(self, job: Job, line: str, handle: object | None) -> None:
        with self._lock:
            job.lines.append(line)
            if len(job.lines) > MAX_LINES:
                del job.lines[:-MAX_LINES]
        if handle is not None:
            handle.write(line + "\n")
            handle.flush()

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
            log.info(
                "задача %s завершена: %s за %.1f с",
                job.task,
                job.status,
                job.duration,
            )


def task_list() -> list[tuple[str, str, str]]:
    """Задачи для кнопок: ключ, название, пояснение."""
    return [(key, title, help_text) for key, (title, _, help_text) in TASKS.items()]


# Единый экземпляр на процесс интерфейса.
runner = Runner()


__all__ = ("HISTORY", "Job", "MAX_LINES", "Runner", "TASKS", "runner", "task_list")
