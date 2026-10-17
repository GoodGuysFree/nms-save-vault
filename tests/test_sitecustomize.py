"""The packaged runtime's bootstrap (``packaging/sitecustomize.py``), driven without
starting anything: the front end it would launch is replaced by a recorder.

P11: ``nmsvault.exe -c "..."`` / ``-m ...`` must leave the interpreter alone. By the time
``site`` runs, Python has already rewritten ``sys.argv`` to ``['-c', ...]``, so the
decision -- and the arguments handed to the app -- come from ``sys.orig_argv``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SITECUSTOMIZE = Path(__file__).resolve().parents[1] / "packaging" / "sitecustomize.py"
EXE = str(Path("C:/Programs/NMSSaveVault/nmsvault.exe"))


@pytest.fixture
def boot(monkeypatch):
    """The bootstrap module, loaded under another name (importing it under the dev
    interpreter starts nothing: ``python.exe`` is not one of its launchers)."""
    spec = importlib.util.spec_from_file_location("nmsvault_sitecustomize_under_test", SITECUSTOMIZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv(module.BYPASS_ENV, raising=False)
    started: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(module, "_run", lambda front_end, argv: started.append((front_end, argv)))
    module.started = started
    return module


def _launch(monkeypatch, boot, orig_argv: list[str] | None, argv: list[str]) -> list:
    """Run the bootstrap as ``nmsvault.exe`` with the command line Python would leave."""
    monkeypatch.setattr(sys, "executable", EXE)
    monkeypatch.setattr(sys, "argv", argv)
    if orig_argv is None:
        monkeypatch.delattr(sys, "orig_argv", raising=False)
    else:
        monkeypatch.setattr(sys, "orig_argv", orig_argv)
    boot._bootstrap()
    return boot.started


@pytest.mark.parametrize(("orig_argv", "argv"), [
    (["nmsvault.exe", "-c", "print(1)"], ["-c"]),
    (["nmsvault.exe", "-m", "pytest", "-q"], ["-m", "-q"]),
    (["nmsvault.exe", "-mpytest"], ["-m"]),
    (["nmsvault.exe", "-I", "-c", "pass"], ["-c"]),
    (["nmsvault.exe", "-X", "utf8", "-c", "pass"], ["-c"]),
    (["nmsvault.exe", "-Ic", "pass"], ["-c"]),
    (["nmssavevault.exe", "-I", "-c", "from multiprocessing.spawn import spawn_main", "--multiprocessing-fork"],
     ["-c", "--multiprocessing-fork"]),
], ids=["-c", "-m", "-mpytest", "-I -c", "-X utf8 -c", "-Ic", "multiprocessing child"])
def test_driving_the_launcher_as_python_does_not_start_the_app(monkeypatch, boot, orig_argv, argv):
    assert _launch(monkeypatch, boot, orig_argv, argv) == []


@pytest.mark.parametrize(("orig_argv", "argv", "app_argv"), [
    (["nmsvault.exe", "list"], ["list"], ["nmsvault.exe", "list"]),
    (["nmsvault.exe", "list", "-c"], ["list", "-c"], ["nmsvault.exe", "list", "-c"]),
    (["nmsvault.exe", "list", "--multiprocessing-fork"], ["list", "--multiprocessing-fork"],
     ["nmsvault.exe", "list", "--multiprocessing-fork"]),
    (["nmsvault.exe"], [""], ["nmsvault.exe"]),
], ids=["a command", "-c after the command", "the fork flag after the command", "no arguments"])
def test_the_app_starts_with_its_own_command_line(monkeypatch, boot, orig_argv, argv, app_argv):
    assert _launch(monkeypatch, boot, orig_argv, argv) == [("cli", app_argv)]


@pytest.mark.parametrize("orig_argv", [
    ["nmsvault.exe", "-u", "list", "--live", "x"],
    ["nmsvault.exe", "-X", "utf8", "list", "--live", "x"],
    ["nmsvault.exe", "-Xutf8", "-E", "list", "--live", "x"],
    ["nmsvault.exe", "--check-hash-based-pycs", "never", "list", "--live", "x"],
], ids=["-u", "-X utf8", "-Xutf8 -E", "--check-hash-based-pycs never"])
def test_pythons_own_options_are_not_handed_to_the_app(monkeypatch, boot, orig_argv):
    assert _launch(monkeypatch, boot, orig_argv, ["list", "--live", "x"]) == [
        ("cli", ["nmsvault.exe", "list", "--live", "x"])
    ]


@pytest.mark.parametrize(("argv", "starts"), [
    (["list", "--live", "x"], True),
    (["list", "--multiprocessing-fork"], True),
    (["-c"], False),
    (["-m", "-q"], False),
    (["-c", "--multiprocessing-fork"], False),
], ids=["a command", "the fork flag after the command", "-c", "-m", "multiprocessing child"])
def test_without_orig_argv_sys_argv_decides_and_keeps_every_app_argument(monkeypatch, boot, argv, starts):
    """At site time sys.argv[0] is already the app's first argument (or -c / -m)."""
    started = _launch(monkeypatch, boot, None, argv)
    assert started == ([("cli", [EXE, *argv])] if starts else [])
