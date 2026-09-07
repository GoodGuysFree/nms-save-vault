"""Every string the app can draw must be ASCII.

The portable Linux kit bundles python-build-standalone's Tcl/Tk, which is built without
Xft, fontconfig or Xrender -- ``libtk8.6.so`` references neither, and falls back to legacy
X11 core fonts advertising ISO8859-1. Those fonts cannot render anything outside that
charset, so an em dash or an ellipsis came out as garbage on a real Linux desktop
(reported against v0.1.2: "(nothing chosen yet <garbage> press Change<garbage>)").

Docstrings and comments are exempt -- they are never drawn.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "nms_save_vault"

#: What to write instead. Kept here so the failure message tells you the fix.
REPLACEMENTS = {
    "—": "-",     # em dash
    "–": "-",     # en dash
    "…": "...",   # horizontal ellipsis
    "‘": "'",     # curly quotes
    "’": "'",
    "“": '"',
    "”": '"',
    " ": " ",     # non-breaking space
}


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def non_ascii_literals(path: Path) -> list[str]:
    """Every non-docstring string literal in ``path`` that holds a non-ASCII character.

    ``ast`` is used rather than ``tokenize`` on purpose: since PEP 701 an f-string is no
    longer a single STRING token, so a token-level scan silently misses every f-string --
    which is exactly how the first attempt at this fix left thirteen of them behind.
    """
    tree = ast.parse(path.read_text("utf-8"))
    docs = _docstring_ids(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docs:
            continue
        offenders = sorted({c for c in node.value if ord(c) > 127})
        if offenders:
            fixes = ", ".join(
                f"U+{ord(c):04X} -> {REPLACEMENTS.get(c, 'an ASCII equivalent')!r}"
                for c in offenders
            )
            out.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}  [{fixes}]")
    return out


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name)
def test_drawable_strings_are_ascii(path: Path):
    offenders = non_ascii_literals(path)
    assert not offenders, (
        "these string literals would render as garbage on the bundled Linux Tk "
        "(no Xft, so X11 core fonts, so ISO8859-1 only):\n  " + "\n  ".join(offenders)
    )
