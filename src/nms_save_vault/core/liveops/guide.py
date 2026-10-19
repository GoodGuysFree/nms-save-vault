"""Emptying a slot is the owner's own job (F8): the app only replaces. ``empty_slot_guide``
(D5 2.4.2) gives the manual steps for the slot's cloud mode and the warning that a cloud may
fill an empty slot again. Read-only."""
from __future__ import annotations

from ..organizer.model import LiveTarget
from .context import OpContext
from .model import EmptySlotGuide
from .planner import durable, warning
from .slots import read_account
from .steamwrite import file_names
from .targets import resolve_account, resolve_target

REFILL = {
    "steam-cloud": "While this slot is empty, Steam Cloud may put a save back into it at the "
                   "next game start.",
    "xbox": "While this slot is empty, Xbox sync may put a save back into it.",
}


def empty_slot_guide(ctx: OpContext, target: LiveTarget) -> EmptySlotGuide:
    rt = resolve_target(ctx, target)
    slot = read_account(ctx, resolve_account(ctx, target.ref)).slots[target.slot]
    refill = None
    if rt.cloud_mode in REFILL:
        refill = warning("EMPTY_SLOT_MAY_REFILL", refill=REFILL[rt.cloud_mode])
    return EmptySlotGuide(target, rt.label, rt.cloud_mode, bool(slot.present),
                          bool(slot.present) and all(durable(ctx, m.vid) for m in slot.present),
                          refill, steps(rt.cloud_mode, target.slot))


def steps(cloud_mode: str, slot: int) -> tuple[str, ...]:
    """The manual steps, one sentence each (the UI numbers them)."""
    files = ", ".join(n for p in ("A", "B") for n in file_names(slot, p))
    delete = f"In the save folder delete the four files of this slot ({files})."
    if cloud_mode == "steam-cloud":
        return ("Close No Man's Sky.",
                "In Steam: No Man's Sky > Properties > General, turn off \"Keep game saves in "
                "the Steam Cloud\".",
                delete,
                "Start the game once and quit.",
                "Turn Steam Cloud back on; if Steam asks, keep the copy on this PC.")
    if cloud_mode == "steam-local":
        return ("Close No Man's Sky.", delete)
    return ("Start No Man's Sky.",
            "Delete the save in the game's own load menu.",
            "Quit and let Xbox sync finish.")
