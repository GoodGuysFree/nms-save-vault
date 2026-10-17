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

from .. import safety
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
from .store import OrganizerStore, liveop_lock_path

log = logging.getLogger(__name__)

LOCKED = "locked"


class Organizer:
    """One vault's organizer as this process sees it.

    ``read_only`` opens everything without writing in the vault: no journal, no notes, no
    catalog tidying. The per-machine scan cache is kept on disk unless ``read_only`` (then
    in memory); ``machine_cache`` overrides that, and ``cache`` replaces it.
    ``busy_targets`` names the live slots an operation is writing (never journaled then).
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
        on_disk = not (machine_cache is False or (machine_cache is None and read_only))
        machine_dir = cache_dir(self.vault_root, self.store.vault_id, host)
        if cache is None:
            if on_disk:
                cache = ScanCache.load(machine_dir / FILENAME, self.store.vault_id)
            else:
                cache = ScanCache(None, self.store.vault_id)
        self.cache = cache
        self.seen_marker = SeenMarker(machine_dir / SEEN_FILENAME if on_disk else None)
        self.notes = Notes(self.store, clock)
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

    def refresh(self, *, record: bool = True, deep: bool = False,
                progress: Callable[[int, int], None] | None = None,
                cancel=None, request_facts: bool = True) -> ReadModel:
        """Scan, journal the live slots when ``record`` (listings do not), build the model.

        The vault's entries are scanned first, without any lock. Recording then tries the
        live-op lock and, holding it, looks at the live folders again and journals what
        that second look found, so no live write is under way between the look and the
        record (as the bootstrap does). When a write holds the lock the slots are not
        journaled this time ("operation in progress"); when the lock cannot be taken at all
        they are not either ("live-op lock failed")."""
        running = self.game_running()
        self.scan = scan(self.vault, self.sources, self.cache, game_running=running,
                         deep=deep, now=self.clock, progress=progress, cancel=cancel)
        self.observed = None
        recording = record and not self.store.read_only
        with ExitStack() as stack:
            lock = self._try_live_lock(stack) if recording else None
            if lock == LOCKED:
                fresh = scan(None, self.sources, self.cache, game_running=running,
                             now=self.clock)
                self.scan = with_live(self.scan, fresh)
                journal = Journal(self.store, self.scan.versions)
                self.observed = journal.observe(self.scan, src="scan", skip=self.busy_targets())
            elif lock is not None:
                self.observed = ObserveResult(0, (), tuple(
                    (ref.target(c.slot), lock)
                    for ref, folder in self.scan.live.items() for c in folder.captures))
        journal = Journal(self.store, self.scan.versions)
        self.model = ReadModel.build(self.scan, journal.replay(), read_lineage(self.store),
                                     read_notes(self.store), self.sources, now=int(self.clock()),
                                     facts=self.facts)
        self.locator = VaultLocator([FolderLocator(self.scan, self._folders())], None)
        if request_facts:
            self.request_facts()
        return self.model

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
        self.seen_marker.save(known)
        return queued

    def bootstrap(self, *, dry_run: bool = False,
                  progress: Callable[[int, int], None] | None = None,
                  cancel=None) -> BootstrapReport:
        return _bootstrap.bootstrap(self.vault, self.store, self.sources, self.cache,
                                    game_running=self.game_running,
                                    dry_run=dry_run or self.read_only, clock=self.clock,
                                    progress=progress, cancel=cancel)

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


__all__ = ["Organizer"]
