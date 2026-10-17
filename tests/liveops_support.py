"""Shared helpers for the live-operation tests: an OpContext over a sandbox vault, live
sources and a whole-tree fingerprint. Every path is under the test's tmp_path."""
from __future__ import annotations

import hashlib
from pathlib import Path

from nms_save_vault.core import platform
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.liveops import OpContext, Policies
from nms_save_vault.core.organizer.store import OrganizerStore
from nms_save_vault.core.state import Source

STEAM_ACCOUNT = "st_76561198000000001"
STEAM_ACCOUNT_2 = "st_76561198000000002"
XBOX_ACCOUNT = "0000000000000001_29070100B936489ABCE8B9AF3980429C"


def host_in(tmp_path: Path) -> platform.Host:
    """A Windows host whose per-user folders are inside the sandbox."""
    local = tmp_path / "localappdata"
    return platform.Host(platform.WINDOWS, tmp_path / "home", {"LOCALAPPDATA": str(local)})


def live_source(path: Path, *, platform_name: str = "steam", label: str = "",
                writable: bool = True, enabled: bool = True) -> Source:
    tag = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:6]
    return Source(id=f"{platform_name}-{path.name}-{tag}",
                  platform=platform_name, account=path.name, path=str(path), label=label,
                  writable=writable, enabled=enabled)


def make_ctx(tmp_path: Path, sources=(), *, vault_root: Path | None = None,
             clock=lambda: 1_792_500_000.0, machine_id: str = "m-00000000000a",
             game_probe=lambda: False, faults=None, lock_timeout: float = 10.0) -> OpContext:
    root = vault_root or tmp_path / "vault"
    root.mkdir(parents=True, exist_ok=True)
    return OpContext(
        vault=Vault(root), versions=None,
        store=OrganizerStore(root, create=False, clock=clock, lock_timeout=lock_timeout),
        locator=None, lineage=None, readmodel=None, sources=list(sources),
        policies=Policies(), host=host_in(tmp_path), app_version="nms-save-vault test",
        machine_id=machine_id, game_probe=game_probe, clock=clock, faults=faults)


def tree_hash(root: Path) -> dict[str, tuple[str, int]]:
    """sha256 and mtime_ns of every file under ``root``: the "nothing changed" check."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                                                   p.stat().st_mtime_ns)
    return out
