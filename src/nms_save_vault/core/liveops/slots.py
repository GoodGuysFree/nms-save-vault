"""What a live account's slots hold right now, as the planner, ``execute`` and recovery see
them (D5 2.3.2): D1's scanner gives each member's version id, playthrough id and meta facts
(hashed through the per-machine cache, never decoding save data), and the save folder's own
view says which slots are partial or unreadable.

Reading only: nothing here writes, not even the scan cache.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import msstore, savedir
from ..organizer.cache import FILENAME, ScanCache, cache_dir
from ..organizer.model import LiveRef, LiveTarget, MemberObs, MetaSnap, PairKey
from ..organizer.recorder import _renamed        # the scanner's lone-folder naming, once
from ..organizer.scanner import ScanResult, scan
from ..state import Source
from .context import OpContext
from .errors import TargetError
from .model import Position, SlotState
from .steamwrite import file_names
from .targets import ResolvedAccount


@dataclass(frozen=True)
class MemberNow:
    position: Position
    vid: str
    pid: str
    meta: MetaSnap
    valid: bool
    sync_state: int | None                # Xbox record state; None on Steam


@dataclass(frozen=True)
class SlotNow:
    target: LiveTarget
    state: SlotState
    a: MemberNow | None
    b: MemberNow | None
    newer: Position | None                # the member the game loads now
    members_obs: tuple[MemberObs, ...]    # D1's observations of the present members

    def member(self, position: Position) -> MemberNow | None:
        return self.a if position == "A" else self.b

    @property
    def present(self) -> tuple[MemberNow, ...]:
        return tuple(m for m in (self.a, self.b) if m is not None)

    @property
    def pair(self) -> PairKey:
        return (self.a.vid if self.a else None, self.b.vid if self.b else None)

    @property
    def head(self) -> MemberNow | None:
        return self.member(self.newer) if self.newer else None


@dataclass(frozen=True)
class AccountNow:
    account: ResolvedAccount
    slots: Mapping[int, SlotNow]
    scan: ScanResult                      # one live folder, named as the target names it
    index_mtime: float | None             # Xbox: containers.index mtime (sync activity)
    total_bytes: int                      # Xbox: the account folder's size (cloud quota)


def source_of(ctx: OpContext, acct: ResolvedAccount) -> Source:
    return next(s for s in ctx.sources if s.id == acct.source_id)


def read_account(ctx: OpContext, acct: ResolvedAccount) -> AccountNow:
    """Every slot of one resolved live account, as it is now."""
    source = source_of(ctx, acct)
    result = _renamed(scan(None, [source], _cache(ctx), game_running=None, now=ctx.clock),
                      acct.account)
    folder_scan = result.live[acct.account]
    if not folder_scan.readable:
        raise TargetError("FOLDER_UNREADABLE", "the live folder cannot be read")
    view = savedir.scan_any(acct.folder)
    slots = {}
    for capture in folder_scan.captures:
        sv = view.slots[capture.slot]
        target = acct.account.target(capture.slot)
        members = {m.member: m for m in capture.members}
        now = {pos: _member_now(pos, members[pos], sv.members[0 if pos == "A" else 1])
               for pos in ("A", "B") if pos in members}
        slots[capture.slot] = SlotNow(target, _state(sv, now), now.get("A"), now.get("B"),
                                      capture.head_member, capture.members)
    index_mtime, total = None, 0
    if acct.account.platform == "xbox":
        index_mtime = (acct.folder / "containers.index").stat().st_mtime
        total = sum(p.stat().st_size for p in acct.folder.rglob("*") if p.is_file())
    return AccountNow(acct, slots, result, index_mtime, total)


def slot_fingerprint(folder: Path, platform: str, slot: int) -> str:
    """What must not change between plan and execute: per position the existence, size
    and mtime of the data and meta files; on Xbox the containers.index bytes, which name
    every blob and its time."""
    h = hashlib.sha256()
    if platform == "xbox":
        index = folder / "containers.index"
        h.update(hashlib.sha256(index.read_bytes()).digest() if index.is_file() else b"-")
        return h.hexdigest()
    for position in ("A", "B"):
        for name in file_names(slot, position):
            try:
                st = (folder / name).stat()
                h.update(f"{name}:{st.st_size}:{st.st_mtime_ns};".encode("ascii"))
            except FileNotFoundError:
                h.update(f"{name}:-;".encode("ascii"))
    return h.hexdigest()


def _member_now(position: Position, obs: MemberObs, mv: savedir.MemberView) -> MemberNow:
    sync = getattr(mv.xbox, "sync_state", None)
    return MemberNow(position, obs.vid, obs.pid, obs.meta, obs.meta.valid, sync)


def _state(sv: savedir.SlotView, now: Mapping[str, MemberNow]) -> SlotState:
    """D5 2.3.2: empty, partial (half a Steam pair, an Xbox record whose blob is missing),
    unreadable (a member whose meta does not read), mixed (two playthroughs), occupied."""
    if any(_half_there(mv) for mv in sv.members):
        return "partial"
    if not now:
        return "empty"
    if any(not m.valid for m in now.values()):
        return "unreadable"
    if len(now) == 2 and now["A"].pid != now["B"].pid:
        return "mixed"
    return "occupied"


def _half_there(mv: savedir.MemberView) -> bool:
    if mv.exists:
        return False
    if mv.xbox is not None:
        return getattr(mv.xbox, "sync_state", None) != msstore.SYNC_STATE_DELETED
    return mv.data_path.is_file() or mv.meta_path.is_file()


def _cache(ctx: OpContext) -> ScanCache:
    """The per-machine hash cache, read only: planning writes nothing anywhere."""
    vault_id = ctx.store.vault_id
    return ScanCache.load(cache_dir(ctx.store.vault_root, vault_id, ctx.host) / FILENAME,
                          vault_id, read_only=True)
