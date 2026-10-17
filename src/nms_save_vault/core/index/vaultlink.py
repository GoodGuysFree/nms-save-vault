"""Connecting the index to a vault for the CLI and tools, through D1's organizer.

A read-only :class:`organizer.Organizer` gives everything the index needs: the scan, the
``VaultLocator`` that reads a version's bytes (sha256 checked on every read), the read model
(representatives, tips, version references) and the journal's recorded parents. Opened
read-only, it writes nothing in the vault - not even the catalog tidy-up - so the only files
written are the per-machine scan cache and the index cache.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .. import locations, platform, safety
from ..organizer import Organizer
from ..organizer import cache as org_cache
from ..organizer.journal import Journal
from ..organizer.locator import OpenedVersion, VersionUnavailable
from ..organizer.model import Vid
from ..organizer.scanner import ScanResult
from ..state import Source
from .cache import cache_file
from .pool import ExecutionPolicy
from .service import IndexService

TIER_RANK = {"owner": 3, "recorded": 2, "observed": 1}     # the read model's own order


class OrganizerLocator:
    """The version locator of the organizer's latest refresh, followed as it is replaced:
    the dispatcher thread opens bytes while the UI thread may refresh."""

    def __init__(self, organizer: Organizer) -> None:
        self.organizer = organizer

    def open(self, vid: Vid, prefer=None) -> OpenedVersion:
        locator = self.organizer.locator
        if locator is None:
            raise VersionUnavailable("the vault has not been scanned yet")
        return locator.open(vid, prefer)


def read_only_organizer(vault_root: Path, sources: Sequence[Source], *, host: platform.Host | None = None,
                        game_running: Callable[[], bool | None] = safety.is_game_running) -> Organizer:
    """The organizer opened only to look: no journal, no notes, no catalog tidying."""
    return Organizer(Path(vault_root), sources, read_only=True, machine_cache=True, game_running=game_running,
                     host=host)


def drawn_parents(organizer: Organizer) -> dict[Vid, tuple[Vid, str]]:
    """child -> (parent, edge kind) from the journal's edges: the highest tier, the later
    within a tier, as the read model draws them. Never inferred from content (N1)."""
    if organizer.scan is None:
        raise ValueError("refresh the organizer first")
    state = Journal(organizer.store, organizer.scan.versions).replay()
    best: dict[Vid, tuple[tuple[int, int], Vid, str]] = {}
    for edge in state.edges:
        rank = (TIER_RANK.get(edge.tier, 0), edge.created)
        if edge.child not in best or rank > best[edge.child][0]:
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


def live_vids(scan: ScanResult) -> list[Vid]:
    return [vid for f in scan.live.values() for c in f.captures for m in c.members for vid in [m.vid] if vid]


def forbidden_roots(vault_root: Path, sources: Sequence[Source], host: platform.Host | None = None) -> list[Path]:
    """Where the cache must never be: the vault, every live folder, every NMS save root."""
    return [Path(vault_root), *(Path(s.path) for s in sources), *platform.nms_roots(host)]


def open_service(vault_root: Path, sources: Sequence[Source], *, workers: str | int = "auto",
                 host: platform.Host | None = None,
                 game_running: Callable[[], bool | None] = safety.is_game_running,
                 ) -> tuple[IndexService, Organizer]:
    """A refreshed read-only organizer and an index service reading through its locator."""
    host = host or platform.Host.current()
    organizer = read_only_organizer(vault_root, sources, host=host, game_running=game_running)
    organizer.refresh(record=False)
    policy: ExecutionPolicy = platform.index_execution_policy(
        host, portable_kit=bool(host.env.get("NMSVAULT_PORTABLE_ROOT")), cpu_count=os.cpu_count(), setting=workers)
    vault_id = organizer.store.vault_id

    def meta(vid: Vid):
        info = organizer.scan.versions.get(vid) if organizer.scan else None
        return info.meta if info else None

    service = IndexService(
        cache_path=cache_file(org_cache.cache_dir(Path(vault_root), vault_id, host)), vault_id=vault_id or "nostore",
        locator=OrganizerLocator(organizer), policy=policy,
        size_hint=lambda vid: (m.size_decompressed or None) if (m := meta(vid)) else None,
        when=lambda vid: m.meta_ts if (m := meta(vid)) else None,
        forbidden=forbidden_roots(vault_root, sources, host), app_version=_app_version(), game_running=game_running)
    return service, organizer


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
