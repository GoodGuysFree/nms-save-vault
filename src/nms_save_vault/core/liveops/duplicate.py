"""A save copied into an empty slot as a new playthrough (NMS-Unpack's response to Savecraft's
query 6, part A, traced in exe 185034, 2026-11-10).

The game knows a playthrough by one number, ``CommonStateData.SaveUniversalId`` (a u64 the
JSON writes as ``"0x"`` and upper-case hex, no leading zeros); the meta holds the same number
at 0x15C, and on load the meta's wins. Two slots with one id are one playthrough to
cross-save, and the game re-rolls the others when it notices. So a duplicate gets:

- a new random, non-zero id, used by no other slot of that account, in the save and in both
  metas of the target slot (the place writes the one save into both positions);
- ``MultiplayerLobbyID`` 0 in each context (it only steers where the player appears in that
  lobby);
- nothing else changed: bases, discoveries, rewards stay (they are not tied to the id).

The copy is kept in the vault first, as an edit is (:mod:`.edit`), then placed into the empty
slot like any version: the same journal, rollback and undo. Steam saves only.
"""
from __future__ import annotations

import dataclasses
import secrets
import struct

from .. import formats, savejson
from ..hashname import key as K
from ..organizer.model import LiveTarget
from ..savepatch import Replace
from .context import OpContext
from .edit import EditRefused, _gates, _stage, _still
from .model import PlacePlan
from .planner import plan_place
from .slots import read_account
from .targets import resolve_account, resolve_target

__all__ = ["plan_duplicate"]

CONTEXTS = ("BaseContext", "ExpeditionContext")


def plan_duplicate(ctx: OpContext, source: LiveTarget, target: LiveTarget, *, what: str,
                   expected: str | None = None) -> PlacePlan:
    """The plan to put a copy of the save ``source`` loads into the empty slot ``target``, as
    a new playthrough. ``what`` labels the copy in the vault; ``expected``: the SHA-256 of the
    save the app read (another version there now is refused). Raises :class:`EditRefused`
    (a slot that is not empty, a save with no playthrough id, an Xbox slot, the game running)."""
    _gates(ctx, [source, target])
    if (source.platform, source.account_key, source.slot, source.instance) == (
            target.platform, target.account_key, target.slot, target.instance):
        raise EditRefused("a save goes into another, empty slot")
    rt = resolve_target(ctx, target)
    there = read_account(ctx, resolve_account(ctx, target.ref))
    if there.slots[target.slot].present:
        raise EditRefused(f"{rt.label} is not empty: a duplicate goes into an empty slot")
    root = _root(ctx, source)
    common = root.get(K("CommonStateData")) if isinstance(root, dict) else None
    if not isinstance(common, dict) or not isinstance(common.get(K("SaveUniversalId")), str):
        raise EditRefused("this save has no playthrough id the game would read; load and "
                          "save it once in the game first")
    taken = {_id(s.head.pid) for s in there.slots.values() if s.head is not None}
    new = _new_id(taken | {_value(common[K("SaveUniversalId")])})
    edits = [Replace(("CommonStateData", "SaveUniversalId"), b'"0x%X"' % new)]
    for name in CONTEXTS:
        psd = root.get(K(name), {}).get(K("PlayerStateData")) if isinstance(
            root.get(K(name)), dict) else None
        if isinstance(psd, dict) and K("MultiplayerLobbyID") in psd \
                and psd[K("MultiplayerLobbyID")] != 0:
            edits.append(Replace((name, "PlayerStateData", "MultiplayerLobbyID"), b"0"))

    def with_id(plain: bytes) -> bytes:
        out = bytearray(plain)
        struct.pack_into("<Q", out, formats.OFF_SLOT_IDENTIFIER, new)
        return bytes(out)

    pair, new_vid, base = _stage(ctx, source, edits, what, expected, alone=True,
                                 meta_hook=with_id)
    plan = plan_place(ctx, pair, target)
    _still(ctx, source, base)
    # the slot was empty when asked, not "emptied outside the app"; its new id is no twin
    quiet = {"EMPTY_SLOT", "TWIN_LIVE"}
    return dataclasses.replace(
        plan, made=(new_vid,), warnings=tuple(w for w in plan.warnings if w.code not in quiet),
        confirmations=plan.confirmations - quiet)


def _root(ctx: OpContext, source: LiveTarget) -> object:
    """The JSON of the save ``source`` loads (EditRefused when there is none to read)."""
    rt = resolve_target(ctx, source)
    now = read_account(ctx, resolve_account(ctx, source.ref)).slots[source.slot]
    if now.newer is None or now.member(now.newer) is None:
        raise EditRefused(f"{rt.label} holds no save to copy")
    from .steamwrite import file_names
    data = (rt.folder / file_names(source.slot, now.newer)[0]).read_bytes()
    try:
        return savejson.decode_save(data).root
    except (ValueError, OSError):
        raise EditRefused(f"{rt.label}: the save the game loads cannot be read") from None


def _value(text: str) -> int | None:
    try:
        return int(text, 16)
    except ValueError:
        return None


def _id(pid: object) -> int | None:
    """A pid's number (``pid_from_id`` writes 16 hex digits), None for a legacy pid."""
    return _value(pid) if isinstance(pid, str) else None


def _new_id(taken: set) -> int:
    """A random non-zero u64 that is not in ``taken``, as the game's own ids are random."""
    while True:
        value = secrets.randbits(64)
        if value and value not in taken:
            return value
