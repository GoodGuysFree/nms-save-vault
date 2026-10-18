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
from ..organizer.locator import OpenedVersion, VersionUnavailable
from ..organizer.model import Vid
from ..state import Source
from .cache import cache_file
from .pool import ExecutionPolicy, NotReady
from .service import IndexService


class OrganizerLocator:
    """The version locator of the organizer's latest refresh, followed as it is replaced:
    the dispatcher thread opens bytes while the UI thread may refresh."""

    def __init__(self, organizer: Organizer) -> None:
        self.organizer = organizer

    def open(self, vid: Vid, prefer=None) -> OpenedVersion:
        locator = self.organizer.locator
        if locator is None:
            raise NotReady("the vault has not been scanned yet")
        return locator.open(vid, prefer)


def read_only_organizer(vault_root: Path, sources: Sequence[Source], *, host: platform.Host | None = None,
                        game_running: Callable[[], bool | None] = safety.is_game_running) -> Organizer:
    """The organizer opened only to look: no journal, no notes, no catalog tidying."""
    return Organizer(Path(vault_root), sources, read_only=True, machine_cache=True, game_running=game_running,
                     host=host)


def drawn_parents(organizer: Organizer) -> dict[Vid, tuple[Vid, str | None]]:
    """version -> (drawn parent, edge kind), exactly as the read model draws them
    (``VersionRow.parent`` / ``parent_kind``). Never inferred from content (N1)."""
    model = organizer.model
    if model is None:
        raise ValueError("refresh the organizer first")
    out: dict[Vid, tuple[Vid, str | None]] = {}
    for line in model.lines():
        for r in model.versions(line.pid):
            if r.parent is None:
                continue
            out[r.head] = (r.parent, r.parent_kind)
            if r.partner is not None:                   # the pair is one drawn entry: its other member too
                out.setdefault(r.partner, (r.parent, r.parent_kind))
    return out


def resolve(ref: str, vids: Sequence[Vid] | Mapping[Vid, object]) -> Vid:
    """A vid from a unique hex prefix (at least 6 characters)."""
    ref = ref.strip().lower()
    if len(ref) < 6 or any(c not in "0123456789abcdef" for c in ref):
        raise LookupError("give at least 6 hex characters of a version id")
    found = [v for v in vids if v.startswith(ref)]
    if len(found) != 1:
        raise LookupError("no version matches" if not found else f"{len(found)} versions match; give more characters")
    return found[0]


def forbidden_roots(vault_root: Path, sources: Sequence[Source], host: platform.Host | None = None) -> list[Path]:
    """Where the cache must never be: the vault, every live folder, every NMS save root."""
    return [Path(vault_root), *(Path(s.path) for s in sources), *platform.nms_roots(host)]


def open_service(vault_root: Path, sources: Sequence[Source], *, workers: str | int = "auto",
                 host: platform.Host | None = None,
                 game_running: Callable[[], bool | None] = safety.is_game_running,
                 ) -> tuple[IndexService, Organizer]:
    """A read-only organizer, refreshed with the index attached (so its read model carries
    facts), and the index service reading through its locator. Nothing is requested yet."""
    host = host or platform.Host.current()
    organizer = read_only_organizer(vault_root, sources, host=host, game_running=game_running)
    policy: ExecutionPolicy = platform.index_execution_policy(
        host, portable_kit=bool(host.env.get("NMSVAULT_PORTABLE_ROOT")), cpu_count=os.cpu_count(), setting=workers)
    vault_id = organizer.store.vault_id

    def meta(vid: Vid):
        info = organizer.scan.versions.get(vid) if organizer.scan else None
        return info.meta if info else None

    service = IndexService(
        cache_path=cache_file(org_cache.cache_dir(Path(vault_root), vault_id, host)), vault_id=vault_id or "nostore",
        locator=OrganizerLocator(organizer), policy=policy, defer_refresh=True,
        size_hint=lambda vid: (m.size_decompressed or None) if (m := meta(vid)) else None,
        when=lambda vid: m.meta_ts if (m := meta(vid)) else None,
        forbidden=forbidden_roots(vault_root, sources, host), app_version=_app_version(), game_running=game_running)
    organizer.facts = service
    service.flush()                     # the cached views are loaded (a command line may wait)
    organizer.refresh(record=False, request_facts=False)
    service.queue_refresh()                    # only now can the locator open bytes
    return service, organizer


def attach_service(organizer: Organizer, sources: Sequence[Source], *, workers: str | int = "auto",
                   host: platform.Host | None = None,
                   game_running: Callable[[], bool | None] = safety.is_game_running) -> IndexService:
    """For the app: an index service reading through ``organizer``'s own locator, attached as
    its facts provider. Nothing is requested and nothing is read until the organizer has
    scanned (``defer_refresh``): the caller queues the refresh after its first scan."""
    host = host or organizer.host
    vault_root = organizer.vault_root
    policy: ExecutionPolicy = platform.index_execution_policy(
        host, portable_kit=bool(host.env.get("NMSVAULT_PORTABLE_ROOT")), cpu_count=os.cpu_count(), setting=workers)

    def meta(vid: Vid):
        info = organizer.scan.versions.get(vid) if organizer.scan else None
        return info.meta if info else None

    service = IndexService(
        cache_path=cache_file(org_cache.cache_dir(vault_root, organizer.store.vault_id, host)),
        vault_id=organizer.store.vault_id or "nostore", locator=OrganizerLocator(organizer), policy=policy,
        defer_refresh=True,
        size_hint=lambda vid: (m.size_decompressed or None) if (m := meta(vid)) else None,
        when=lambda vid: m.meta_ts if (m := meta(vid)) else None,
        forbidden=forbidden_roots(vault_root, sources, host), app_version=_app_version(),
        game_running=game_running)
    organizer.facts = service
    return service


def index_enabled(setting: object = None) -> bool:
    """The app runs the content index unless ``NMSVAULT_INDEX=off`` (tests, support) or the
    ``ui.index`` preference says off."""
    return os.environ.get("NMSVAULT_INDEX", "").lower() != "off" and setting != "off"


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
