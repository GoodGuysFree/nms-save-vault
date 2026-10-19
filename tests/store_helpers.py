"""Helpers for the version-store tests: a store in a tmp vault, and tree fingerprints."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import versionstore

from .conftest import steam_save

FIXED_NOW = datetime(2026, 10, 16, 11, 15, 0, tzinfo=timezone.utc)
STEAM_ACCOUNT = "st_76561198000000001"
BIG = "x" * 9000          # makes a save's data larger than the inline limit
IDENT = 0x0110000100000001


def steam_live(parent: Path, *, cache: bool = True) -> Path:
    """A live Steam folder: slot 1 (both members, big data), slot 2 A (small data), account
    files, the autocloud marker, a stray file and three cache files."""
    folder = parent / STEAM_ACCOUNT
    steam_save(folder, 1, 0, "Main", 7200, IDENT, 1_790_000_000, extra=BIG)
    steam_save(folder, 1, 1, "Main", 7100, IDENT, 1_789_990_000, extra=BIG + "b")
    steam_save(folder, 2, 0, "Second", 600, IDENT + 1, 1_789_000_000)
    (folder / "accountdata.hg").write_bytes(bytes(range(256)) * 50)
    (folder / "mf_accountdata.hg").write_bytes(b"\x01" * 432)
    (folder / "steam_autocloud.vdf").write_bytes(b'"cloud" { "accountid" "1" }\n')
    (folder / "notes.txt").write_bytes(b"not a save file")
    if cache:
        (folder / "cache").mkdir()
        for i in range(3):
            (folder / "cache" / f"thumb{i}.dds").write_bytes(bytes([i]) * (4000 + 6000 * i))
    return folder


def new_vault(tmp_path: Path, name: str = "vault") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    vstore.create_store(root, clock=lambda: FIXED_NOW)
    return root


def open_vault_store(vault: Path, **kw) -> vstore.VersionStore:
    """The vault's store with a real lock. No live folders unless the test names some, so no
    test depends on (or even looks at) this machine's real save folders."""
    kw.setdefault("lock", versionstore.vault_lock(vault))
    kw.setdefault("app_version", "test")
    kw.setdefault("live_roots", ())
    return vstore.VersionStore.open(vault, **kw)


def fingerprint(folder: Path) -> dict[str, tuple[int, int, str]]:
    """relpath -> (size, mtime_ns, sha256) of every file under ``folder``."""
    out = {}
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(folder).as_posix()] = (
                st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out
