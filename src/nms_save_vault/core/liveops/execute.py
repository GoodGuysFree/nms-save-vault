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
from .slots import AccountNow, SlotNow, read_account
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
    records: list = field(default_factory=list)      # Xbox: this target's XboxRecordWrite
    stamped: bool = True                              # False: a panic restore (metas verbatim)
    outside: dict = field(default_factory=dict)
    excluded: tuple = (frozenset(), frozenset())      # (file names, record dirs) the op writes

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
    run = _keep if plan.kind == "keep" else _run
    try:
        with opjournal.liveop_lock(ctx, timeout=LIVEOP_TIMEOUT):
            return run(ctx, plan, confirmed, progress or (lambda p: None), note)
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
    blocked = recovery_pending(ctx, accounts)
    if blocked is not None:
        return refused(None, blocked.code, blocked.text)
    game = _game_problem(ctx, confirmed)
    if game is not None:
        return refused(None, game.code, game.text)
    scanned = _observe(ctx, accounts, "scan", opjournal.busy_targets(ctx))

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
        wts = _prepare(ctx, plan, rts, op_id, tag, progress, scanned)
    except _Abandon as why:
        append_quietly(ctx, op_id, "abandoned", {"reason": why.reason})
        if why.reason == "stale":
            return _stale(ctx, plan, op_id)
        return refused(op_id, why.code, why.text or warning_text(why.code))
    except Exception as exc:  # noqa: BLE001 - nothing live was written: never left 'preparing'
        log.warning("op %s: could not be prepared: %s", op_id, exc)
        append_quietly(ctx, op_id, "abandoned", {"reason": "prepare_failed"})
        return refused(op_id, "PREPARE_FAILED",
                       f"The move could not be prepared ({exc}). Nothing was changed.")
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
    applying = False
    written_plans: set = set()
    try:
        for i, wt in enumerate(wts, 1):
            applied.append(wt)
            applying = True
            _apply(ctx, wt, written_plans)
            applying = False
            progress(Progress("writing", i, len(wts)))
        progress(Progress("verifying", 0, 1))
        for wt in wts:
            validate(wt, tag)
    except (LiveChanged, msstore.IndexChanged) as exc:
        # The target that changed was refused before anything was written to it: the game
        # wrote it, so it is not put back. Only the targets this op wrote are.
        written = applied[:-1] if applying else applied
        if not written:
            append_quietly(ctx, op_id, "abandoned", {"reason": "stale"})
            opjournal.remove_marker(ctx, op_id)
            return _stale(ctx, plan, op_id)
        return rollback_and_report(ctx, op_id, plan, written, exc, "apply", confirmed, note)
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
    try:
        for wt in wts:
            finalize(wt)
    except Exception as exc:  # noqa: BLE001 - the write is verified; recovery finishes this
        log.warning("op %s: written and checked, not yet finalized: %s", op_id, exc)
        return OpOutcome(op_id, "done", outcome_text(plan, wts), (RECORDING_PENDING,),
                         undo_offer(plan, wts, op_id), "pending", None)
    progress(Progress("recording", 0, 1))
    warnings = finish(ctx, op_id, plan.kind, plan.undo_of, wts, confirmed, note)
    return OpOutcome(op_id, "done", outcome_text(plan, wts), tuple(warnings),
                     undo_offer(plan, wts, op_id), "pending", None)


def _keep(ctx: OpContext, plan: PlacePlan, confirmed: frozenset[str],
          progress: Callable[[Progress], None], note: str) -> OpOutcome:
    """Keep a copy (D5 2.4.1): one live slot into the vault as an ``extract`` entry; nothing
    live is written, so the game may run and no op record goes to D1 (its scanner sees the
    entry)."""
    gate = vault_problem(ctx)
    if gate is not None:
        return refused(None, gate.code, gate.text)
    (tp,) = plan.targets
    rt = resolve_target(ctx, tp.target)
    if fingerprint([(rt.folder, rt.target.platform, rt.target.slot)], []) != plan.fingerprint:
        return OpOutcome(None, "stale", f"{rt.label} changed since you opened this. Please "
                         "check again.", (), None, "n/a", None, "STALE",
                         _replan_keep(ctx, tp.target))
    op_id = new_op_id(ctx.clock)
    try:
        opjournal.append(ctx, op_id, "preparing", {
            "kind": "keep", "app": ctx.app_version,
            "targets": [opjournal.target_json(rt.target)], "note": note,
            "confirmed": sorted(confirmed), "plan_digest": plan.fingerprint})
    except (OrganizerBusy, StoreReadOnly) as exc:
        return refused(None, "BUSY", f"The vault is busy. Try again. ({exc})")
    progress(Progress("preserving", 0, 1))
    try:
        entry = ctx.versions.add_from_live(
            rt.folder, platform=rt.target.platform, account_key=rt.target.account_key,
            kind="extract", label=f"kept: {rt.label}",
            scope=CaptureScope(slots=(rt.target.slot,)), op_id=op_id, reason="keep")
    except (StoreError, OSError) as exc:
        append_quietly(ctx, op_id, "abandoned", {"reason": "preserve_failed"})
        return refused(op_id, "PRESERVE_FAILED", f"The save could not be kept in the vault "
                       f"({exc}). Nothing was changed.")
    name = next((m.name for m in entry.members if m.slot == rt.target.slot and m.name), "")
    name = name or "<unnamed>"
    utc = datetime.fromtimestamp(ctx.clock(), timezone.utc)
    try:
        ctx.vault.append_oplog({
            "ts": utc.astimezone().replace(tzinfo=None).isoformat(timespec="seconds"),
            "op": "keep", "entry_id": entry.id, "snapshot_id": None,
            "detail": f"kept '{name}' from {rt.label}", "changed": [],
            "v": 2, "op_id": op_id, "utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "app": ctx.app_version, "kind": "keep", "status": "done", "undo_of": None,
            "targets": [{"platform": rt.target.platform, "account_key": rt.target.account_key,
                         "instance": rt.target.instance, "slot": rt.target.slot,
                         "entry_id": entry.id}],
            "confirmed": sorted(confirmed), "note": note, "error": None})
    except OSError as exc:
        log.warning("op %s: the oplog line was not written: %s", op_id, exc)
    opjournal.append(ctx, op_id, "done", {"entry_id": entry.id})
    return OpOutcome(op_id, "done", f"'{name}' from {rt.label} is in the vault.", (), None,
                     "n/a", None)


def _replan_keep(ctx: OpContext, target: LiveTarget) -> PlacePlan | None:
    from . import planner

    try:
        return planner.plan_keep(ctx, target)
    except LiveOpError:
        return None


def finish(ctx: OpContext, op_id: str, kind: str, undo_of: str | None, wts: list[OpTarget],
           confirmed: Iterable[str], note: str) -> list[Warning]:
    """After a verified write: the oplog line, D1's op records, D1's look at the result,
    ``done``. A failure here leaves the op ``validated``; recovery finishes it."""
    try:
        record_oplog(ctx, op_id, kind, undo_of, wts, "done", confirmed, note, None)
        at(ctx.faults, "after_oplog")
        after = {ref: read_account(ctx, resolve_account(ctx, ref))
                 for ref in {wt.rt.target.ref for wt in wts}}
        record_lineage(ctx, op_id, kind, undo_of, wts, "done", note, after)
        at(ctx.faults, "after_record")
    except Exception as exc:  # noqa: BLE001 - history only: the write is verified
        log.warning("op %s: the history was not recorded yet: %s", op_id, exc)
        return [RECORDING_PENDING]
    own = {wt.rt.target for wt in wts}
    _observe(ctx, {wt.rt.target.ref for wt in wts}, "verify",
             opjournal.busy_targets(ctx) - own, after)
    try:
        opjournal.append(ctx, op_id, "done", {"oplog": True, "lineage": True})
        opjournal.remove_marker(ctx, op_id)
    except (OSError, OrganizerBusy, StoreReadOnly) as exc:
        log.warning("op %s: recorded, not yet closed: %s", op_id, exc)
        return [RECORDING_PENDING]
    return []


# --- preparing (nothing live is written) ---------------------------------------------------


def _prepare(ctx: OpContext, plan: PlacePlan, rts: list[ResolvedTarget], op_id: str, tag: str,
             progress: Callable[[Progress], None],
             scanned: Mapping[LiveRef, AccountNow] | None = None) -> list[OpTarget]:
    wts = []
    for tp, rt in zip(plan.targets, rts):
        try:
            src = load_source(ctx, tp.source)
        except SourceUnreadable as exc:
            raise _Abandon("not_started", "SOURCE_UNREADABLE", str(exc)) from exc
        now = (scanned or {}).get(rt.target.ref) or             read_account(ctx, resolve_account(ctx, rt.target.ref))
        before = now.slots[rt.target.slot]       # a change since is caught by the pre-image checks
        wts.append(OpTarget(tp, rt, src, before, stamped=plan.kind != "panic_restore"))
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
        _prepare_writer(ctx, wt, now, tag)
    _prepare_xbox(wts)
    set_containment(wts)
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


def _prepare_writer(ctx: OpContext, wt: OpTarget, now: int, tag: str) -> None:
    """The writes stamped now (F5b), the writer's plan, its payload; the slot as the writer
    sees it must still be what was preserved."""
    wt.writes = pairmeta.compute(wt.src.pair, wt.rt, now, stamped=wt.stamped)
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
        _check_against_snapshot(ctx, wt)
    else:
        wt.records = [msstore.XboxRecordWrite(identifier(slot, pm.write.position),
                                              data[pm.write.vid], pm.meta_disk,
                                              pm.write.disk_time) for pm in wt.writes]


def _prepare_xbox(wts: list[OpTarget]) -> None:
    """One combined plan per Xbox account folder: a swap of two slots of one account writes
    both through one index (a second plan would see the first one's write as a change)."""
    groups: dict[Path, list[OpTarget]] = {}
    for wt in wts:
        if wt.steam is None:
            groups.setdefault(wt.folder, []).append(wt)
    for folder, group in groups.items():
        records = [r for wt in group for r in wt.records]
        try:
            plan = msstore.prepare_pair(folder, records)
        except msstore.XgsMismatch as exc:
            raise _Abandon("not_started", "XGS_MISMATCH",
                           "The game's second copy of this slot differs from its save. "
                           "Start and quit the game once, then try again.") from exc
        payload = {r.identifier: (r.data, r.meta) for r in records}
        for wt in group:
            wt.xbox, wt.payload = plan, payload
            if _xbox_vids(folder, wt.rt.target.slot) != {m.position: m.vid
                                                         for m in wt.before.present}:
                raise _Abandon("stale", "STALE")


def _check_against_snapshot(ctx: OpContext, wt: OpTarget) -> None:
    """Every file the op touches is, right now, exactly what the safety snapshot holds (a
    member that appeared after the snapshot would be lost by a rollback): else abandon."""
    snap = ctx.versions.entry(wt.snapshot_id)
    for name, pre in wt.steam.pre.items():
        if pre is None:
            if snap.has_file(name):
                raise _Abandon("stale", "STALE")
        elif not snap.has_file(name) or snap.file(name).sha256 != pre.sha256:
            raise _Abandon("stale", "STALE")


def set_containment(wts: list[OpTarget]) -> None:
    """What validation compares afterwards: every file of each target folder outside the
    slots the op writes there (a swap writes two slots of one folder)."""
    for wt in wts:
        names, dirs = set(), set()
        for other in wts:
            if other.folder != wt.folder:
                continue
            if other.steam is not None:
                names |= {n for p in ("A", "B")
                          for n in steamwrite.file_names(other.rt.target.slot, p)}
            else:
                names.add("containers.index")
                dirs |= {b.dir_name for b in other.xbox.blobs}
        wt.excluded = (frozenset(names), frozenset(dirs))
        wt.outside = outside_files(wt.folder, names, dirs)


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


def _apply(ctx: OpContext, wt: OpTarget, written_plans: set) -> None:
    if wt.steam is not None:
        steamwrite.stage(wt.steam, wt.payload, ctx.faults)
        steamwrite.swap(wt.steam, ctx.faults)
    elif plan_key(wt) not in written_plans:       # one plan can cover two slots
        msstore.apply_pair(wt.xbox, wt.payload, ctx.faults)
        written_plans.add(plan_key(wt))


def plan_key(wt: OpTarget) -> tuple:
    """Identifies an Xbox plan shared by the targets of one account."""
    return (wt.folder, wt.xbox.pre_index_sha256, wt.xbox.new_header_raw)


def validate(wt: OpTarget, tag: str) -> None:
    """D5 2.9.1: bytes, metas, order, mirror, containment. Raises ValidationError."""
    if wt.steam is not None:
        _validate_steam(wt, tag)
    else:
        _validate_xbox(wt)
    names, dirs = wt.excluded
    if outside_files(wt.folder, set(names), set(dirs)) != wt.outside:
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
                                               "rollback_error": "; ".join(problems),
                                               "settled": settled_files(applied)})
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


def settled_files(wts: list[OpTarget]) -> dict:
    """Steam: what each file of the op's slots holds at the moment it failed, so recovery can
    tell the op's own damaged output (still as it was left) from a save the game wrote since."""
    out = {}
    for wt in wts:
        if wt.steam is not None:
            for name in wt.steam.pre:
                state = steamwrite.file_state(wt.folder / name)
                out[f"{wt.rt.target.slot}/{name}"] = state.sha256 if state else None
    return out


def rollback_target(ctx: OpContext, wt: OpTarget) -> None:
    if wt.steam is not None:
        pre = {}
        for name, state in wt.steam.pre.items():       # only what was actually changed
            if state is None:
                continue
            now = steamwrite.file_state(wt.folder / name)
            if now is None or (now.sha256, now.size) != (state.sha256, state.size):
                pre[name] = ctx.versions.read_file(wt.snapshot_id, name)
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
            changed += [identifier(wt.rt.target.slot, pm.write.position) for pm in wt.writes]
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
                   wts: list[OpTarget], status: str, note: str,
                   nows: Mapping[LiveRef, AccountNow] | None = None) -> None:
    """D1's op record per target (idempotent on op id and target). ``nows``: the accounts
    as they are, when the caller has just read them."""
    recs = []
    for wt in wts:
        placed = {pm.write.position: pm.write.vid for pm in wt.writes}
        now = (nows or {}).get(wt.rt.target.ref) or             read_account(ctx, resolve_account(ctx, wt.rt.target.ref))
        after = now.slots[wt.rt.target.slot]
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
             skip: frozenset[LiveTarget],
             nows: Mapping[LiveRef, AccountNow] | None = None) -> dict[LiveRef, AccountNow]:
    """D1's journal looks at the accounts (before the op: what it is about to replace;
    after it: what it wrote). Recording is a by-product: a failure is logged only. Returns
    the accounts as read, for the caller to reuse."""
    out: dict[LiveRef, AccountNow] = {}
    for ref in accounts:
        try:
            now = (nows or {}).get(ref) or read_account(ctx, resolve_account(ctx, ref))
            out[ref] = now
            Journal(ctx.store, now.scan.versions).observe(now.scan, src=src, skip=skip)
        except Exception as exc:  # noqa: BLE001
            log.warning("the live slots were not journaled (%s): %s", src, exc)
    return out


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
        if plan.kind == "undo" and plan.undo_of:      # keeps undo_of and the UNDO_STALE block
            from .undo import plan_undo

            replan = plan_undo(ctx, plan.undo_of)
        elif plan.kind == "promote" and tp is not None and tp.source is not None:
            replan = planner.plan_promote(ctx, tp.target, tp.source.load or "A")
        elif plan.kind == "swap" and len(plan.targets) == 2:
            replan = planner.plan_swap(ctx, plan.targets[0].target, plan.targets[1].target)
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
    if plan.kind == "panic_restore":
        return f"The folder is restored from the snapshot ({len(wts)} slots)."
    if plan.kind == "swap":
        return f"Swapped {label} and {wts[1].rt.label}."
    if plan.kind == "undo":
        if len(wts) == 2:
            return f"Swapped {label} and {wts[1].rt.label} back."
        return f"Put '{name}' back into {label}."
    text = f"Moved '{name}' into {label}."
    return text + (f" '{evicted}' is in the vault." if evicted else "")


def undo_offer(plan: PlacePlan, wts: list[OpTarget], op_id: str) -> UndoOffer | None:
    wt = wts[0]
    if plan.kind == "panic_restore":
        return UndoOffer(op_id, "Undo: restore the folder from the safety snapshot")
    if plan.kind == "swap" or len(wts) == 2:
        return UndoOffer(op_id, f"Undo: swap {wt.rt.label} and {wts[1].rt.label} back")
    if not wt.before.present:
        return None                       # undoing a fill would empty the slot (F8)
    name = wt.before.head.meta.name if wt.before.head is not None else ""
    return UndoOffer(op_id, f"Undo: put '{name or '<unnamed>'}' back into {wt.rt.label}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
