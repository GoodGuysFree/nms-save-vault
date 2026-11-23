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


def _inside(package: str, target: str) -> bool:
    """``target`` is ``package`` or one of its modules - not merely a name that starts the same."""
    return target == package or target.startswith(package + ".")


def test_inside_means_the_package_not_a_name_that_starts_the_same():
    assert _inside("nms_save_vault.core", "nms_save_vault.core.store")
    assert not _inside("nms_save_vault.core", "nms_save_vault.core_x")


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
        elif _inside("nms_save_vault", target) and not _inside("nms_save_vault.core", target):
            outside.append(f"{mod} imports {target}")
    assert not outside, "\n".join(outside)


#: What the core may use besides the standard library when it is there, never needing it:
#: the LZ4 C library the kits ship (lz4-accel branch, 2026-11-23), with the pure-Python
#: code as the fallback.
OPTIONAL = {("nms_save_vault.core.lz4_block", "lz4.block")}


def test_the_core_needs_only_the_standard_library():
    foreign = sorted({f"{mod} imports {target}" for mod, target, _ in _imports()
                      if target and not _inside("nms_save_vault", target)
                      and target.split(".")[0] not in sys.stdlib_module_names
                      and (mod, target) not in OPTIONAL})
    assert not foreign, "\n".join(foreign)


def test_the_core_runs_without_its_optional_library():
    """The core imported with the LZ4 C library missing: the pure-Python code does the work."""
    import subprocess
    code = ("import sys; sys.modules['lz4'] = None; sys.modules['lz4.block'] = None\n"
            "from nms_save_vault.core import lz4_block\n"
            "raw = b'{\"a\":1}' * 999 + bytes(1)\n"
            "assert not lz4_block.FAST\n"
            "assert lz4_block.decompress(lz4_block.compress(raw)) == raw\n"
            "print('ok')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.stdout.strip() == "ok", out.stderr
