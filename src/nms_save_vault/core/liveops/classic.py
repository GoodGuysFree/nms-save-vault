"""The classic operations as adapters over the live-operation engine (D5 4.1).

``repopulate_slot``, ``promote_member`` and the restore of an extract keep their signatures
and their :class:`OpResult` for today's callers, but every live write is one ``execute``: the
same planner (both positions are always written, nothing is ever emptied), preserve before
replace, journal, validation and exact rollback. A refusal or a failure raises the
:class:`OperationError` the callers already handle.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
import shutil
import struct
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Iterable
from pathlib import Path

from .. import gameversion, msstore
from .. import operations as ops
from .. import platform as hostmod
from .. import safety, savedir
from ..catalog import Vault
from ..organizer import Organizer
from ..organizer.model import LiveRef, LiveTarget
from ..organizer.recorder import LiveOpBusy
from ..state import Source
from ..store import listing
from .context import OpContext, context_for
from .errors import LiveOpError
from . import opjournal as jr
from .execute import LIVEOP_TIMEOUT, execute
from .model import OpOutcome, PairSource, PlacePlan
from .opjournal import machine_id
from .planner import plan_place, plan_promote

APP_VERSION = "nms-save-vault classic"


def open_context(vault: Vault, live_dir: Path, *, host: hostmod.Host | None = None,
                 game_probe=None) -> tuple[OpContext, LiveRef]:
    """A context over ``vault`` whose live sources are the app's configured ones plus
    ``live_dir`` itself (the caller named that folder). Raises OperationError when the vault
    has not been upgraded: live operations need the version store (D3)."""
    from ..organizer import recorder

    host = host or hostmod.Host.current()
    live_dir = Path(live_dir)
    refuse_disabled(live_dir)
    sources = list(recorder._configured_sources())
    here = _same_folder(live_dir)
    refs_of = {_key(s.path): s for s in sources}
    if here not in refs_of:
        sources.append(_ad_hoc(live_dir))
    try:
        organizer = Organizer(vault.root, sources, host=host, game_running=safety.is_game_running)
        from ... import __version__

        ctx = context_for(organizer, host=host, machine_id=machine_id(host),
                          app_version=f"nms-save-vault {__version__}",
                          game_probe=game_probe or safety.is_game_running)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ops.OperationError(f"The vault cannot be opened for a live write ({exc}).") from exc
    from ..organizer.scanner import live_refs

    for source, ref in zip(ctx.sources, live_refs(ctx.sources)):
        if _key(source.path) == here:
            return ctx, ref
    raise ops.OperationError("That folder is not a live save folder of this app.")


def repopulate(vault: Vault, source_dir: Path, source_slot: int, live_dir: Path,
               dest_slot: int, allow_game_running: bool = False) -> ops.OpResult:
    """Put the pair in ``source_dir`` slot ``source_slot`` into live slot ``dest_slot``."""
    source_dir, live_dir = require_folder(source_dir), Path(live_dir)
    ops._check_slot(source_slot)
    ops._check_slot(dest_slot)
    ops._guard_same_platform(source_dir, live_dir)
    ctx, ref = open_context(vault, live_dir)
    source = folder_pair(source_dir, source_slot)
    plan = plan_place(ctx, source, ref.target(dest_slot))
    return _run(ctx, plan, "repopulate_slot", allow_game_running,
                f"slot {source_slot} -> {dest_slot}")


def repopulate_entry(vault: Vault, entry_id: str, source_slot: int, live_dir: Path,
                     dest_slot: int, allow_game_running: bool = False) -> ops.OpResult:
    """Put slot ``source_slot`` of a vault entry into a live slot. An entry held by the
    version store has no folder: its slot is read through the store (a verified temporary copy,
    removed afterwards), never from a path built out of nothing."""
    from ..store import classic as store_classic

    entry = store_classic.find_entry(vault, entry_id)
    if entry is None:
        raise ops.OperationError(f"The backup '{entry_id}' is not in the vault.")
    with store_classic.entry_folder(vault, entry, slots=[source_slot]) as folder:
        return repopulate(vault, folder, source_slot, live_dir, dest_slot, allow_game_running)


def extract_from_entry(vault: Vault, entry_id: str, slot: int, *, warnings: list[str],
                       live_dirs=()):
    """Extract one slot of a vault entry into the vault (through the store when the entry has
    no folder of its own)."""
    from ..store import classic as store_classic

    entry = store_classic.find_entry(vault, entry_id)
    if entry is None:
        raise ops.OperationError(f"The backup '{entry_id}' is not in the vault.")
    with store_classic.entry_folder(vault, entry, slots=[slot]) as folder:
        return ops.extract_slot(vault, folder, slot, warnings=warnings, live_dirs=live_dirs)


def promote(vault: Vault, live_dir: Path, slot: int, member: int,
            allow_game_running: bool = False) -> ops.OpResult:
    """Make member ``member`` (0 = A, 1 = B) of a live slot the one the game loads."""
    live_dir = Path(live_dir)
    ops._check_slot(slot)
    ctx, ref = open_context(vault, live_dir)
    position = "AB"[member]
    plan = plan_promote(ctx, ref.target(slot), position)
    for why in plan.blocked:
        if why.code == "ALREADY_LOADS":
            return ops.OpResult(True, "promote_member",
                                f"slot {slot}{position} is already the newest; nothing was changed")
    return _run(ctx, plan, "promote_member", allow_game_running, f"slot {slot}{position}")


def restore_full(vault: Vault, entry, live_dir: Path, mirror: bool = True,
                 allow_game_running: bool = False, *, undo_of: str | None = None,
                 restore_accountdata: bool = False,
                 expected_fingerprint: str | None = None) -> ops.OpResult:
    """Put a whole backup or snapshot back: the panic restore (D5 4.3). ``mirror`` is kept
    for today's callers: nothing is ever deleted, so slots the backup does not hold are
    always left alone."""
    from ..store import classic as store_classic
    from . import panic

    live_dir = Path(live_dir)
    if entry.kind in store_classic.SLOT_KINDS:
        raise ops.OperationError(
            f"'{entry.id}' is a single-slot extract, not a full backup. Restoring it as one "
            "would replace every other save in the live folder. Use restore_entry(), which "
            "puts the slot back where it came from.")
    if entry.path:
        ops._guard_same_platform(Path(entry.path), live_dir)
    refuse_disabled(live_dir)
    ctx, ref = open_context(vault, live_dir)
    found = listing.get_vault_entry(ctx.vault, ctx.versions, entry.id, live_roots=[])
    if found is not None and found.platform != ref.platform:
        raise ops.FeatureNotYetAvailableError(ops.CROSS_PLATFORM_MSG)
    notes = repair_damaged_index(ctx, ref, vault, entry, live_dir, allow_game_running)
    if notes:
        ctx, ref = open_context(vault, live_dir)      # the folder reads again
    plan = panic.plan_panic_restore(ctx, ref, entry.id, restore_accountdata=restore_accountdata)
    for why in plan.blocked:
        if why.code == "FOREIGN_ACCOUNT":
            raise ops.ForeignAccountError(why.text)
    if expected_fingerprint is not None and plan.fingerprint != expected_fingerprint:
        raise ops.OperationError("The folder changed since you looked at what would happen. "
                                 "Nothing was changed; please check again.")
    op = "undo" if undo_of else "restore_full"
    if undo_of:                       # the whole-folder Undo: later Undos step past it
        plan = dataclasses.replace(plan, via_undo=True)
    result = _run(ctx, plan, op, allow_game_running, f"restored {entry.id}")
    result.warnings += notes
    if ref.platform == "xbox" and not restore_accountdata:
        _give_missing_xbox_account_data(ctx, entry.id, live_dir)
    if restore_accountdata:
        panic._restore_accountdata(ctx, plan, ref, entry.id)
    elif ref.platform == "steam":
        theirs = panic.account_files(ctx, entry.id)
        if not (live_dir / "accountdata.hg").is_file():
            for name, data in theirs.items():      # a folder with none of its own gets the backup's
                safety.atomic_write_bytes(live_dir / name, data)
        elif any(not (live_dir / n).is_file() or (live_dir / n).read_bytes() != data
                 for n, data in theirs.items()):
            result.warnings.append(ops.ACCOUNT_DATA_KEPT)
    return result


def refuse_disabled(live_dir: Path) -> None:
    """A folder the owner disabled (or marked read-only, or keeps as a backup source) is never
    written, whatever path it is reached by."""
    from .. import state as appstate

    try:
        state = appstate.load()
    except Exception:  # noqa: BLE001 - an unreadable config gives no verdict
        return
    here = _key(live_dir)
    for s in (state.sources if state is not None else ()):
        if _key(s.path) == here and (not s.enabled or not s.writable or s.role != "live"):
            raise ops.OperationError("That save folder is switched off (or read-only) in the "
                                     "app's settings, so nothing is written to it.")


def repair_damaged_index(ctx: OpContext, ref: LiveRef, vault: Vault, entry, live_dir: Path,
                         allow_game_running: bool) -> list[str]:
    """A restore is how a damaged Xbox folder is repaired: when the live ``containers.index``
    does not read, its damaged bytes are kept in the vault and an index of the records that
    still read (with the identity of the account) is written, and the restore then fills in
    the rest from the backup. Runs under the live-op lock and is journaled as an op of kind
    ``index_repair``. Returns the notes for the result."""
    index = Path(live_dir) / "containers.index"
    if not index.is_file():
        return []
    raw = index.read_bytes()
    try:
        msstore.split_index(raw)
        return []
    except ValueError as exc:
        why = str(exc)
    running = ctx.game_probe()
    if running is True or (running is None and not allow_game_running):
        raise ops.GameRunningError("No Man's Sky appears to be running. Close the game first.")
    try:
        with jr.liveop_lock(ctx, timeout=LIVEOP_TIMEOUT):
            return _repair_locked(ctx, ref, vault, entry, live_dir, raw, why)
    except (LiveOpBusy, RuntimeError):
        raise ops.OperationError("Another operation is still running. Try again in a "
                                 "moment.") from None


def _repair_locked(ctx, ref, vault, entry, live_dir: Path, raw: bytes, why: str) -> list[str]:
    from ..organizer.model import new_op_id

    index = Path(live_dir) / "containers.index"
    if index.read_bytes() != raw:                    # someone else repaired or wrote it
        return []
    plan = _index_repair_plan(vault, entry, Path(live_dir), raw, why)   # the guard: refuses, writes nothing
    op_id = new_op_id(ctx.clock)
    jr.append(ctx, op_id, "preparing", {
        "kind": "index_repair", "app": ctx.app_version,
        "targets": [jr.target_json(ref.target(1))], "sources": [None], "undo_of": None,
        "note": "containers.index damaged", "confirmed": [], "plan_digest": ""})
    try:
        notes = _repair_files(vault, Path(live_dir), index, raw, why, plan)
    except BaseException:
        jr.append(ctx, op_id, "abandoned", {"reason": "repair_failed"})
        raise
    jr.append(ctx, op_id, "done", {"oplog": False, "lineage": False})
    return notes


_BLOB_DIR = re.compile(r"[0-9A-F]{32}")


@dataclass(frozen=True)
class _IndexRepairPlan:
    header: bytes
    records: list                  # (identifier, raw record bytes), whole and rebuilt
    rebuilt: int                   # records rebuilt for blobs the damaged index lost


def index_repair_problem(vault: Vault, entry, live_dir: Path) -> str | None:
    """Read-only: why a damaged live ``containers.index`` could not be repaired from ``entry``
    (None: it can). The same guard the repair itself runs before it writes anything."""
    index = Path(live_dir) / "containers.index"
    if not index.is_file():
        return None
    raw = index.read_bytes()
    try:
        msstore.split_index(raw)
        return None
    except ValueError as exc:
        why = str(exc)
    try:
        _index_repair_plan(vault, entry, Path(live_dir), raw, why)
    except ops.OperationError as exc:
        return str(exc)
    return None


def _index_repair_plan(vault: Vault, entry, live_dir: Path, raw: bytes, why: str) -> _IndexRepairPlan:
    """The guard (nothing is written): an index of an unknown or newer version, a save from a
    newer game (in the folder or the backup), a backup of another account, or a lost record
    that cannot be rebuilt each refuse the repair - never a guess."""
    from ..store import classic as store_classic

    refuse_newer = ops.OperationError(gameversion.GAME_NEWER_TEXT)
    refuse_newer.code = gameversion.GAME_NEWER
    if len(raw) >= 4 and struct.unpack_from("<i", raw, 0)[0] != msstore.CONTAINERSINDEX_HEADER:
        raise refuse_newer                     # an index version this app does not know
    header, records = msstore.salvage_index(raw)
    with store_classic.entry_folder(vault, entry) as folder:
        theirs_raw = (Path(folder) / "containers.index").read_bytes()
        theirs = msstore.split_index(theirs_raw)[0]
        _info, theirs_containers = msstore.parse_containers_index(Path(folder))
        for c in theirs_containers:
            if c.meta_file is not None and c.meta_file.is_file() and \
                    gameversion.newer_plain("xbox", c.meta_file.read_bytes()):
                raise refuse_newer             # the backup holds a save from a newer game
    if header is None:
        if entry.source != Path(live_dir).name:
            raise ops.OperationError(
                f"The live save index is damaged ({why}), and '{entry.id}' was not taken of this "
                "account's folder, so it cannot be confirmed to be the same account. "
                "Nothing was changed.")
        header = theirs
    elif msstore.index_header_account(header) != msstore.index_header_account(theirs):
        raise ops.ForeignAccountError(
            f"'{entry.id}' was taken from another Xbox account. Restoring it would write that "
            "account's save index into this one, so it is refused. Nothing was changed.")
    for _ident, rec in records:                # what still reads must not be from a newer game
        c = msstore._parse_record(rec, live_dir)
        if c.meta_file is not None and c.meta_file.is_file() and \
                gameversion.newer_plain("xbox", c.meta_file.read_bytes()):
            raise refuse_newer
    held = {msstore._parse_record(rec, live_dir).dir_guid for _i, rec in records}
    orphans = sorted(p.name for p in live_dir.iterdir()
                     if p.is_dir() and _BLOB_DIR.fullmatch(p.name) and p.name not in held)
    rebuilt = []
    if orphans:
        rebuilt = _rebuild_lost_records(raw, header, records, orphans, live_dir)
        if rebuilt is None:
            raise ops.OperationError(
                f"The live save index is damaged ({why}) and {len(orphans)} save(s) in the folder "
                "are no longer listed in it. They cannot be listed again safely, so the index was "
                "not repaired. Nothing was changed.")
    return _IndexRepairPlan(header, list(records) + rebuilt, len(rebuilt))


def _rebuild_lost_records(raw: bytes, header: bytes, records: list, orphans: list[str],
                          live_dir: Path):
    """The records a truncated index lost, rebuilt from their blobs - only when exactly one
    record is cut off (its identifier still reads) and exactly one blob is unlisted, so which
    belongs to which is not a guess. None otherwise."""
    if len(orphans) != 1:
        return None
    start = 12 + len(header)
    for _ident, rec in records:
        start += len(rec)
    try:
        identifier, off = msstore._read_lp_string(raw, start)
        second, off = msstore._read_lp_string(raw, off)
        sync, off = msstore._read_lp_string(raw, off)
    except ValueError:
        return None
    if not identifier or len(raw) - off >= msstore.RECORD_TAIL_LENGTH:
        return None                            # nothing cut off here, or not a record at all
    folder = live_dir / orphans[0]
    containers = sorted(folder.glob("container.*"), key=lambda p: p.name)
    if not containers:
        return None
    try:
        extension = int(containers[-1].suffix[1:])
    except ValueError:
        return None
    files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith("container.")]
    size = sum(p.stat().st_size for p in files)
    guid = uuid.UUID(hex=orphans[0]).bytes_le
    tail = (bytes([extension]) + struct.pack("<i", msstore.INDEX_SYNC_MODIFIED) + guid
            + struct.pack("<q", msstore._unix_to_filetime(containers[-1].stat().st_mtime))
            + bytes(8) + struct.pack("<q", size))
    assert len(tail) == msstore.RECORD_TAIL_LENGTH

    def lp(s: str) -> bytes:
        return struct.pack("<i", len(s)) + s.encode("utf-16-le")
    return [(identifier, lp(identifier) + lp(second) + lp(sync) + tail)]


def _preserve_folder(vault: Vault, live_dir: Path, stamp: str) -> Path:
    """The whole live account folder - the index and every blob, orphans included - copied into
    the vault and verified file by file, before anything in it is changed."""
    dest = Path(vault.root) / "damaged-index" / f"{live_dir.name}-{stamp}"
    shutil.copytree(live_dir, dest)
    for src in sorted(p for p in live_dir.rglob("*") if p.is_file()):
        copy = dest / src.relative_to(live_dir)
        if not copy.is_file() or hashlib.sha256(copy.read_bytes()).digest() != \
                hashlib.sha256(src.read_bytes()).digest():
            shutil.rmtree(dest, ignore_errors=True)
            raise ops.OperationError("The folder could not be copied into the vault completely, "
                                     "so the index was not repaired. Nothing was changed.")
    return dest


def _repair_files(vault, live_dir: Path, index: Path, raw: bytes, why: str,
                  plan: _IndexRepairPlan) -> list[str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    kept = Path(vault.root) / "damaged-index"
    kept.mkdir(parents=True, exist_ok=True)
    _preserve_folder(vault, live_dir, stamp)                 # first: the whole folder, verified
    safety.atomic_write_bytes(kept / f"{live_dir.name}-{stamp}.index", raw)
    repaired = msstore.join_index(plan.header, plan.records, b"")
    msstore.split_index(repaired)                            # it must read before it is written
    held = {msstore._parse_record(rec, live_dir).dir_guid for _i, rec in plan.records}
    blobs = {p.name for p in live_dir.iterdir() if p.is_dir() and _BLOB_DIR.fullmatch(p.name)}
    if blobs - held:
        raise ops.OperationError("The repaired index would not list every save in the folder, "
                                 "so it was not written. Nothing was changed.")
    safety.atomic_write_bytes(index, repaired)
    note = (f"The live save index was damaged ({why}); it was rebuilt from the {len(plan.records)} "
            "records that could be kept")
    if plan.rebuilt:
        note += f" ({plan.rebuilt} rebuilt from its save files)"
    return [note + ". The whole folder as it was, and the damaged file, are in the vault "
            "(damaged-index)."]


def _give_missing_xbox_account_data(ctx: OpContext, entry_id: str, live_dir: Path) -> None:
    """A live Xbox account with no AccountData record gets the backup's (it has none of its
    own to keep)."""
    from ..store import listing

    try:
        _info, live = msstore.parse_containers_index(live_dir)
        if any(c.identifier == "AccountData" and c.data_file is not None and c.data_file.is_file()
               for c in live):
            return
        entry = listing.get_vault_entry(ctx.vault, ctx.versions, entry_id, live_roots=[])
        with listing.open_entry_folder(ctx.vault, ctx.versions, entry, slots=[]) as folder:
            _i, theirs = msstore.parse_containers_index(Path(folder))
            acct = next((c for c in theirs if c.identifier == "AccountData" and c.data_file
                         and c.data_file.is_file() and c.meta_file and c.meta_file.is_file()), None)
            if acct is not None:
                msstore.write_save(live_dir, "AccountData", acct.data_file.read_bytes(),
                                   acct.meta_file.read_bytes(), ctx.clock(),
                                   create_if_missing=True)
    except (OSError, ValueError) as exc:
        ops.log.warning("the backup's account data was not copied: %s", exc)


def undo_plan(vault: Vault, live_dir: Path) -> ops.UndoPlan:
    """What :func:`undo_last` would restore: the newest snapshot or full backup taken of
    this very folder (P4)."""
    from . import panic

    ctx, ref = open_context(vault, Path(live_dir))
    found = panic.candidates(ctx, ref)
    if not found:
        raise ops.OperationError("There is nothing to undo in this folder: no snapshot of it "
                                 "exists. Backing up, extracting and importing only add to the "
                                 "vault -- they never change your live saves.")
    newest = found[0]
    return ops.UndoPlan(op="panic_restore", ts=newest.created, snapshot_id=newest.id,
                        token=newest.id, detail=newest.label or newest.id)


def undo_last(vault: Vault, live_dir: Path, allow_game_running: bool = False,
              restore_accountdata: bool = False, expected: str | None = None) -> ops.OpResult:
    """Restore the newest snapshot of this folder (the whole-folder undo, kept as the panic
    restore). ``expected``: the snapshot the user was shown."""
    from ..store import classic as store_classic

    plan = undo_plan(vault, live_dir)
    if expected is not None and plan.token != expected:
        raise ops.OperationError(
            "The newest snapshot of this folder changed after you were asked. Nothing was "
            "changed; press Undo again to see what it would restore now.")
    entry = store_classic.find_entry(vault, plan.snapshot_id)
    if entry is None:
        raise ops.OperationError(f"The snapshot {plan.snapshot_id} can no longer be found.")
    result = restore_full(vault, entry, live_dir, True, allow_game_running,
                          undo_of=plan.snapshot_id, restore_accountdata=restore_accountdata)
    result.detail = f"restored the snapshot {plan.snapshot_id}"
    return result


def empty_guide(vault: Vault, live_dir: Path, slot: int):
    """The steps to empty a live slot by hand (the app never does), D5 2.4.2."""
    from .guide import empty_slot_guide

    ops._check_slot(slot)
    ctx, ref = open_context(vault, Path(live_dir))
    return empty_slot_guide(ctx, ref.target(slot))


def guide_text(guide) -> str:
    lines = [f"The app never empties a slot. To empty {guide.label} yourself:", ""]
    if guide.refill is not None:
        lines += [guide.refill.text, ""]
    lines += [f"{n}. {step}" for n, step in enumerate(guide.steps, 1)]
    return "\n".join(lines)


def keep_copy(vault: Vault, live_dir: Path, slot: int) -> ops.OpResult:
    """Copy one live slot into the vault now (Keep a copy, D5 2.4.1); nothing live changes."""
    from .planner import plan_keep

    ctx, ref = open_context(vault, Path(live_dir))
    plan = plan_keep(ctx, ref.target(slot))
    return _run(ctx, plan, "keep", True, f"slot {slot}")


def require_folder(folder: str | Path) -> Path:
    """A source folder is an absolute path to a real folder. An empty path (a vault entry with
    no folder of its own) or a relative one would be read from the current directory, and a
    save from there would be written into a live slot: refused."""
    text = str(folder or "")
    path = Path(text) if text.strip() else None
    if path is None or not path.is_absolute():
        raise ops.OperationError("That source has no folder of its own to read from; a "
                                 "relative or empty path is never used. Nothing was changed.")
    return path


def folder_pair(folder: Path, slot: int) -> PairSource:
    """The pair a save folder holds in ``slot``, read directly from the folder."""
    folder = require_folder(folder)
    try:
        view = savedir.scan_any(folder).slots[slot]
    except (OSError, KeyError) as exc:
        raise ops.OperationError(f"source slot {slot} cannot be read in {folder}") from exc
    vids: dict[str, str | None] = {"A": None, "B": None}
    for pos, mv in zip("AB", view.members):
        if mv.exists:
            vids[pos] = hashlib.sha256(Path(mv.data_path).read_bytes()).hexdigest()
    if not any(vids.values()):
        raise ops.OperationError(f"source slot {slot} has no saves in {folder}")
    newest = view.newest.label if view.newest is not None and vids[view.newest.label] else None
    return PairSource(vids["A"], vids["B"], newest, None, folder_slot=(Path(folder), slot))


# --- running a plan ----------------------------------------------------------------------------


def _run(ctx: OpContext, plan: PlacePlan, op: str, allow_game_running: bool,
         detail: str) -> ops.OpResult:
    """Execute ``plan`` for a caller that cannot answer confirmations: it confirms what the
    plan asks, except that an unknown game state needs ``allow_game_running``."""
    for why in plan.blocked:
        if why.code == "ALREADY_LIVE":                 # what was asked for is already there
            return ops.OpResult(True, op, f"{detail}: {why.text} Nothing was changed.")
        raise _error(why.code, why.text)
    confirmed: Iterable[str] = {c for c in plan.confirmations
                                if c != "GAME_STATE_UNKNOWN" or allow_game_running}
    try:
        outcome = execute(ctx, plan, confirmed)
    except LiveOpError as exc:
        raise ops.OperationError(str(exc)) from exc
    if outcome.status == "stale" and outcome.replan is not None:
        raise ops.OperationError(outcome.outcome)
    if outcome.status != "done":
        raise _error(outcome.code or "", outcome.outcome)
    warnings = [w.text for w in outcome.warnings] + [w.text for w in plan.warnings
                                                      if w.severity != "info"]
    changed = []
    for tp in plan.targets:
        changed.append(tp.label)
    return ops.OpResult(True, op, detail, snapshot_id=None, changed=changed,
                        warnings=warnings, op_id=outcome.op_id)


def _error(code: str, text: str) -> ops.OperationError:
    if code in ("GAME_RUNNING", "GAME_STATE_UNKNOWN"):
        return ops.GameRunningError(text)
    return ops.OperationError(text)


def outcome_result(outcome: OpOutcome, op: str) -> ops.OpResult:
    return ops.OpResult(outcome.status == "done", op, outcome.outcome,
                        warnings=[w.text for w in outcome.warnings], op_id=outcome.op_id)


# --- the folder as the engine names it -----------------------------------------------------------


def _same_folder(path: Path) -> str:
    return _key(path)


def _key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).lower()


def _ad_hoc(live_dir: Path) -> Source:
    platform = "xbox" if savedir.platform_of(live_dir) == "xbox" else "steam"
    tag = hashlib.sha1(str(live_dir).encode("utf-8")).hexdigest()[:6]
    return Source(id=f"{platform}-{live_dir.name}-{tag}", platform=platform,
                  account=live_dir.name, path=str(live_dir), label="", writable=True,
                  enabled=True)


def target_of(ref: LiveRef, slot: int) -> LiveTarget:
    return ref.target(slot)
