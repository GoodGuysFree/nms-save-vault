"""Assemble the portable Linux and macOS kits for NMS Save Vault.

Output: ``build/kits/NMSSaveVault-<os>-<arch>.tar.gz`` -- a self-contained folder that runs
on a machine with no Python installed.

The Windows kit is intricate because it has an Authenticode signature to preserve, which is
why it cannot patch its launcher and dispatches from ``sitecustomize.py`` instead. Off
Windows there is no signature, so the launcher is simply a shell script. The *layout* is
kept identical to the Windows one on purpose (a launcher with ``_runtime/app`` beside it,
exporting ``NMSVAULT_PORTABLE_ROOT``) so a future cross-platform self-updater is a small
job rather than a redesign -- see DESIGN.md, D4.

This script runs anywhere, including on Windows: the runtime is repacked tar-to-tar without
ever being written to the local filesystem, so POSIX permissions and symlinks survive being
built on a machine that has neither.

Usage:
    python packaging/build_posix_kit.py [linux | macos | all]
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# The interpreter shipped to users: the 3.12 series the project is developed against, and
# the last python-build-standalone release that still builds it against Tcl/Tk 8.6.
#
# Everything from 20260203 onwards links 3.12 against Tcl/Tk 9.0, which CPython only
# supports officially from 3.13. Matching the Windows kit's Tk 8.6 matters more than a
# newer interpreter here: it means a tester's GUI report is about the port rather than
# about a Tk major version the app has never run on. Revisit once the GUI is confirmed
# working on Linux and macOS -- moving up is these two constants and TK_EVIDENCE.
PYTHON_VERSION = "3.12.12"
PBS_RELEASE = "20251217"
PBS_BASE = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_RELEASE}"

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "runtime-cache"
OUTDIR = ROOT / "build" / "kits"

KIT_NAME = "NMSSaveVault"
APP_BUNDLE = "NMSSaveVault.app"
BUNDLE_ID = "com.goodguysfree.nmssavevault"

#: Proof that the runtime we are about to ship can actually draw the GUI, and that it is
#: the Tk the app is known to work against. A kit whose Python has no Tk is useless and the
#: failure would only surface on the tester's machine, so the build refuses instead -- the
#: same stance build_portable.ps1 takes on signatures. The explicit 8.6 also pins the Tk
#: major version: a runtime that quietly moved to 9.0 fails the build rather than shipping.
TK_EVIDENCE = ("_tkinter", "libtk8.6", "libtcl8.6", "lib/tk8.6/", "lib/tcl8.6/")


@dataclass(frozen=True)
class Target:
    key: str
    triple: str
    arch: str
    bundle: bool  # macOS also gets a double-clickable .app around the same runtime


TARGETS = {
    "linux": Target("linux", "x86_64-unknown-linux-gnu", "x86_64", bundle=False),
    "macos": Target("macos", "aarch64-apple-darwin", "arm64", bundle=True),
}


def asset_name(target: Target) -> str:
    # "stripped" is the same runtime without debug symbols: a third of the download for
    # an identical install, Tk included (the build asserts that below).
    return f"cpython-{PYTHON_VERSION}+{PBS_RELEASE}-{target.triple}-install_only_stripped.tar.gz"


# --- fetching -----------------------------------------------------------------


def download(url: str, dest: Path) -> Path:
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as fh:
        shutil.copyfileobj(response, fh)
    tmp.replace(dest)
    return dest


def expected_sha256(asset: str) -> str:
    """The published checksum for one release asset, from the release's SHA256SUMS."""
    sums = download(f"{PBS_BASE}/SHA256SUMS", CACHE / f"SHA256SUMS-{PBS_RELEASE}")
    for line in sums.read_text("utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if name.strip() == asset:
            return digest.strip()
    raise SystemExit(f"{asset} is not listed in SHA256SUMS for release {PBS_RELEASE}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def runtime_tarball(target: Target) -> Path:
    asset = asset_name(target)
    path = download(f"{PBS_BASE}/{asset}", CACHE / asset)
    actual, expected = sha256_file(path), expected_sha256(asset)
    if actual != expected:
        path.unlink(missing_ok=True)
        raise SystemExit(
            f"checksum mismatch for {asset}\n  expected {expected}\n  got      {actual}\n"
            "The cached copy has been removed; re-run to download it again."
        )
    return path


# --- the app payload ----------------------------------------------------------


def app_version() -> str:
    text = (ROOT / "src" / "nms_save_vault" / "__init__.py").read_text("utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not read __version__ from src/nms_save_vault/__init__.py")
    return match.group(1)


def app_sources() -> list[tuple[str, Path]]:
    """(archive path under _runtime/app, source file) for every module we ship."""
    package = ROOT / "src" / "nms_save_vault"
    out = []
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out.append((str(path.relative_to(package.parent).as_posix()), path))
    if not out:
        raise SystemExit("no application sources found under src/nms_save_vault")
    return out


LAUNCHER = """\
#!/bin/sh
# NMS Save Vault launcher.
#
# Keep this file together with the _runtime folder beside it -- copying it out on its own
# will not work, because the interpreter and the application both live in there.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")"{up} && pwd -P)
NMSVAULT_PORTABLE_ROOT="$here"
export NMSVAULT_PORTABLE_ROOT
PYTHONPATH="$here/_runtime/app"
export PYTHONPATH
exec "$here/_runtime/python/bin/python3" -m nms_save_vault.{module} "$@"
"""

INFO_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>NMS Save Vault</string>
    <key>CFBundleDisplayName</key>       <string>NMS Save Vault</string>
    <key>CFBundleIdentifier</key>        <string>{bundle_id}</string>
    <key>CFBundleExecutable</key>        <string>NMSSaveVault</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleShortVersionString</key><string>{version}</string>
    <key>CFBundleVersion</key>           <string>{version}</string>
    <key>LSMinimumSystemVersion</key>    <string>11.0</string>
    <key>NSHighResolutionCapable</key>   <true/>
</dict>
</plist>
"""


#: The one command that makes this kit safe to try. Kept per-platform and copy-pasteable,
#: because "back up first" that the reader has to translate is advice nobody follows.
BACKUP_COMMAND = {
    "linux": """\
    # If the game is on a second drive or an SD card this path differs. Run
    #     ./nmsvault status
    # first -- it prints the folder it found -- and copy that one instead.
    cp -a ~/.local/share/Steam/steamapps/compatdata/275850/pfx/drive_c/users/steamuser/\\
AppData/Roaming/HelloGames/NMS  ~/nms-save-backup-$(date +%F)
""",
    "macos": """\
    cp -a ~/Library/Application\\ Support/HelloGames/NMS  ~/nms-save-backup-$(date +%F)
""",
}

#: How much this kit has actually been run, stated plainly rather than implied.
MATURITY = {
    "linux": "has been launched a handful of times on one Linux desktop",
    "macos": "has never been run on a Mac at all -- you may well be the first",
}


def readme(target: Target, version: str) -> str:
    if target.bundle:
        start = (
            "  1. Extract this archive somewhere you can keep it (your home folder is fine).\n"
            "  2. Open the NMSSaveVault folder and double-click NMSSaveVault.app.\n"
            "\n"
            "  macOS has not seen this app signed by a registered developer, so the first\n"
            "  launch is refused. To allow it: right-click NMSSaveVault.app, choose Open,\n"
            "  then confirm. You only have to do that once.\n"
            "\n"
            "  Keep NMSSaveVault.app inside this folder -- it uses the _runtime folder next\n"
            "  to it, so dragging just the app to /Applications will not work.\n"
        )
    else:
        start = (
            "  1. Extract this archive somewhere you can keep it (your home folder is fine).\n"
            "  2. Run ./NMSSaveVault from that folder, or launch it from your file manager.\n"
        )
    return f"""\
NMS Save Vault {version} -- portable kit for {target.key} ({target.arch})
================================================================================

Safe backup, catalog and slot management for No Man's Sky save files.
https://github.com/GoodGuysFree/nms-save-vault


  ##########################################################################
  #                                                                        #
  #   STOP -- COPY YOUR SAVE FOLDER BEFORE YOU RUN THIS PROGRAM.           #
  #                                                                        #
  ##########################################################################

  On Windows this tool has months of real use behind it. This build
  {MATURITY[target.key]}.

  The safety machinery is the same on every platform: it refuses to write
  while the game is running, snapshots your live folder before every
  destructive operation, writes atomically, and verifies every copy by hash.
  But none of that has been PROVEN on {target.key} yet, and your save folder is
  not the place to find out.

  Make your own copy first. One command:

{BACKUP_COMMAND[target.key]}
  Then check it is not empty:

    ls ~/nms-save-backup-*/st_*/

  You should see save*.hg and mf_save*.hg files in there. Keep that copy
  until you are satisfied nothing has gone wrong.

  To roll back: close the game, empty the live folder, copy your backup back.

  Steam Cloud is a sync, not a backup -- it will happily replace a good save
  with whatever it saw last. It is not a substitute for the copy above.


Python {PYTHON_VERSION} and Tk are bundled. Nothing is installed and nothing is
written outside this folder unless you ask for it.

Getting started
---------------
{start}
  NMSSaveVault   the graphical app
  nmsvault       the command line (./nmsvault status, ./nmsvault --help)

Where your saves are expected to be
-----------------------------------
  Linux: No Man's Sky runs under Proton, so its saves live inside a Steam library at
    steamapps/compatdata/275850/pfx/drive_c/users/steamuser/AppData/Roaming/HelloGames/NMS
  macOS:
    ~/Library/Application Support/HelloGames/NMS

The app finds these itself. If it does not, point it at the folder by hand:
    ./nmsvault status --live /path/to/st_<your steam id>

If the graphical app does not start
-----------------------------------
Run this and send us what it prints:

    ./_runtime/python/bin/python3 -c "import tkinter; print(tkinter.TkVersion); tkinter.Tk()"

This kit is UNTESTED on {target.key}. Please report anything that goes wrong at
https://github.com/GoodGuysFree/nms-save-vault/issues -- including the output above.

Licence: GPL-3.0-or-later. Bundled Python is the PSF licence.
"""


# --- assembly -----------------------------------------------------------------


def add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(Path(__file__).stat().st_mtime)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def copy_runtime(tar: tarfile.TarFile, source: Path, prefix: str) -> list[str]:
    """Stream the interpreter across tar-to-tar, keeping modes, symlinks and hardlinks.

    Nothing is extracted to disk, which is what lets a Windows machine produce a working
    POSIX kit: the mode bits and symlinks are archive metadata here, not filesystem state
    that Windows would have to be able to represent.
    """
    names: list[str] = []
    with tarfile.open(source, "r:gz") as src:
        for member in src:
            member.name = f"{prefix}/{member.name}"
            if member.islnk():  # hardlink targets are archive paths and move with it
                member.linkname = f"{prefix}/{member.linkname}"
            names.append(member.name)
            if member.isfile():
                extracted = src.extractfile(member)
                tar.addfile(member, extracted)
            else:
                tar.addfile(member)
    return names


def assert_has_tk(names: list[str]) -> None:
    missing = [token for token in TK_EVIDENCE if not any(token in n for n in names)]
    if missing:
        raise SystemExit(
            "the downloaded runtime has no usable Tk "
            f"(nothing matching {', '.join(missing)}). Refusing to package it."
        )


def build(target: Target) -> Path:
    version = app_version()
    print(f"building the {target.key} kit (app {version}, python {PYTHON_VERSION})")
    source = runtime_tarball(target)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    out = OUTDIR / f"{KIT_NAME}-v{version}-{target.key}-{target.arch}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        names = copy_runtime(tar, source, f"{KIT_NAME}/_runtime")
        assert_has_tk(names)

        for arcname, path in app_sources():
            add_bytes(tar, f"{KIT_NAME}/_runtime/app/{arcname}", path.read_bytes())

        for name, module in (("NMSSaveVault", "gui"), ("nmsvault", "cli")):
            script = LAUNCHER.format(module=module, up="")
            add_bytes(tar, f"{KIT_NAME}/{name}", script.encode("utf-8"), mode=0o755)

        add_bytes(tar, f"{KIT_NAME}/README.txt", readme(target, version).encode("utf-8"))

        if target.bundle:
            contents = f"{KIT_NAME}/{APP_BUNDLE}/Contents"
            plist = INFO_PLIST.format(bundle_id=BUNDLE_ID, version=version)
            add_bytes(tar, f"{contents}/Info.plist", plist.encode("utf-8"))
            # The bundle is a thin wrapper over the same runtime three levels up, so the
            # kit ships one interpreter rather than two.
            app_launcher = LAUNCHER.format(module="gui", up='/../../..')
            add_bytes(
                tar,
                f"{contents}/MacOS/NMSSaveVault",
                app_launcher.encode("utf-8"),
                mode=0o755,
            )

    print(f"  -> {out}  ({out.stat().st_size / 1048576:.1f} MB)")
    return out


def main(argv: list[str]) -> int:
    which = argv[1] if len(argv) > 1 else "all"
    if which == "all":
        chosen = list(TARGETS.values())
    elif which in TARGETS:
        chosen = [TARGETS[which]]
    else:
        raise SystemExit(f"usage: {Path(__file__).name} [linux | macos | all]")
    for target in chosen:
        build(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
