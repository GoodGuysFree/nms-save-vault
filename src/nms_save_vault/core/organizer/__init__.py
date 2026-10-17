"""The save-file organizer: one line per playthrough, its versions, where each is live, and
the history the app records (docs/organizer/designs/D1-core-model.md).

:class:`Organizer` is the one entry point the GUI and the CLI use: it scans, records what
the live slots hold (when asked to), and builds the read model.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from pathlib import Path

from .. import discover, safety
from ..catalog import Vault
from ..platform import Host
from ..state import Source
from . import bootstrap as _bootstrap
from .bootstrap import BootstrapReport
from .cache import FILENAME, ScanCache, cache_dir
from .facts import SEEN_FILENAME, SeenMarker, facts_targets
from .folders import FolderLocator
from .journal import SKIP_BUSY, SKIP_LOCK_FAILED, Journal, ObserveResult
from .lineage import read_lineage
from .locator import VaultLocator
from .model import EntryRef, LiveTarget
from .notes import Notes, read_notes
from .readmodel import FactsProvider, ReadModel
from .scanner import ScanResult, live_refs, scan, with_live
from .store import OrganizerStore, StoreBusy, StoreReadOnly, liveop_lock_path

log = logging.getLogger(__name__)

LOCKED = "locked"


class Organizer:
    """One vault's organizer as this process sees it.

    ``read_only`` opens everything without writing in the vault: no journal, no notes, no
    catalog tidying. The per-machine scan cache is kept on disk unless ``read_only`` (then
    in memory); ``machine_cache`` overrides that, and ``cache`` replaces it.
    ``busy_targets`` names the live slots an operation is writing (never journaled then):
    the hook D5's live operations fill in.

    When the vault is in store mode (D3), its version store is opened with the organizer:
    the scan reads store entries from their manifests, the locator reads the store first,
    and the store is the one ``preserve`` provider (D1 step 12). Otherwise there is none and
    ``preserve`` fails with "store-unavailable".
    """

    def __init__(self, vault_root: Path, sources: Sequence[Source], *, read_only: bool = False,
                 cache: ScanCache | None = None, machine_cache: bool | None = None,
                 game_running: Callable[[], bool | None] = safety.is_game_running,
                 busy_targets: Callable[[], frozenset[LiveTarget]] = frozenset,
                 clock: Callable[[], float] = time.time, host: Host | None = None,
                 facts: FactsProvider | None = None) -> None:
        self.vault_root = Path(vault_root)
        self._facts: FactsProvider | None = facts   # D4's content index, when it runs (phase 3)
        self._facts_seen: set | None = None
        self.sources = list(sources)
        self.read_only = read_only
        self.game_running = game_running
        self.busy_targets = busy_targets
        self.clock = clock
        self.vault = Vault(self.vault_root, read_only=read_only)
        self.store = OrganizerStore(self.vault_root, create=False, read_only=read_only, clock=clock)
        self._host = host = host or Host.current()   # once: never re-read from a later env
        self._rekey = False
        on_disk = not (machine_cache is False or (machine_cache is None and read_only))
        machine_dir = cache_dir(self.vault_root, self.store.vault_id, host)
        if cache is None:
            if on_disk:
                cache = ScanCache.load(machine_dir / FILENAME, self.store.vault_id)
                self._rekey = self.store.vault_id is None
            else:
                cache = ScanCache(None, self.store.vault_id)
        self.cache = cache
        self.seen_marker = SeenMarker(machine_dir / SEEN_FILENAME if on_disk else None)
        self.notes = Notes(self.store, clock)
        self.version_store = _open_version_store(self.vault_root, self.sources, read_only)
        self.scan: ScanResult | None = None
        self.model: ReadModel | None = None
        self.locator: VaultLocator | None = None
        self.observed: ObserveResult | None = None

    @property
    def facts(self) -> FactsProvider | None:
        return self._facts

    @facts.setter
    def facts(self, provider: FactsProvider | None) -> None:
        """A new provider starts its session afresh: its first request asks for everything."""
        if provider is not self._facts:
            self._facts = provider
            self._facts_seen = None

    @property
    def needs_bootstrap(self) -> bool:
        """The existing vault's history has not been read in yet (and can be)."""
        return not self.store.read_only and read_lineage(self.store).bootstrap is None

    @property
    def host(self) -> Host:
        return self._host

    def refresh(self, *, record: bool = True, deep: bool = False,
                progress: Callable[[int, int], None] | None = None,
                cancel=None, request_facts: bool = True) -> ReadModel:
        """Scan, journal the live slots when ``record`` (listings do not), build the model.

        Recording scans the vault's entries first, without any lock, then tries the
        live-op lock and, holding it, looks at the live folders - once - and journals what
        it saw, so no live write is under way between the look and the record (as the
        bootstrap does). When a write holds the lock the live folders are looked at all the
        same but not journaled this time ("operation in progress"); when the lock cannot be
        taken at all, neither ("live-op lock failed"). A listing scans everything at once.
        The catalog is read again each time: a backup made since (the classic tab, the CLI)
        is part of this look."""
        self.vault.load()
        running = self.game_running()
        recording = record and not self.store.read_only
        if recording:
            self._adopt_vault_id()
        self.scan = scan(self.vault, self.sources, self.cache, game_running=running,
                         deep=deep, now=self.clock, progress=progress, cancel=cancel,
                         look_live=not recording, store=self.version_store)
        self.observed = None
        if recording:
            with ExitStack() as stack:
                lock = self._try_live_lock(stack)
                fresh = scan(None, self.sources, self.cache, game_running=running,
                             deep=deep, now=self.clock)
                self.scan = with_live(self.scan, fresh)
                if lock == LOCKED:
                    self.observed = Journal(self.store, self.scan.versions).observe(
                        self.scan, src="scan", skip=self.busy_targets())
                else:
                    self.observed = ObserveResult(0, (), tuple(
                        (ref.target(c.slot), lock)
                        for ref, folder in self.scan.live.items() for c in folder.captures))
        journal = Journal(self.store, self.scan.versions)
        self.model = ReadModel.build(self.scan, journal.replay(), read_lineage(self.store),
                                     read_notes(self.store), self.sources, now=int(self.clock()),
                                     facts=self.facts)
        self.locator = self._locator()
        if request_facts:
            self.request_facts()
        return self.model

    def _adopt_vault_id(self) -> None:
        """A vault with no store header yet gets one now, and the per-machine scan cache that
        was keyed by location moves to the vault-id key, so the next launch finds it warm."""
        if self.store.vault_id is None:
            try:
                with self.store.transaction():
                    pass
            except (StoreBusy, StoreReadOnly):
                return
        if self._rekey and self.store.vault_id is not None:
            self._rekey = False
            path = cache_dir(self.vault_root, self.store.vault_id, self._host) / FILENAME
            self.cache.path, self.cache.vault_id = path, self.store.vault_id
            self.cache._dirty = True

    def request_facts(self) -> int:
        """Ask the content index for what the view shows first (D4 2.10.2): the live members
        at LIVE, each save file's representative and branch tips at TIP, the versions first
        seen since the last run (or, later, the last refresh) at NEW, and on the first
        refresh of the session every other version at BACKFILL. Queueing only: nothing
        waits. Returns how many versions were newly queued."""
        facts = self._facts
        if facts is None or self.scan is None or self.model is None:
            return 0
        tier = facts.Priority
        known = set(self.scan.versions)
        live, tips = facts_targets(self.scan, self.model)
        queued = facts.request(live, tier.LIVE) + facts.request(tips, tier.TIP)
        rest = known - set(live) - set(tips)
        if self._facts_seen is None:
            last_run = self.seen_marker.load()
            new = {v for v in rest if last_run is not None and not SeenMarker.contains(last_run, v)}
            queued += facts.request(sorted(new), tier.NEW)
            queued += facts.request(sorted(rest - new), tier.BACKFILL)
        else:
            queued += facts.request(sorted(rest - self._facts_seen), tier.NEW)
        self._facts_seen = (self._facts_seen or set()) | known
        if getattr(facts, "active", True):          # an index that is off keeps nothing: do not mark
            self.seen_marker.save(known)
        return queued

    def _locator(self) -> VaultLocator:
        """D1 2.11 by phase: the folders only; from store mode the store first, then the
        folders, with the store as the one preserver."""
        folders = FolderLocator(self.scan, self._folders())
        if self.version_store is None:
            return VaultLocator([folders], None)
        from ..store import StoreLocator

        live = {ref: Path(s.path) for s, ref in zip(self.sources, live_refs(self.sources))}
        store = StoreLocator(self.version_store, live_dir=lambda target: live[target.ref])
        return VaultLocator([store, folders], store)

    def bootstrap(self, *, dry_run: bool = False,
                  progress: Callable[[int, int], None] | None = None,
                  cancel=None) -> BootstrapReport:
        return _bootstrap.bootstrap(self.vault, self.store, self.sources, self.cache,
                                    game_running=self.game_running,
                                    dry_run=dry_run or self.read_only, clock=self.clock,
                                    progress=progress, cancel=cancel,
                                    version_store=self.version_store)

    def _try_live_lock(self, stack: ExitStack) -> str:
        """LOCKED, or why not: SKIP_BUSY (a live write holds it), SKIP_LOCK_FAILED."""
        try:
            stack.enter_context(safety.exclusive_lock(liveop_lock_path(self.vault_root),
                                                      timeout=0))
        except (safety.LockTimeout, RuntimeError):
            log.info("a live write is under way; the live slots are journaled next time")
            return SKIP_BUSY
        except OSError as exc:
            log.warning("the live slots are not journaled: the live-op lock failed: %s",
                        exc.strerror or exc)
            return SKIP_LOCK_FAILED
        return LOCKED

    @property
    def problems(self) -> list[str]:
        """What the user should know about the organizer's own state (store, journal)."""
        return list(self.store.problems)

    def _folders(self) -> dict:
        folders: dict = {ref: Path(s.path) for s, ref in zip(self.sources, live_refs(self.sources))}
        for entry in self.vault.entries:
            folders[EntryRef(entry.id)] = Path(entry.path)
        return folders


def _open_version_store(vault_root: Path, sources: Sequence[Source], read_only: bool):
    """The vault's version store when the vault is in store mode, else None. A store that
    cannot be opened is logged and left out: the folders still answer."""
    from ... import __version__
    from ..store import StoreError, VersionStore, vault_lock, vault_mode

    if vault_mode(vault_root) != "store":
        return None
    live = [Path(s.path) for s in sources] + [Path(p) for p in discover.all_live_paths(None)]
    try:
        return VersionStore.open(vault_root, lock=vault_lock(vault_root),
                                 app_version=__version__, live_roots=live, read_only=read_only)
    except StoreError as exc:
        log.warning("the vault's version store is not used: %s", exc)
        return None


__all__ = ["Organizer"]
