"""Helpers for the version-store tests: a store in a tmp vault, and tree fingerprints."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from nms_save_vault.core import store as vstore
from nms_save_vault.core.store import versionstore

FIXED_NOW = datetime(2026, 10, 16, 11, 15, 0, tzinfo=timezone.utc)


def new_vault(tmp_path: Path, name: str = "vault") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    vstore.create_store(root, clock=lambda: FIXED_NOW)
    return root


def open_vault_store(vault: Path, **kw) -> vstore.VersionStore:
    kw.setdefault("lock", versionstore.vault_lock(vault))
    kw.setdefault("app_version", "test")
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
