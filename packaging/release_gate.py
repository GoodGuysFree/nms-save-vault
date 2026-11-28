"""The kill tests gate every release (GGF 2026-11-28: "It needs to run every time before we
release. We cannot compromise on the user's file safety").

A run of the whole kill-safety suite (``uv run pytest -m killtest``, every case, nothing
filtered out) that passes writes ``build/killtest-pass.json``: a digest of every file of the
package as it was tested. The release builds (``build_core.py``, ``make_installer_zip.ps1``)
refuse while the package on disk differs from that digest - a change made after the kill
tests ran is not released until they ran again.

``python packaging/release_gate.py check`` prints the problem and exits 1, or exits 0.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "nms_save_vault"
STAMP = ROOT / "build" / "killtest-pass.json"


def src_digest(package: Path = PACKAGE) -> str:
    """Every file of the package (but caches), by path and content; line endings do not count
    (git may check a file out either way)."""
    h = hashlib.sha256()
    for path in sorted(p for p in package.rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"):
        h.update(path.relative_to(package).as_posix().encode("utf-8") + b"\0")
        h.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def write_stamp(tests: int) -> None:
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(json.dumps({"src": src_digest(), "tests": tests,
                                 "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
                     encoding="utf-8")


def problem() -> str | None:
    """Why a release must not be built now, or None."""
    try:
        stamp = json.loads(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("the kill tests have not passed for this code: run "
                "`uv run pytest -m killtest` (RELEASING.md step 2)")
    if stamp.get("src") != src_digest():
        return ("the code changed since the kill tests passed (" + str(stamp.get("utc")) +
                "): run `uv run pytest -m killtest` again (RELEASING.md step 2)")
    return None


def main(argv: list[str]) -> int:
    if argv[1:] == ["check"]:
        why = problem()
        if why:
            print(f"Refusing: {why}.")
            return 1
        print("The kill tests passed for this code.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
