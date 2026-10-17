"""What every live operation runs against (D5 2.2): the vault, the organizer's store,
locator, recorder and read model, the configured live sources, and the outside world.

Everything arrives as a field, so tests swap any part (a scripted game probe, a fixed
clock, a fault injector, a fake version store) without patching modules.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from .. import safety

if TYPE_CHECKING:
    from ..catalog import Vault
    from ..organizer import Organizer
    from ..organizer.locator import VersionLocator
    from ..organizer.readmodel import ReadModel
    from ..organizer.recorder import LineageRecorder
    from ..organizer.store import OrganizerStore
    from ..platform import Host
    from ..state import Source
    from .faults import FaultInjector


class VersionStore(Protocol):
    """What the live operations need of the version store (D3): the pre-op snapshot and a
    kept copy as store entries, and one file of an entry back for an exact rollback."""

    def add_from_live(self, folder: Path, *, platform: str, account_key: str, kind: str,
                      label: str, scope: object, op_id: str, reason: str) -> object: ...

    def read_file(self, entry_id: str, path: str) -> bytes: ...


@dataclass(frozen=True)
class Policies:
    """The owner's decisions F4a, F5b, F7b and F8 are fixed behaviour, not switches. Only the
    Steam <-> Xbox transfer opens per direction after the owner's validation runs (F6)."""

    cross_platform: Literal["off", "validated"] = "off"


def context_for(organizer: Organizer, *, host: Host, machine_id: str, app_version: str,
                policies: Policies | None = None,
                game_probe: Callable[[], bool | None] = safety.is_game_running,
                clock: Callable[[], float] = time.time,
                faults: FaultInjector | None = None) -> OpContext:
    """The context of a refreshed :class:`Organizer` (refreshed here when it is not yet):
    its vault, version store, organizer store, locator (the store first, the one preserver)
    and read model, and a recorder over its registry."""
    from ..organizer.journal import Journal
    from ..organizer.recorder import LineageRecorder

    if organizer.model is None or organizer.locator is None or organizer.scan is None:
        organizer.refresh()
    return OpContext(
        vault=organizer.vault, versions=organizer.version_store, store=organizer.store,
        locator=organizer.locator,
        lineage=LineageRecorder(organizer.store, Journal(organizer.store, organizer.scan.versions)),
        readmodel=organizer.model, sources=list(organizer.sources),
        policies=policies or Policies(), host=host, app_version=app_version,
        machine_id=machine_id, game_probe=game_probe, clock=clock, faults=faults)


@dataclass
class OpContext:
    vault: Vault                          # catalog.Vault (oplog, legacy entries until fold)
    versions: VersionStore | None         # D3; None while the vault is not in store mode
    store: OrganizerStore                 # D1: paths, vault_id, transactions (store lock)
    locator: VersionLocator               # D1's composite VaultLocator
    lineage: LineageRecorder              # D1: record_op, observe, expected_pair, op_outcome
    readmodel: ReadModel | None           # D1: live board, line names, pins
    sources: Sequence[Source]             # configured live sources (state.json)
    policies: Policies
    host: Host                            # DESIGN.md D7: environment and home as arguments
    app_version: str
    machine_id: str                       # random per machine (opjournal.machine_id)
    game_probe: Callable[[], bool | None] = safety.is_game_running
    clock: Callable[[], float] = time.time
    faults: FaultInjector | None = None   # tests only; None in production
