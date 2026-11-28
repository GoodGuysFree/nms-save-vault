"""The kill tests gate every release (GGF 2026-11-28): the release builds refuse a package that
differs from the one the last whole kill-test run passed with (packaging/release_gate.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "packaging" / "release_gate.py"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("release_gate_under_test", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "src" / "nms_save_vault"
    package.mkdir(parents=True)
    (package / "core.py").write_bytes(b"x = 1\r\n")
    monkeypatch.setattr(module, "PACKAGE", package)
    monkeypatch.setattr(module, "STAMP", tmp_path / "build" / "killtest-pass.json")
    monkeypatch.setattr(module, "src_digest", lambda p=package, real=module.src_digest: real(p))
    return module, package


def test_no_kill_run_no_release(gate):
    module, _package = gate
    assert "have not passed" in module.problem()


def test_the_package_as_tested_may_be_released(gate):
    module, _package = gate
    module.write_stamp(14)
    assert module.problem() is None


def test_a_change_after_the_kill_run_stops_the_release(gate):
    module, package = gate
    module.write_stamp(14)
    (package / "core.py").write_bytes(b"x = 2\r\n")
    assert "changed since" in module.problem()


def test_line_endings_and_caches_do_not_count(gate):
    module, package = gate
    module.write_stamp(14)
    (package / "core.py").write_bytes(b"x = 1\n")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "core.cpython-312.pyc").write_bytes(b"\0")
    assert module.problem() is None


def test_the_release_builds_check_the_gate():
    root = GATE.parent
    assert "release_gate" in (root / "build_core.py").read_text(encoding="utf-8")
    assert "release_gate.py" in (root / "make_installer_zip.ps1").read_text(encoding="utf-8")
