"""The save-file organizer: one line per playthrough, its versions, where each is live, and
the history the app records (docs/organizer/designs/D1-core-model.md).

:class:`Organizer` is the one entry point the GUI and the CLI use: it scans, records what
the live slots hold (when asked to), and builds the read model.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .. import safety
from ..catalog import Vault
from ..platform import Host
from ..state import Source
from . import bootstrap as _bootstrap
from .bootstrap import BootstrapReport
from .cache import FILENAME, ScanCache, cache_dir
from .folders import FolderLocator
from .journal import Journal, ObserveResult
from .lineage import read_lineage
from .locator import VaultLocator
from .model import EntryRef, LiveTarget
from .notes import Notes, read_notes
from .readmodel import ReadModel
from .scanner import ScanResult, live_refs, scan
from .store import OrganizerStore


class Organizer:
    """One vault's organizer as this process sees it.

    ``read_only`` opens everything without writing anywhere: no journal, no notes, no
    catalog tidying, and the scan cache kept in memory unless ``cache`` says otherwise.
    ``busy_targets`` names the live slots an operation is writing (never journaled then).
    """

    def __init__(self, vault_root: Path, sources: Sequence[Source], *, read_only: bool = False,
                 cache: ScanCache | None = None,
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
            if read_only:
                cache = ScanCache(None, self.store.vault_id)
            else:
                path = cache_dir(self.vault_root, self.store.vault_id, host) / FILENAME
                cache = ScanCache.load(path, self.store.vault_id)
        self.cache = cache
        self.notes = Notes(self.store, clock)
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
        """Scan, journal the live slots when ``record`` (listings do not), build the model."""
        self.scan = scan(self.vault, self.sources, self.cache, game_running=self.game_running(),
                         deep=deep, now=self.clock, progress=progress, cancel=cancel)
        journal = Journal(self.store, self.scan.versions)
        self.observed = None
        if record and not self.store.read_only:
            self.observed = journal.observe(self.scan, src="scan", skip=self.busy_targets())
        self.model = ReadModel.build(self.scan, journal.replay(), read_lineage(self.store),
                                     read_notes(self.store), self.sources, now=int(self.clock()))
        self.locator = VaultLocator([FolderLocator(self.scan, self._folders())], None)
        return self.model

    def bootstrap(self, *, dry_run: bool = False,
                  progress: Callable[[int, int], None] | None = None,
                  cancel=None) -> BootstrapReport:
        return _bootstrap.bootstrap(self.vault, self.store, self.sources, self.cache,
                                    game_running=self.game_running(),
                                    dry_run=dry_run or self.read_only, clock=self.clock,
                                    progress=progress, cancel=cancel)

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
