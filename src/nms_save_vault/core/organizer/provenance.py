"""Which versions an app made, and the last version before it changed a playthrough (CL1.5).

Read from the organizer's journal. Every operation record names the app that ran it
(:mod:`..appinfo`), the pair it placed, the pair it evicted, where each evicted member was
kept, and - for an operation that writes new content, such as an editor's edit - the versions
it made (``made``). From that:

- A version an app **made** is one an operation of that app names in ``made``. Nothing is
  inferred: a copy, a move or an undo makes nothing, whichever app runs it. (NMS Save Vault's
  own operations only ever copy.)
- A version an app **touched** is one it made, or one the game saved from one: a descent edge
  from it, or a version of that playthrough first seen in a slot after an operation placed a
  touched version there, while the slot still shows it (see :func:`_windows`). The game may
  have loaded either member, so the window does not rely on the replay's guess of which one
  was the parent; an edge from an untouched version overrules it. Without edges, a revert
  and a new save between two scans cannot be told from playing on.
- The **last version before** an app changed a playthrough is the newest pair of that
  playthrough that an operation of the app evicted from a slot, and that the app had not
  touched, together with where the vault kept it. If the app edited, the game played on and
  the app edited again, that is still the pair before the first edit. Asked by slot, the
  answer comes from that slot's own history and is always of the playthrough the slot holds
  now - or, when the app itself put that playthrough there, the pair it replaced. A
  playthrough that moved between slots is asked by playthrough.

Read-only, and the same for every app.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .. import appinfo
from .journal import JournalState, OpRecord
from .model import LiveTarget, PairKey, Pid, Vid, pids_compatible


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
    parents: dict[Vid, set[Vid]] = {}
    for edge in state.edges:
        children.setdefault(edge.parent, []).append(edge.child)
        parents.setdefault(edge.child, set()).add(edge.parent)
    while True:
        before = len(touched)
        todo = list(touched)
        while todo:
            for child in children.get(todo.pop(), ()):
                if child not in touched:
                    touched.add(child)
                    todo.append(child)
        _windows(state, touched, parents)
        if len(touched) == before:
            return touched


def _windows(state: JournalState, touched: set[Vid], parents: Mapping[Vid, set[Vid]]) -> None:
    """A slot's window opens at an operation that placed a touched version there. While it is
    open, a version the scan sees there for the first time is touched when it belongs to the
    placed playthrough and no descent edge says it came from an untouched one. The window
    closes at the slot's next operation, or when the slot shows nothing touched or new of
    that playthrough (the game or a sync reverted it, it was emptied, another playthrough is
    there)."""
    known = dict(state.first_seen)
    for rec in state.records:            # an evicted version was there before its operation
        if isinstance(rec, OpRecord):
            for vid in rec.evicted or ():
                if vid and (vid not in known or rec.order < known[vid]):
                    known[vid] = rec.order
    open_: dict[LiveTarget, tuple[tuple[int, int], set[Pid]]] = {}
    for rec in _deduped(state):
        if isinstance(rec, OpRecord):
            if rec.status != "done":
                continue
            placed = [v for v in rec.pair if v and v in touched]
            pids = {pid for v in placed for pid in _pids(state, (v,))}
            if placed:
                open_[rec.target] = (rec.order, pids)
            else:
                open_.pop(rec.target, None)
            continue
        window = open_.get(rec.target)
        if window is None:
            continue
        start, pids = window
        new = [v for v in rec.pair if v and v not in touched
               and known.get(v, start) > start
               and _compatible(_pids(state, (v,)), pids)
               and not (parents.get(v, set()) - touched)]
        if new:
            touched.update(new)
        elif not any(v in touched for v in rec.pair if v):
            del open_[rec.target]


def _deduped(state: JournalState):
    """Records in replay order, one operation record per operation and slot (as replay)."""
    seen: set[tuple[str, LiveTarget]] = set()
    for rec in state.records:
        if isinstance(rec, OpRecord):
            if (rec.op_id, rec.target) in seen:
                continue
            seen.add((rec.op_id, rec.target))
        yield rec


def playthrough_in(state: JournalState, target: LiveTarget) -> set[Pid]:
    """The playthrough(s) of the pair the slot holds as the journal last saw it (A and B
    may name two forms of one); empty when not known."""
    for t, now in state.targets.items():
        if same_slot(t, target):
            return _pids(state, now.pair)
    for rec in reversed(_ops(state)):
        if same_slot(rec.target, target):
            return _pids(state, rec.pair)
    return set()


def last_before(state: JournalState, app: str, *, target: LiveTarget | None = None,
                pid: Pid | None = None) -> Before | None:
    """The newest pair an operation of ``app`` evicted that ``app`` had not touched.

    - ``pid``: of that playthrough, in whatever slot.
    - ``target``: of the playthrough the slot holds now, from that slot's own history; when
      ``app`` itself put that playthrough there (a copy), the pair it replaced. Never another
      playthrough's pair: a playthrough moved here from another slot is asked by ``pid``.
    - Neither: the newest of all.

    None when there is none."""
    touched = touched_by(state, app)
    changes = changes_by(state, app)
    if target is None:
        return _newest(state, changes, touched, pid)
    held = playthrough_in(state, target)
    if not held:
        return None
    mine = [c for c in changes if same_slot(c.target, target)]
    found = _newest(state, mine, touched, None, held)
    if found is not None:
        return found
    last = next((rec for rec in reversed(_ops(state)) if same_slot(rec.target, target)), None)
    if last is None or appinfo.app_of(last.app) != app or not (_pids(state, last.pair) & held):
        return None
    return _newest(state, [_change(last)], touched, None)


def _newest(state: JournalState, changes, touched: set[Vid], pid: Pid | None,
            pids: set[Pid] | None = None) -> Before | None:
    best: Before | None = None
    for change in changes:
        if change.evicted is None:
            continue
        vids = [v for v in change.evicted if v]
        if not vids or any(v in touched for v in vids):
            continue
        have = _pids(state, change.evicted)
        if pid is not None and not _compatible(have, {pid}):
            continue
        if pids is not None and not _compatible(have, pids):
            continue
        before = Before(change, change.evicted,
                        {v: change.kept[v] for v in vids if v in change.kept})
        if best is None or (change.at, change.seq) >= (best.change.at, best.change.seq):
            best = before
    return best


def _pids(state: JournalState, pair) -> set[Pid]:
    out = set()
    for vid in pair:
        meta = state.metas.get(vid) if vid else None
        if meta is not None and meta.pid:
            out.add(meta.pid)
    return out


def _compatible(have: set[Pid], want: set[Pid]) -> bool:
    return any(pids_compatible(a, b) for a in have for b in want)
