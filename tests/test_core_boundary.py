"""The core is a library other apps bundle (NMS Savecraft): it never imports the app around it
(CLI, GUI, UI, updates, theme) and needs nothing beyond the standard library. Only appinfo may
read the package root, for the core's own version."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import nms_save_vault

SRC = Path(nms_save_vault.__file__).resolve().parent.parent
CORE = SRC / "nms_save_vault" / "core"


def _module(path: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    is_pkg = parts[-1] == "__init__"
    return ".".join(parts[:-1] if is_pkg else parts), is_pkg


def _imports():
    """(core module, imported module, names) for every import statement in the core."""
    for py in sorted(CORE.rglob("*.py")):
        mod, is_pkg = _module(py)
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield mod, alias.name, ()
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = mod.split(".") if is_pkg else mod.split(".")[:-1]
                    base = base[:len(base) - (node.level - 1)]
                    target = ".".join(base + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                yield mod, target, tuple(a.name for a in node.names)


def test_the_core_never_imports_the_app_around_it():
    outside = []
    for mod, target, names in _imports():
        if target == "nms_save_vault":
            if (mod, names) != ("nms_save_vault.core.appinfo", ("__version__",)):
                outside.append(f"{mod} imports nms_save_vault {names}")
        elif target.startswith("nms_save_vault.") and not target.startswith("nms_save_vault.core"):
            outside.append(f"{mod} imports {target}")
    assert not outside, "\n".join(outside)


def test_the_core_needs_only_the_standard_library():
    foreign = sorted({f"{mod} imports {target}" for mod, target, _ in _imports()
                      if target and not target.startswith("nms_save_vault")
                      and target.split(".")[0] not in sys.stdlib_module_names})
    assert not foreign, "\n".join(foreign)
