"""The suite's write guard (conftest): no test reads this machine's config, and a test that
writes under the real user folders fails - %LOCALAPPDATA%, %APPDATA% and the owner's
safety backup are strictly read-only."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from nms_save_vault.core import platform as host_platform
from nms_save_vault.core import state as appstate

from . import conftest

REAL_LOCAL = conftest._REAL_ENV["LOCALAPPDATA"]
REAL_ROAMING = conftest._REAL_ENV["APPDATA"]
needs_windows_folders = pytest.mark.skipif(not (REAL_LOCAL and REAL_ROAMING),
                                           reason="no %LOCALAPPDATA% / %APPDATA% here")


def test_the_user_folders_are_temp_folders(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    for folder in (appstate.default_state_path(), host_platform.user_config_dir(),
                   host_platform.user_data_dir(), host_platform.user_cache_dir()):
        assert base in Path(folder).resolve().parents, folder


@needs_windows_folders
@pytest.mark.parametrize("event, args", [
    ("open", ("{local}/NMSSaveVault/state.json", "w", 0)),
    ("open", ("{local}/NMSSaveVault/log.txt", "a", 0)),
    ("open", ("{local}/NMSSaveVault/x", None, os.O_WRONLY | os.O_CREAT)),
    ("os.rename", ("{local}/NMSSaveVault/a.tmp", "{local}/NMSSaveVault/state.json", -1, -1)),
    ("os.remove", ("{roaming}/HelloGames/NMS/st_76561198000000001/save.hg", -1)),
    ("os.mkdir", ("{roaming}/HelloGames/NMS/_SaveVault/organizer", 0o777, -1)),
    ("shutil.copyfile", ("{tmp}/a", "{roaming}/HelloGames/NMS/save2.hg")),
    ("os.utime", ("{roaming}/HelloGames/NMS/save2.hg", None, None, -1)),
])
def test_a_write_under_the_real_folders_is_caught(event, args):
    names = {"local": REAL_LOCAL, "roaming": REAL_ROAMING, "tmp": tempfile.gettempdir()}
    real = tuple(a.format(**names) if isinstance(a, str) else a for a in args)
    assert conftest.real_write_target(event, real)


@needs_windows_folders
@pytest.mark.parametrize("event, args", [
    ("open", ("{local}/NMSSaveVault/state.json", "r", 0)),          # reading is fine
    ("open", ("{local}/NMSSaveVault/state.json", "rb", 0)),
    ("open", ("{local}/NMSSaveVault/state.json", None, os.O_RDONLY)),
    ("open", ("{tmp}/pytest-of-x/state.json", "w", 0)),             # tmp_path is allowed
    ("open", ("{roaming}/uv/python/lib/__pycache__/x.cpython-312.pyc", "wb", 0)),
    ("os.mkdir", ("C:/somewhere/else", 0o777, -1)),
    ("os.rename", ("{tmp}/a", "{tmp}/b", -1, -1)),
    ("shutil.copyfile", ("{roaming}/HelloGames/NMS/save2.hg", "{tmp}/copy.hg")),  # a copy out
])
def test_reads_and_temp_writes_pass(event, args):
    names = {"local": REAL_LOCAL, "roaming": REAL_ROAMING, "tmp": tempfile.gettempdir()}
    real = tuple(a.format(**names) if isinstance(a, str) else a for a in args)
    assert conftest.real_write_target(event, real) is None


@needs_windows_folders
def test_the_hook_is_installed_and_records_a_real_write():
    """End to end without writing anything: an audit event as open() raises it."""
    target = os.path.join(REAL_LOCAL, "NMSSaveVault", "state.json")
    sys.audit("open", target, "w", 0)
    hits = conftest.take_violations()
    assert len(hits) == 1 and hits[0].startswith("open: ")
