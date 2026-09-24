"""Диагностический прогон: подробный след одного запуска в один файл.

Зачем отдельно от лога. Обычный лог написан для человека: строки, пороги,
ошибки. Когда что-то идёт не так — «собрал не то», «баллы одинаковые», «модель
молчит», — по нему приходится гадать, потому что в нём нет чисел, по которым
можно проверить гипотезу: какой запрос ушёл на площадку, сколько узлов нашлось
на странице, из чего сложился балл каждой вакансии, что ответили гейты.

Здесь это пишется машиночитаемо: JSONL, событие на строку, плюс шапка с
настройками и итог. Файл кладётся в `data/diag/` и рассчитан на то, чтобы его
целиком отдать на разбор — себе через месяц или помощнику.

Что НЕ попадает в файл [CORE-012], [CORE-013]:

- секреты из окружения: ключи, токены, куки, пароли, прокси с логином;
- контакты людей: письма, телефоны, ФИО из досье и контактов;
- тексты отзывов и описаний целиком — только длина и признаки.

Выключено по умолчанию и ничего не стоит: без `DIAG_RUN=1` (или флага
`--diag`) каждая запись — это проверка одного булева поля [CORE-017]. Размер
ограничен: `MAX_EVENTS` строк, дальше пишется только итог, чтобы ночной прогон
на тысячу вакансий не оставил гигабайт.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIR = Path("data/diag")
MAX_EVENTS = 5000
MAX_TEXT = 200

# Что вырезаем из окружения: подстрока в имени переменной.
SECRET_PARTS = ("key", "token", "secret", "password", "cookie", "proxy", "dsn", "webhook")


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_env() -> dict[str, str]:
    """Настройки без секретов: значение секрета заменяется на «есть»/«нет»."""
    out: dict[str, str] = {}
    for name, value in sorted(os.environ.items()):
        if not name.isupper() or len(name) < 3:
            continue
        low = name.lower()
        if any(part in low for part in SECRET_PARTS):
            out[name] = "задано" if (value or "").strip() else "пусто"
        else:
            out[name] = (value or "")[:MAX_TEXT]
    return out


class Recorder:
    """Пишет события прогона в JSONL. Выключенный не делает ничего."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.count = 0
        self.skipped = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def event(self, kind: str, **fields: Any) -> None:
        """Одно событие. Никогда не бросает: диагностика не ломает прогон."""
        if self.path is None:
            return
        with self._lock:
            if self.count >= MAX_EVENTS:
                self.skipped += 1
                return
            row = {"t": _stamp(), "kind": kind}
            row.update(fields)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                self.count += 1
            except OSError:
                self.path = None  # диск не пишется — молча выключаемся


_current = Recorder()


def current() -> Recorder:
    return _current


def event(kind: str, **fields: Any) -> None:
    """Короткий путь для мест вызова: diag.event(...)."""
    _current.event(kind, **fields)


def enabled() -> bool:
    return _current.enabled


def start(reason: str = "", directory: Path | str = DIR) -> Recorder:
    """Включает запись и создаёт файл с шапкой. Возвращает регистратор."""
    global _current
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    name = "run-{}.jsonl".format(datetime.now().strftime("%Y%m%d-%H%M%S"))
    _current = Recorder(folder / name)
    _current.event(
        "начало",
        reason=reason,
        env=safe_env(),
        cwd=str(Path.cwd()),
    )
    return _current


def finish(**totals: Any) -> Path | None:
    """Пишет итог и выключает запись. Возвращает путь к файлу."""
    global _current
    path = _current.path
    if path is None:
        return None
    _current.event("итог", пропущено_событий=_current.skipped, **totals)
    _current = Recorder()
    return path


def wanted(flag: bool = False) -> bool:
    """Просил ли владелец диагностику: флаг команды или DIAG_RUN в окружении."""
    if flag:
        return True
    return (os.getenv("DIAG_RUN") or "0").strip().lower() in ("1", "true", "yes", "on")


def latest(directory: Path | str = DIR) -> Path | None:
    """Последний диагностический файл. Нет папки — None."""
    folder = Path(directory)
    files = sorted(folder.glob("run-*.jsonl")) if folder.exists() else []
    return files[-1] if files else None


def summary(path: Path) -> dict[str, Any]:
    """Короткая сводка по файлу: сколько чего и на чём споткнулись.

    Нужна, чтобы не читать тысячу строк глазами, когда вопрос простой: сколько
    вакансий пришло по каждому запросу и почему их отклонили.
    """
    kinds: dict[str, int] = {}
    queries: list[dict[str, Any]] = []
    rejects: dict[str, int] = {}
    scores: list[float] = []
    stages: dict[str, dict[str, int]] = {}
    totals: dict[str, Any] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = str(row.get("kind") or "?")
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == "запрос":
                queries.append(
                    {
                        "площадка": row.get("площадка"),
                        "текст": row.get("текст"),
                        "найдено": row.get("найдено"),
                    }
                )
            elif kind == "вакансия":
                if row.get("отклонена"):
                    why = str(row.get("почему") or "без причины")
                    rejects[why] = rejects.get(why, 0) + 1
                else:
                    scores.append(float(row.get("балл") or 0))
            elif kind == "модель":
                cell = stages.setdefault(str(row.get("этап")), {})
                outcome = str(row.get("исход") or "?")
                cell[outcome] = cell.get(outcome, 0) + 1
            elif kind == "итог":
                totals = {k: v for k, v in row.items() if k not in ("t", "kind")}
    scores.sort()
    return {
        "файл": str(path),
        "событий": sum(kinds.values()),
        "по видам": kinds,
        "запросы": queries,
        "отказы": dict(sorted(rejects.items(), key=lambda p: -p[1])),
        "баллы": {
            "прошло": len(scores),
            "минимум": scores[0] if scores else None,
            "медиана": scores[len(scores) // 2] if scores else None,
            "максимум": scores[-1] if scores else None,
            "уникальных": len(set(scores)),
        },
        "модель": stages,
        "итог": totals,
    }


def main(argv: Any = None) -> int:
    import argparse  # noqa: PLC0415 — нужен только здесь

    parser = argparse.ArgumentParser(description="Сводка диагностического прогона")
    parser.add_argument("path", nargs="?", help="файл; по умолчанию последний в data/diag")
    args = parser.parse_args(argv)
    path = Path(args.path) if args.path else latest()
    if path is None or not path.exists():
        print("диагностических файлов нет: включи «Диагностический прогон» и собери ещё раз")
        return 1
    print(json.dumps(summary(path), ensure_ascii=False, indent=2))
    return 0


__all__ = (
    "DIR",
    "MAX_EVENTS",
    "Recorder",
    "current",
    "enabled",
    "event",
    "finish",
    "latest",
    "main",
    "safe_env",
    "start",
    "summary",
    "wanted",
)


if __name__ == "__main__":
    raise SystemExit(main())
