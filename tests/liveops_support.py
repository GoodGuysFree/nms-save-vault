"""Shared helpers for the live-operation tests: an OpContext over a sandbox vault, live
sources and a whole-tree fingerprint. Every path is under the test's tmp_path."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from nms_save_vault.core import platform
from nms_save_vault.core.catalog import Vault
from nms_save_vault.core.liveops import OpContext, PairSource
from nms_save_vault.core.organizer.locator import Location
from nms_save_vault.core.organizer.model import StoreRef
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
        host=host_in(tmp_path), app_version="nms-save-vault test",
        machine_id=machine_id, game_probe=game_probe, clock=clock, faults=faults)


MAIN = 0xDDDAD1BDD39F9C72          # the playthrough live in Steam slot 1
SECOND = 0x0123456789ABCDEF        # Steam slot 2 (one member)
POLESTAR = 0x7E57AB1E00000001      # only in the other account, and in its vault backup
T0 = 1_790_000_000


def steam_account(parent: Path, name: str = STEAM_ACCOUNT, *, cloud: bool = True) -> Path:
    """A live Steam folder: 'Main' in slot 1 (A newer), 'Second' in slot 2 (A only),
    account files, the cloud marker and a cache file."""
    from .conftest import steam_save

    folder = parent / name
    steam_save(folder, 1, 0, "Main", 7200, MAIN, T0 + 100, extra="main-a" + name)
    steam_save(folder, 1, 1, "Main", 7100, MAIN, T0 + 50, extra="main-b" + name)
    steam_save(folder, 2, 0, "Second", 600, SECOND, T0 + 10, extra="second-a" + name)
    (folder / "accountdata.hg").write_bytes(b"account data")
    (folder / "mf_accountdata.hg").write_bytes(b"account meta")
    if cloud:
        (folder / "steam_autocloud.vdf").write_bytes(b'"cloud" { "accountid" "1" }\n')
    (folder / "cache").mkdir(exist_ok=True)
    (folder / "cache" / "thumb0.dds").write_bytes(b"thumb")
    return folder


@dataclass
class World:
    tmp: Path
    vault_root: Path
    live: Path                     # Steam account 1, configured
    other: Path                    # Steam account 2, configured, holds 'Polestar' in slot 3
    backup_id: str                 # a vault full backup of the other account
    sources: list
    host: platform.Host
    organizer: object = None
    ctx: OpContext | None = None

    def refresh(self, **kw) -> OpContext:
        """A fresh organizer and context (what the app has after a refresh)."""
        from nms_save_vault.core.liveops.context import context_for
        from nms_save_vault.core.organizer import Organizer

        self.organizer = Organizer(self.vault_root, self.sources, host=self.host,
                                   game_running=lambda: False)
        kw.setdefault("game_probe", lambda: False)
        self.ctx = context_for(self.organizer, host=self.host, machine_id="m-00000000000a",
                               app_version="nms-save-vault test", **kw)
        return self.ctx

    def polestar(self, load=None) -> PairSource:
        """Polestar as kept in the vault backup of the other account (slot 3)."""
        entry = self.ctx.versions.entry(self.backup_id)
        a, b = entry.member(3, "A"), entry.member(3, "B")
        return PairSource(a.vid, b.vid, load,
                          Location(a.vid, "store", StoreRef(self.backup_id), 3, "A", "steam", True))


def steam_world(tmp_path: Path, *, cloud: bool = True, other_name: str = STEAM_ACCOUNT_2) -> World:
    """Two configured Steam-format accounts and a vault in store mode holding a backup of the
    second one ('Polestar' in its slot 3, played more on A). ``other_name="DefaultUser"``
    makes the second one GOG's folder."""
    from nms_save_vault.core import store as vstore

    from .conftest import steam_save

    live = steam_account(tmp_path / "NMS", cloud=cloud)
    other = steam_account(tmp_path / "NMS", other_name, cloud=cloud)
    steam_save(other, 3, 0, "Polestar", 4000, POLESTAR, T0 + 300, extra="pole-a")
    steam_save(other, 3, 1, "Polestar", 3900, POLESTAR, T0 + 200, extra="pole-b")
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    vstore.create_store(vault_root)
    store = vstore.open_store(vault_root, live_roots=[live, other])
    backup = store.add_from_live(other, platform="steam", account_key=other.name, kind="full",
                                 label="backup of laptop", scope=vstore.CaptureScope())
    sources = [live_source(live, label="Desktop"), live_source(other, label="Laptop")]
    world = World(tmp_path, vault_root, live, other, backup.id, sources, host_in(tmp_path))
    world.refresh()
    return world


def tree_hash(root: Path) -> dict[str, tuple[str, int]]:
    """sha256 and mtime_ns of every file under ``root``: the "nothing changed" check."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                                                   p.stat().st_mtime_ns)
    return out
