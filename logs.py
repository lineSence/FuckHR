"""Логи: файл плюс stdout и приглушённые чужие логгеры.

Нижний слой: его зовут все запускалки, поэтому здесь нет импортов проекта.
"""

from __future__ import annotations

import logging
import os
import sys
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Болтливые чужие логгеры. Каждый из них пишет INFO о том, что нам знать не
# надо: httpx — строку на каждый HTTP-запрос (а их сотни за прогон),
# huggingface_hub и gliner — как они ищут и грузят веса при каждом старте.
# В логе из-за этого тонули наши собственные строки, по которым видно ход
# прогона. На --verbose (DEBUG) всё возвращается: чинить поломку без чужих
# строк бывает нечем.
NOISY = (
    "httpx",
    "httpcore",
    "huggingface_hub",
    "filelock",
    "urllib3",
    "gliner",
    "transformers",
    "sentence_transformers",
    "torch",
    "asyncio",
)


def quiet_libraries(verbose: bool = False) -> None:
    """Чужие логгеры — только предупреждения и ошибки.

    Заодно выключаются полоски загрузки Hugging Face: в лог они попадают
    сплошной строкой из «100%|██████████|», а никакой информации в файле не
    несут. Телеметрия выключается по той же причине, по которой её выключают
    везде в этом проекте: лишний запрос в чужой сервис.
    """
    if verbose:
        return
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # FutureWarning от torch.jit и UserWarning про resume_download — чужие
    # предупреждения о чужом коде: сделать с ними мы ничего не можем.
    warnings.filterwarnings("ignore", category=FutureWarning, module=r"torch\..*")
    warnings.filterwarnings("ignore", message=r".*resume_download.*")


def setup_logging(log_path: Path, verbose: bool) -> None:
    """Лог всегда идёт и в файл, и в stdout.

    Строки в stdout — единственный источник обратной связи для интерфейса и
    планировщика: он читает их и по счётчикам вида «[3/30]» рисует полоску.
    Раньше stdout появлялся только при --verbose, и запуск из браузера выглядел
    как зависание. Теперь --verbose меняет только подробность (DEBUG).

    Потоки досье пишут в тот же лог: имя потока в формате нужно, иначе
    переплетённые строки нескольких компаний невозможно различить.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    file_handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s")
    )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[file_handler, stream],
        force=True,
    )
    # После basicConfig: force=True сбрасывает уровни, выставленные до него.
    quiet_libraries(verbose)
