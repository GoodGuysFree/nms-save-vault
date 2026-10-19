"""The packaged runtime's bootstrap (``packaging/sitecustomize.py``), driven without
starting anything: the front end it would launch is replaced by a recorder.

P11: ``nmsvault-cli.exe -c "..."`` / ``-m ...`` must leave the interpreter alone. By the time
``site`` runs, Python has already rewritten ``sys.argv`` to ``['-c', ...]``, so the
decision -- and the arguments handed to the app -- come from ``sys.orig_argv``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SITECUSTOMIZE = Path(__file__).resolve().parents[1] / "packaging" / "sitecustomize.py"
EXE = str(Path("C:/Programs/NMSSaveVault/nmsvault-cli.exe"))


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
    """Run the bootstrap as ``nmsvault-cli.exe`` with the command line Python would leave."""
    monkeypatch.setattr(sys, "executable", EXE)
    monkeypatch.setattr(sys, "argv", argv)
    if orig_argv is None:
        monkeypatch.delattr(sys, "orig_argv", raising=False)
    else:
        monkeypatch.setattr(sys, "orig_argv", orig_argv)
    boot._bootstrap()
    return boot.started


@pytest.mark.parametrize(("orig_argv", "argv"), [
    (["nmsvault-cli.exe", "-c", "print(1)"], ["-c"]),
    (["nmsvault-cli.exe", "-m", "pytest", "-q"], ["-m", "-q"]),
    (["nmsvault-cli.exe", "-mpytest"], ["-m"]),
    (["nmsvault-cli.exe", "-I", "-c", "pass"], ["-c"]),
    (["nmsvault-cli.exe", "-X", "utf8", "-c", "pass"], ["-c"]),
    (["nmsvault-cli.exe", "-Ic", "pass"], ["-c"]),
    (["nmssavevault.exe", "-I", "-c", "from multiprocessing.spawn import spawn_main", "--multiprocessing-fork"],
     ["-c", "--multiprocessing-fork"]),
], ids=["-c", "-m", "-mpytest", "-I -c", "-X utf8 -c", "-Ic", "multiprocessing child"])
def test_driving_the_launcher_as_python_does_not_start_the_app(monkeypatch, boot, orig_argv, argv):
    assert _launch(monkeypatch, boot, orig_argv, argv) == []


@pytest.mark.parametrize(("orig_argv", "argv", "app_argv"), [
    (["nmsvault-cli.exe", "list"], ["list"], ["nmsvault-cli.exe", "list"]),
    (["nmsvault-cli.exe", "list", "-c"], ["list", "-c"], ["nmsvault-cli.exe", "list", "-c"]),
    (["nmsvault-cli.exe", "list", "--multiprocessing-fork"], ["list", "--multiprocessing-fork"],
     ["nmsvault-cli.exe", "list", "--multiprocessing-fork"]),
    (["nmsvault-cli.exe"], [""], ["nmsvault-cli.exe"]),
], ids=["a command", "-c after the command", "the fork flag after the command", "no arguments"])
def test_the_app_starts_with_its_own_command_line(monkeypatch, boot, orig_argv, argv, app_argv):
    assert _launch(monkeypatch, boot, orig_argv, argv) == [("cli", app_argv)]


@pytest.mark.parametrize("orig_argv", [
    ["nmsvault-cli.exe", "-u", "list", "--live", "x"],
    ["nmsvault-cli.exe", "-X", "utf8", "list", "--live", "x"],
    ["nmsvault-cli.exe", "-Xutf8", "-E", "list", "--live", "x"],
    ["nmsvault-cli.exe", "--check-hash-based-pycs", "never", "list", "--live", "x"],
], ids=["-u", "-X utf8", "-Xutf8 -E", "--check-hash-based-pycs never"])
def test_pythons_own_options_are_not_handed_to_the_app(monkeypatch, boot, orig_argv):
    assert _launch(monkeypatch, boot, orig_argv, ["list", "--live", "x"]) == [
        ("cli", ["nmsvault-cli.exe", "list", "--live", "x"])
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


ROOT = Path(__file__).resolve().parents[1]


def test_launcher_names_are_the_same_in_every_place_that_spells_them(boot):
    """The kit's launcher names live in four places; a rename in one must not strand the rest."""
    assert boot.ENTRY_POINTS == {"nmssavevault.exe": "gui", "nmsvault-cli.exe": "cli"}
    build = (ROOT / "packaging" / "build_portable.ps1").read_text(encoding="utf-8")
    assert '"NMSSaveVault.exe")' in build and '"nmsvault-cli.exe")' in build
    assert '"nmsvault.exe"' not in build
    posix = (ROOT / "packaging" / "build_posix_kit.py").read_text(encoding="utf-8")
    assert '("NMSSaveVault", "gui"), ("nmsvault-cli", "cli")' in posix
    scripts = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'nmsvault-cli = "nms_save_vault.cli:main"' in scripts
    assert 'prog="nmsvault-cli"' in (ROOT / "src" / "nms_save_vault" / "cli.py").read_text(encoding="utf-8")


def test_an_old_nmsvault_exe_is_cleaned_up_on_install_update_and_uninstall():
    from nms_save_vault import updates
    old = "nmsvault.exe"
    assert old in updates._UPDATER
    for name in ("install.bat", "uninstall.bat"):
        assert old in (ROOT / "installer" / name).read_text(encoding="ascii")
