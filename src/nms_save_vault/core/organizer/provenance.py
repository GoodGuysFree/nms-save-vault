"""Which versions an app made, and the last version before it changed a playthrough (CL1.5).

Read from the organizer's journal. Every operation record names the app that ran it
(:mod:`..appinfo`), the pair it placed, the pair it evicted, where each evicted member was
kept, and - for an operation that writes new content, such as an editor's edit - the versions
it made (``made``). From that:

- A version an app **made** is one an operation of that app names in ``made``. Nothing is
  inferred: a copy, a move or an undo makes nothing, whichever app runs it. (NMS Save Vault's
  own operations only ever copy.)
- A version an app **touched** is one it made, or one the game saved from one: a descent edge
  from it, or any version seen in a slot after an operation placed a touched version there,
  up to the slot's next operation. The game may have loaded either member, so the window does
  not trust the replay's guess of which one was the parent.
- The **last version before** an app changed a playthrough is the newest pair of that
  playthrough that an operation of the app evicted from a slot, and that the app had not
  touched, together with where the vault kept it. If the app edited, the game played on and
  the app edited again, that is still the pair before the first edit. Asked by slot, it is
  asked for the playthrough the slot holds, so a slot whose saves were swapped answers for
  what is in it now; when the app never changed that playthrough (it put it there), the
  answer is what the slot held before the app wrote it.

Read-only, and the same for every app.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .. import appinfo
from .journal import JournalState, ObsRecord, OpRecord
from .model import LiveTarget, PairKey, Pid, Vid


@dataclass(frozen=True)
class KeptCopy:
    """Where the vault kept a member an operation evicted: slot ``slot``, member ``member`` of
    entry ``entry`` (a store entry when ``kind`` is ``"store"``)."""

    kind: str
    entry: str
    slot: int
    member: str

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> KeptCopy | None:
        try:
            return cls(str(value["kind"]), str(value["entry"]), int(value["slot"]),
                       str(value["member"]))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class AppChange:
    """One operation of an app on one live slot that went through ("done")."""

    op_id: str
    kind: str
    at: int                                 # UTC unix
    app: str                                # its stamp, "<name> <version>"
    target: LiveTarget
    placed: PairKey
    evicted: PairKey | None
    kept: Mapping[Vid, KeptCopy]            # where each evicted version was kept
    snapshot: str | None                    # the entry the slot's folder was kept in first
    made: tuple[Vid, ...] = ()              # the versions it wrote new content into
    seq: int = 0


@dataclass(frozen=True)
class Before:
    """A playthrough's pair just before ``change``, and where the vault kept it. When A and B
    were the same version, one copy holds both."""

    change: AppChange
    pair: PairKey
    kept: Mapping[Vid, KeptCopy]


def same_slot(a: LiveTarget, b: LiveTarget) -> bool:
    """One slot of one live folder. An instance tag (two folders of one name) is compared only
    when both name one: an app outside the organizer names a folder by platform and name."""
    if (a.platform, a.account_key, a.slot) != (b.platform, b.account_key, b.slot):
        return False
    return not (a.instance and b.instance) or a.instance == b.instance


def _ops(state: JournalState) -> list[OpRecord]:
    """Operation records that went through, one per operation and slot, in replay order."""
    out, seen = [], set()
    for rec in state.records:
        if isinstance(rec, OpRecord) and (rec.op_id, rec.target) not in seen:
            seen.add((rec.op_id, rec.target))
            if rec.status == "done":
                out.append(rec)
    return out


def _change(rec: OpRecord) -> AppChange:
    kept = {}
    for vid, loc in rec.preserved.items():
        copy = KeptCopy.from_json(loc) if isinstance(loc, Mapping) else None
        if copy is not None:
            kept[vid] = copy
    return AppChange(rec.op_id, rec.kind, rec.at, rec.app, rec.target, rec.pair, rec.evicted,
                     kept, rec.snapshot, tuple(rec.made), rec.seq)


def changes_by(state: JournalState, app: str) -> list[AppChange]:
    """Every operation ``app`` (a name, ``"nms-savecraft"``) ran that went through, oldest
    first; one per operation and slot."""
    return [_change(rec) for rec in _ops(state) if appinfo.app_of(rec.app) == app]


def made_by(state: JournalState, app: str) -> set[Vid]:
    """The versions ``app`` made, as its operations name them."""
    return {vid for change in changes_by(state, app) for vid in change.made if vid}


def touched_by(state: JournalState, app: str) -> set[Vid]:
    """The versions ``app`` made, and every version the game saved from one of them."""
    touched = made_by(state, app)
    children: dict[Vid, list[Vid]] = {}
    for edge in state.edges:
        children.setdefault(edge.parent, []).append(edge.child)
    while True:
        before = len(touched)
        todo = list(touched)
        while todo:
            for child in children.get(todo.pop(), ()):
                if child not in touched:
                    touched.add(child)
                    todo.append(child)
        # A slot's window opens at an operation that placed a touched version: what the game
        # saves there afterwards (versions first seen since) is touched, up to the next one.
        window: dict[LiveTarget, tuple[int, int] | None] = {}
        for rec in state.records:
            if isinstance(rec, OpRecord):
                if rec.status == "done":
                    opened = any(v in touched for v in rec.pair if v)
                    window[rec.target] = rec.order if opened else None
            elif isinstance(rec, ObsRecord) and window.get(rec.target) is not None:
                start = window[rec.target]
                touched.update(v for v in rec.pair
                               if v and state.first_seen.get(v, rec.order) >= start)
        if len(touched) == before:
            return touched


def playthrough_in(state: JournalState, target: LiveTarget) -> Pid | None:
    """The playthrough the slot holds as the journal last saw it, else the one an operation
    last placed there."""
    for t, now in state.targets.items():
        if same_slot(t, target):
            pid = _pid_of(state, now.pair)
            if pid is not None:
                return pid
    for rec in reversed(_ops(state)):
        if same_slot(rec.target, target):
            pid = _pid_of(state, rec.pair)
            if pid is not None:
                return pid
    return None


def last_before(state: JournalState, app: str, *, target: LiveTarget | None = None,
                pid: Pid | None = None) -> Before | None:
    """The newest pair an operation of ``app`` evicted that ``app`` had not touched: of
    playthrough ``pid``; of the playthrough slot ``target`` holds, else of what that slot held
    before ``app`` wrote it (the app put another playthrough there); or of any. None when
    there is none."""
    if target is not None and pid is None:
        held = playthrough_in(state, target)
        if held is not None:
            found = _last_before(state, app, None, held)
            if found is not None:
                return found
        return _last_before(state, app, target, None)
    return _last_before(state, app, target, pid)


def _last_before(state: JournalState, app: str, target: LiveTarget | None,
                 pid: Pid | None) -> Before | None:
    slot_only = target is not None
    touched = touched_by(state, app)
    best: Before | None = None
    for change in changes_by(state, app):
        if change.evicted is None:
            continue
        if slot_only and not same_slot(change.target, target):
            continue
        vids = [v for v in change.evicted if v]
        if not vids or any(v in touched for v in vids):
            continue
        if pid is not None and _pid_of(state, change.evicted) != pid:
            continue
        before = Before(change, change.evicted, {v: change.kept[v] for v in vids if v in change.kept})
        if best is None or (change.at, change.seq) >= (best.change.at, best.change.seq):
            best = before
    return best


def _pid_of(state: JournalState, pair: PairKey) -> Pid | None:
    for vid in pair:
        meta = state.metas.get(vid) if vid else None
        if meta is not None and meta.pid:
            return meta.pid
    return None
