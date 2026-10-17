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
from .folders import FolderLocator
from .journal import SKIP_BUSY, SKIP_LOCK_FAILED, Journal, ObserveResult
from .lineage import read_lineage
from .locator import VaultLocator
from .model import EntryRef, LiveTarget
from .notes import Notes, read_notes
from .readmodel import ReadModel
from .scanner import ScanResult, live_refs, scan, with_live
from .store import OrganizerStore, liveop_lock_path

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
                 clock: Callable[[], float] = time.time, host: Host | None = None) -> None:
        self.vault_root = Path(vault_root)
        self.sources = list(sources)
        self.read_only = read_only
        self.game_running = game_running
        self.busy_targets = busy_targets
        self.clock = clock
        self.vault = Vault(self.vault_root, read_only=read_only)
        self.store = OrganizerStore(self.vault_root, create=False, read_only=read_only, clock=clock)
        if cache is None:
            if machine_cache is False or (machine_cache is None and read_only):
                cache = ScanCache(None, self.store.vault_id)
            else:
                path = cache_dir(self.vault_root, self.store.vault_id, host) / FILENAME
                cache = ScanCache.load(path, self.store.vault_id)
        self.cache = cache
        self.notes = Notes(self.store, clock)
        self.version_store = _open_version_store(self.vault_root, self.sources, read_only)
        self.scan: ScanResult | None = None
        self.model: ReadModel | None = None
        self.locator: VaultLocator | None = None
        self.observed: ObserveResult | None = None

    @property
    def needs_bootstrap(self) -> bool:
        """The existing vault's history has not been read in yet (and can be)."""
        return not self.store.read_only and read_lineage(self.store).bootstrap is None

    def refresh(self, *, record: bool = True, deep: bool = False,
                progress: Callable[[int, int], None] | None = None,
                cancel=None) -> ReadModel:
        """Scan, journal the live slots when ``record`` (listings do not), build the model.

        The vault's entries are scanned first, without any lock. Recording then tries the
        live-op lock and, holding it, looks at the live folders again and journals what
        that second look found, so no live write is under way between the look and the
        record (as the bootstrap does). When a write holds the lock the slots are not
        journaled this time ("operation in progress"); when the lock cannot be taken at all
        they are not either ("live-op lock failed")."""
        running = self.game_running()
        self.scan = scan(self.vault, self.sources, self.cache, game_running=running,
                         deep=deep, now=self.clock, progress=progress, cancel=cancel,
                         store=self.version_store)
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
                                     read_notes(self.store), self.sources, now=int(self.clock()))
        self.locator = self._locator()
        return self.model

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
