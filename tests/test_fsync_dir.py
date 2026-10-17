"""safety.fsync_dir: a directory fsync on POSIX after a rename, a no-op on Windows."""
from __future__ import annotations

import errno
import os
import types

import pytest

from nms_save_vault.core import safety


def _fake_os(name: str, calls: list, fsync_error: OSError | None = None):
    def _open(path, flags):
        calls.append(("open", str(path), flags))
        return 7

    def _fsync(fd):
        calls.append(("fsync", fd))
        if fsync_error is not None:
            raise fsync_error

    def _close(fd):
        calls.append(("close", fd))

    return types.SimpleNamespace(name=name, O_RDONLY=os.O_RDONLY, open=_open, fsync=_fsync,
                                 close=_close)


def test_posix_fsyncs_the_directory_and_closes_it(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(safety, "os", _fake_os("posix", calls))
    safety.fsync_dir(tmp_path)
    assert calls == [("open", str(tmp_path), os.O_RDONLY), ("fsync", 7), ("close", 7)]


def test_windows_does_nothing(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(safety, "os", _fake_os("nt", calls))
    safety.fsync_dir(tmp_path)
    assert calls == []


def test_a_file_system_that_cannot_fsync_a_directory_is_accepted(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(safety, "os", _fake_os("posix", calls, OSError(errno.EINVAL, "no")))
    safety.fsync_dir(tmp_path)
    assert ("close", 7) in calls


def test_other_fsync_errors_are_raised_and_the_handle_closed(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(safety, "os", _fake_os("posix", calls, OSError(errno.EIO, "disk")))
    with pytest.raises(OSError):
        safety.fsync_dir(tmp_path)
    assert calls[-1] == ("close", 7)


def test_runs_on_this_machine(tmp_path):
    safety.fsync_dir(tmp_path)
