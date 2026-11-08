"""packaging/build_core.py: the core library wheel other apps bundle holds the core and nothing
else, builds the same bytes every time, and works on its own."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

from nms_save_vault import __version__
from nms_save_vault.core import api

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_core", ROOT / "packaging" / "build_core.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

INFO = f"nms_save_vault_core-{__version__}.dist-info"


def test_the_wheel_holds_the_package_root_and_the_core_and_nothing_else(tmp_path):
    wheel = tool.build(tmp_path)
    assert wheel.name == f"nms_save_vault_core-{__version__}-py3-none-any.whl"
    names = zipfile.ZipFile(wheel).namelist()
    code = sorted(n for n in names if not n.startswith(INFO))
    core = sorted(p.relative_to(ROOT / "src").as_posix()
                  for p in (ROOT / "src" / "nms_save_vault" / "core").rglob("*.py"))
    assert code == sorted(["nms_save_vault/__init__.py", *core])
    assert sorted(n for n in names if n.startswith(INFO)) == [
        f"{INFO}/{n}" for n in ("LICENSE", "METADATA", "RECORD", "WHEEL")]


def test_the_record_matches_every_file_and_the_metadata_names_the_api(tmp_path):
    zf = zipfile.ZipFile(tool.build(tmp_path))
    rows = [line.split(",") for line in zf.read(f"{INFO}/RECORD").decode().splitlines()]
    assert rows[-1] == [f"{INFO}/RECORD", "", ""]
    for name, digest, size in rows[:-1]:
        data = zf.read(name)
        expect = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        assert (digest, int(size)) == (f"sha256={expect}", len(data)), name
    assert len(rows) - 1 == len(zf.namelist()) - 1
    metadata = zf.read(f"{INFO}/METADATA").decode()
    assert f"Version: {__version__}\n" in metadata
    assert f"API_VERSION {api.API_VERSION}" in metadata


def test_two_builds_are_the_same_bytes_and_carry_no_crlf(tmp_path):
    first = tool.build(tmp_path / "a").read_bytes()
    assert first == tool.build(tmp_path / "b").read_bytes()
    zf = zipfile.ZipFile(tmp_path / "a" / f"nms_save_vault_core-{__version__}-py3-none-any.whl")
    assert all(b"\r\n" not in zf.read(n) for n in zf.namelist())
    # Nothing that depends on the building machine: time, OS, zlib build.
    assert {(i.date_time, i.create_system, i.compress_type) for i in zf.infolist()} == {
        (tool.TIMESTAMP, 3, zipfile.ZIP_STORED)}


def test_only_what_git_tracks_is_shipped(tmp_path):
    names = [n for n in zipfile.ZipFile(tool.build(tmp_path)).namelist() if not n.startswith(INFO)]
    assert names == tool.tracked()


def test_a_release_build_refuses_a_tree_that_differs_from_the_commit(tmp_path, monkeypatch,
                                                                    capsys):
    monkeypatch.setattr(tool, "dirty", lambda: ["?? src/nms_save_vault/core/stray.py"])
    assert tool.main(["build_core.py", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out").exists()
    assert "stray.py" in capsys.readouterr().err


def test_the_wheel_alone_gives_the_api_without_the_source_tree(tmp_path):
    wheel = tool.build(tmp_path)
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
             "from nms_save_vault.core import api; import nms_save_vault; "
             "print(api.API_VERSION, api.CORE_VERSION, nms_save_vault.__file__.startswith(sys.argv[1]))")
    # -I -S: no site-packages, so the editable install of this checkout cannot be found.
    run = subprocess.run([sys.executable, "-I", "-S", "-c", probe, str(wheel)], cwd=tmp_path,
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.split() == [str(api.API_VERSION), __version__, "True"]
