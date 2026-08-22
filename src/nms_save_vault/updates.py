"""Check GitHub for a newer release, and install it.

Network use is opt-in: the first run asks, the answer is stored in ``state.json`` as
``update_check`` (``ask`` / ``on`` / ``off``), and nothing is contacted until the answer
is ``on``. Checks are throttled to once a day.

Installing is a second, separate consent -- the check alone never downloads anything.
When the user does ask for it, the flow is:

    fetch_latest -> download the release zip -> unpack to a temp folder -> verify it
    -> write a small updater script -> launch it detached and exit

The hand-off is not optional. A running app holds its own launcher and its runtime DLLs
open, so it cannot overwrite itself; something outside the process has to wait for it to
exit and then swap the files. That is all the updater script does.

Only the packaged portable app can update itself. Running from a source checkout there is
no ``_runtime`` layout to replace, and :func:`portable_root` returns ``None``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import __version__

# Values for AppState.update_check
ASK = "ask"
ON = "on"
OFF = "off"

API_LATEST = "https://api.github.com/repos/GoodGuysFree/nms-save-vault/releases/latest"
RELEASES_PAGE = "https://github.com/GoodGuysFree/nms-save-vault/releases/latest"

TIMEOUT_SECONDS = 6
#: A release zip is tens of megabytes; the per-read timeout has to be far more generous
#: than the one used for a small JSON call.
DOWNLOAD_TIMEOUT_SECONDS = 60
_CHUNK = 64 * 1024

_USER_AGENT = f"NMSSaveVault/{__version__} (+{RELEASES_PAGE})"  # GitHub rejects requests without one

_NUMBER = re.compile(r"\d+")
_VERSION_LINE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

#: The install folder is a folder, not a file: this is its launcher and the folder the
#: release zip wraps everything in.
LAUNCHER = "NMSSaveVault.exe"
APP_FOLDER = "NMSSaveVault"
#: Exported by ``packaging/sitecustomize.py``; the install directory of the packaged app.
ROOT_ENV = "NMSVAULT_PORTABLE_ROOT"
#: Temp folders and scripts left by an update, cleaned up on the next start.
TEMP_PREFIX = "nmsvault-update-"
#: How long an update log is kept before the sweep takes it (a week).
LOG_RETENTION_SECONDS = 7 * 24 * 60 * 60

#: Where a release asset is allowed to come from. GitHub redirects the download to a
#: storage host, so the check has to cover the redirect target too (see _TrustedRedirect).
_TRUSTED_HOSTS = frozenset({"github.com", "api.github.com", "codeload.github.com"})
_TRUSTED_SUFFIX = ".githubusercontent.com"


class UpdateCheckError(Exception):
    """The check could not be completed (offline, rate-limited, bad response, ...)."""


class UpdateInstallError(Exception):
    """The download or the install could not be completed."""


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    name: str = ""
    #: The downloadable installer zip. Empty when the release has no zip attached, in
    #: which case the app can only offer the download page.
    asset_url: str = ""
    asset_name: str = ""
    asset_size: int = 0

    @property
    def has_asset(self) -> bool:
        return bool(self.asset_url)


def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.0.10"`` -> ``(0, 0, 10)``. Non-numeric junk is ignored rather than fatal."""
    return tuple(int(n) for n in _NUMBER.findall(text or ""))


def is_newer(candidate: str, current: str = __version__) -> bool:
    """True if ``candidate`` is a strictly higher version than ``current``.

    Compared component-wise as integers, so 0.0.10 correctly beats 0.0.9 (string
    comparison would not).
    """
    a, b = parse_version(candidate), parse_version(current)
    if not a or not b:
        return False  # unparseable: never nag on a guess
    length = max(len(a), len(b))
    return a + (0,) * (length - len(a)) > b + (0,) * (length - len(b))


def due(last_check: str, today: date | None = None) -> bool:
    """True if a check has not yet run today. A blank or unparseable date means 'due'."""
    today = today or date.today()
    try:
        return date.fromisoformat(last_check) < today
    except (TypeError, ValueError):
        return True


# --- talking to GitHub -------------------------------------------------------


def is_trusted_url(url: str) -> bool:
    """True for an HTTPS URL on a GitHub host.

    The download URL arrives inside a JSON document, so it is data, not something this
    program decided. Pinning the host means a surprising or tampered-with response cannot
    aim the downloader at an arbitrary server.
    """
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    return host in _TRUSTED_HOSTS or host.endswith(_TRUSTED_SUFFIX)


class _TrustedRedirect(urllib.request.HTTPRedirectHandler):
    """Keeps the host check honest across the redirect GitHub uses for asset downloads."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_trusted_url(newurl):
            raise UpdateInstallError(f"the download was redirected off GitHub, to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _pick_asset(payload: dict) -> tuple[str, str, int]:
    """The installer zip attached to a release: ``(url, name, size)``, or blanks."""
    for asset in payload.get("assets") or ():
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
        if name.lower().endswith(".zip") and is_trusted_url(url):
            return url, name, int(asset.get("size") or 0)
    return "", "", 0


def fetch_latest(url: str = API_LATEST, timeout: int = TIMEOUT_SECONDS) -> Release:
    """Ask GitHub for the latest release. Raises :class:`UpdateCheckError` on any failure."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateCheckError(f"GitHub returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError(f"could not reach GitHub: {exc}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateCheckError(f"unexpected response from GitHub: {exc}") from exc

    tag = payload.get("tag_name") or payload.get("name") or ""
    if not tag:
        raise UpdateCheckError("GitHub returned a release with no version tag")
    asset_url, asset_name, asset_size = _pick_asset(payload)
    return Release(
        version=tag.lstrip("vV"),
        page_url=payload.get("html_url") or RELEASES_PAGE,
        name=payload.get("name") or "",
        asset_url=asset_url,
        asset_name=asset_name,
        asset_size=asset_size,
    )


def check(current: str = __version__, url: str = API_LATEST) -> Release | None:
    """The newer release, or ``None`` if this build is already current."""
    latest = fetch_latest(url)
    return latest if is_newer(latest.version, current) else None


# --- is this build even replaceable? -----------------------------------------


def portable_root(env_value: str | None = None) -> Path | None:
    """The install folder of the packaged app, or ``None`` when running from source.

    Both parts of the layout are checked rather than trusting the variable, because the
    updater replaces whatever this points at.
    """
    root = env_value if env_value is not None else os.environ.get(ROOT_ENV)
    if not root:
        return None
    path = Path(root)
    ok = (path / LAUNCHER).is_file() and (path / "_runtime" / "app").is_dir()
    return path if ok else None


def can_install(release: Release | None = None) -> tuple[bool, str]:
    """Whether an update can be installed in place, and why not when it cannot."""
    if os.name != "nt":
        return False, "In-place updating is only implemented for the Windows app."
    if portable_root() is None:
        return False, (
            "This copy is running from a source checkout, not the packaged app, so there "
            "is nothing for the updater to replace. Update it with git instead."
        )
    if release is not None and not release.has_asset:
        return False, "That release has no downloadable zip attached to it."
    return True, ""


# --- downloading -------------------------------------------------------------


def download(url: str, dest: Path, progress=None, timeout: int = DOWNLOAD_TIMEOUT_SECONDS) -> Path:
    """Stream ``url`` into ``dest``. ``progress(done, total)`` is called as it arrives.

    ``total`` is 0 when the server does not say how big the file is.
    """
    if not is_trusted_url(url):
        raise UpdateInstallError(f"refusing to download from an unexpected host: {url}")
    dest = Path(dest)
    opener = urllib.request.build_opener(_TrustedRedirect)
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/octet-stream"}
    )
    done = 0
    try:
        with opener.open(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if progress:
                progress(0, total)
            with dest.open("wb") as fh:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except UpdateInstallError:
        raise
    except urllib.error.HTTPError as exc:
        raise UpdateInstallError(f"the download failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateInstallError(f"the download failed: {exc}") from exc

    if total and done != total:
        raise UpdateInstallError(
            f"the download stopped early ({done} of {total} bytes); nothing was installed"
        )
    return dest


def unpack(zip_path: Path, dest: Path) -> Path:
    """Extract the release zip into ``dest`` and return the app folder inside it."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            _reject_escaping_members(archive)
            archive.extractall(dest)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateInstallError(f"the downloaded file is not a usable zip: {exc}") from exc

    app_dir = dest / APP_FOLDER
    if not (app_dir / LAUNCHER).is_file():
        raise UpdateInstallError(
            f"the download does not look like a NMS Save Vault release "
            f"(no {APP_FOLDER}\\{LAUNCHER} inside it)"
        )
    return app_dir


def _reject_escaping_members(archive: zipfile.ZipFile) -> None:
    """Refuse an archive whose entries would write outside the folder we extract into."""
    for name in archive.namelist():
        parts = Path(name.replace("\\", "/")).parts
        if name.startswith("/") or ".." in parts or (len(name) > 1 and name[1] == ":"):
            raise UpdateInstallError(f"the zip contains an unsafe path: {name}")


def staged_version(app_dir: Path) -> str:
    """The version the extracted tree declares, or ``""`` if it cannot be read."""
    init = Path(app_dir) / "_runtime" / "app" / "nms_save_vault" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    match = _VERSION_LINE.search(text)
    return match.group(1) if match else ""


def _powershell() -> str:
    """Windows PowerShell by full path, so PATH cannot decide which one runs."""
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    built_in = system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(built_in) if built_in.is_file() else "powershell"


def _verify_env(exe: Path) -> dict:
    """The environment for the signature check, with PowerShell's own variables removed.

    They are dropped rather than inherited because they may have been set by a *different*
    PowerShell: when the app is started from a PowerShell 7 session, the PS7 variables get
    passed down to Windows PowerShell 5.1, which then fails to load the module
    ``Get-AuthenticodeSignature`` lives in ("the member AuditToString is already present")
    and reports nothing at all. That is a refusal to install every genuine release, so the
    check is given a clean environment instead of a borrowed one.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PS") and "POWERSHELL" not in key.upper()
    }
    env["NMSVAULT_VERIFY_PATH"] = str(exe)
    return env


def verify_signature(exe: Path) -> None:
    """Refuse a launcher that is not a validly signed Python Software Foundation binary.

    The packaged launcher is a verbatim renamed copy of CPython's signed ``pythonw.exe``
    (see ``packaging/build_portable.ps1``, which refuses to package anything else), so a
    download whose launcher fails this check is either corrupt or not ours.

    This proves the launcher is genuine; it says nothing about the Python code in
    ``_runtime\\app``, which is unsigned. That part rests on the HTTPS connection to
    GitHub and the host pinning in :func:`is_trusted_url`.
    """
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$s = Get-AuthenticodeSignature -LiteralPath $env:NMSVAULT_VERIFY_PATH; "
        "Write-Output $s.Status; "
        "Write-Output $s.SignerCertificate.Subject"
    )
    try:
        # -ExecutionPolicy Bypass applies to this one process and changes nothing on the
        # machine; without it the default Restricted policy can stop PowerShell loading
        # the module Get-AuthenticodeSignature lives in.
        proc = subprocess.run(
            [
                _powershell(), "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=_verify_env(exe),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateInstallError(
            f"could not verify the signature of the downloaded program: {exc}"
        ) from exc

    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    status = lines[0] if lines else ""
    subject = lines[1] if len(lines) > 1 else ""
    if not status:
        # No answer at all means the check did not run. Say why rather than reporting an
        # "unknown" signature, which reads like a verdict on the file.
        detail = " ".join((proc.stderr or "").split())[:300] or "no output"
        raise UpdateInstallError(
            f"the signature of the downloaded {LAUNCHER} could not be checked ({detail}). "
            "Nothing was installed."
        )
    if status != "Valid":
        raise UpdateInstallError(
            f"the downloaded {LAUNCHER} is not validly signed (status: {status}). "
            "Nothing was installed."
        )
    if "Python Software Foundation" not in subject:
        raise UpdateInstallError(
            f"the downloaded {LAUNCHER} is signed by an unexpected publisher: "
            f"{subject or 'unknown'}. Nothing was installed."
        )


# --- the hand-off ------------------------------------------------------------

# Batch, because it is the one interpreter guaranteed to be on the machine and it has no
# dependency on the runtime we are in the middle of replacing.
_UPDATER = r"""@echo off
setlocal EnableExtensions
title NMS Save Vault - installing update
set "SRC={src}"
set "DST={dst}"
set "WORK={work}"
set "LOG={log}"
set "EXE={launcher}"
set "BACKUP=%WORK%\launcher.backup"

> "%LOG%" echo NMS Save Vault update (started by process {pid})
>>"%LOG%" echo   from %SRC%
>>"%LOG%" echo   into %DST%

rem Keep a copy of the launcher before anything else. Deleting the original is how the
rem loop below learns the app has really gone, so it has to be possible to put back if a
rem later step fails. Copying a running executable is allowed; deleting one is not.
copy /y "%DST%\%EXE%" "%BACKUP%" >nul 2>&1

rem Wait for the app to exit. Windows refuses to delete the image of a running process,
rem so a delete that fails means it is still up -- the same test install.bat uses. It is
rem checked this way rather than by asking about the process id because this can only
rem fail safe: if anything goes wrong the delete does not succeed, the loop times out,
rem and not one file has been touched.
set /a tries=0
:wait
del /f /q "%DST%\%EXE%" >nul 2>&1
if not exist "%DST%\%EXE%" goto ready
set /a tries+=1
if %tries% GEQ 60 (
    >>"%LOG%" echo ERROR: timed out waiting for the app to close
    call :say "NMS Save Vault did not close, so the update was not installed. Nothing was changed."
    goto cleanup
)
ping -n 2 127.0.0.1 >nul
goto wait

:ready
rem Move the old runtime aside instead of deleting it, so a failed copy can put back
rem exactly what was there. state.json and accounts.ini live in the install folder and
rem are never in the zip, so the user's settings survive untouched.
if exist "%DST%\_runtime.old" rmdir /s /q "%DST%\_runtime.old"
if exist "%DST%\_runtime" ren "%DST%\_runtime" "_runtime.old"
if exist "%DST%\_runtime" (
    >>"%LOG%" echo ERROR: could not move the old _runtime aside
    copy /y "%BACKUP%" "%DST%\%EXE%" >nul 2>&1
    call :say "The update could not replace the program files, so it was cancelled. Your existing version is unchanged."
    start "" "%DST%\%EXE%"
    goto cleanup
)

xcopy "%SRC%" "%DST%" /e /i /y /q >>"%LOG%" 2>&1
if errorlevel 1 goto rollback

>>"%LOG%" echo Installed.
if exist "%DST%\_runtime.old" rmdir /s /q "%DST%\_runtime.old"
start "" "%DST%\%EXE%"
goto cleanup

:rollback
>>"%LOG%" echo ERROR: the copy failed; restoring the previous version
if exist "%DST%\_runtime" rmdir /s /q "%DST%\_runtime"
if exist "%DST%\_runtime.old" ren "%DST%\_runtime.old" "_runtime"
copy /y "%BACKUP%" "%DST%\%EXE%" >nul 2>&1
call :say "The update could not be installed and the previous version was restored. Details: %LOG%"
start "" "%DST%\%EXE%"

:cleanup
if exist "%WORK%" rmdir /s /q "%WORK%"
exit /b

:say
rem This script has no console of its own, so a message box is the only way to be heard.
set "MSG=%~1"
powershell -NoProfile -NonInteractive -Command "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show($env:MSG, 'NMS Save Vault', 'OK', 'Warning')"
exit /b 0
"""


def updater_script(*, pid: int, src: Path, dst: Path, work: Path, log: Path) -> str:
    """The batch that waits for this process to exit and then swaps the folders."""
    return _UPDATER.format(
        pid=pid, src=str(src), dst=str(dst), work=str(work), log=str(log), launcher=LAUNCHER
    )


@dataclass(frozen=True)
class StagedUpdate:
    """A verified update sitting in a temp folder, waiting to be applied."""

    version: str
    script: Path
    app_dir: Path
    install_dir: Path
    log: Path


def prepare(
    release: Release,
    install_dir: Path | None = None,
    *,
    progress=None,
    workdir: Path | None = None,
    pid: int | None = None,
) -> StagedUpdate:
    """Download, unpack and verify ``release``, ready to be applied on exit.

    Nothing in the install folder is touched here; on any failure the running program is
    exactly as it was.
    """
    install_dir = Path(install_dir) if install_dir else portable_root()
    if install_dir is None:
        raise UpdateInstallError(can_install(release)[1])
    if not release.has_asset:
        raise UpdateInstallError("that release has no downloadable zip attached to it")

    pid = os.getpid() if pid is None else pid
    work = Path(workdir) if workdir else Path(tempfile.gettempdir()) / f"{TEMP_PREFIX}{pid}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    zip_path = download(release.asset_url, work / "release.zip", progress=progress)
    app_dir = unpack(zip_path, work / "unpacked")

    found = staged_version(app_dir)
    if found and not _same_version(found, release.version):
        raise UpdateInstallError(
            f"the download says it is version {found}, but {release.version} was expected. "
            "Nothing was installed."
        )
    verify_signature(app_dir / LAUNCHER)

    log = work.parent / f"{TEMP_PREFIX}{pid}.log"
    script = work.parent / f"{TEMP_PREFIX}{pid}.cmd"
    script.write_text(
        updater_script(pid=pid, src=app_dir, dst=install_dir, work=work, log=log),
        encoding="utf-8",
    )
    return StagedUpdate(
        version=found or release.version,
        script=script,
        app_dir=app_dir,
        install_dir=install_dir,
        log=log,
    )


def _same_version(a: str, b: str) -> bool:
    return parse_version(a) == parse_version(b)


def launch(staged: StagedUpdate) -> None:
    """Start the updater and return. The caller must then exit promptly -- the script is
    already waiting for this process to release its files."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    try:
        subprocess.Popen(  # noqa: S603 - a script this module just wrote, in our temp dir
            ["cmd", "/c", str(staged.script)],
            cwd=str(staged.script.parent),
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise UpdateInstallError(f"could not start the updater: {exc}") from exc


def clean_temp(pid: int | None = None, keep_logs_for: float = LOG_RETENTION_SECONDS) -> None:
    """Remove leftovers from a previous update.

    Logs outlive the rest: when an install fails the user is shown the log's path, and
    deleting it on the next start would take the evidence away before they read it.

    Failures are ignored on purpose -- this is tidying, and a file still in use is simply
    cleaned up on a later run.
    """
    mine = os.getpid() if pid is None else pid
    try:
        entries = list(Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*"))
    except OSError:
        return
    now = time.time()
    for entry in entries:
        if entry.name.startswith(f"{TEMP_PREFIX}{mine}"):
            continue  # never delete what this run is using
        try:
            if entry.suffix.lower() == ".log" and now - entry.stat().st_mtime < keep_logs_for:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except OSError:
            pass
