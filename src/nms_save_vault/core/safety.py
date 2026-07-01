"""Safety primitives: game-running detection, hashing, and atomic file writes.

Atomic writes use the write-temp-then-os.replace pattern so a crash or power loss can
never leave a half-written file in the live save directory.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

# The game executable is "NMS.exe" for both the Steam and the Microsoft Store / Game Pass
# editions (same HelloGames binary, just packaged differently), so this one name covers both.
NMS_PROCESS_NAME = "NMS.exe"

# Suppress the console window that ``tasklist`` would otherwise flash on-screen every time
# this runs from the windowed GUI build. Defined only on Windows; 0 elsewhere is a no-op.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_game_running() -> bool | None:
    """Return True/False if NMS is running, or None if it could not be determined.

    Uses Windows ``tasklist``. Callers should treat None as "unknown" and warn.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {NMS_PROCESS_NAME}", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return NMS_PROCESS_NAME.lower() in (result.stdout or "").lower()


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file in same dir, fsync, os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on same volume (Windows & POSIX)
    except BaseException:
        _silent_unlink(tmp)
        raise


def atomic_copy(src: str | Path, dst: str | Path, preserve_mtime: bool = True) -> None:
    """Copy ``src`` to ``dst`` atomically, preserving modification time by default."""
    src = Path(src)
    data = src.read_bytes()
    atomic_write_bytes(dst, data)
    if preserve_mtime:
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))


def set_file_mtime(path: str | Path, unix_ts: float) -> None:
    os.utime(path, (unix_ts, unix_ts))


def _silent_unlink(path: str | Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
