"""Connecting the index to a vault for the CLI and tools: scan, read bytes, find parents.

D1's ``VersionLocator`` and read model are not built yet; until they are, this module gives
the index what it needs from the organizer scanner (D1 2.5) and journal: a read-only
locator over the scan's locations (sha256 checked on every read), the vault's id for the
per-machine cache folder, and each version's drawn parent from the journal's edges. Nothing
here writes anywhere except the index cache and the scanner's own per-machine cache.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import locations, platform, safety, savedir
from ..catalog import Vault
from ..organizer import cache as org_cache
from ..organizer import scanner
from ..organizer.journal import Journal
from ..organizer.model import EntryRef, LiveRef, Vid
from ..organizer.store import OrganizerStore, store_root
from ..state import Source
from .cache import cache_file
from .pool import ExecutionPolicy
from .service import IndexService

TIER_ORDER = {"owner": 0, "recorded": 1, "observed": 2}


class ReadOnlyVault(Vault):
    """The vault as the index sees it: loaded and read, never written - not even the clean-up
    of a catalog save's stale temp files that ``Vault.load`` does."""

    def _remove_stale_temp_files(self) -> None:
        pass

    def ensure(self) -> None:
        raise PermissionError("the content index never writes the vault")

    def save(self) -> None:
        raise PermissionError("the content index never writes the vault")


class VersionUnavailable(LookupError):
    """No location of the version could be read with the right bytes."""


@dataclass(frozen=True)
class OpenedVersion:
    data: bytes
    meta_plain: bytes        # Xbox metas are plaintext; Steam metas are not needed here
    platform: str
    path: Path


class ScanLocator:
    """``open(vid)`` over a :class:`scanner.ScanResult`: each location in turn, bytes checked."""

    def __init__(self, scan: scanner.ScanResult, vault: Vault | None) -> None:
        self.scan = scan
        self.vault = vault

    def _folder(self, where) -> Path | None:
        if isinstance(where, LiveRef):
            found = self.scan.live.get(where)
            return Path(found.source.path) if found else None
        if isinstance(where, EntryRef) and self.vault is not None:
            entry = self.vault.get(where.entry_id)
            return Path(entry.path) if entry else None
        return None

    def open(self, vid: Vid, prefer=None) -> OpenedVersion:
        info = self.scan.versions.get(vid)
        for loc in info.locations if info else ():
            folder = self._folder(loc.where)
            if folder is None:
                continue
            try:
                view = savedir.scan_any(folder)
                slot = view.slots.get(loc.slot)
                member = None if slot is None else (slot.a if loc.member == "A" else slot.b)
                if member is None or not member.exists:
                    continue
                data = Path(member.data_path).read_bytes()
                if hashlib.sha256(data).hexdigest() != vid:
                    continue                      # changed since the scan
                meta = Path(member.meta_path).read_bytes() if info.platform == "xbox" else b""
                return OpenedVersion(data, meta, info.platform, Path(member.data_path))
            except OSError:
                continue
        raise VersionUnavailable(f"no readable copy of version {vid[:12]}")


def vault_id_of(vault_root: Path) -> str | None:
    if not store_root(vault_root).is_dir():
        return None
    return OrganizerStore(vault_root, create=False, read_only=True).vault_id


def scan_vault(vault: Vault, sources: Sequence[Source], vault_id: str | None,
               host: platform.Host | None = None) -> scanner.ScanResult:
    cache = org_cache.ScanCache.load(org_cache.cache_dir(vault.root, vault_id, host) / org_cache.FILENAME, vault_id)
    return scanner.scan(vault, sources, cache, game_running=safety.is_game_running())


def drawn_parents(vault_root: Path, scan: scanner.ScanResult) -> dict[Vid, tuple[Vid, str]]:
    """child -> (parent, edge kind) from the journal's edges, the best tier when there are
    several; empty without an organizer store. Never inferred from content (N1)."""
    if not store_root(vault_root).is_dir():
        return {}
    state = Journal(OrganizerStore(vault_root, create=False, read_only=True), scan.versions).replay()
    best: dict[Vid, tuple[int, Vid, str]] = {}
    for edge in state.edges:
        rank = TIER_ORDER.get(edge.tier, 9)
        if edge.child not in best or rank < best[edge.child][0]:
            best[edge.child] = (rank, edge.parent, edge.kind)
    return {child: (parent, kind) for child, (_rank, parent, kind) in best.items()}


def resolve(ref: str, vids: Sequence[Vid] | Mapping[Vid, object]) -> Vid:
    """A vid from a unique hex prefix (at least 6 characters)."""
    ref = ref.strip().lower()
    if len(ref) < 6 or any(c not in "0123456789abcdef" for c in ref):
        raise LookupError("give at least 6 hex characters of a version id")
    found = [v for v in vids if v.startswith(ref)]
    if len(found) != 1:
        raise LookupError("no version matches" if not found else f"{len(found)} versions match; give more characters")
    return found[0]


def live_vids(scan: scanner.ScanResult) -> list[Vid]:
    return [vid for f in scan.live.values() for c in f.captures for m in c.members for vid in [m.vid] if vid]


def forbidden_roots(vault: Vault, sources: Sequence[Source], host: platform.Host | None = None) -> list[Path]:
    """Where the cache must never be: the vault, every live folder, every NMS save root."""
    return [vault.root, *(Path(s.path) for s in sources), *platform.nms_roots(host)]


def open_service(vault: Vault, sources: Sequence[Source], *, workers: str | int = "auto",
                 host: platform.Host | None = None,
                 game_running: Callable[[], bool | None] = safety.is_game_running,
                 ) -> tuple[IndexService, scanner.ScanResult, str | None]:
    host = host or platform.Host.current()
    vault_id = vault_id_of(vault.root)
    scan = scan_vault(vault, sources, vault_id, host)
    policy: ExecutionPolicy = platform.index_execution_policy(
        host, portable_kit=bool(host.env.get("NMSVAULT_PORTABLE_ROOT")), cpu_count=os.cpu_count(), setting=workers)
    sizes = {vid: info.meta.size_decompressed for vid, info in scan.versions.items()
             if getattr(info.meta, "size_decompressed", 0)}
    stamps = {vid: info.meta.meta_ts for vid, info in scan.versions.items()}
    service = IndexService(
        cache_path=cache_file(org_cache.cache_dir(vault.root, vault_id, host)), vault_id=vault_id or "nostore",
        locator=ScanLocator(scan, vault), policy=policy, size_hint=sizes.get, when=stamps.get,
        forbidden=forbidden_roots(vault, sources, host), app_version=_app_version(), game_running=game_running)
    return service, scan, vault_id


def _app_version() -> str:
    from ... import __version__
    return __version__


def default_sources() -> list[Source]:
    from .. import state as appstate
    st = appstate.load()
    if st is not None:
        return [s for s in st.live_sources if s.exists]
    found = locations.default_live_save_dir()
    return [Source(id="live", platform="steam", account=found.name, path=str(found))] if found else []
