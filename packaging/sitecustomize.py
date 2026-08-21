"""Entry point for the packaged portable runtime.

The two launchers in the install folder are verbatim renamed copies of CPython's
Authenticode-signed ``pythonw.exe`` / ``python.exe``. They are byte-for-byte the
files the Python Software Foundation published, which is the whole point -- the
signature (and SmartScreen's opinion of it) only survives if nothing is patched
into them. That leaves no embedded entry point, so the app is dispatched from
here instead: ``site`` imports ``sitecustomize`` during interpreter startup,
which is *before* the command line is interpreted as a script name.

Laid out by ``packaging/build_portable.ps1`` as ``_runtime/sitecustomize.py``,
reached via the ``import site`` line in the adjacent ``pythonNNN._pth``.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

#: Launcher file name -> which front-end it starts.
ENTRY_POINTS = {"nmssavevault.exe": "gui", "nmsvault.exe": "cli"}

#: Set to "1" to get a plain interpreter for debugging the packaged runtime.
BYPASS_ENV = "NMSVAULT_NO_BOOTSTRAP"

#: Exported so ``core.state.install_dir`` keeps the config next to the program.
ROOT_ENV = "NMSVAULT_PORTABLE_ROOT"


def _windows_argv() -> list[str]:
    """The original command line, which ``python.exe`` has already consumed.

    The interpreter rewrites ``sys.argv`` for its own option parsing before we
    run, so the real arguments have to come from the OS.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32.GetCommandLineW.restype = wintypes.LPWSTR
        shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        argc = ctypes.c_int(0)
        argv = shell32.CommandLineToArgvW(kernel32.GetCommandLineW(), ctypes.byref(argc))
        if not argv:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return [argv[i] for i in range(argc.value)]
        finally:
            kernel32.LocalFree(argv)
    except OSError:
        return [sys.executable, *sys.argv[1:]]


def _report_startup_failure(details: str) -> None:
    """Surface a failure that happened before the app could show its own error.

    ``NMSSaveVault.exe`` has no console, so an exception raised here would
    otherwise be invisible. The usual cause is an incomplete install -- someone
    copied the launcher out of the folder and left ``_runtime`` behind.
    """
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home()
    log = base / "NMSSaveVault" / "startup-error.txt"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(details, encoding="utf-8")
        where = str(log)
    except OSError:
        where = "(the error log could not be written)"
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            "NMS Save Vault could not start.\n\n"
            "Keep the launcher together with the _runtime folder and the .dll files "
            "next to it; copying the .exe out on its own will not work.\n\n"
            f"Details were written to:\n{where}",
            "NMS Save Vault",
            0x10,  # MB_ICONERROR
        )
    except OSError:
        pass


def _run(front_end: str, argv: list[str]) -> None:
    runtime = Path(sys.executable).resolve().parent / "_runtime"
    os.environ.setdefault("TCL_LIBRARY", str(runtime / "tcl" / "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", str(runtime / "tcl" / "tk8.6"))
    sys.argv = list(argv)

    try:
        if front_end == "gui":
            from nms_save_vault.gui import main
        else:
            from nms_save_vault.cli import main
        code = int(main() or 0)
    except SystemExit as exc:
        code = int(exc.code or 0)
    except BaseException:
        import traceback

        _report_startup_failure(traceback.format_exc())
        code = 1

    # os._exit skips interpreter shutdown, so the streams are never flushed for us.
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    # Bypass shutdown deliberately: Tk teardown can otherwise stall a windowed exit.
    os._exit(code)


def _bootstrap() -> None:
    exe = Path(sys.executable)
    front_end = ENTRY_POINTS.get(exe.name.lower())
    if front_end is None or os.environ.get(BYPASS_ENV) == "1":
        return

    # Leave the interpreter alone when it is being driven as one, so the packaged
    # runtime stays inspectable: NMSSaveVault.exe -c "..." / nmsvault.exe -m pytest.
    args = [a.lower() for a in sys.argv[1:]]
    if args[:1] in (["-c"], ["-m"]) or "--multiprocessing-fork" in args:
        return

    _run(front_end, _windows_argv())


# Runs for every launcher, including the bypassed ones, so the config location is
# consistent however the packaged runtime was started.
if (Path(sys.executable).resolve().parent / "_runtime" / "app").is_dir():
    os.environ[ROOT_ENV] = str(Path(sys.executable).resolve().parent)

_bootstrap()
