"""Recovery of an interrupted operation (D5 2.10), at start-up and whenever the UI asks.

An op is open when its last lifecycle event is ``preparing``, ``writing``, ``validated`` or
``failed``. Each target slot is compared with the op's write-ahead record: every file (Steam)
or the index and the xgs copies (Xbox) is the pre-image, the post-image, or other. Then:

==========  ================  =================================================
last event  live state        action
==========  ================  =================================================
preparing   (nothing written) automatic: abandoned (not started)
writing     all pre           automatic: the op's temp files go; abandoned
writing     all post          forward (recommended)
writing     pre and post      forward (recommended when the source re-verifies) or back
writing     any other         leave only: the slot holds someone else's save now
validated   (verified)        automatic: finish the records
failed      any               back (retry the rollback) or leave
==========  ================  =================================================

Another machine's ops are listed and never touched. Nothing is decided by time. Every write
happens under the live-op lock and only while the game is known not to run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .. import msstore, safety
from ..organizer.locator import LiveMember
from ..organizer.model import LiveTarget, MetaSnap, target_from_json
from ..organizer.recorder import LiveOpBusy
from . import opjournal, pairmeta, steamwrite
from .context import OpContext
from .execute import (
    LIVEOP_TIMEOUT,
    OpTarget,
    append_quietly,
    finalize,
    finish,
    identifier,
    outside_files,
    record_lineage,
    record_oplog,
    refused,
    rollback_target,
    validate,
)
from .errors import LiveOpError, SourceUnreadable, TargetError
from .model import OpOutcome, PairSource, RecoveryItem, TargetPlan
from .pairmeta import PlannedMember, SourcePair
from .slots import MemberNow, SlotNow, read_account
from .source import LoadedSource, load_source
from .targets import resolve_account, resolve_target

log = logging.getLogger(__name__)

Choice = Literal["forward", "back", "leave"]
TEXTS = {
    "not_started": "A move into {labels} was stopped before anything was written. Nothing changed.",
    "complete": "A move into {labels} was interrupted after it was written. Finish it "
                "(recommended).",
    "torn": "A move into {labels} was interrupted half-way. Finish it (recommended) or put "
            "the slot back.",
    "superseded": "A move into {labels} was interrupted, and the slot has changed since. The "
                  "save it replaced is in the vault.",
    "failed": "A move into {labels} failed and could not be put back automatically. Put it "
              "back (recommended).",
    "other_machine": "A move into {labels} was started on another computer. Finish it there.",
    "vault_missing": "A move into {labels} was interrupted and its records are on the vault "
                     "drive. Connect it before playing.",
}
GAME_TEXT = "Close No Man's Sky to finish."


@dataclass
class _Open:
    """One open op as recovery sees it."""

    record: opjournal.OpRecord
    targets: list[OpTarget]
    states: list[str]                  # per target: pre / post / torn / other
    problem: str = ""                  # why it cannot be acted on (a damaged record, ...)

    @property
    def state(self) -> str:
        if self.problem or "other" in self.states:
            return "other"
        if all(s == "pre" for s in self.states):
            return "pre"
        if all(s == "post" for s in self.states):
            return "post"
        return "torn"


def pending_recovery(ctx: OpContext) -> list[RecoveryItem]:
    """The interrupted ops the owner must decide on, after the automatic cases are dealt
    with. Empty while another operation holds the live-op lock (it is running, not
    interrupted)."""
    items: list[RecoveryItem] = []
    try:
        with opjournal.liveop_lock(ctx, timeout=0):
            items = _pending(ctx)
    except (LiveOpBusy, RuntimeError):
        return []
    return items + _missing_vaults(ctx)


def resolve_recovery(ctx: OpContext, op_id: str, choice: Choice) -> OpOutcome:
    """Apply the owner's choice to one open op of this machine."""
    try:
        with opjournal.liveop_lock(ctx, timeout=LIVEOP_TIMEOUT):
            return _resolve(ctx, op_id, choice)
    except LiveOpBusy:
        return refused(op_id, "BUSY", "Another operation is still running. Try again in a moment.")


def _pending(ctx: OpContext) -> list[RecoveryItem]:
    items = []
    game_free = ctx.game_probe() is False
    for rec in opjournal.read_ops(ctx).values():
        if not rec.open:
            continue
        if rec.machine != ctx.machine_id:
            items.append(_item(rec, "other_machine", "none", ()))
            continue
        if rec.status == "preparing":                 # nothing live can have changed
            append_quietly(ctx, rec.op_id, "abandoned", {"reason": "not_started"})
            opjournal.remove_marker(ctx, rec.op_id)
            continue
        op = _open(ctx, rec)
        if rec.status == "validated" and not op.problem:
            if game_free:
                _finish_validated(ctx, op)
                continue
        elif rec.status == "writing" and op.state == "pre" and game_free:
            _back(ctx, op, recovered=False)
            append_quietly(ctx, rec.op_id, "abandoned", {"reason": "not_started"})
            opjournal.remove_marker(ctx, rec.op_id)
            continue
        items.append(_classify(ctx, op, game_free))
    _drop_stale_markers(ctx)
    return items


def _classify(ctx: OpContext, op: _Open, game_free: bool) -> RecoveryItem:
    rec = op.record
    if rec.status == "failed":
        item = _item(rec, "failed", "back", ("back", "leave"))
    elif rec.status == "validated" or op.state == "post":
        item = _item(rec, "complete", "forward", ("forward",))
    elif op.state == "other":
        item = _item(rec, "superseded", "leave", ("leave",))
    elif op.state == "pre":
        item = _item(rec, "not_started", "back", ("back",))
    else:
        item = _item(rec, "torn", "forward" if _can_forward(ctx, op) else "back",
                     ("forward", "back"))
    if not game_free:
        item = RecoveryItem(item.op_id, item.kind, item.labels, item.state, item.recommended,
                            item.choices, f"{item.text} {GAME_TEXT}", item.started)
    return item


def _resolve(ctx: OpContext, op_id: str, choice: Choice) -> OpOutcome:
    rec = opjournal.read_ops(ctx).get(op_id)
    if rec is None or not rec.open or rec.machine != ctx.machine_id:
        return refused(op_id, "NOT_OPEN", "That move is not waiting to be recovered here.")
    if ctx.game_probe() is not False:
        return refused(op_id, "GAME_RUNNING", GAME_TEXT)
    op = _open(ctx, rec)
    if op.problem:
        return refused(op_id, "DAMAGED", f"The record of that move cannot be used: {op.problem}")
    if rec.status == "preparing":
        append_quietly(ctx, op_id, "abandoned", {"reason": "not_started"})
        opjournal.remove_marker(ctx, op_id)
        return OpOutcome(op_id, "rolled_back", "Nothing had been written.", (), None, "n/a", None)
    if choice == "leave":
        return _leave(ctx, op)
    # A failed op's own write may have left a slot that is neither image (a corrupt file):
    # retrying its rollback is what "failed" offers. Anything else is someone else's save.
    if op.state == "other" and not (rec.status == "failed" and choice == "back"):
        return refused(op_id, "SLOT_CHANGED", TEXTS["superseded"].format(labels=_labels(rec)))
    if choice == "forward" and rec.status == "failed":
        return refused(op_id, "NOT_OFFERED", "That move can only be put back or left.")
    if choice == "forward":
        return _forward(ctx, op)
    return _back(ctx, op, recovered=True)


# --- the three ways out ----------------------------------------------------------------------


def _forward(ctx: OpContext, op: _Open) -> OpOutcome:
    """Finish the write: the post bytes re-derived from the source must hash exactly to the
    record; only what is not yet post is written; then the usual check and records."""
    rec = op.record
    try:
        for wt in op.targets:
            _rederive(ctx, wt)
        for wt, state in zip(op.targets, op.states):
            if wt.steam is not None:
                _forward_steam(wt)
            elif state != "post" or msstore.pair_index_state(wt.xbox) != "post":
                if msstore.pair_index_state(wt.xbox) == "pre":
                    msstore.apply_pair(wt.xbox, wt.payload)
                else:
                    msstore.complete_pair(wt.xbox, wt.payload)
        for wt in op.targets:
            validate(wt, steamwrite.op_tag(rec.op_id))
    except (LiveOpError, SourceUnreadable, msstore.IndexChanged, OSError, ValueError) as exc:
        log.warning("op %s: could not be finished: %s", rec.op_id, exc)
        return refused(rec.op_id, "FORWARD_FAILED",
                           f"The move could not be finished ({exc}). Put the slot back instead.")
    opjournal.append(ctx, rec.op_id, "recovered", {"choice": "forward"})
    opjournal.append(ctx, rec.op_id, "validated", {"targets": [
        {"target": opjournal.target_json(wt.rt.target), "ok": True} for wt in op.targets]})
    return _finish_validated(ctx, op)


def _finish_validated(ctx: OpContext, op: _Open) -> OpOutcome:
    rec = op.record
    for wt in op.targets:
        finalize(wt)
    first = rec.events[0]
    warnings = finish(ctx, rec.op_id, rec.kind, first.get("undo_of"), op.targets,
                         first.get("confirmed", ()), first.get("note", ""))
    return OpOutcome(rec.op_id, "done", f"The move into {_labels(rec)} is finished.",
                     tuple(warnings), None, "pending", None)


def _back(ctx: OpContext, op: _Open, *, recovered: bool) -> OpOutcome:
    """The exact rollback of 2.9.2 from the recorded pre-image."""
    rec = op.record
    problems = []
    for wt in reversed(op.targets):
        try:
            rollback_target(ctx, wt)
        except Exception as exc:  # noqa: BLE001 - reported; the op stays open
            problems.append(f"{wt.rt.label}: {exc}")
    if problems:
        return OpOutcome(rec.op_id, "failed", "The slot could not be put back: "
                         + "; ".join(problems), (), None, "n/a", "; ".join(problems),
                         "ROLLBACK_FAILED")
    if recovered:
        first = rec.events[0]
        opjournal.append(ctx, rec.op_id, "recovered", {"choice": "back"})
        opjournal.append(ctx, rec.op_id, "rolled_back", {"stage": "recovery", "error": None,
                                                         "restored": True})
        try:
            record_oplog(ctx, rec.op_id, rec.kind, first.get("undo_of"), op.targets,
                            "rolled_back", first.get("confirmed", ()), first.get("note", ""),
                            "interrupted")
            record_lineage(ctx, rec.op_id, rec.kind, first.get("undo_of"), op.targets,
                              "rolled_back", first.get("note", ""))
        except Exception as exc:  # noqa: BLE001 - history only
            log.warning("op %s: the rollback was not recorded: %s", rec.op_id, exc)
        opjournal.remove_marker(ctx, rec.op_id)
    return OpOutcome(rec.op_id, "rolled_back", f"{_labels(rec)} is as it was before the move.",
                     (), None, "n/a", None)


def _leave(ctx: OpContext, op: _Open) -> OpOutcome:
    """Keep the slots as they are now: what they hold is kept in the vault when it is not
    there yet; the op ends (superseded). The save the op replaced is in the vault already."""
    rec = op.record
    for wt in op.targets:
        try:
            now = read_account(ctx, resolve_account(ctx, wt.rt.target.ref)).slots[wt.rt.target.slot]
            members = [LiveMember(wt.rt.target, m.position, m.vid) for m in now.present
                       if not ctx.locator.is_durable(m.vid)]
            if members:
                ctx.locator.preserve(members, op_id=rec.op_id, reason="recovery")
        except Exception as exc:  # noqa: BLE001 - the op is left as it is, said so
            return refused(rec.op_id, "PRESERVE_FAILED",
                               f"What {wt.rt.label} holds now could not be kept in the vault "
                               f"({exc}). Nothing was changed.")
        if wt.steam is not None:
            steamwrite.sweep(wt.folder, steamwrite.op_tag(rec.op_id))
    opjournal.append(ctx, rec.op_id, "abandoned", {"reason": "superseded"})
    opjournal.remove_marker(ctx, rec.op_id)
    return OpOutcome(rec.op_id, "rolled_back",
                     f"{_labels(rec)} is left as it is now; it is kept in the vault.", (), None,
                     "n/a", None)


# --- reading an open op back -------------------------------------------------------------------


def _open(ctx: OpContext, rec: opjournal.OpRecord) -> _Open:
    writing = rec.last("writing")
    if writing is None:
        return _Open(rec, [], [], "")
    targets, states = [], []
    try:
        for t in writing["targets"]:
            wt = _target(ctx, rec.op_id, t)
            targets.append(wt)
            states.append(_state(wt, rec.op_id))
    except (KeyError, TypeError, ValueError, TargetError) as exc:
        log.warning("op %s: its write-ahead record cannot be used: %s", rec.op_id, exc)
        return _Open(rec, targets, states, str(exc))
    return _Open(rec, targets, states)


def _target(ctx: OpContext, op_id: str, t: dict) -> OpTarget:
    target = target_from_json(t["target"])
    rt = resolve_target(ctx, target)
    load = t["load"]
    post = {p: opjournal.member_write_from_json(v) for p, v in t["post"].items()}
    writes = (PlannedMember(post[load], b""), PlannedMember(post[pairmeta.other(load)], b""))
    source = opjournal.pair_source_from_json(t["source"])
    before = _before(target, t["pre"], t.get("pre_newer"))
    stand_in = LoadedSource(SourcePair(target.platform, load, None, None), {}, t.get("name", ""),
                            0, 0, 0, None, "")
    wt = OpTarget(TargetPlan(target, rt.label, rt.cloud_mode, before.state, (), (), source),
                    rt, stand_in, before, writes=writes)
    wt.snapshot_id = t["snapshot_id"]
    wt.preserved = [opjournal.location_from_json(p["location"]) for p in t["preserved"]]
    if "steam" in t:
        wt.steam = opjournal.steam_plan_from_json(t["steam"], rt.folder, slot=target.slot,
                                                  op_id=op_id)
    else:
        wt.xbox = opjournal.xbox_plan_from_json(t["xbox"], rt.folder, slot=target.slot)
    wt.outside = outside_files(rt.folder, _slot_names(wt), _blob_dirs(wt))
    return wt


def _before(target: LiveTarget, pre: dict, newer: str | None) -> SlotNow:
    members = {}
    for pos in ("A", "B"):
        d = pre.get(pos)
        if d:
            snap = MetaSnap(d.get("play_time", 0), d.get("meta_ts", 0), "meta", d.get("name", ""),
                            "", 0, 0, 0, 0, 0, 0, 0, "", True, 0.0)
            members[pos] = MemberNow(pos, d["vid"], d.get("pid") or "", snap, True, None)
    state = "occupied" if members else "empty"
    return SlotNow(target, state, members.get("A"), members.get("B"), newer, ())


def _state(wt: OpTarget, op_id: str) -> str:
    """pre / post / torn / other for one target."""
    if wt.steam is not None:
        posts = {f.name: f.sha256 for f in wt.steam.files}
        kinds = set()
        for name, pre in wt.steam.pre.items():
            now = steamwrite.file_state(wt.folder / name)
            now_sha = now.sha256 if now else None
            pre_sha = pre.sha256 if pre else None
            if pre_sha == posts.get(name) and now_sha == pre_sha:
                continue                                  # unchanged by the write either way
            kinds.add("pre" if now_sha == pre_sha else "post" if now_sha == posts.get(name)
                      else "other")
        return _combine(kinds)
    index = msstore.pair_index_state(wt.xbox)
    mirror = set(msstore.pair_xgs_states(wt.xbox).values())
    if index == "other" or "other" in mirror:
        return "other"
    if index == "pre":
        return "pre" if mirror <= {"pre"} else "other"     # xgs is written after the index
    return "post" if mirror <= {"post"} else "torn"


def _combine(kinds: set[str]) -> str:
    if "other" in kinds:
        return "other"
    if kinds <= {"pre"}:
        return "pre"
    if kinds == {"post"}:
        return "post"
    return "torn"


def _rederive(ctx: OpContext, wt: OpTarget) -> None:
    """The post bytes again, from the source (or its preserved copy), stamped with the
    recorded time: they must hash exactly to the record."""
    source = wt.tp.source
    try:
        src = load_source(ctx, source)
    except SourceUnreadable:
        if source.prefer is None:
            raise
        src = load_source(ctx, PairSource(source.vid_a, source.vid_b, source.load, None))
    now = int(wt.writes[0].write.disk_time)
    planned = pairmeta.compute(src.pair, wt.rt, now)
    for got, recorded in zip(planned, wt.writes):
        g, r = got.write, recorded.write
        if (g.position, g.vid, g.meta_disk_sha256) != (r.position, r.vid, r.meta_disk_sha256):
            raise LiveOpError("the move cannot be written again exactly as recorded")
    wt.src, wt.writes = src, planned
    slot = wt.rt.target.slot
    if wt.steam is not None:
        triples = [(pm.write, src.data[pm.write.vid], pm.meta_disk) for pm in planned]
        wt.payload = steamwrite.payload_of(triples, slot)
    else:
        wt.payload = {identifier(slot, pm.write.position): (src.data[pm.write.vid], pm.meta_disk)
                      for pm in planned}


def _forward_steam(wt: OpTarget) -> None:
    """Stage and swap only the names that are not post yet, then put every planned mtime."""
    plan = wt.steam
    todo = tuple(f for f in plan.files if _sha_of(plan.folder / f.name) != f.sha256)
    if todo:
        sub = steamwrite.SteamWritePlan(
            plan.folder, todo, {f.name: steamwrite.file_state(plan.folder / f.name) for f in todo},
            plan.op_tag)
        steamwrite.sweep(plan.folder, plan.op_tag)
        steamwrite.stage(sub, wt.payload)
        steamwrite.swap(sub)
    for f in plan.files:
        os.utime(plan.folder / f.name, (f.mtime, f.mtime))
    safety.fsync_dir(plan.folder)


def _can_forward(ctx: OpContext, op: _Open) -> bool:
    """Forward is recommended for a torn op when its source and preserved copies open."""
    try:
        for wt in op.targets:
            _rederive(ctx, wt)
            for loc in wt.preserved:
                ctx.locator.open(loc.vid, loc)
    except Exception:  # noqa: BLE001 - then going back is the safer recommendation
        return False
    return True


# --- listing ---------------------------------------------------------------------------------


def _item(rec: opjournal.OpRecord, state: str, recommended: str,
          choices: tuple[str, ...]) -> RecoveryItem:
    return RecoveryItem(rec.op_id, rec.kind, tuple(_target_labels(rec)), state,  # type: ignore[arg-type]
                        recommended, choices, TEXTS[state].format(labels=_labels(rec)),  # type: ignore[arg-type]
                        _local(rec.started))


def _missing_vaults(ctx: OpContext) -> list[RecoveryItem]:
    """Markers of ops whose vault is not this one and cannot be reached (a removed drive)."""
    out = []
    for marker in opjournal.list_markers(ctx.host):
        if marker.vault_id == ctx.store.vault_id:
            continue
        if (Path(marker.vault_root) / "organizer").is_dir():
            continue
        labels = ", ".join(marker.labels) or "a live slot"
        out.append(RecoveryItem(marker.op_id, "", marker.labels, "vault_missing", "none", (),
                                TEXTS["vault_missing"].format(labels=labels),
                                _local(marker.utc)))
    return out


def _drop_stale_markers(ctx: OpContext) -> None:
    """A marker of this vault whose op is closed is left over from a crash after ``done``."""
    ops = opjournal.read_ops(ctx)
    for marker in opjournal.list_markers(ctx.host):
        rec = ops.get(marker.op_id)
        if marker.vault_id == ctx.store.vault_id and rec is not None and not rec.open:
            opjournal.remove_marker(ctx, marker.op_id)


def _labels(rec: opjournal.OpRecord) -> str:
    return ", ".join(_target_labels(rec)) or "a live slot"


def _target_labels(rec: opjournal.OpRecord) -> list[str]:
    return [f"{'Steam' if t.platform == 'steam' else 'Xbox'} slot {t.slot}" for t in rec.targets]


def _local(utc: str) -> str:
    try:
        moment = datetime.strptime(utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc
    return moment.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def _slot_names(wt: OpTarget) -> set[str]:
    if wt.steam is None:
        return {"containers.index"}
    return {n for p in ("A", "B") for n in steamwrite.file_names(wt.rt.target.slot, p)}


def _blob_dirs(wt: OpTarget) -> set[str]:
    return {b.dir_name for b in wt.xbox.blobs} if wt.xbox is not None else set()


def _sha_of(path: Path) -> str | None:
    state = steamwrite.file_state(path)
    return state.sha256 if state else None
