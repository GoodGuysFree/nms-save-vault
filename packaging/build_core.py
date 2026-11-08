"""Build the core library wheel: NMS Save Vault's core, for apps that share the vault.

Output: ``build/core/nms_save_vault_core-<version>-py3-none-any.whl``. It holds
``nms_save_vault/__init__.py`` (the release version) and ``nms_save_vault/core/`` and nothing
else: no GUI, no CLI, no tests. An app such as NMS Savecraft pins one release's wheel by its
SHA-256 and bundles it; it uses only ``nms_save_vault.core.api`` (README, "The core as a
library").

The wheel is reproducible: files in a fixed order, a fixed timestamp, and text with LF line
endings whatever the checkout uses, so the same commit builds the same bytes on any machine
and the published SHA-256 can be checked by rebuilding.

It is a ``.whl``, not a ``.zip``, on purpose: the in-app updater takes the release's first
``.zip`` asset (RELEASING.md, step 5).

Usage:
    python packaging/build_core.py [output folder]
"""
from __future__ import annotations

import ast
import base64
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PACKAGE = "nms_save_vault"
DIST = "nms_save_vault_core"
NAME = "nms-save-vault-core"
TIMESTAMP = (1980, 1, 1, 0, 0, 0)          # the earliest a zip can hold: no build time leaks in


def _constant(path: Path, name: str) -> object:
    """A module-level constant, read without importing (or running) the module."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"{path} has no {name}")


def sources() -> list[tuple[str, bytes]]:
    """(path in the wheel, LF bytes) of the package root and every core module, sorted."""
    files = [SRC / PACKAGE / "__init__.py",
             *(p for p in (SRC / PACKAGE / "core").rglob("*") if p.is_file()
               and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo"))]
    out = []
    for path in sorted(files, key=lambda p: p.relative_to(SRC).as_posix()):
        data = path.read_bytes().replace(b"\r\n", b"\n")
        out.append((path.relative_to(SRC).as_posix(), data))
    return out


def _metadata(version: str, api_version: int) -> bytes:
    return (
        "Metadata-Version: 2.1\n"
        f"Name: {NAME}\n"
        f"Version: {version}\n"
        "Summary: The core of NMS Save Vault (No Man's Sky saves and the vault), for apps that "
        f"share the vault. Public API: nms_save_vault.core.api, API_VERSION {api_version}.\n"
        "License: GPL-3.0-or-later\n"
        "Requires-Python: >=3.10\n"
    ).encode("utf-8")


def _wheel() -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: nms-save-vault packaging/build_core.py\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def build(out_dir: Path) -> Path:
    version = _constant(SRC / PACKAGE / "__init__.py", "__version__")
    api_version = _constant(SRC / PACKAGE / "core" / "api.py", "API_VERSION")
    info = f"{DIST}-{version}.dist-info"
    members = sources() + [
        (f"{info}/LICENSE", (ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n")),
        (f"{info}/METADATA", _metadata(version, api_version)),
        (f"{info}/WHEEL", _wheel()),
    ]
    record = "".join(f"{name},{_record_hash(data)},{len(data)}\n" for name, data in members)
    members.append((f"{info}/RECORD", (record + f"{info}/RECORD,,\n").encode("utf-8")))

    out_dir.mkdir(parents=True, exist_ok=True)
    wheel = out_dir / f"{DIST}-{version}-py3-none-any.whl"
    partial = wheel.with_name(wheel.name + ".partial")
    with zipfile.ZipFile(partial, "w") as zf:
        for name, data in members:
            entry = zipfile.ZipInfo(name, date_time=TIMESTAMP)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            zf.writestr(entry, data, compresslevel=9)
    partial.replace(wheel)
    return wheel


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "build" / "core"
    wheel = build(out_dir)
    print(wheel)
    print("sha256", hashlib.sha256(wheel.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
