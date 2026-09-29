"""Границы пакетов: слои только вниз и ни одного цикла импортов ([CORE-024]).

Граф строится по AST, без импорта модулей, поэтому тест дешёвый и видит
импорты внутри функций тоже: ленивый импорт не прячет зависимость.
Импорты под `if TYPE_CHECKING:` не считаются — в рантайме их нет.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "fuckhr"

# Снизу вверх. Модуль импортирует свой пакет и пакеты левее, но не правее.
LAYERS: tuple[tuple[str, ...], ...] = (
    ("core",),
    ("llm",),
    ("text",),
    ("sources",),
    ("vacancy",),
    ("company",),
    ("outreach",),
    ("bot",),
    ("pipeline",),
    ("lab",),
    ("web", "tools"),
)
RANK = {name: i for i, names in enumerate(LAYERS) for name in names}
ROOT_SCRIPTS = {"run.py", "webui.py"}


def module_name(path: Path) -> str:
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def modules() -> dict[str, Path]:
    return {module_name(p): p for p in PKG.rglob("*.py")}


def imports_of(path: Path, known: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = {
        id(node)
        for block in ast.walk(tree)
        if isinstance(block, ast.If) and "TYPE_CHECKING" in ast.unparse(block.test)
        for node in ast.walk(block)
    }
    out: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names if a.name in known)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in known:
            for alias in node.names:
                child = f"{node.module}.{alias.name}"
                out.add(child if child in known else node.module)
    return out - {module_name(path)}


def graph() -> dict[str, set[str]]:
    mods = modules()
    known = set(mods)
    return {name: imports_of(path, known) for name, path in mods.items()}


def layer(module: str) -> str:
    return module.split(".")[1] if module.count(".") >= 1 else ""


def upward(edges: dict[str, set[str]]) -> list[str]:
    bad = []
    for src, targets in edges.items():
        for dst in targets:
            if layer(dst) and RANK[layer(dst)] > RANK[layer(src)]:
                bad.append(f"{src} -> {dst}")
            elif layer(dst) and RANK[layer(dst)] == RANK[layer(src)] and layer(dst) != layer(src):
                bad.append(f"{src} -> {dst} (соседи одного слоя)")
    return sorted(bad)


def cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    found: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        for nxt in edges.get(node, ()):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            comp = []
            while True:
                item = stack.pop()
                comp.append(item)
                if item == node:
                    break
            if len(comp) > 1:
                found.append(sorted(comp))

    for node in sorted(edges):
        if node not in index:
            visit(node)
    return found


def test_каждый_модуль_лежит_в_известном_слое() -> None:
    stray = sorted(m for m in modules() if m != "fuckhr" and layer(m) not in RANK)
    assert not stray, "модули вне слоёв из LAYERS: {}".format(stray)


def test_в_корне_только_запускалки() -> None:
    assert {p.name for p in ROOT.glob("*.py")} == ROOT_SCRIPTS


def test_импорты_только_вниз_по_слоям() -> None:
    bad = upward(graph())
    assert not bad, "импорт вверх по слоям (docs/architecture.md):\n" + "\n".join(bad)


def test_нет_циклов_импортов() -> None:
    found = cycles(graph())
    assert not found, "циклы импортов: {}".format(found)


def test_проверки_ловят_нарушения() -> None:
    fake = {
        "fuckhr.core.db": {"fuckhr.web.ui_core"},
        "fuckhr.web.ui_core": {"fuckhr.core.db"},
    }
    assert upward(fake) == ["fuckhr.core.db -> fuckhr.web.ui_core"]
    assert cycles(fake) == [["fuckhr.core.db", "fuckhr.web.ui_core"]]
