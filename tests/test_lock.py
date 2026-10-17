"""safety.exclusive_lock: one holder at a time across processes and threads.

The organizer store and the live-op lock build on it, so the two-process cases run real
child interpreters: a read-modify-write that loses no update, a held lock that times out
the other process, and a holder killed mid-hold that leaves no stale lock behind.
"""
from __future__ import annotations

import errno
import logging
import math
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core.safety import LockTimeout, exclusive_lock

# Runs in a child interpreter: `count` does N locked read-modify-writes of a counter file
# after a shared start signal; `hold` takes the lock, says so, and keeps it until released
# (or killed).
_CHILD = r"""
import sys, time
from pathlib import Path
from nms_save_vault.core.safety import exclusive_lock

mode, lock, work = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

def wait_for(p, limit):
    end = time.monotonic() + limit
    while not p.exists():
        if time.monotonic() > end:
            sys.exit("timed out waiting for " + p.name)
        time.sleep(0.001)

if mode == "count":
    wait_for(work / "go", 30)
    counter = work / "counter"
    for _ in range(int(sys.argv[4])):
        with exclusive_lock(lock, timeout=30, poll=0.002):
            value = int(counter.read_text(encoding="ascii"))
            time.sleep(0.002)  # widen the window an unlocked writer would race through
            counter.write_text(str(value + 1), encoding="ascii")
else:
    with exclusive_lock(lock, timeout=30):
        (work / "ready").write_text("1", encoding="ascii")
        wait_for(work / "release", 60)
"""


def _spawn(*args: object) -> subprocess.Popen:
    env = dict(os.environ)
    src = str(Path(safety.__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, *map(str, args)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )


def _finish(proc: subprocess.Popen) -> None:
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err


def _wait_for(path: Path, proc: subprocess.Popen, limit: float = 30.0) -> None:
    end = time.monotonic() + limit
    while not path.exists():
        if proc.poll() is not None:
            pytest.fail(f"child exited early: {proc.communicate()[1]}")
        assert time.monotonic() < end, f"child never wrote {path.name}"
        time.sleep(0.005)


def _registered(lock: Path) -> bool:
    return os.path.normcase(str(lock.resolve())) in safety._lock_records


def _hold_in_child(lock: Path, work: Path) -> subprocess.Popen:
    holder = _spawn("hold", lock, work)
    try:
        _wait_for(work / "ready", holder)
    except BaseException:
        holder.kill()
        raise
    return holder


def test_two_processes_never_lose_an_update(tmp_path):
    lock = tmp_path / "org" / ".lock"
    (tmp_path / "counter").write_text("0", encoding="ascii")
    n = 100
    procs = [_spawn("count", lock, tmp_path, n) for _ in range(2)]
    try:
        (tmp_path / "go").write_text("1", encoding="ascii")
        for proc in procs:
            _finish(proc)
    finally:
        for proc in procs:
            proc.kill()
    assert (tmp_path / "counter").read_text(encoding="ascii") == str(2 * n)
    assert lock.is_file()  # created on demand (parents too) and never deleted


def test_a_lock_held_by_another_process_times_out_until_released(tmp_path):
    lock = tmp_path / ".lock"
    holder = _hold_in_child(lock, tmp_path)
    try:
        started = time.monotonic()
        with pytest.raises(LockTimeout):
            with exclusive_lock(lock, timeout=0.3):
                pytest.fail("acquired a lock another process holds")
        assert 0.25 <= time.monotonic() - started < 5
        (tmp_path / "release").write_text("1", encoding="ascii")
        _finish(holder)
    finally:
        holder.kill()
    with exclusive_lock(lock, timeout=5) as handle:
        assert handle.warning is None


def test_a_killed_holder_leaves_no_stale_lock(tmp_path):
    lock = tmp_path / ".lock"
    holder = _hold_in_child(lock, tmp_path)
    try:
        with pytest.raises(LockTimeout):
            with exclusive_lock(lock, timeout=0.1):
                pass
    finally:
        holder.kill()
        holder.communicate(timeout=60)
    with exclusive_lock(lock, timeout=10):
        pass


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root opens a read-only file for writing anyway")
def test_a_read_only_lock_file_still_locks(tmp_path):
    """A lock file that cannot be opened for writing is locked through a read-only handle."""
    lock = tmp_path / ".lock"
    lock.write_bytes(b"")
    os.chmod(lock, stat.S_IREAD)
    try:
        holder = _hold_in_child(lock, tmp_path)
        try:
            with pytest.raises(LockTimeout):
                with exclusive_lock(lock, timeout=0.2):
                    pass
            (tmp_path / "release").write_text("1", encoding="ascii")
            _finish(holder)
        finally:
            holder.kill()
        with exclusive_lock(lock, timeout=5) as handle:
            assert handle.warning is None
    finally:
        os.chmod(lock, stat.S_IREAD | stat.S_IWRITE)


def test_a_read_only_file_system_opens_the_lock_file_read_only(tmp_path, monkeypatch):
    lock = tmp_path / ".lock"
    lock.write_bytes(b"")
    real_open = os.open
    read_only_opens = []

    def read_only_fs(path, flags, *args):
        if flags & os.O_RDWR:
            raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(path))
        read_only_opens.append(path)
        return real_open(path, flags, *args)

    monkeypatch.setattr(safety.os, "open", read_only_fs)
    with exclusive_lock(lock, timeout=1) as handle:
        assert handle.warning is None
    assert read_only_opens == [lock]


def test_the_lock_is_not_reentrant_on_one_thread(tmp_path):
    lock = tmp_path / ".lock"
    with exclusive_lock(lock):
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="not re-entrant"):
            with exclusive_lock(lock, timeout=5):
                pass
        assert time.monotonic() - started < 1  # refused at once, not after the timeout
    with exclusive_lock(lock, timeout=1):  # the refused attempt left nothing behind
        pass


def test_another_spelling_of_the_path_is_the_same_lock(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        os.symlink(real, alias, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not permit creating symlinks")
    with exclusive_lock(real / ".lock"):
        with pytest.raises(RuntimeError, match="not re-entrant"):
            with exclusive_lock(alias / ".lock", timeout=1):
                pass


def test_another_thread_waits_and_times_out(tmp_path):
    lock = tmp_path / ".lock"
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with exclusive_lock(lock):
            held.set()
            release.wait(30)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(10)
        with pytest.raises(LockTimeout):
            with exclusive_lock(lock, timeout=0.1):
                pass
    finally:
        release.set()
        thread.join(30)
    with exclusive_lock(lock, timeout=5):
        pass
    assert not _registered(lock)  # nobody holds or waits on it: nothing is remembered


def test_an_interrupt_while_waiting_leaks_nothing(tmp_path, monkeypatch):
    class InterruptedWait:
        def acquire(self, timeout: float = -1) -> bool:
            raise KeyboardInterrupt

        def release(self) -> None:
            raise AssertionError("released a lock that was never taken")

    real_record = safety._LockRecord
    monkeypatch.setattr(safety, "_LockRecord", lambda: real_record(thread_lock=InterruptedWait()))
    lock = tmp_path / ".lock"
    with pytest.raises(KeyboardInterrupt):
        with exclusive_lock(lock):
            pytest.fail("ran the body without the lock")
    assert not _registered(lock)
    monkeypatch.undo()
    with exclusive_lock(lock, timeout=1):
        pass


def test_a_nan_timeout_is_refused(tmp_path):
    with pytest.raises(ValueError, match="NaN"):
        with exclusive_lock(tmp_path / ".lock", timeout=math.nan):
            pytest.fail("ran the body with a NaN timeout")


def test_an_infinite_timeout_waits_for_the_holder(tmp_path):
    lock = tmp_path / ".lock"
    held = threading.Event()

    def hold_briefly() -> None:
        with exclusive_lock(lock):
            held.set()
            time.sleep(0.2)

    thread = threading.Thread(target=hold_briefly)
    thread.start()
    try:
        assert held.wait(10)
        with exclusive_lock(lock, timeout=math.inf):
            pass
    finally:
        thread.join(30)


def _refuse_with(monkeypatch, code: int) -> None:
    def refuse(fd: int) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(safety, "_try_os_lock", refuse)


def test_a_file_system_without_locking_runs_unlocked_with_a_warning(
    tmp_path, monkeypatch, caplog
):
    """flock reports ENOLCK on some NFS mounts."""
    _refuse_with(monkeypatch, errno.ENOLCK)
    ran = False
    with caplog.at_level(logging.WARNING, logger="nms_save_vault.core.safety"):
        with exclusive_lock(tmp_path / ".lock", timeout=1) as handle:
            ran = True
    assert ran
    assert handle.warning and "not supported" in handle.warning
    assert "not supported" in caplog.text


def test_einval_means_no_locking_only_on_windows(tmp_path, monkeypatch):
    """msvcrt reports a share that cannot lock as EINVAL; from flock it is a real error."""
    _refuse_with(monkeypatch, errno.EINVAL)
    if os.name == "nt":
        with exclusive_lock(tmp_path / ".lock", timeout=1) as handle:
            assert handle.warning and "not supported" in handle.warning
    else:
        with pytest.raises(OSError) as raised:
            with exclusive_lock(tmp_path / ".lock", timeout=1):
                pytest.fail("ran the body after a real locking error")
        assert raised.value.errno == errno.EINVAL


# --- one path identity for the core ---------------------------------------------------------


def test_same_path_resolves_and_compares_as_the_os_does(tmp_path):
    from nms_save_vault.core.safety import same_path

    folder = tmp_path / "st_76561198000000001"
    folder.mkdir()
    assert same_path(folder, tmp_path / "x" / ".." / "st_76561198000000001")
    assert same_path(str(folder), folder)
    assert not same_path(folder, tmp_path / "st_76561198000000002")
    if sys.platform == "win32":
        assert same_path(folder, str(folder).upper())
    assert same_path(tmp_path / "missing" / "a", tmp_path / "missing" / "b" / ".." / "a")
