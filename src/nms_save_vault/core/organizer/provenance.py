"""Which versions an app made, and the last version before it changed a slot (CL1.5).

Read from the organizer's journal. Every operation record names the app that ran it
(:mod:`..appinfo`), the pair it placed, the pair it evicted and where each evicted member was
kept. From that:

- A version an app **made** is one whose first record is that app placing it, or keeping it
  in an entry of a kind that makes versions. Anything seen first otherwise - in a slot the
  scan observed, as what an operation evicted, or in a copy entry (backup, snapshot,
  preserve, ...) - was not made by an app: the game wrote it. So an app that only moves a
  version or puts one back (a copy, an undo) makes nothing, and the snapshot it takes of the
  slot before writing claims nothing either.
- A version an app **touched** is one it made, or one the game saved from such a version
  (a descent edge): both come after the app's change.
- The **last version before** an app changed a slot is the newest pair one of the app's
  operations evicted from it that the app had not touched. If the app edited a slot, the game
  played on and the app edited again, that is still the pair before the first edit.

Read-only, and the same for every app: NMS Save Vault's own records answer for
``"nms-save-vault"``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import appinfo
from .journal import JournalState, OpRecord
from .model import LiveTarget, PairKey, Pid, Vid

#: What a vault entry is to this module: its stamp (None for a copy), its UTC time, the
#: versions it holds.
EntryOrigin = tuple[str | None, int, tuple[Vid, ...]]

#: Entry kinds that copy versions that already exist: every kind NMS Save Vault keeps. A kind
#: that makes versions (an editor's output) is not listed, so its entry names its app.
COPY_KINDS = frozenset({"full", "snapshot", "extract", "imported", "preserve"})


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
    kept: Mapping[Vid, Mapping[str, Any]]   # where each evicted member was kept (a Location)
    snapshot: str | None                    # the entry the slot was kept in before the write
    seq: int = 0


@dataclass(frozen=True)
class Before:
    """The slot as it was just before ``change``, and where its members were kept."""

    change: AppChange
    pair: PairKey
    kept: Mapping[Vid, Mapping[str, Any]]


def same_slot(a: LiveTarget, b: LiveTarget) -> bool:
    """One slot of one live folder. The instance tag (two folders of one name) is ignored: an
    app outside the organizer names a folder by platform and name only."""
    return (a.platform, a.account_key, a.slot) == (b.platform, b.account_key, b.slot)


def changes_by(state: JournalState, app: str) -> list[AppChange]:
    """Every operation ``app`` (a name, ``"nms-savecraft"``) ran that went through, oldest
    first; one per operation and slot."""
    out: list[AppChange] = []
    seen: set[tuple[str, LiveTarget]] = set()
    for rec in state.records:
        if not isinstance(rec, OpRecord) or (rec.op_id, rec.target) in seen:
            continue
        seen.add((rec.op_id, rec.target))
        if rec.status != "done" or appinfo.app_of(rec.app) != app:
            continue
        out.append(AppChange(rec.op_id, rec.kind, rec.at, rec.app, rec.target, rec.pair,
                             rec.evicted, dict(rec.preserved), rec.snapshot, rec.seq))
    return out


def entry_origins(entries: Iterable[Any]) -> list[EntryOrigin]:
    """(stamp, UTC unix, vids) of vault entries (``StoreEntry``). One without a readable time
    is left out, so it never claims a version first."""
    out: list[EntryOrigin] = []
    for entry in entries:
        try:
            when = datetime.strptime(entry.created_utc, "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            continue
        at = int(when.replace(tzinfo=timezone.utc).timestamp())
        stamp = None if entry.kind in COPY_KINDS else entry.app_version
        out.append((stamp, at, tuple(m.vid for m in entry.members if m.vid)))
    return out


def first_recorded_by(state: JournalState, entries: Iterable[EntryOrigin] = ()
                      ) -> dict[Vid, str | None]:
    """The app (a name) that made each version, from its first record; None when the game
    wrote it. At one second, what an operation evicted was there before anything kept, and
    an entry was kept before the operation placed it."""
    first: dict[Vid, tuple[int, int, str | None]] = {}

    def offer(vid: Vid, at: int, rank: int, who: str | None) -> None:
        if vid not in first or (at, rank) < first[vid][:2]:
            first[vid] = (at, rank, who)

    for stamp, at, vids in entries:
        for vid in vids:
            offer(vid, at, 1, appinfo.app_of(stamp) if stamp else None)
    for rec in state.records:
        if isinstance(rec, OpRecord):
            for vid in rec.evicted or ():
                if vid:
                    offer(vid, rec.at, 0, None)
        who = appinfo.app_of(rec.app) if isinstance(rec, OpRecord) else None
        for vid in rec.pair:
            if vid:
                offer(vid, rec.at, 2, who)
    return {vid: who for vid, (_at, _rank, who) in first.items()}


def made_by(state: JournalState, app: str, entries: Iterable[EntryOrigin] = ()) -> set[Vid]:
    """The versions ``app`` made: the first record of each names it."""
    return {vid for vid, who in first_recorded_by(state, entries).items() if who == app}


def touched_by(state: JournalState, app: str, entries: Iterable[EntryOrigin] = ()) -> set[Vid]:
    """The versions ``app`` made, and every version the game saved from one of them."""
    touched = made_by(state, app, entries)
    children: dict[Vid, list[Vid]] = {}
    for edge in state.edges:
        children.setdefault(edge.parent, []).append(edge.child)
    todo = list(touched)
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in touched:
                touched.add(child)
                todo.append(child)
    return touched


def last_before(state: JournalState, app: str, *, target: LiveTarget | None = None,
                pid: Pid | None = None, entries: Iterable[EntryOrigin] = ()) -> Before | None:
    """The newest pair an operation of ``app`` evicted, of ``target`` (a slot) or of ``pid``
    (a playthrough, in whatever slot), that ``app`` had not touched; None when it never
    changed one."""
    touched = touched_by(state, app, entries)
    best: Before | None = None
    for change in changes_by(state, app):
        if change.evicted is None or (target is not None and not same_slot(change.target, target)):
            continue
        vids = [v for v in change.evicted if v]
        if not vids or any(v in touched for v in vids):
            continue
        if pid is not None and not any(_pid(state, v) == pid for v in vids):
            continue
        before = Before(change, change.evicted, {v: change.kept[v] for v in vids if v in change.kept})
        if best is None or (change.at, change.seq) >= (best.change.at, best.change.seq):
            best = before
    return best


def _pid(state: JournalState, vid: Vid) -> Pid | None:
    meta = state.metas.get(vid)
    return meta.pid if meta is not None else None
