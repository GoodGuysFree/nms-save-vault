"""``execute`` (D5 2.8): one live operation, start to finish, under the live-op lock.

Order, each step only after the one before succeeded: re-check the plan (vault mode,
fingerprint, open ops, space, the game); journal ``preparing``; read the source; preserve
every member that will be replaced (a verified store entry); a whole-folder store snapshot;
prepare the writers; journal ``writing`` (the write-ahead record of every file the op may
create) and a local marker; look at the game once more; write; validate; on any failure roll
the slot back exactly and say so. Then ``validated``, finalize, the oplog line, D1's op
record, D1's observation of the result, ``done``.

Nothing live is written before ``writing`` is on disk, and nothing is deleted except files
the op itself created (a rollback) and the replaced blobs of its own Xbox records (D5 I9).
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import formats, lz4_block, meta, msstore, savedir
from ..organizer.journal import Journal
from ..organizer.locator import LiveMember, Location, PreserveFailed
from ..organizer.model import LiveRef, LiveTarget, new_op_id, pid_kind
from ..organizer.recorder import LIVEOP_TIMEOUT, LiveOpBusy, OpLineage
from ..organizer.store import StoreBusy as OrganizerBusy
from ..organizer.store import StoreReadOnly
from ..store import CaptureScope, PreserveMismatch, StoreError
from ..store import errors as store_errors
from . import opjournal, pairmeta, steamwrite
from .context import OpContext
from .errors import ConfirmationRequired, LiveChanged, LiveOpError, SourceUnreadable
from .faults import at
from .model import OpOutcome, PlacePlan, Progress, ResolvedTarget, TargetPlan, UndoOffer, Warning
from .planner import fingerprint, game_warning, recovery_pending, vault_problem, warning
from .slots import SlotNow, read_account
from .source import LoadedSource, load_source
from .targets import resolve_account, resolve_target

log = logging.getLogger(__name__)


RECORDING_PENDING = Warning("RECORDING_PENDING", "info",
                            "Done. The history will finish updating at the next start.", {})


class ValidationError(LiveOpError):
    """A written slot is not what the op planned (D5 2.9.1)."""


class _Abandon(Exception):
    """The op ends before any live write: ``reason`` for the journal, ``code`` for the
    caller."""

    def __init__(self, reason: str, code: str, text: str = "") -> None:
        super().__init__(text or code)
        self.reason, self.code, self.text = reason, code, text


@dataclass
class OpTarget:
    """One target slot of a running op."""

    tp: TargetPlan
    rt: ResolvedTarget
    src: LoadedSource
    before: SlotNow
    preserved: list[Location] = field(default_factory=list)
    snapshot_id: str | None = None
    writes: tuple = ()                    # (loading, other) PlannedMember
    steam: steamwrite.SteamWritePlan | None = None
    xbox: msstore.XboxPairPlan | None = None
    payload: dict = field(default_factory=dict)
    outside: dict = field(default_factory=dict)

    @property
    def folder(self) -> Path:
        return self.rt.folder


def execute(ctx: OpContext, plan: PlacePlan, confirmed: Iterable[str] = frozenset(),
            progress: Callable[[Progress], None] | None = None, *, note: str = "") -> OpOutcome:
    """Run ``plan``. Returns an outcome for every operational result; raises only
    :class:`ConfirmationRequired` (the caller did not echo a confirmation) and
    ``TargetError`` (a target no write may use)."""
    if plan.blocked:
        return refused(None, plan.blocked[0].code, plan.blocked[0].text)
    confirmed = frozenset(confirmed)
    missing = plan.confirmations - confirmed
    if missing:
        raise ConfirmationRequired(missing)
    try:
        with opjournal.liveop_lock(ctx, timeout=LIVEOP_TIMEOUT):
            return _run(ctx, plan, confirmed, progress or (lambda p: None), note)
    except LiveOpBusy:
        return refused(None, "BUSY", "Another operation is still running. Try again in a moment.")


def _run(ctx: OpContext, plan: PlacePlan, confirmed: frozenset[str],
         progress: Callable[[Progress], None], note: str) -> OpOutcome:
    progress(Progress("checking", 0, 1))
    gate = vault_problem(ctx)
    if gate is not None:
        return refused(None, gate.code, gate.text)
    rts = [resolve_target(ctx, tp.target) for tp in plan.targets]
    if fingerprint([(rt.folder, rt.target.platform, rt.target.slot) for rt in rts],
                   [tp.source for tp in plan.targets]) != plan.fingerprint:
        return _stale(ctx, plan)
    accounts = {rt.target.ref for rt in rts}
    if recovery_pending(ctx, accounts):
        w = warning("RECOVERY_PENDING")
        return refused(None, w.code, w.text)
    game = _game_problem(ctx, confirmed)
    if game is not None:
        return refused(None, game.code, game.text)
    _observe(ctx, accounts, "scan", opjournal.busy_targets(ctx))

    op_id = new_op_id(ctx.clock)
    tag = steamwrite.op_tag(op_id)
    try:
        opjournal.append(ctx, op_id, "preparing", {
            "kind": plan.kind, "app": ctx.app_version,
            "targets": [opjournal.target_json(rt.target) for rt in rts],
            "sources": [None if tp.source is None else opjournal.pair_source_to_json(tp.source)
                        for tp in plan.targets],
            "undo_of": plan.undo_of, "note": note, "confirmed": sorted(confirmed),
            "plan_digest": plan.fingerprint})
    except (OrganizerBusy, StoreReadOnly) as exc:
        return refused(None, "BUSY", f"The vault is busy. Try again. ({exc})")
    at(ctx.faults, "after_preparing")
    try:
        wts = _prepare(ctx, plan, rts, op_id, tag, progress)
    except _Abandon as why:
        append_quietly(ctx, op_id, "abandoned", {"reason": why.reason})
        if why.reason == "stale":
            return _stale(ctx, plan, op_id)
        return refused(op_id, why.code, why.text or warning_text(why.code))
    try:
        opjournal.append(ctx, op_id, "writing", {"targets": [_writing(wt) for wt in wts]})
    except (OrganizerBusy, StoreReadOnly) as exc:
        append_quietly(ctx, op_id, "abandoned", {"reason": "not_started"})
        return refused(op_id, "BUSY", f"The vault is busy. Try again. ({exc})")
    at(ctx.faults, "after_writing")
    opjournal.write_marker(ctx, op_id, [wt.rt.label for wt in wts])
    game = _game_problem(ctx, confirmed)
    if game is not None:
        append_quietly(ctx, op_id, "abandoned", {"reason": "game_started"})
        opjournal.remove_marker(ctx, op_id)
        return refused(op_id, game.code, game.text)

    progress(Progress("writing", 0, len(wts)))
    applied: list[OpTarget] = []
    try:
        for i, wt in enumerate(wts, 1):
            applied.append(wt)
            _apply(ctx, wt)
            progress(Progress("writing", i, len(wts)))
        progress(Progress("verifying", 0, 1))
        for wt in wts:
            validate(wt, tag)
    except (LiveChanged, msstore.IndexChanged) as exc:
        if len(applied) == 1:           # the first target changed: nothing was written at all
            append_quietly(ctx, op_id, "abandoned", {"reason": "stale"})
            opjournal.remove_marker(ctx, op_id)
            return _stale(ctx, plan, op_id)
        return rollback_and_report(ctx, op_id, plan, applied, exc, "apply", confirmed, note)
    except Exception as exc:
        log.warning("op %s: the write or its check failed: %s", op_id, exc)
        return rollback_and_report(ctx, op_id, plan, applied, exc, "apply", confirmed, note)
    try:
        opjournal.append(ctx, op_id, "validated", {"targets": [
            {"target": opjournal.target_json(wt.rt.target), "ok": True} for wt in wts]})
    except (OSError, OrganizerBusy, StoreReadOnly) as exc:
        log.warning("op %s: written and checked, not yet journaled: %s", op_id, exc)
        return OpOutcome(op_id, "done", outcome_text(plan, wts), (RECORDING_PENDING,),
                         undo_offer(plan, wts, op_id), "pending", None)
    at(ctx.faults, "after_validated")
    for wt in wts:
        finalize(wt)
    progress(Progress("recording", 0, 1))
    warnings = finish(ctx, op_id, plan.kind, plan.undo_of, wts, confirmed, note)
    return OpOutcome(op_id, "done", outcome_text(plan, wts), tuple(warnings),
                     undo_offer(plan, wts, op_id), "pending", None)


def finish(ctx: OpContext, op_id: str, kind: str, undo_of: str | None, wts: list[OpTarget],
           confirmed: Iterable[str], note: str) -> list[Warning]:
    """After a verified write: the oplog line, D1's op records, D1's look at the result,
    ``done``. A failure here leaves the op ``validated``; recovery finishes it."""
    try:
        record_oplog(ctx, op_id, kind, undo_of, wts, "done", confirmed, note, None)
        at(ctx.faults, "after_oplog")
        record_lineage(ctx, op_id, kind, undo_of, wts, "done", note)
        at(ctx.faults, "after_record")
    except Exception as exc:  # noqa: BLE001 - history only: the write is verified
        log.warning("op %s: the history was not recorded yet: %s", op_id, exc)
        return [RECORDING_PENDING]
    own = {wt.rt.target for wt in wts}
    _observe(ctx, {wt.rt.target.ref for wt in wts}, "verify",
             opjournal.busy_targets(ctx) - own)
    opjournal.append(ctx, op_id, "done", {"oplog": True, "lineage": True})
    opjournal.remove_marker(ctx, op_id)
    return []


# --- preparing (nothing live is written) ---------------------------------------------------


def _prepare(ctx: OpContext, plan: PlacePlan, rts: list[ResolvedTarget], op_id: str, tag: str,
             progress: Callable[[Progress], None]) -> list[OpTarget]:
    wts = []
    for tp, rt in zip(plan.targets, rts):
        try:
            src = load_source(ctx, tp.source)
        except SourceUnreadable as exc:
            raise _Abandon("not_started", "SOURCE_UNREADABLE", str(exc)) from exc
        before = read_account(ctx, resolve_account(ctx, rt.target.ref)).slots[rt.target.slot]
        wts.append(OpTarget(tp, rt, src, before))
    progress(Progress("preserving", 0, len(wts)))
    for wt in wts:
        wt.preserved = _preserve(ctx, wt, op_id, plan.kind)
    at(ctx.faults, "in_preserve")
    progress(Progress("snapshot", 0, 1))
    snapshots: dict[Path, str] = {}
    for wt in wts:
        if wt.folder not in snapshots:
            snapshots[wt.folder] = _snapshot(ctx, wt, op_id, plan.kind)
        wt.snapshot_id = snapshots[wt.folder]
    at(ctx.faults, "after_snapshot")
    now = int(ctx.clock())
    for wt in wts:
        _prepare_writer(wt, now, tag)
    return wts


def _preserve(ctx: OpContext, wt: OpTarget, op_id: str, kind: str) -> list[Location]:
    members = [LiveMember(wt.rt.target, m.position, m.vid) for m in wt.before.present]
    if not members:
        return []
    try:
        return ctx.locator.preserve(members, op_id=op_id, reason=kind)
    except PreserveMismatch as exc:
        raise _Abandon("stale", "STALE", str(exc)) from exc
    except PreserveFailed as exc:
        cause = exc.__cause__
        if str(exc) == "store-unavailable":
            raise _Abandon("preserve_failed", "VAULT_UPGRADE_NEEDED") from exc
        if isinstance(cause, store_errors.StoreUnavailable):
            raise _Abandon("preserve_failed", "VAULT_UNAVAILABLE") from exc
        if isinstance(cause, store_errors.InsufficientSpace):
            raise _Abandon("preserve_failed", "VAULT_SPACE") from exc
        if isinstance(cause, (store_errors.StoreBusy, OrganizerBusy)):
            raise _Abandon("preserve_failed", "BUSY",
                           "Another operation is still running. Try again in a moment.") from exc
        name = type(cause or exc).__name__
        raise _Abandon("preserve_failed", f"PRESERVE_FAILED:{name}",
                       f"The current save could not be copied to the vault ({exc}). "
                       "Nothing was changed.") from exc


def _snapshot(ctx: OpContext, wt: OpTarget, op_id: str, kind: str) -> str:
    try:
        entry = ctx.versions.add_from_live(
            wt.folder, platform=wt.rt.target.platform, account_key=wt.rt.target.account_key,
            kind="snapshot", label=f"auto: {kind}", scope=CaptureScope(), op_id=op_id,
            reason=kind)
    except (StoreError, OSError) as exc:
        code = "BUSY" if isinstance(exc, store_errors.StoreBusy) else "SNAPSHOT_FAILED"
        raise _Abandon("snapshot_failed", code,
                       "The safety snapshot failed. Nothing was changed.") from exc
    return entry.id


def _prepare_writer(wt: OpTarget, now: int, tag: str) -> None:
    """The writes stamped now (F5b), the writer's plan, its payload; the slot as the writer
    sees it must still be what was preserved."""
    wt.writes = pairmeta.compute(wt.src.pair, wt.rt, now)
    slot = wt.rt.target.slot
    data = wt.src.data
    if wt.rt.target.platform == "steam":
        triples = [(pm.write, data[pm.write.vid], pm.meta_disk) for pm in wt.writes]
        wt.steam = steamwrite.prepare(wt.folder, triples, slot, tag)
        wt.payload = steamwrite.payload_of(triples, slot)
        for m in wt.before.present:
            name = steamwrite.file_names(slot, m.position)[0]
            pre = wt.steam.pre.get(name)
            if pre is None or pre.sha256 != m.vid:
                raise _Abandon("stale", "STALE")
        names = {n for p in ("A", "B") for n in steamwrite.file_names(slot, p)}
        wt.outside = outside_files(wt.folder, names, set())
    else:
        records = [msstore.XboxRecordWrite(identifier(slot, pm.write.position),
                                           data[pm.write.vid], pm.meta_disk, pm.write.disk_time)
                   for pm in wt.writes]
        try:
            wt.xbox = msstore.prepare_pair(wt.folder, records)
        except msstore.XgsMismatch as exc:
            raise _Abandon("not_started", "XGS_MISMATCH",
                           "The game's second copy of this slot differs from its save. "
                           "Start and quit the game once, then try again.") from exc
        wt.payload = {r.identifier: (r.data, r.meta) for r in records}
        if _xbox_vids(wt.folder, slot) != {m.position: m.vid for m in wt.before.present}:
            raise _Abandon("stale", "STALE")
        dirs = {b.dir_name for b in wt.xbox.blobs}
        wt.outside = outside_files(wt.folder, {"containers.index"}, dirs)


def identifier(slot: int, position: str) -> str:
    return f"Slot{slot}{'Auto' if position == 'A' else 'Manual'}"


def _xbox_vids(folder: Path, slot: int) -> dict:
    view = msstore.scan(folder).slots[slot]
    return {mv.label: hashlib.sha256(Path(mv.data_path).read_bytes()).hexdigest()
            for mv in view.present_members}


def outside_files(folder: Path, names: set[str], dirs: set[str]) -> dict:
    """size and mtime_ns of every file the op must not touch (containment, D5 2.9.1)."""
    out = {}
    for p in folder.rglob("*"):
        rel = p.relative_to(folder)
        if not p.is_file() or rel.as_posix() in names or rel.parts[0] in dirs:
            continue
        if rel.parts[0].startswith(".tmp_"):
            continue
        st = p.stat()
        out[rel.as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def _writing(wt: OpTarget) -> dict:
    """The write-ahead record of one target."""
    before = {m.position: {"vid": m.vid, "pid": m.pid, "name": m.meta.name,
                           "play_time": m.meta.play_time, "meta_ts": m.meta.meta_ts}
              for m in wt.before.present}
    out = {
        "target": opjournal.target_json(wt.rt.target), "cloud_mode": wt.rt.cloud_mode,
        "snapshot_id": wt.snapshot_id,
        "preserved": [{"position": loc.member, "vid": loc.vid,
                       "location": opjournal.location_to_json(loc)} for loc in wt.preserved],
        "pre": {p: before.get(p) for p in ("A", "B")}, "pre_newer": wt.before.newer,
        "post": {pm.write.position: opjournal.member_write_to_json(pm.write) for pm in wt.writes},
        "load": wt.writes[0].write.position,
        "source": opjournal.pair_source_to_json(wt.tp.source),
        "name": wt.src.name,
    }
    if wt.steam is not None:
        out["steam"] = opjournal.steam_plan_to_json(wt.steam)
    else:
        out["xbox"] = opjournal.xbox_plan_to_json(wt.xbox)
    return out


# --- writing, checking, putting back ---------------------------------------------------------


def _apply(ctx: OpContext, wt: OpTarget) -> None:
    if wt.steam is not None:
        steamwrite.stage(wt.steam, wt.payload, ctx.faults)
        steamwrite.swap(wt.steam, ctx.faults)
    else:
        msstore.apply_pair(wt.xbox, wt.payload, ctx.faults)


def validate(wt: OpTarget, tag: str) -> None:
    """D5 2.9.1: bytes, metas, order, mirror, containment. Raises ValidationError."""
    if wt.steam is not None:
        _validate_steam(wt, tag)
    else:
        _validate_xbox(wt)
    dirs = {b.dir_name for b in wt.xbox.blobs} if wt.xbox is not None else set()
    names = ({n for p in ("A", "B") for n in steamwrite.file_names(wt.rt.target.slot, p)}
             if wt.steam is not None else {"containers.index"})
    if outside_files(wt.folder, names, dirs) != wt.outside:
        raise ValidationError("a file of the folder outside the slot changed")


def _validate_steam(wt: OpTarget, tag: str) -> None:
    folder, slot = wt.folder, wt.rt.target.slot
    for pm in wt.writes:
        mw = pm.write
        data_name, meta_name = steamwrite.file_names(slot, mw.position)
        data, raw = (folder / data_name).read_bytes(), (folder / meta_name).read_bytes()
        if _sha(data) != mw.vid or _sha(raw) != mw.meta_disk_sha256:
            raise ValidationError(f"{data_name} or its meta is not what was written")
        if not meta.is_valid_for(raw, mw.ordinal):
            raise ValidationError(f"{meta_name} does not decrypt for its slot")
        info = meta.parse(meta.decrypt(raw, mw.ordinal), mw.ordinal)
        if data[:4] == formats.SAVE_STREAMING_HEADER and \
                lz4_block.stats(data).total_decompressed != info.size_decompressed:
            raise ValidationError(f"{data_name} does not match its meta's size")
        if mw.pid and pid_kind(mw.pid) == "id" and info.slot_identifier and \
                f"{info.slot_identifier:016X}" != mw.pid:
            raise ValidationError(f"{meta_name} names another playthrough")
    view = savedir.scan_slot(folder, slot)
    loading, other = wt.writes
    newest = view.newest
    if newest is None or newest.label != loading.write.position:
        raise ValidationError("the chosen save would not load")
    l_data = folder / steamwrite.file_names(slot, loading.write.position)[0]
    o_data = folder / steamwrite.file_names(slot, other.write.position)[0]
    if not l_data.stat().st_mtime > o_data.stat().st_mtime:
        raise ValidationError("the chosen save is not the newest file")
    if list(folder.glob(f".tmp_{tag}_*")):
        raise ValidationError("temporary files were left behind")


def _validate_xbox(wt: OpTarget) -> None:
    plan, slot = wt.xbox, wt.rt.target.slot
    if msstore.pair_index_state(plan) != "post":
        raise ValidationError("containers.index is not what was written")
    by_id = {b.identifier: b for b in plan.blobs}
    for pm in wt.writes:
        b = by_id[identifier(slot, pm.write.position)]
        d = wt.folder / b.dir_name
        if _sha((d / b.data_name).read_bytes()) != pm.write.vid or \
                _sha((d / b.meta_name).read_bytes()) != pm.write.meta_disk_sha256:
            raise ValidationError(f"{b.identifier} is not what was written")
    newest = msstore.scan(wt.folder).slots[slot].newest
    loading, other = wt.writes
    if newest is None or newest.label != loading.write.position:
        raise ValidationError("the chosen save would not load")
    times = {i: msstore.record_info(raw).filetime for i, raw in plan.new_records_raw.items()}
    if not times[identifier(slot, loading.write.position)] > times[identifier(slot, other.write.position)]:
        raise ValidationError("the chosen save is not the newest record")
    if any(state != "post" for state in msstore.pair_xgs_states(plan).values()):
        raise ValidationError("the xgs copy does not match")


def finalize(wt: OpTarget) -> None:
    if wt.xbox is not None:
        msstore.finalize_pair(wt.xbox)
    elif wt.steam is not None:
        steamwrite.sweep(wt.folder, wt.steam.op_tag)


def rollback_and_report(ctx: OpContext, op_id: str, plan: PlacePlan, applied: list[OpTarget],
                        exc: BaseException, stage: str, confirmed: Iterable[str],
                        note: str) -> OpOutcome:
    """D5 2.9.2: put every target the op touched back exactly; journal, oplog and D1 say
    rolled back, or failed when a slot could not be put back (the op stays open)."""
    labels = ", ".join(wt.rt.label for wt in applied)
    problems = []
    for wt in reversed(applied):
        try:
            rollback_target(ctx, wt)
        except Exception as rollback_exc:  # noqa: BLE001 - reported, the op stays open
            log.error("op %s: %s could not be put back: %s", op_id, wt.rt.label, rollback_exc)
            problems.append(f"{wt.rt.label}: {rollback_exc}")
    if problems:
        append_quietly(ctx, op_id, "failed", {"stage": stage, "error": str(exc),
                                               "rollback_error": "; ".join(problems)})
        _oplog_quietly(ctx, op_id, plan, applied, "failed", confirmed, note, str(exc))
        return OpOutcome(op_id, "failed",
                         f"The move failed and {labels} could not be put back automatically. "
                         "Use Recover (recommended) or restore the folder from the safety "
                         "snapshot.", (), None, "n/a", str(exc), "ROLLBACK_FAILED")
    append_quietly(ctx, op_id, "rolled_back", {"stage": stage, "error": str(exc),
                                                "restored": True})
    _oplog_quietly(ctx, op_id, plan, applied, "rolled_back", confirmed, note, str(exc))
    try:
        record_lineage(ctx, op_id, plan.kind, plan.undo_of, applied, "rolled_back", note)
    except Exception as rec_exc:  # noqa: BLE001 - history only
        log.warning("op %s: the rollback was not recorded: %s", op_id, rec_exc)
    opjournal.remove_marker(ctx, op_id)
    return OpOutcome(op_id, "rolled_back",
                     f"Nothing changed in {labels}. The move failed: {exc}", (), None, "n/a",
                     str(exc), "ROLLED_BACK")


def rollback_target(ctx: OpContext, wt: OpTarget) -> None:
    if wt.steam is not None:
        pre = {name: ctx.versions.read_file(wt.snapshot_id, name)
               for name, state in wt.steam.pre.items() if state is not None}
        steamwrite.rollback(wt.steam, pre)
    else:
        msstore.rollback_pair(wt.xbox)


# --- recording -------------------------------------------------------------------------------


def record_oplog(ctx: OpContext, op_id: str, kind: str, undo_of: str | None,
                 wts: list[OpTarget], status: str, confirmed: Iterable[str], note: str,
                 error: str | None) -> None:
    """One v2 line in the vault's oplog.jsonl (D5 2.13.1): today's six keys first. Written
    once per op: a retry finds its line and writes none."""
    if any(r.get("op_id") == op_id for r in list(ctx.vault.read_oplog())[-50:]):
        return
    utc = datetime.fromtimestamp(ctx.clock(), timezone.utc)
    first = wts[0]
    changed = []
    for wt in wts:
        if wt.steam is not None:
            changed += [f.name for f in wt.steam.files]
        else:
            changed += [b.identifier for b in wt.xbox.blobs]
    evicted = [m.meta.name or "<unnamed>" for m in first.before.present if m.position ==
               first.before.newer]
    detail = f"{kind} '{first.src.name}' into {first.rt.label}"
    if evicted:
        detail += f"; '{evicted[0]}' kept in the vault"
    ctx.vault.append_oplog({
        "ts": utc.astimezone().replace(tzinfo=None).isoformat(timespec="seconds"),
        "op": kind, "entry_id": first.preserved[0].ref.key if first.preserved else None,
        "snapshot_id": first.snapshot_id, "detail": detail, "changed": changed,
        "v": 2, "op_id": op_id, "utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app": ctx.app_version, "kind": kind, "status": status, "undo_of": undo_of,
        "targets": [_oplog_target(wt) for wt in wts], "confirmed": sorted(confirmed),
        "note": note, "error": error})


def _oplog_target(wt: OpTarget) -> dict:
    t = wt.rt.target
    preserved = {loc.vid: loc for loc in wt.preserved}
    return {
        "platform": t.platform, "account_key": t.account_key, "instance": t.instance,
        "slot": t.slot, "cloud_mode": wt.rt.cloud_mode, "snapshot_id": wt.snapshot_id,
        "written": [opjournal.member_write_to_json(pm.write) for pm in wt.writes],
        "evicted": [{"position": m.position, "vid": m.vid, "pid": m.pid,
                     "play_time": m.meta.play_time, "meta_ts": m.meta.meta_ts,
                     "preserved_at": (preserved[m.vid].to_json() if m.vid in preserved else None),
                     "already_durable": False} for m in wt.before.present],
    }


def record_lineage(ctx: OpContext, op_id: str, kind: str, undo_of: str | None,
                   wts: list[OpTarget], status: str, note: str) -> None:
    """D1's op record per target (idempotent on op id and target)."""
    recs = []
    for wt in wts:
        placed = {pm.write.position: pm.write.vid for pm in wt.writes}
        after = read_account(ctx, resolve_account(ctx, wt.rt.target.ref)).slots[wt.rt.target.slot]
        members = {m.vid: m for m in after.members_obs if m.vid in placed.values()}
        evicted = wt.before.pair if wt.before.present else None
        loading = wt.writes[0].write
        recs.append(OpLineage(
            op_id, kind, int(ctx.clock()), wt.rt.target, (placed.get("A"), placed.get("B")),
            evicted, source=dict(loading.origin),
            preserved={loc.vid: loc.to_json() for loc in wt.preserved}, status=status,
            undo_of=undo_of, note=note[:200], app=ctx.app_version, members=members,
            snapshot=wt.snapshot_id))
    ctx.lineage.record_ops(recs)


def _observe(ctx: OpContext, accounts: Iterable[LiveRef], src: str,
             skip: frozenset[LiveTarget]) -> None:
    """D1's journal looks at the accounts (before the op: what it is about to replace;
    after it: what it wrote). Recording is a by-product: a failure is logged only."""
    for ref in accounts:
        try:
            now = read_account(ctx, resolve_account(ctx, ref))
            Journal(ctx.store, now.scan.versions).observe(now.scan, src=src, skip=skip)
        except Exception as exc:  # noqa: BLE001
            log.warning("the live slots were not journaled (%s): %s", src, exc)


def _oplog_quietly(ctx, op_id, plan, wts, status, confirmed, note, error) -> None:
    try:
        record_oplog(ctx, op_id, plan.kind, plan.undo_of, wts, status, confirmed, note, error)
    except OSError as exc:
        log.warning("op %s: the oplog line was not written: %s", op_id, exc)


def append_quietly(ctx: OpContext, op_id: str, event: str, payload: Mapping) -> None:
    try:
        opjournal.append(ctx, op_id, event, payload)
    except (OSError, OrganizerBusy, StoreReadOnly) as exc:
        log.warning("op %s: the %s event was not journaled (recovery will): %s",
                    op_id, event, exc)


# --- outcomes ----------------------------------------------------------------------------


def _game_problem(ctx: OpContext, confirmed: frozenset[str]) -> Warning | None:
    w = game_warning(ctx)
    if w is None or (w.code == "GAME_STATE_UNKNOWN" and w.code in confirmed):
        return None
    return w


def refused(op_id: str | None, code: str, text: str) -> OpOutcome:
    return OpOutcome(op_id, "refused", text, (), None, "n/a", text, code)


def _stale(ctx: OpContext, plan: PlacePlan, op_id: str | None = None) -> OpOutcome:
    from . import planner

    label = plan.targets[0].label if plan.targets else "The slot"
    text = f"{label} changed since you opened this. Please check again."
    replan = None
    tp = plan.targets[0] if plan.targets else None
    try:
        if plan.kind == "promote" and tp is not None and tp.source is not None:
            replan = planner.plan_promote(ctx, tp.target, tp.source.load or "A")
        elif tp is not None and tp.source is not None and len(plan.targets) == 1:
            replan = planner.plan_place(ctx, tp.source, tp.target)
    except LiveOpError as exc:
        log.info("no fresh plan: %s", exc)
    return OpOutcome(op_id, "stale", text, (), None, "n/a", None, "STALE", replan)


def warning_text(code: str) -> str:
    from .planner import TEXTS

    return TEXTS[code][1] if code in TEXTS else code


def outcome_text(plan: PlacePlan, wts: list[OpTarget]) -> str:
    wt = wts[0]
    label, name = wt.rt.label, wt.src.name or "<unnamed>"
    evicted = wt.before.head.meta.name if wt.before.head is not None else ""
    if plan.kind == "place_into_empty":
        return f"Put '{name}' into {label}."
    if plan.kind == "promote":
        which = "auto-save" if wt.writes[0].write.position == "A" else "restore point"
        return f"The {which} of {label} loads now."
    if plan.kind == "restore_older":
        return f"{label} holds this version of '{name}' now. The one it replaced is in the vault."
    text = f"Moved '{name}' into {label}."
    return text + (f" '{evicted}' is in the vault." if evicted else "")


def undo_offer(plan: PlacePlan, wts: list[OpTarget], op_id: str) -> UndoOffer | None:
    wt = wts[0]
    if not wt.before.present:
        return None                       # undoing a fill would empty the slot (F8)
    name = wt.before.head.meta.name if wt.before.head is not None else ""
    return UndoOffer(op_id, f"Undo: put '{name or '<unnamed>'}' back into {wt.rt.label}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
