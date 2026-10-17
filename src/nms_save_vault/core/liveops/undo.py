"""Undo and the history of one slot (D5 2.11).

``plan_undo`` builds an ordinary plan from the op's journal record and the slot's current
content, so an undo never overwrites progress silently and never touches another slot. Per
target the current pair is compared with what the op wrote and what it evicted:
``same`` and ``progressed`` (a descendant, played since) go back; ``already`` (a cloud revert
did it), ``stale`` (anything else) and an op that put a save into an empty slot (undoing it
would empty the slot, F8) are blocked. An undo is an op of kind ``undo`` with ``undo_of``, so
it can itself be undone.
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

from ..organizer.model import LiveTarget, target_from_json
from . import opjournal
from .context import OpContext
from .errors import LiveOpError, SourceUnreadable
from .model import Estimate, OpSummary, PairSource, PlacePlan, Warning
from .planner import _live_pair, _plan, _plan_warnings, _refusal, _summary, duration, durable, warning
from .slots import SlotNow, read_account
from .source import load_source
from .targets import resolve_account, resolve_target

VERIFY_ORDER = ("reverted", "deleted", "replaced", "pending", "expired", "superseded",
                "confirmed")


def plan_undo(ctx: OpContext, op_id: str) -> PlacePlan:
    """The plan that puts back what ``op_id`` replaced. Raises TargetError for a target no
    write may use; a move that cannot be undone gives a plan that says why (``blocked``)."""
    rec = opjournal.read_ops(ctx).get(op_id)
    writing = None if rec is None else rec.last("writing")
    if rec is None or rec.status != "done" or writing is None or rec.kind in ("keep", ""):
        return _unavailable(ctx)
    try:
        entries = [_Entry.of(t) for t in writing["targets"]]
    except (KeyError, TypeError, ValueError, opjournal.JournalError, LiveOpError):
        return _unavailable(ctx)
    swap = rec.kind == "swap"
    parts, warnings, blocks = [], [], []
    for e in entries:
        rt = resolve_target(ctx, e.target)
        now = read_account(ctx, resolve_account(ctx, e.target.ref))
        slot = now.slots[e.target.slot]
        parts.append((rt, now, slot, e.source))
        if not e.evicted:
            blocks.append(warning("UNDO_WOULD_EMPTY", slot=rt.label))
            continue
        state = e.state(slot)
        if state == "already":
            blocks.append(warning("ALREADY_UNDONE", slot=rt.label, name=e.evicted_name))
        elif state == "stale" or (swap and state == "empty"):
            blocks.append(warning("UNDO_STALE", slot=rt.label, name=_name(slot),
                                  was=e.evicted_name))
        elif state == "progressed":
            played = slot.head.meta.play_time - e.written_play if slot.head else 0
            warnings.append(warning("PLAYED_SINCE", slot=rt.label, played=duration(played),
                                    name=_name(slot)))      # the save that was played
    if blocks:
        return _with_blocks(_refusal(ctx, parts[0][0], "undo", blocks[0], parts[0][3]),
                            blocks, op_id)
    if swap and len(parts) == 2:
        # Progress travels with its save: each slot gets what the other holds now, which is
        # the evicted pair unless it was played since (then the played version goes back).
        (rt0, now0, slot0, _s0), (rt1, now1, slot1, _s1) = parts
        parts = [(rt0, now0, slot0, _live_pair(entries[1].target, slot1)),
                 (rt1, now1, slot1, _live_pair(entries[0].target, slot0))]
    loaded = []
    try:
        for rt, now, slot, source in parts:
            loaded.append((rt, now, slot, load_source(ctx, source), source))
    except SourceUnreadable:
        why = warning("SOURCE_UNREADABLE")
        return _with_blocks(_refusal(ctx, parts[0][0], "undo", why, parts[0][3]), [why], op_id)
    for rt, now, slot, src, _source in loaded:
        warnings += [w for w in _plan_warnings(ctx, src, rt.target.ref, now)
                     if w.code not in ("LIVE_ELSEWHERE", "OTHER_ACCOUNT")]
        if any(not durable(ctx, m.vid) for m in slot.present):
            warnings.append(warning("NOT_IN_VAULT"))
        code = {"mixed": "MIXED_SLOT", "partial": "PARTIAL_SLOT"}.get(slot.state)
        if code:
            warnings.append(warning(code))
    first = loaded[0][0].label
    outcome = (f"Swap {first} and {loaded[1][0].label} back" if len(loaded) == 2
               else f"Undo: put '{entries[0].evicted_name}' back into {first}")
    return _plan(ctx, "undo", loaded, warnings, outcome,
                 source=_summary(loaded[0][3]) if len(loaded) == 1 else None, undo_of=op_id)


def op_history(ctx: OpContext, target: LiveTarget) -> list[OpSummary]:
    """The ops that touched ``target``, newest first, for the history list."""
    oplog = {r.get("op_id"): r for r in ctx.vault.read_oplog() if r.get("v") == 2}
    out = []
    for rec in reversed(list(opjournal.read_ops(ctx).values())):
        if target not in rec.targets:
            continue
        out.append(OpSummary(
            rec.op_id, rec.kind, _local(rec.started), tuple(_labels(rec)),
            _text(rec, oplog.get(rec.op_id)), _status(rec), _worst_verify(rec),
            rec.events[0].get("undo_of")))
    return out


# --- one target of the op -------------------------------------------------------------------


class _Entry:
    """One target of an op as its write-ahead record tells it."""

    def __init__(self, target: LiveTarget, post: dict, pre: dict, newer: str | None,
                 source: PairSource, loading: str) -> None:
        self.target, self.post, self.newer = target, post, newer
        self.evicted = {p: d for p, d in pre.items() if d}
        self.source = source
        self.loading = loading

    @classmethod
    def of(cls, t: dict) -> "_Entry":
        target = target_from_json(t["target"])
        post = {p: opjournal.member_write_from_json(v) for p, v in t["post"].items()}
        pre = t["pre"]
        newer = t.get("pre_newer")
        a, b = (pre.get(p) for p in ("A", "B"))
        prefer = None
        for kept in t.get("preserved", ()):
            prefer = opjournal.location_from_json(kept["location"])
            break
        source = PairSource(a["vid"] if a else None, b["vid"] if b else None, newer, prefer)
        return cls(target, post, pre, newer, source, t["load"])

    @property
    def evicted_name(self) -> str:
        d = self.evicted.get(self.newer or "A") or next(iter(self.evicted.values()), None)
        return (d or {}).get("name") or "<unnamed>"

    @property
    def written_pair(self) -> tuple:
        return tuple(self.post[p].vid if p in self.post else None for p in ("A", "B"))

    @property
    def evicted_pair(self) -> tuple:
        return tuple(self.evicted[p]["vid"] if p in self.evicted else None for p in ("A", "B"))

    @property
    def written_play(self) -> int:
        return self.post[self.loading].play_time

    def state(self, slot: SlotNow) -> str:
        """same / progressed / already / empty / stale."""
        if not slot.present:
            return "empty"
        cur = slot.pair
        if cur == self.evicted_pair and (cur != self.written_pair or slot.newer == self.newer):
            return "already"
        if cur == self.written_pair:
            return "same"
        head, loading = slot.head, self.post[self.loading]
        if head is not None and loading.pid and head.pid == loading.pid and \
                head.meta.play_time >= loading.play_time:
            return "progressed"
        return "stale"


def _name(slot: SlotNow) -> str:
    return (slot.head.meta.name if slot.head is not None else "") or "<unnamed>"


def _unavailable(ctx: OpContext) -> PlacePlan:
    """A plan that only says the move cannot be undone (there is no target to plan against)."""
    why = warning("UNDO_UNAVAILABLE")
    return PlacePlan(uuid.uuid4().hex, "undo", (), None, None, (), frozenset(), (why,),  # type: ignore[arg-type]
                     why.text, "", Estimate(0, 0, 0, 0.0), ctx.clock())


def _with_blocks(plan: PlacePlan, blocks: list[Warning], op_id: str) -> PlacePlan:
    return dataclasses.replace(plan, blocked=tuple(blocks),
                               outcome=" ".join(w.text for w in blocks), undo_of=op_id)


# --- the history rows -----------------------------------------------------------------------


def _status(rec) -> str:
    return rec.status if rec.status in ("done", "rolled_back", "failed", "abandoned") else "open"


def _worst_verify(rec) -> str:
    states = {s for s in (rec.verify_state(t) for t in rec.targets) if s}
    for state in VERIFY_ORDER:
        if state in states:
            return state
    return "n/a"


def _text(rec, line: dict | None) -> str:
    if line is not None and line.get("detail"):
        return str(line["detail"])
    return f"{rec.kind.replace('_', ' ')} {', '.join(_labels(rec)) or 'a live slot'}".strip()


def _labels(rec) -> list[str]:
    return [f"{'Steam' if t.platform == 'steam' else 'Xbox'} slot {t.slot}" for t in rec.targets]


def _local(utc: str) -> str:
    try:
        moment = datetime.strptime(utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc
    return moment.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
