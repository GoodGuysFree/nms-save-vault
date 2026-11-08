"""Build the core library wheel: NMS Save Vault's core, for apps that share the vault.

Output: ``build/core/nms_save_vault_core-<version>-py3-none-any.whl``. It holds
``nms_save_vault/__init__.py`` (the release version) and ``nms_save_vault/core/`` and nothing
else: no GUI, no CLI, no tests. An app such as NMS Savecraft pins one release's wheel by its
SHA-256 and bundles it; it uses only ``nms_save_vault.core.api`` (README, "The core as a
library").

The wheel is reproducible: the files git tracks, in a fixed order, with a fixed timestamp,
LF line endings whatever the checkout uses, stored uncompressed (deflate output depends on the
zlib build) and marked as made on POSIX, so the same commit builds the same bytes on any
machine and Python, and the published SHA-256 can be checked by rebuilding. A release build
refuses a working tree whose package differs from the commit.

The wheel installs the ``nms_save_vault`` package, as the app does: install one or the other in
an environment, never both.

It is a ``.whl``, not a ``.zip``, on purpose: the in-app updater takes the release's first
``.zip`` asset (RELEASING.md, step 5).

Usage:
    uv run python packaging/build_core.py [output folder]
"""
from __future__ import annotations

import ast
import base64
import hashlib
import subprocess
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


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def tracked() -> list[str]:
    """The package root and every core file git tracks, as paths under ``src/``, sorted."""
    listed = _git("ls-files", "-z", "--", f"src/{PACKAGE}/__init__.py", f"src/{PACKAGE}/core")
    return sorted(p.removeprefix("src/") for p in listed.split("\0") if p)


def dirty() -> list[str]:
    """What in the package differs from the commit: changed, staged or untracked."""
    status = _git("status", "--porcelain", "--untracked-files=all", "--", f"src/{PACKAGE}")
    return [line for line in status.splitlines() if line.strip()]


def sources() -> list[tuple[str, bytes]]:
    """(path in the wheel, LF bytes) of every file :func:`tracked` names."""
    return [(rel, (SRC / rel).read_bytes().replace(b"\r\n", b"\n")) for rel in tracked()]


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
            entry.compress_type = zipfile.ZIP_STORED
            entry.create_system = 3                  # POSIX on every OS (the default follows it)
            entry.external_attr = 0o644 << 16
            zf.writestr(entry, data)
    partial.replace(wheel)
    return wheel


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "build" / "core"
    changed = dirty()
    if changed:
        print("Refusing: src/nms_save_vault differs from the commit, so the wheel would not "
              "match it:", *changed, sep="\n  ", file=sys.stderr)
        return 1
    wheel = build(out_dir)
    print(wheel)
    print("sha256", hashlib.sha256(wheel.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
