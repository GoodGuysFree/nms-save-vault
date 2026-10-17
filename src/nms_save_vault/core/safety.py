"""Safety primitives: game-running detection, hashing, atomic file writes and an
inter-process lock.

Atomic writes use the write-temp-then-os.replace pattern so a crash or power loss can
never leave a half-written file in the live save directory.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

log = logging.getLogger(__name__)

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


def copy_and_hash(src: str | Path, dst: str | Path, chunk: int = 1 << 20) -> str:
    """Copy ``src`` to ``dst`` (contents, then times, like ``shutil.copy2``) reading the
    source once, and return the sha256 of the bytes read."""
    h = hashlib.sha256()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        for block in iter(lambda: fi.read(chunk), b""):
            h.update(block)
            fo.write(block)
    shutil.copystat(src, dst)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: str | Path, data: bytes, temp_prefix: str = ".tmp_") -> None:
    """Write ``data`` to ``path`` atomically (temp file in same dir, fsync, os.replace). The
    temp file is ``<temp_prefix><random>.part``, so a caller can recognise its own leftovers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=temp_prefix, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on same volume (Windows & POSIX)
    except BaseException:
        _silent_unlink(tmp)
        raise


def fsync_dir(path: str | Path) -> None:
    """Make a rename or a new file inside ``path`` durable. POSIX: fsync the directory itself,
    since a file's fsync does not cover its directory entry. Windows: no-op (no directory
    handle to flush; NTFS journals the rename). A file system that cannot fsync a directory
    (EINVAL, some network mounts) is accepted: nothing more can be done there."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno != errno.EINVAL:
            raise
    finally:
        os.close(fd)


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


# --- inter-process lock -------------------------------------------------------

#: How a non-blocking lock attempt reports "someone else holds it": msvcrt says EACCES,
#: flock says EWOULDBLOCK.
_LOCK_BUSY = frozenset(
    e for e in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", None))
    if e is not None
)

#: How a file system without locking support (some network shares) refuses the attempt.
#: msvcrt reports such a share as EINVAL; from flock, EINVAL is a real error and is raised.
_LOCK_UNSUPPORTED = frozenset(
    e for e in (errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", None),
                errno.EINVAL if os.name == "nt" else None)
    if e is not None
)


@dataclass
class _LockRecord:
    """This process's side of one lock file. Its threading.Lock makes the threads of this
    process wait on each other instead of all polling the OS lock (no fairness promised), and
    ``owner`` catches a second acquisition on the holding thread."""

    thread_lock: threading.Lock = field(default_factory=threading.Lock)
    owner: int | None = None
    users: int = 0  # the holder plus the waiters; the record is dropped when it reaches 0


_registry_guard = threading.Lock()
_lock_records: dict[str, _LockRecord] = {}


class LockTimeout(OSError):
    """Another holder kept an :func:`exclusive_lock` past the caller's timeout."""


@dataclass
class LockHandle:
    path: Path
    warning: str | None = None  # set when the file system cannot lock: the body ran unlocked


@contextmanager
def exclusive_lock(
    path: str | Path, *, timeout: float = 10.0, poll: float = 0.05
) -> Iterator[LockHandle]:
    """Exclusive inter-process lock on ``path`` (created if absent, never deleted).

    Windows: ``msvcrt.locking`` on byte 0; POSIX: ``flock(LOCK_EX | LOCK_NB)``. Retries every
    ``poll`` s until ``timeout`` (``math.inf`` waits for ever), then raises
    :class:`LockTimeout`. The OS releases the lock when the holder dies, so there are no stale
    locks. Not re-entrant: acquiring it again on the thread that holds it raises
    ``RuntimeError``. If the file system cannot lock, the body runs anyway with a warning on
    the yielded handle (and in the log).
    """
    if math.isnan(timeout):
        raise ValueError("exclusive_lock timeout must be a number of seconds, not NaN")
    path = Path(path)
    # resolve() so a junction, symlink, 8.3 name or subst drive is the same lock. Best
    # effort: hard links and case variants are not unified; callers use one canonical path.
    key = os.path.normcase(str(path.resolve()))
    me = threading.get_ident()
    deadline = time.monotonic() + timeout
    record: _LockRecord | None = None
    acquired = False
    try:
        with _registry_guard:
            candidate = _lock_records.setdefault(key, _LockRecord())
            if candidate.owner == me:
                raise RuntimeError(f"the lock on {path.name} is not re-entrant")
            candidate.users += 1
            record = candidate
        wait = min(max(0.0, timeout), threading.TIMEOUT_MAX)
        acquired = record.thread_lock.acquire(timeout=wait)
        if not acquired:
            raise _lock_timeout(path, timeout)
        with _registry_guard:
            record.owner = me
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = _open_lock_file(path)
        try:
            handle = LockHandle(path)
            locked = _acquire_os_lock(fd, handle, deadline, timeout, poll)
            try:
                yield handle
            finally:
                if locked:
                    _release_os_lock(fd, path)
        finally:
            os.close(fd)
    finally:
        # Also reached by an interrupt while waiting: the count and the lock never leak.
        if record is not None:
            with _registry_guard:
                if acquired:
                    record.owner = None
                    record.thread_lock.release()
                record.users -= 1
                _drop_unused(key, record)


def _drop_unused(key: str, record: _LockRecord) -> None:
    """Forget a record nobody holds or waits on (call under ``_registry_guard``)."""
    if record.users == 0 and _lock_records.get(key) is record:
        del _lock_records[key]


def _open_lock_file(path: Path) -> int:
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    except OSError as denied:
        if not isinstance(denied, PermissionError) and denied.errno != errno.EROFS:
            raise
        # A read-only lock file or file system still locks through a read-only descriptor,
        # with msvcrt and flock alike.
        try:
            return os.open(path, os.O_RDONLY)
        except OSError:
            raise denied from None


def _acquire_os_lock(
    fd: int, handle: LockHandle, deadline: float, timeout: float, poll: float
) -> bool:
    """Take the OS lock on ``fd``; False when the file system cannot lock at all."""
    while True:
        try:
            _try_os_lock(fd)
            return True
        except OSError as e:
            if e.errno in _LOCK_UNSUPPORTED:
                handle.warning = (
                    f"file locking is not supported where {handle.path.name} lives "
                    f"({e.strerror}); continuing without the lock"
                )
                log.warning(handle.warning)
                return False
            if e.errno not in _LOCK_BUSY:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _lock_timeout(handle.path, timeout)
        time.sleep(min(poll, remaining))


def _lock_timeout(path: Path, timeout: float) -> LockTimeout:
    return LockTimeout(f"timed out after {timeout:g} s waiting for the lock on {path.name}")


def _release_os_lock(fd: int, path: Path) -> None:
    try:
        _os_unlock(fd)
    except OSError as e:
        # Closing the descriptor right after releases the lock anyway.
        log.warning("could not unlock %s explicitly: %s", path.name, e)


if os.name == "nt":

    def _try_os_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _os_unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:

    def _try_os_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _os_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _silent_unlink(path: str | Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
