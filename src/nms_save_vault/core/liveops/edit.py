"""An edit of a live save: its content changed, then placed like any other version (CL5).

The save the game loads from a slot is read, the edits are made and proven
(:func:`saveedit.rebuild`), and the result is kept in the vault first, as a one-slot extract
labelled "edited by <app>: <what>" (GGF, 2026-11-08: an existing entry kind, so every NMS Save
Vault release can read the vault). The slot's other member goes into that extract as it is.
The plan is then an ordinary place of that extract into the same slot: the same preserve,
snapshot, journal, validation, rollback and undo as every live write. The plan names the
edited version as made by the operation (``PlacePlan.made``), so the records say who made it.

Nothing is kept in the vault for an edit that could not be written now (the game running, an
interrupted operation waiting, a vault that cannot be written); a plan the planner blocks for
another reason (no room in the vault) leaves its edited version there, which is harmless.

Steam saves only for now; an Xbox slot is refused (:class:`EditRefused`).
"""
from __future__ import annotations

import dataclasses
import hashlib
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from .. import meta, slotmap
from ..organizer.locator import Location
from ..organizer.model import LiveTarget, StoreRef
from ..saveedit import EditRefused, rebuild
from ..savepatch import Edit
from ..store import CaptureScope
from .context import OpContext
from .model import PairSource, PlacePlan
from .planner import (SourceUnreadable, _plan, _plan_warnings, _slot_warnings, classify,
                      durable, game_warning, load_source, plan_place, recovery_pending,
                      vault_problem)
from .slots import read_account
from .steamwrite import file_names
from .targets import resolve_account, resolve_target

__all__ = ["EditRefused", "plan_edit", "plan_edits"]


def _sha256_text(value: object) -> str:
    """``value`` as a SHA-256 in lower-case hex; ValueError for anything else."""
    if not isinstance(value, str) or len(value) != 64 or any(
            c not in "0123456789abcdefABCDEF" for c in value):
        raise ValueError("expected must be a SHA-256 in hex (64 characters)")
    return value.lower()


def plan_edit(ctx: OpContext, target: LiveTarget, edits: Iterable[Edit], *,
              what: str, expected: str | None = None) -> PlacePlan:
    """The plan to put the save in ``target`` back with ``edits`` made. ``what`` says what the
    edit does, for the vault's label ("rename ship 'Ark'"). Run it with ``execute`` as any
    plan. The edited version is in the vault before this returns. Raises
    :class:`EditRefused` (also when the edit could not be written now: the game running, an
    interrupted operation waiting) and ``savepatch.PatchRefused``."""
    _gates(ctx, [target])
    source, new_vid = _stage(ctx, target, edits, what, expected)
    plan = plan_place(ctx, source, target)
    return dataclasses.replace(plan, made=(new_vid,))


def plan_edits(ctx: OpContext, edits: Sequence[tuple[LiveTarget, Iterable[Edit], str | None]],
               *, what: str) -> PlacePlan:
    """The plan to edit several slots as one operation (CL6: a move between two saves takes
    from one and puts into the other): ``edits`` holds (slot, its edits, the SHA-256 of the
    version they were made from or None). Each edited version is in the vault before this
    returns; then all are written together, rolled back together when one fails, and undone
    together. Raises as :func:`plan_edit` does, and ``EditRefused`` for a slot named twice."""
    targets = [t for t, _e, _x in edits]
    if not targets:
        raise EditRefused("nothing to edit")
    if len({(t.platform, t.account_key, t.slot, t.instance) for t in targets}) != len(targets):
        raise EditRefused("a slot is named twice")
    _gates(ctx, targets)
    staged = [(t, _stage(ctx, t, e, what, x)) for t, e, x in edits]
    parts, warnings, labels = [], [], []
    for target, (source, _vid) in staged:
        rt = resolve_target(ctx, target)
        now = read_account(ctx, resolve_account(ctx, target.ref))
        slot = now.slots[target.slot]
        try:
            src = load_source(ctx, source)
        except SourceUnreadable:
            raise EditRefused(f"{rt.label}: the edited version cannot be read back") from None
        kind = classify(src, slot)
        warnings += _plan_warnings(ctx, src, target.ref, now)
        warnings += _slot_warnings(ctx, src, now, slot, kind, rt.label,
                                   {m.vid: durable(ctx, m.vid) for m in slot.present})
        parts.append((rt, now, slot, src, source))
        labels.append(rt.label)
    plan = _plan(ctx, "place", parts, warnings, f"Change {' and '.join(labels)} together",
                 source=None)
    return dataclasses.replace(plan, made=tuple(vid for _t, (_s, vid) in staged))


def _gates(ctx: OpContext, targets: list[LiveTarget]) -> None:
    for target in targets:
        if target.platform != "steam":
            raise EditRefused("only Steam saves can be edited for now")
    if ctx.versions is None:
        raise EditRefused("the vault has no version store yet (upgrade it first)")
    for target in targets:
        resolve_target(ctx, target)
    for gate in (vault_problem(ctx), game_warning(ctx),
                 recovery_pending(ctx, [t.ref for t in targets])):
        if gate is not None and gate.severity == "block":
            raise EditRefused(gate.text)


def _stage(ctx: OpContext, target: LiveTarget, edits: Iterable[Edit], what: str,
           expected: str | None) -> tuple[PairSource, str]:
    """The slot's save with ``edits`` made, kept in the vault as a one-slot extract: the pair
    to place, and the edited version's id."""
    rt = resolve_target(ctx, target)
    account = resolve_account(ctx, target.ref)
    now = read_account(ctx, account).slots[target.slot]
    if now.newer is None or now.member(now.newer) is None:
        raise EditRefused(f"{rt.label} holds no save to edit")
    loaded = now.member(now.newer)
    if not loaded.valid:
        raise EditRefused(f"{rt.label}: the save the game loads cannot be read")
    data_name, meta_name = file_names(target.slot, loaded.position)
    data = (rt.folder / data_name).read_bytes()
    if hashlib.sha256(data).hexdigest() != loaded.vid:
        raise EditRefused(f"{rt.label} changed while it was read; try again")
    if expected is not None and loaded.vid != _sha256_text(expected):
        # the edits name places in the version the app read; in another they mean other things
        raise EditRefused(f"{rt.label} changed since it was read (the game or another app "
                          f"saved it); read it again and make the change again")
    ordinal = slotmap.storage_ordinal(slotmap.file_no(target.slot,
                                                      slotmap.member_index(loaded.position)))
    blob = (rt.folder / meta_name).read_bytes()
    # Only a meta keyed for this slot is known to be this save's: one keyed for another slot
    # was moved here by hand, perhaps from another save, and the game does not read it.
    if not meta.is_valid_for(blob, ordinal):
        raise EditRefused(f"{rt.label}: the save's meta was written for another slot (files "
                          "moved by hand?); it is not edited")
    out = rebuild("steam", data, meta.decrypt(blob, ordinal), edits)
    new_vid = hashlib.sha256(out.data).hexdigest()
    if new_vid == loaded.vid:
        raise EditRefused("the edits change nothing")

    stage = Path(tempfile.mkdtemp(prefix="nmsvault-edit-"))
    try:
        for member in now.present:
            names = file_names(target.slot, member.position)
            if member.position == loaded.position:
                (stage / names[0]).write_bytes(out.data)
                (stage / names[1]).write_bytes(meta.encrypt(out.meta, ordinal))
            else:
                for name in names:
                    shutil.copyfile(rt.folder / name, stage / name)
        entry = ctx.versions.add_from_folder(
            stage, kind="extract", label=f"edited by {ctx.app().split(' ')[0]}: {what}"[:120],
            source={"type": "folder", "name": "edit"}, scope=CaptureScope(slots=(target.slot,)),
            origin={"made_by": ctx.app(), "edit_of": loaded.vid, "what": what[:200]},
            reason="edit")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    pair = {m.position: m.vid for m in now.present}
    pair[loaded.position] = new_vid
    source = PairSource(pair.get("A"), pair.get("B"), loaded.position,
                        Location(new_vid, "store", StoreRef(entry.id), target.slot,
                                 loaded.position, "steam", True))
    return source, new_vid
