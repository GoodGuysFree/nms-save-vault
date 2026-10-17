"""Recording what the app does to live slots (D1 2.6.6, 2.6.7).

:class:`LineageRecorder` is the API the live operations call: an ``op`` record per slot an
operation wrote, the observation before and after, and what became of each operation.

:func:`classic_write` is the phase-1 hook around today's live writers (repopulate, full
restore, clear, promote, undo): it serialises every live write on the live-op lock, so the
GUI, the CLI and a second app instance never write one live folder at once, journals the
slot as it was before the write, and records the write as an operation. Recording is a
by-product: a failure to record is logged and never fails the operation.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .. import safety, savedir
from ..state import Source
from .cache import ScanCache
from .journal import Journal, ObserveResult, OpStatus, op_fields
from .model import LiveRef, LiveTarget, MemberObs, PairKey, Vid, new_op_id
from .scanner import ScanResult, scan
from .store import APP, OrganizerStore, liveop_lock_path

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


class LineageRecorder:
    def __init__(self, store: OrganizerStore, journal: Journal) -> None:
        self.store = store
        self.journal = journal

    def record_op(self, rec: OpLineage) -> bool:
        """Append one op record; nothing when ``(op_id, target)`` is recorded already (a
        retry). Its time never goes before the journal's latest record."""
        with self.store.transaction() as tx:
            for r in tx.journal():
                if (r["t"] == "op" and r.get("op_id") == rec.op_id
                        and _same_target(r.get("target", {}), rec.target)):
                    return False
            described = {vid for r in tx.journal() for vid in r.get("m", {})}
            members = {vid: m for vid, m in rec.members.items() if vid not in described}
            tx.append(op_fields("app", rec.op_id, rec.kind, rec.target, rec.placed, rec.evicted,
                                source=rec.source, preserved=rec.preserved, status=rec.status,
                                undo_of=rec.undo_of, note=rec.note, app=rec.app,
                                members=members),
                      at=max(rec.at, tx.last_at))
        return True

    def observe(self, scan_result: ScanResult, *, src: Literal["scan", "verify"],
                skip: frozenset[LiveTarget]) -> ObserveResult:
        return self.journal.observe(scan_result, src=src, skip=skip)

    def expected_pair(self, target: LiveTarget) -> PairKey | None:
        """The placed pair of the newest done op on ``target`` (no later op superseded it)."""
        bindings = [b for (_, t), b in self.journal.replay().ops.items() if t == target]
        return max(bindings, key=lambda b: b.at).pair if bindings else None

    def op_outcome(self, op_id: str, target: LiveTarget) -> OpOutcomeOrUnknown:
        binding = self.journal.replay().ops.get((op_id, target))
        return binding.outcome if binding is not None else "unknown"


def _same_target(raw: Mapping[str, Any], target: LiveTarget) -> bool:
    return (raw.get("platform"), raw.get("account"), raw.get("instance"), raw.get("slot")) == (
        target.platform, target.account_key, target.instance, target.slot)


# --- the phase-1 hook around classic live writes ------------------------------------


class LiveOpBusy(Exception):
    """Another window holds the live-op lock past the timeout."""


_writing = threading.local()


@contextmanager
def classic_write(vault_root: Path, live_dir: Path, kind: str, *,
                  clock: Callable[[], float] = time.time) -> Iterator[_ClassicWrite | None]:
    """Run one classic live write under the live-op lock, journaling the folder before it;
    the caller hands the result to :meth:`_ClassicWrite.done` to record it. A write nested
    in another on this thread (undo runs a full restore) is part of the outer one: it gets
    None and records nothing itself."""
    if getattr(_writing, "active", False):
        yield None
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(safety.exclusive_lock(liveop_lock_path(vault_root),
                                                      timeout=LIVEOP_TIMEOUT))
        except safety.LockTimeout as exc:
            raise LiveOpBusy() from exc
        except OSError as exc:
            log.warning("live writes are not serialised: the live-op lock failed: %s",
                        exc.strerror)
        _writing.active = True
        stack.callback(setattr, _writing, "active", False)
        write = _ClassicWrite(Path(vault_root), Path(live_dir), kind, clock)
        write.before()
        yield write


class _ClassicWrite:
    def __init__(self, vault_root: Path, live_dir: Path, kind: str,
                 clock: Callable[[], float]) -> None:
        self.vault_root = vault_root
        self.live_dir = live_dir
        self.kind = kind
        self.clock = clock
        platform = "xbox" if savedir.platform_of(live_dir) == "xbox" else "steam"
        self.ref = LiveRef(platform, live_dir.name)
        self.source = Source(id="classic-op", platform=platform, account=live_dir.name,
                             path=str(live_dir))
        self.pre: ScanResult | None = None

    def _scan(self) -> ScanResult:
        # In memory: the hook must not write the per-machine cache from inside an operation.
        return scan(None, [self.source], ScanCache(None, None), game_running=None, now=self.clock)

    def _journal(self, *scans: ScanResult) -> tuple[OrganizerStore, Journal]:
        store = OrganizerStore(self.vault_root, create=False, clock=self.clock)
        registry = {vid: info for s in scans for vid, info in s.versions.items()}
        return store, Journal(store, registry)

    def before(self) -> None:
        """Journal the slots as they are, before anything is replaced."""
        try:
            self.pre = self._scan()
            _, journal = self._journal(self.pre)
            journal.observe(self.pre, src="scan", skip=frozenset())
        except Exception as exc:  # noqa: BLE001 - recording never fails the operation
            log.warning("organizer: the live folder was not journaled before %s: %s",
                        self.kind, exc)

    def done(self, result: Any, *, slot: int | None = None,
             source: Mapping[str, Any] | None = None) -> None:
        """Record a finished write: one op record per written slot (``slot``, or every slot
        whose pair changed for a whole-folder restore), then journal the folder as it is."""
        if getattr(result, "snapshot_id", None) is None:
            return                      # nothing was written (e.g. already the newest)
        try:
            post = self._scan()
            store, journal = self._journal(*(s for s in (self.pre, post) if s is not None))
            folder = post.live[self.ref]
            if not folder.readable:
                return
            before = journal.replay().targets
            pre_pairs = ({c.slot: c.pair for c in self.pre.live[self.ref].captures}
                         if self.pre is not None and self.pre.live[self.ref].readable else {})
            recorder = LineageRecorder(store, journal)
            op_id = new_op_id(self.clock)
            for capture in folder.captures:
                target = self.ref.target(capture.slot)
                prior = before[target].pair if target in before else pre_pairs.get(capture.slot)
                if slot is not None and capture.slot != slot:
                    continue
                if slot is None and prior is not None and capture.pair == prior:
                    continue
                preserved = {vid: {"kind": "entry", "entry": result.snapshot_id,
                                   "slot": capture.slot, "member": "AB"[i]}
                             for i, vid in enumerate(prior or ()) if vid}
                recorder.record_op(OpLineage(
                    op_id, self.kind, int(self.clock()), target, capture.pair, prior,
                    source=source, preserved=preserved,
                    members={m.vid: m for m in capture.members}))
            journal.observe(post, src="scan", skip=frozenset())
        except Exception as exc:  # noqa: BLE001 - recording never fails the operation
            log.warning("organizer: %s was not recorded: %s", self.kind, exc)


def observe_folder(vault_root: Path, live_dir: Path,
                   clock: Callable[[], float] = time.time) -> None:
    """Journal a live folder the app has just read (a backup), narrowing the gaps between
    observations. Never fails the caller."""
    try:
        write = _ClassicWrite(Path(vault_root), Path(live_dir), "observe", clock)
        result = write._scan()
        _, journal = write._journal(result)
        journal.observe(result, src="scan", skip=frozenset())
    except Exception as exc:  # noqa: BLE001
        log.warning("organizer: the live folder was not journaled after a backup: %s", exc)

