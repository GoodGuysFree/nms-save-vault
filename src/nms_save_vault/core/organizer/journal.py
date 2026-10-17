"""The live journal (D1 2.6): what every live slot held, as the app saw it, and the history
that follows from it.

``observe`` appends one ``obs`` record per live slot whose pair differs from the last record
for that slot; ``op`` records (an app operation binding a slot) are written by the
recorder. ``replay`` is a pure function of the records and the scan's registry: the
current state of each slot, the edges (evidence of descent only, never a guess), the events
(deleted, reverted, returned, ...) and what became of each operation.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ..formats import MAX_SAVE_SLOTS
from .model import (
    EMPTY_PAIR,
    Edge,
    EdgeMethod,
    EdgeTier,
    JournalEvent,
    LiveTarget,
    MemberObs,
    MetaSnap,
    ModelError,
    ObsOrder,
    PairKey,
    Pid,
    TimeQuality,
    VersionInfo,
    Vid,
    edge_kind,
    is_op_id,
    is_pid,
    member_from_json,
    member_to_json,
    pair_from_json,
    pair_to_json,
    pids_compatible,
    target_from_json,
    target_to_json,
)
from .store import NOTES, OrganizerStore, StoreReadOnly

ObsSrc = Literal["scan", "verify", "backfill"]
OpSrc = Literal["app", "oplog"]
OpStatus = Literal["done", "failed", "rolled_back"]
OpOutcome = Literal["pending", "confirmed", "reverted", "superseded", "unexpected"]

#: Operation kinds: D5's, then the classic and legacy ones (2.6.1).
OP_KINDS = ("place", "place_into_empty", "restore_older", "swap", "undo", "promote",
            "panic_restore", "repopulate", "clear", "restore_full")
OP_STATUSES = ("done", "failed", "rolled_back")

SKIP_BUSY = "operation in progress"
SKIP_GAME = "game running"
SKIP_UNSTABLE = "unstable"
SKIP_UNREADABLE = "unreadable"
SKIP_READ_ONLY = "organizer read-only"


# --- records ------------------------------------------------------------------


@dataclass(frozen=True)
class ObsRecord:
    id: str
    seq: int
    at: int
    src: ObsSrc
    target: LiveTarget
    pair: PairKey
    members: Mapping[Vid, MemberObs]    # "m": the vids this journal describes for the first time
    note_revs: Mapping[Pid, int]        # save-file note revision of those vids' pids, then
    entry: str | None                   # the entry a backfill record came from
    quality: TimeQuality

    @property
    def order(self) -> ObsOrder:
        return (self.at, self.seq)


@dataclass(frozen=True)
class OpRecord:
    id: str
    seq: int
    at: int
    src: OpSrc
    op_id: str
    kind: str
    target: LiveTarget
    pair: PairKey                       # what the operation placed; (None, None) for a clear
    evicted: PairKey | None
    source: Mapping[str, Any] | None    # where the placed bytes came from (a Location)
    preserved: Mapping[Vid, Mapping[str, Any]]
    status: OpStatus
    undo_of: str | None
    note: str
    app: str
    members: Mapping[Vid, MemberObs] = field(default_factory=dict)   # "m": placed vids the
                                        # journal had not described, so it alone draws history

    @property
    def order(self) -> ObsOrder:
        return (self.at, self.seq)


Record = ObsRecord | OpRecord


def obs_fields(src: ObsSrc, target: LiveTarget, pair: PairKey,
               members: Mapping[Vid, MemberObs], note_revs: Mapping[Pid, int], *,
               entry: str | None = None, quality: TimeQuality = "exact") -> dict:
    """An ``obs`` record before the store gives it ``id``, ``seq`` and ``at``."""
    return {"t": "obs", "src": src, "target": target_to_json(target), "pair": pair_to_json(pair),
            "m": {vid: member_to_json(m, with_vid=False) for vid, m in members.items()},
            "note_revs": dict(note_revs), "entry": entry, "q": quality}


def op_fields(src: OpSrc, op_id: str, kind: str, target: LiveTarget, placed: PairKey,
              evicted: PairKey | None, *, source: Mapping[str, Any] | None = None,
              preserved: Mapping[Vid, Mapping[str, Any]] | None = None,
              status: OpStatus = "done", undo_of: str | None = None, note: str = "",
              app: str = "", members: Mapping[Vid, MemberObs] | None = None) -> dict:
    """An ``op`` record before the store gives it ``id``, ``seq`` and ``at``."""
    return {"t": "op", "src": src, "op_id": op_id, "kind": kind,
            "target": target_to_json(target), "pair": pair_to_json(placed),
            "evicted": pair_to_json(evicted) if evicted is not None else None,
            "source": dict(source) if source is not None else None,
            "preserved": {vid: dict(loc) for vid, loc in (preserved or {}).items()},
            "status": status, "undo_of": undo_of, "note": note, "app": app,
            "m": {vid: member_to_json(m, with_vid=False) for vid, m in (members or {}).items()}}


def decode_record(d: Mapping[str, Any]) -> Record:
    """A record as the store reads it (envelope checked) into its typed form."""
    try:
        if d["t"] == "obs":
            return _decode_obs(d)
        return _decode_op(d)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ModelError(f"journal record {d.get('id')!r}: {exc!r}") from None


def _decode_obs(d: Mapping[str, Any]) -> ObsRecord:
    src = d["src"]
    if src not in ("scan", "verify", "backfill"):
        raise ModelError(f"obs record: unknown src {src!r}")
    members = {vid: member_from_json(m, vid=vid) for vid, m in _dict(d["m"]).items()}
    note_revs = _dict(d["note_revs"])
    if not all(is_pid(p) and type(r) is int and r > 0 for p, r in note_revs.items()):
        raise ModelError("obs record: bad note_revs")
    entry = d["entry"]
    if entry is not None and not isinstance(entry, str):
        raise ModelError("obs record: bad entry")
    if d["q"] not in ("exact", "estimated"):
        raise ModelError("obs record: bad q")
    return ObsRecord(d["id"], d["seq"], d["at"], src, target_from_json(d["target"]),
                     pair_from_json(d["pair"]), members, note_revs, entry, d["q"])


def _decode_op(d: Mapping[str, Any]) -> OpRecord:
    if d["src"] not in ("app", "oplog") or not is_op_id(d["op_id"]):
        raise ModelError("op record: bad src or op_id")
    if d["kind"] not in OP_KINDS or d["status"] not in OP_STATUSES:
        raise ModelError("op record: bad kind or status")
    evicted = None if d["evicted"] is None else pair_from_json(d["evicted"])
    source = d["source"]
    if source is not None and not isinstance(source, dict):
        raise ModelError("op record: bad source")
    preserved = _dict(d["preserved"])
    undo_of = d["undo_of"]
    if undo_of is not None and not is_op_id(undo_of):
        raise ModelError("op record: bad undo_of")
    if not isinstance(d["note"], str) or not isinstance(d["app"], str):
        raise ModelError("op record: bad note or app")
    members = {vid: member_from_json(m, vid=vid) for vid, m in _dict(d.get("m", {})).items()}
    return OpRecord(d["id"], d["seq"], d["at"], d["src"], d["op_id"], d["kind"],
                    target_from_json(d["target"]), pair_from_json(d["pair"]), evicted, source,
                    preserved, d["status"], undo_of, d["note"], d["app"], members)


def _dict(value: object) -> dict:
    if not isinstance(value, dict):
        raise ModelError("journal record: expected an object")
    return value


# --- replay -------------------------------------------------------------------


@dataclass(frozen=True)
class TargetState:
    target: LiveTarget
    pair: PairKey               # what the slot holds per the records
    at: int
    by_op: str | None           # op_id when an operation put it there and nothing changed since


@dataclass(frozen=True)
class OpBinding:
    op_id: str
    kind: str
    target: LiveTarget
    at: int
    pair: PairKey
    evicted: PairKey | None
    outcome: OpOutcome          # at the first observation of the slot after the operation


@dataclass(frozen=True)
class JournalState:
    targets: Mapping[LiveTarget, TargetState]
    edges: tuple[Edge, ...]
    events: tuple[JournalEvent, ...]
    events_by_seq: Mapping[int, tuple[JournalEvent, ...]]
    ops: Mapping[tuple[str, LiveTarget], OpBinding]
    metas: Mapping[Vid, MemberObs]      # every vid the journal described, as first described
    first_seen: Mapping[Vid, ObsOrder]
    note_rev: Mapping[Vid, int]         # the save-file note revision when first journaled
    problems: tuple[str, ...]


@dataclass(frozen=True)
class ObserveResult:
    written: int
    events: tuple[JournalEvent, ...]                 # derived for the records just written
    skipped: tuple[tuple[LiveTarget, str], ...]      # (target, reason)


class Journal:
    def __init__(self, store: OrganizerStore, registry: Mapping[Vid, VersionInfo]) -> None:
        self.store = store
        self.registry = registry

    def records(self) -> tuple[tuple[Record, ...], tuple[str, ...]]:
        """The decodable records in file order, and a note for each one that is not."""
        return decode_all(self.store.journal_records())

    def observe(self, scan, *, src: Literal["scan", "verify"],
                skip: frozenset[LiveTarget]) -> ObserveResult:
        """Append an obs record for every live slot of ``scan`` whose pair differs from its
        last record (an obs or a done op). Never recorded: slots in ``skip`` (an operation is
        writing them), every slot while the game runs, slots with a member that is not a
        whole save, and the slots of a folder that could not be read - never as 'deleted'."""
        skipped: list[tuple[LiveTarget, str]] = []
        wanted = []
        for ref, folder in scan.live.items():
            if not folder.readable:
                skipped += [(ref.target(s), SKIP_UNREADABLE)
                            for s in range(1, MAX_SAVE_SLOTS + 1)]
                continue
            for capture in folder.captures:
                target = ref.target(capture.slot)
                if target in skip:
                    skipped.append((target, SKIP_BUSY))
                elif scan.game_running is True:
                    skipped.append((target, SKIP_GAME))
                elif capture.slot in folder.unstable:
                    skipped.append((target, SKIP_UNSTABLE))
                else:
                    wanted.append((target, capture))
        written: list[dict] = []
        try:
            with self.store.transaction() as tx:
                records, problems = decode_all(tx.journal())
                last = _last_pairs(records)
                described = {vid for r in records for vid in r.members}
                notes = tx.doc(NOTES).get("save_files", {}) if wanted else {}
                for target, capture in wanted:
                    if target in last and last[target] == capture.pair:
                        continue
                    members = {m.vid: m for m in capture.members if m.vid not in described}
                    described.update(members)
                    revs = {m.pid: notes[m.pid]["rev"] for m in members.values()
                            if isinstance(notes.get(m.pid), dict)
                            and type(notes[m.pid].get("rev")) is int}
                    written.append(tx.append(obs_fields(src, target, capture.pair, members, revs)))
                    last[target] = capture.pair
        except StoreReadOnly:
            reason = [(target, SKIP_READ_ONLY) for target, _ in wanted]
            return ObserveResult(0, (), tuple(skipped + reason))
        new, _ = decode_all(written)
        state = replay(records + new, self.registry, problems)   # no second read and decode
        events = tuple(e for rec in written for e in state.events_by_seq.get(rec["seq"], ()))
        return ObserveResult(len(written), events, tuple(skipped))

    def replay(self) -> JournalState:
        records, problems = self.records()
        return replay(records, self.registry, problems)


def replay(records: tuple[Record, ...], registry: Mapping[Vid, VersionInfo],
           problems: tuple[str, ...] = ()) -> JournalState:
    """D1 2.6.3, per slot over its records in ``(at, seq)`` order."""
    ordered = sorted(records, key=lambda r: r.order)
    metas: dict[Vid, MemberObs] = {}
    note_rev: dict[Vid, int] = {}
    first_seen: dict[Vid, ObsOrder] = {}
    for info in registry.values():
        for loc in info.locations:
            if loc.entry_kind != "live":     # a live capture is this scan's own observation
                _earliest(first_seen, info.vid, (loc.seen_at, -1))
    for rec in ordered:
        for vid in rec.pair:
            if vid:
                _earliest(first_seen, vid, rec.order)
        for vid, m in rec.members.items():
            if vid not in metas:
                metas[vid] = m
                if isinstance(rec, ObsRecord) and m.pid in rec.note_revs:
                    note_rev[vid] = rec.note_revs[m.pid]
    run = _Replay(metas, registry, first_seen)
    by_target: dict[LiveTarget, list[Record]] = {}
    seen_ops: set[tuple[str, LiveTarget]] = set()
    for rec in ordered:
        if isinstance(rec, OpRecord):
            if (rec.op_id, rec.target) in seen_ops:
                continue                # idempotent on (op_id, target): a repeat binds nothing
            seen_ops.add((rec.op_id, rec.target))
        by_target.setdefault(rec.target, []).append(rec)
    for target, recs in by_target.items():
        run.target(target, recs)
    run.events.sort(key=lambda pair: pair[0])
    by_seq: dict[int, list[JournalEvent]] = {}
    for (_, seq), event in run.events:
        by_seq.setdefault(seq, []).append(event)
    return JournalState(
        targets=run.states, edges=tuple(run.edges), events=tuple(e for _, e in run.events),
        events_by_seq={k: tuple(v) for k, v in by_seq.items()}, ops=run.ops, metas=metas,
        first_seen=first_seen, note_rev=note_rev, problems=problems,
    )


@dataclass
class _State:
    vids: PairKey
    src: Literal["obs", "op"]
    order: ObsOrder
    op: OpRecord | None = None
    evicted: PairKey = EMPTY_PAIR


@dataclass
class _Replay:
    metas: Mapping[Vid, MemberObs]
    registry: Mapping[Vid, VersionInfo]
    first_seen: Mapping[Vid, ObsOrder]
    edges: list[Edge] = field(default_factory=list)
    events: list[tuple[ObsOrder, JournalEvent]] = field(default_factory=list)
    states: dict[LiveTarget, TargetState] = field(default_factory=dict)
    ops: dict[tuple[str, LiveTarget], OpBinding] = field(default_factory=dict)

    # --- per-vid facts: the journal's own description first, else the scan's -------------

    def _meta(self, vid: Vid) -> MetaSnap | None:
        if vid in self.metas:
            return self.metas[vid].meta
        info = self.registry.get(vid)
        return info.meta if info else None

    def _pid(self, vid: Vid) -> Pid | None:
        if vid in self.metas:
            return self.metas[vid].pid
        info = self.registry.get(vid)
        return info.raw_pid if info else None

    def _compatible(self, a: Vid, b: Vid) -> bool:
        pa, pb = self._pid(a), self._pid(b)
        return pa is not None and pb is not None and pids_compatible(pa, pb)

    def _incompatible(self, a: Vid, b: Vid) -> bool:
        """Known to be two different saves (a vid nobody described proves nothing)."""
        pa, pb = self._pid(a), self._pid(b)
        return pa is not None and pb is not None and not pids_compatible(pa, pb)

    def _not_later(self, parent: Vid, child: Vid) -> bool:
        """The parent has no more play than the child and, where both times are on one
        clock, is not saved after it. A meta time against a file time orders nothing."""
        p, c = self._meta(parent), self._meta(child)
        if p is None or c is None or p.play_time > c.play_time:
            return False
        return p.ts_src != c.ts_src or p.meta_ts <= c.meta_ts

    # --- one slot ---------------------------------------------------------------

    def target(self, target: LiveTarget, records: list[Record]) -> None:
        state: _State | None = None
        last_full: PairKey | None = None
        for rec in records:
            if isinstance(rec, OpRecord):
                if rec.status != "done":
                    continue                    # failed / rolled back ops bind nothing
                if state is not None and state.src == "op":
                    self._outcome(state.op, "superseded")
                self.ops[(rec.op_id, target)] = OpBinding(rec.op_id, rec.kind, target, rec.at,
                                                          rec.pair, rec.evicted, "pending")
                state = _State(rec.pair, "op", rec.order, rec,
                               state.vids if state else EMPTY_PAIR)
                if not _empty(rec.pair):
                    last_full = rec.pair
                continue
            cur = rec.pair
            if state is None:
                self._event("first-seen", rec, None, None, ())
                state = _State(cur, "obs", rec.order)
                if not _empty(cur):
                    last_full = cur
                continue
            before = state.vids
            op_id = state.op.op_id if state.src == "op" else None
            # Ordered pairs: a swap of the two members is reverted by the members swapping back.
            if state.src == "op" and cur == state.evicted and cur != state.vids:
                self._event("reverted", rec, op_id, before, ())   # the old save came back
                self._outcome(state.op, "reverted")
                state = _State(cur, "obs", rec.order)
                last_full = cur if not _empty(cur) else last_full
                continue
            if _empty(cur) and not _empty(state.vids):
                self._event("deleted", rec, None, before, ())
                if state.src == "op":
                    self._event("unexpected", rec, op_id, before, ())
                    self._outcome(state.op, "unexpected")
                state = _State(cur, "obs", rec.order)
                continue
            if _empty(state.vids) and not _empty(cur) and set(cur) == set(last_full or ()):
                self._event("resurrected", rec, None, before, ())  # the cloud brought it back
                if state.src == "op":
                    self._outcome(state.op, "unexpected")
                state = _State(cur, "obs", rec.order)
                continue
            if _count(cur) == 1 and _count(state.vids) == 2:
                self._event("partial", rec, None, before, ())
            new = [v for v in cur if v and v not in state.vids]
            back = [v for v in new if self.first_seen.get(v, rec.order) < state.order]
            if back:
                self._event("returned", rec, None, before, tuple(back))   # no edge for it
            new = [v for v in new if v not in back]
            cands = [v for v in state.vids if v]
            made_from_placed = False
            for n in sorted(new, key=self._newness):
                ok = [c for c in cands if self._compatible(c, n) and self._not_later(c, n)]
                if ok:
                    p = max(ok, key=lambda c: (self._meta(c).play_time,  # type: ignore[union-attr]
                                               self._meta(c).meta_ts,    # type: ignore[union-attr]
                                               cands.index(c)))
                    if state.src == "op" and p in state.vids:
                        method: EdgeMethod = "op" if state.op.src == "app" else "oplog"
                        self._edge(p, n, "recorded", method, op_id, rec)
                        made_from_placed = True
                    elif rec.src == "backfill":
                        self._edge(p, n, "observed", "capture-sequence", None, rec)
                    else:
                        self._edge(p, n, "observed", "journal", None, rec)
                elif any(self._incompatible(c, n) for c in cands):
                    self._event("replaced", rec, None, before, (n,))  # another save came in
                cands.append(n)
            if state.src == "op":
                if made_from_placed:
                    self._event("confirmed", rec, op_id, before, ())  # the game saved on top
                    self._outcome(state.op, "confirmed")
                else:
                    self._event("unexpected", rec, op_id, before, ())
                    self._outcome(state.op, "unexpected")
            if new:
                self._event("changed", rec, None, before, tuple(new))
            state = _State(cur, "obs", rec.order)
            if not _empty(cur):
                last_full = cur
        if state is not None:
            self.states[target] = TargetState(target, state.vids, state.order[0],
                                              state.op.op_id if state.src == "op" else None)

    def _newness(self, vid: Vid) -> tuple:
        m = self._meta(vid)
        return (m.meta_ts if m else 0, m.play_time if m else 0, vid)

    def _edge(self, parent: Vid, child: Vid, tier: EdgeTier, method: EdgeMethod,
              op_id: str | None, rec: ObsRecord) -> None:
        p, c = self._meta(parent), self._meta(child)
        kind = edge_kind(p, c) if p is not None and c is not None else "unknown"
        self.edges.append(Edge(parent, child, tier, kind, method, op_id, rec.at, rec.target))

    def _event(self, kind: str, rec: ObsRecord, op_id: str | None, before: PairKey | None,
               vids: tuple[Vid, ...]) -> None:
        event = JournalEvent(kind, rec.target, rec.at, op_id, before, rec.pair,  # type: ignore[arg-type]
                             vids)
        self.events.append((rec.order, event))

    def _outcome(self, op: OpRecord | None, outcome: OpOutcome) -> None:
        if op is None:
            return
        key = (op.op_id, op.target)
        binding = self.ops.get(key)
        if binding is not None and binding.outcome == "pending":
            self.ops[key] = OpBinding(binding.op_id, binding.kind, binding.target, binding.at,
                                      binding.pair, binding.evicted, outcome)


# --- helpers ------------------------------------------------------------------


def decode_all(raw: tuple[dict, ...] | list[dict]) -> tuple[tuple[Record, ...], tuple[str, ...]]:
    records, problems = [], []
    for d in raw:
        try:
            records.append(decode_record(d))
        except ModelError as exc:
            problems.append(f"journal record {d.get('id')!r} skipped: {exc}")
    return tuple(records), tuple(problems)


def _last_pairs(records: tuple[Record, ...]) -> dict[LiveTarget, PairKey]:
    """Each slot's pair per its last obs or done op, in replay order."""
    last: dict[LiveTarget, PairKey] = {}
    for rec in sorted(records, key=lambda r: r.order):
        if isinstance(rec, OpRecord) and rec.status != "done":
            continue
        last[rec.target] = rec.pair
    return last


def _earliest(first: dict[Vid, ObsOrder], vid: Vid, order: ObsOrder) -> None:
    if vid not in first or order < first[vid]:
        first[vid] = order


def _empty(pair: PairKey) -> bool:
    return pair == EMPTY_PAIR


def _count(pair: PairKey) -> int:
    return sum(1 for v in pair if v)
