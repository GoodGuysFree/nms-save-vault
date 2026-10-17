"""The classic operations as adapters over the live-operation engine (D5 4.1).

``repopulate_slot``, ``promote_member`` and the restore of an extract keep their signatures
and their :class:`OpResult` for today's callers, but every live write is one ``execute``: the
same planner (both positions are always written, nothing is ever emptied), preserve before
replace, journal, validation and exact rollback. A refusal or a failure raises the
:class:`OperationError` the callers already handle.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from .. import operations as ops
from .. import platform as hostmod
from .. import safety, savedir
from ..catalog import Vault
from ..organizer import Organizer
from ..organizer.model import LiveRef, LiveTarget
from ..state import Source
from ..store import listing
from .context import OpContext, context_for
from .errors import LiveOpError
from .execute import execute
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
    source_dir, live_dir = Path(source_dir), Path(live_dir)
    ops._check_slot(source_slot)
    ops._check_slot(dest_slot)
    ops._guard_same_platform(source_dir, live_dir)
    ctx, ref = open_context(vault, live_dir)
    source = folder_pair(source_dir, source_slot)
    plan = plan_place(ctx, source, ref.target(dest_slot))
    return _run(ctx, plan, "repopulate_slot", allow_game_running,
                f"slot {source_slot} -> {dest_slot}")


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
                 restore_accountdata: bool = False) -> ops.OpResult:
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
    ctx, ref = open_context(vault, live_dir)
    found = listing.get_vault_entry(ctx.vault, ctx.versions, entry.id, live_roots=[])
    if found is not None and found.platform != ref.platform:
        raise ops.FeatureNotYetAvailableError(ops.CROSS_PLATFORM_MSG)
    plan = panic.plan_panic_restore(ctx, ref, entry.id, restore_accountdata=restore_accountdata)
    for why in plan.blocked:
        if why.code == "FOREIGN_ACCOUNT":
            raise ops.ForeignAccountError(why.text)
    op = "undo" if undo_of else "restore_full"
    result = _run(ctx, plan, op, allow_game_running, f"restored {entry.id}")
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


def folder_pair(folder: Path, slot: int) -> PairSource:
    """The pair a save folder holds in ``slot``, read directly from the folder."""
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
