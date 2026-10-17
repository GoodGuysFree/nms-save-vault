"""Recording what the app does to live slots (D1 2.6.6, 2.6.7).

:class:`LineageRecorder` is the API the live operations call: an ``op`` record per slot an
operation wrote, the observation before and after, and what became of each operation.

Every live write is a D5 operation (``liveops.execute``), which takes the live-op lock and
records itself; :func:`observe_folder` is the one look the classic backup still takes.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .. import safety, savedir
from .. import state as appstate
from ..state import Source
from .cache import FILENAME, ScanCache, cache_dir
from .journal import Journal, ObserveResult, OpStatus, op_fields
from .model import LiveRef, LiveTarget, MemberObs, PairKey, Vid
from .scanner import ScanResult, live_refs, scan
from .store import APP, OrganizerStore

log = logging.getLogger(__name__)

#: How long a live write waits for another window's write to finish.
LIVEOP_TIMEOUT = 10.0

OpOutcomeOrUnknown = Literal["pending", "confirmed", "reverted", "superseded", "unexpected",
                             "unknown"]


@dataclass(frozen=True)
class OpLineage:
    """One operation's binding of one live slot."""

    op_id: str
    kind: str
    at: int
    target: LiveTarget
    placed: PairKey             # (None, None) only for a classic clear
    evicted: PairKey | None
    source: Mapping[str, Any] | None = None             # where the placed bytes came from
    preserved: Mapping[Vid, Mapping[str, Any]] = field(default_factory=dict)  # evicted vid ->
                                                        # where it is kept in the vault
    status: OpStatus = "done"
    undo_of: str | None = None
    note: str = ""
    app: str = APP
    members: Mapping[Vid, MemberObs] = field(default_factory=dict)   # the placed members
    snapshot: str | None = None             # the entry the app kept the folder in first


class LineageRecorder:
    def __init__(self, store: OrganizerStore, journal: Journal) -> None:
        self.store = store
        self.journal = journal

    def record_op(self, rec: OpLineage) -> bool:
        """Append one op record; nothing when ``(op_id, target)`` is recorded already (a
        retry). Its time never goes before the journal's latest record."""
        return self.record_ops([rec]) == 1

    def record_ops(self, recs: Sequence[OpLineage]) -> int:
        """Append the records of one operation (one per slot) in one transaction; returns
        how many were new."""
        written = 0
        with self.store.transaction() as tx:
            journal = tx.journal()
            known = {(r["op_id"], _target_key(r.get("target", {})))
                     for r in journal if r["t"] == "op"}
            described = {vid for r in journal for vid in r.get("m", {})}
            for rec in recs:
                key = (rec.op_id, (rec.target.platform, rec.target.account_key,
                                   rec.target.instance, rec.target.slot))
                if key in known:
                    continue
                known.add(key)
                members = {vid: m for vid, m in rec.members.items() if vid not in described}
                described.update(members)
                tx.append(op_fields("app", rec.op_id, rec.kind, rec.target, rec.placed,
                                    rec.evicted, source=rec.source, preserved=rec.preserved,
                                    status=rec.status, undo_of=rec.undo_of, note=rec.note,
                                    app=rec.app, members=members, snapshot=rec.snapshot),
                          at=max(rec.at, tx.last_at))
                written += 1
        return written

    def observe(self, scan_result: ScanResult, *, src: Literal["scan", "verify"],
                skip: frozenset[LiveTarget]) -> ObserveResult:
        return self.journal.observe(scan_result, src=src, skip=skip)

    def expected_pair(self, target: LiveTarget) -> PairKey | None:
        """The placed pair of the newest done op on ``target`` (no later op superseded it)."""
        bindings = [b for (_, t), b in self.journal.replay().ops.items() if t == target]
        return max(bindings, key=lambda b: (b.at, b.seq)).pair if bindings else None

    def op_outcome(self, op_id: str, target: LiveTarget) -> OpOutcomeOrUnknown:
        binding = self.journal.replay().ops.get((op_id, target))
        return binding.outcome if binding is not None else "unknown"


def _target_key(raw: Mapping[str, Any]) -> tuple:
    return (raw.get("platform"), raw.get("account"), raw.get("instance"), raw.get("slot"))


# --- looking at a live folder -------------------------------------------------------


class LiveOpBusy(Exception):
    """Another window holds the live-op lock past the timeout."""


class _FolderLook:
    """One look at a live folder as the scanner names it (for the journal)."""


    def __init__(self, vault_root: Path, live_dir: Path, kind: str,
                 clock: Callable[[], float]) -> None:
        self.vault_root = vault_root
        self.live_dir = live_dir
        self.kind = kind
        self.clock = clock
        self.platform = "xbox" if savedir.platform_of(live_dir) == "xbox" else "steam"
        self.source = Source(id="classic-op", platform=self.platform, account=live_dir.name,
                             path=str(live_dir))
        self.pre: ScanResult | None = None

    def _prepare(self) -> None:
        if hasattr(self, "cache"):      # the last thing set: a look that failed half-way
            return                      # is prepared again
        self.store = OrganizerStore(self.vault_root, create=False, clock=self.clock)
        self.ref = LiveRef(self.platform, self.live_dir.name, self._instance())
        # The per-machine cache, read only: fixed legacy pids and known hashes are honoured,
        # and the two looks at the folder share what the first one hashed.
        self.cache = ScanCache.load(cache_dir(self.vault_root, self.store.vault_id) / FILENAME,
                                    self.store.vault_id, read_only=True)

    def _instance(self) -> str:
        """The tag the scanner gives this folder (:func:`live_refs` over the configured live
        folders): the location's hash only when another configured live folder of this
        platform has the same name (two Proton prefixes)."""
        sources = list(_configured_sources())
        for source, ref in zip(sources, live_refs(sources)):
            if ref.platform == self.platform and safety.same_path(source.path, self.live_dir):
                return ref.instance
        return ""

    def _scan(self) -> ScanResult:
        result = scan(None, [self.source], self.cache, game_running=None, now=self.clock)
        # The scanner names the folder by its own rule; the journal must use the same name.
        return _renamed(result, self.ref)


def _configured_sources() -> Sequence[Source]:
    """The live folders the app is configured with - what the scanner names folders by.
    Unreadable configuration: none (every folder then goes untagged, as the scanner would
    name the only folder of a name)."""
    try:
        state = appstate.load()
    except Exception as exc:  # noqa: BLE001 - recording never fails the operation
        log.warning("organizer: the live folder list was not read: %s", exc)
        return []
    return state.live_sources if state is not None else []


def _renamed(result: ScanResult, ref: LiveRef) -> ScanResult:
    """``result`` with its one live folder known as ``ref`` (same platform and name)."""
    (old, folder), = result.live.items()
    if old == ref:
        return result
    captures = tuple(dataclasses.replace(c, where=ref) for c in folder.captures)
    return dataclasses.replace(result, live={ref: dataclasses.replace(folder, ref=ref,
                                                                       captures=captures)})


def observe_folder(vault_root: Path, live_dir: Path,
                   clock: Callable[[], float] = time.time) -> None:
    """Journal a live folder the app has just read (a backup), narrowing the gaps between
    observations - only when the game is known not to be running. Never fails the caller."""
    try:
        if safety.is_game_running() is not False:
            return
        write = _FolderLook(Path(vault_root), Path(live_dir), "observe", clock)
        write._prepare()
        result = write._scan()
        Journal(write.store, result.versions).observe(result, src="scan", skip=frozenset())
    except Exception as exc:  # noqa: BLE001
        log.warning("organizer: the live folder was not journaled after a backup: %s", exc)

