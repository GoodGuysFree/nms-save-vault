"""Unlocks written to a Steam account's account data (build item 14 B; GGF chose option A,
2026-11-10: the shared op journal, its own kind).

An app adds reward ids to the account's ``Unlocked*`` lists (the game then offers them to
every save of the account). The write is made as a save's is: the game known closed, one
live write at a time, a whole-folder safety snapshot first (it holds the account files),
journaled before anything is written (``account_edit``), the data file and then its meta
each replaced whole, and both checked. The data is plain JSON text ending in one NUL, as the
game writes it; the meta (storage ordinal 1) gets the new size and the time.

A write that stops part-way is found at the next start: both files as written finish it, both
as they were end it, anything else is put back from the snapshot (or left, if the user says
so). Its journal record names no slot: an older core (0.3.9) reads every write target as a
slot's, finds none here, and only offers to leave it.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import accountdata, formats, meta, safety, savejson, savepatch
from ..organizer.model import LiveRef, new_op_id, source_to_json
from ..organizer.scanner import instance_of
from ..organizer.store import StoreBusy as OrganizerBusy
from ..organizer.store import StoreReadOnly
from ..store import CaptureScope, StoreError
from . import opjournal
from .context import OpContext
from .errors import TargetError
from .execute import _game_problem, append_quietly, refused
from .faults import at
from .model import OpOutcome, Warning
from .planner import recovery_pending, vault_problem, warning
from .targets import account_label, resolve_account

KIND = "account_edit"
DATA, META = formats.ACCOUNT_DATA_NAME, formats.ACCOUNT_META_NAME
#: The account's lists an app may add to (UserSettingsData).
UNLOCK_LISTS = ("UnlockedSeasonRewards", "UnlockedTwitchRewards", "UnlockedPlatformRewards",
                "UnlockedSpecials", "UnlockedTitles")
#: A reward id as the game keeps one: letters, digits and _ - . after an optional ``^``.
_ID = re.compile(r"\^?[A-Za-z0-9_.\-]{1,63}")
_SIZE_AGAIN = 0x54                      # the size the game repeats (saveedit)


@dataclass(frozen=True)
class AccountPlan:
    """The plan to add ``adds`` ((list, id) pairs) to an account's data. ``blocked`` says
    why it cannot be written; ``pre`` is the SHA-256 of each file it was planned from."""

    account: LiveRef
    label: str
    what: str
    adds: tuple[tuple[str, str], ...]
    pre: tuple[str, str]
    blocked: tuple[Warning, ...]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(folder: Path) -> tuple[bytes, bytes] | None:
    try:
        return (folder / DATA).read_bytes(), (folder / META).read_bytes()
    except OSError:
        return None


def check_unlocks(unlocks: Mapping[str, Sequence[str]]) -> list[tuple[str, str]]:
    """``unlocks`` as (list, id) pairs, each list one of :data:`UNLOCK_LISTS` and each id a
    reward id; raises ``ValueError`` for anything else."""
    out = []
    for name, ids in unlocks.items():
        if name not in UNLOCK_LISTS:
            raise ValueError(f"{name!r} is not a list of the account's unlocks")
        if isinstance(ids, (str, bytes)):
            raise ValueError("the ids come as a list")
        for rid in ids:
            if not isinstance(rid, str) or not _ID.fullmatch(rid):
                raise ValueError(f"{str(rid)[:40]!r} is not a reward id")
            if (name, rid) not in out:
                out.append((name, rid))
    return out


def plan_unlock(ctx: OpContext, account: LiveRef, unlocks: Mapping[str, Sequence[str]], *,
                what: str) -> AccountPlan:
    """The plan to add ``unlocks`` (list name -> ids) to ``account``'s account data; ids it
    holds already are left out. Raises ``ValueError`` for a list or id no app may add and
    ``TargetError`` for an account no write may use."""
    wanted = check_unlocks(unlocks)
    acct = resolve_account(ctx, account)
    label = account_label(ctx, account)
    blocked: list[Warning] = []
    adds: list[tuple[str, str]] = []
    pre = ("", "")
    files = _read(acct.folder) if account.platform != "xbox" else None
    if account.platform == "xbox":
        blocked.append(warning("UNLOCK_XBOX"))
    elif files is None:
        blocked.append(warning("NO_ACCOUNT_DATA"))
    else:
        data, _meta = files
        pre = (_sha(data), _sha(_meta))
        try:
            settings = _settings(data) if _meta_ok(_meta) else None
        except (savejson.SaveDecodeError, ValueError):
            settings = None
        if settings is None:
            blocked.append(warning("ACCOUNT_DATA_UNREADABLE"))
        else:
            for name, rid in wanted:
                held = settings.get(savepatch._k(name))
                if not isinstance(held, list):
                    blocked.append(warning("ACCOUNT_LIST_MISSING", name=name))
                    break
                if rid not in held:
                    adds.append((name, rid))
            if not blocked and not adds:
                blocked.append(warning("NOTHING_TO_UNLOCK"))
    return AccountPlan(acct.account, label, what, tuple(adds), pre, tuple(blocked[:1]))


#: The account metas this core writes: the ones with the time at 0x164 (review R22-5).
_META_LENGTHS = (formats.META_LEN_WAYPOINT, formats.META_LEN_WORLDS_I, formats.META_LEN_WORLDS_II)


def _meta_ok(data: bytes) -> bool:
    """Whether ``data`` is an account meta this core can write back."""
    if len(data) not in _META_LENGTHS:
        return False
    try:
        plain = meta.decrypt(data, formats.ACCOUNT_STORAGE_ORDINAL)
    except (ValueError, IndexError, struct.error):
        return False
    return meta.parse(plain, formats.ACCOUNT_STORAGE_ORDINAL).header == formats.META_HEADER


def _settings(data: bytes) -> dict | None:
    """The account's UserSettingsData, from its plain JSON text; None when it has none."""
    if data.lstrip()[:1] != b"{":
        return None                     # only the plain form the game writes is written back
    root = accountdata.decode(data)
    settings = root.get(savepatch._k("UserSettingsData"))
    return settings if isinstance(settings, dict) else None


def _built(plan: AccountPlan, data: bytes, old_meta: bytes, now: int) -> tuple[bytes, bytes]:
    """The new data and meta: the ids appended to their lists (each proven by a re-parse),
    the meta with the new size and ``now``. Raises ``ValueError`` when either does not read
    back as it should."""
    text, nul, tail = data.partition(b"\x00")
    root = accountdata.decode(data)
    settings = root[savepatch._k("UserSettingsData")]
    sizes = {name: len(settings[savepatch._k(name)]) for name, _rid in plan.adds}
    steps = []
    for name, rid in plan.adds:
        steps.append([savepatch.Insert(("UserSettingsData", name), sizes[name],
                                       json.dumps(rid).encode("ascii"))])
        sizes[name] += 1
    new_text = savepatch.apply_steps(text, savepatch.Steps(steps))
    new_data = new_text + nul + tail
    after = _settings(new_data)
    for name, rid in plan.adds:
        if after is None or rid not in after.get(savepatch._k(name), ()):
            raise ValueError("the account data does not read back with the unlocks")
    plain = meta.decrypt(old_meta, formats.ACCOUNT_STORAGE_ORDINAL)
    if meta.parse(plain, formats.ACCOUNT_STORAGE_ORDINAL).header != formats.META_HEADER \
            or len(plain) < _SIZE_AGAIN + 4:
        raise ValueError("the account meta does not decrypt")
    out = bytearray(plain)
    for off in (formats.OFF_SIZE_DECOMPRESSED, formats.OFF_SIZE_DISK, _SIZE_AGAIN):
        struct.pack_into("<I", out, off, len(new_data))
    new_meta = meta.encrypt(meta.set_timestamp(bytes(out), now), formats.ACCOUNT_STORAGE_ORDINAL)
    info = meta.parse(meta.decrypt(new_meta, formats.ACCOUNT_STORAGE_ORDINAL),
                      formats.ACCOUNT_STORAGE_ORDINAL)
    if not info.valid or (info.size_decompressed, info.size_disk) != (len(new_data),) * 2:
        raise ValueError("the account meta does not read back with the new size")
    return new_data, new_meta


_account_of = opjournal.account_ref


def write_unlock(ctx: OpContext, plan: AccountPlan,
                 confirmed: Iterable[str] = frozenset()) -> OpOutcome:
    """Write ``plan``. Returns an outcome for every result; raises only ``TargetError`` (an
    account no write may use)."""
    if plan.blocked:
        return refused(None, plan.blocked[0].code, plan.blocked[0].text)
    try:
        with opjournal.liveop_lock(ctx):
            return _write(ctx, plan, frozenset(confirmed))
    except opjournal.LiveOpBusy:
        return refused(None, "BUSY", "Another operation is still running. Try again in a moment.")


def _write(ctx: OpContext, plan: AccountPlan, confirmed: frozenset[str]) -> OpOutcome:
    gate = vault_problem(ctx)
    if gate is not None:
        return refused(None, gate.code, gate.text)
    game = _game_problem(ctx, confirmed)
    if game is not None:
        return refused(None, game.code, game.text)
    pending = recovery_pending(ctx, [plan.account])     # any open op there (R22-3)
    if pending is not None:
        return refused(None, pending.code, pending.text)
    acct = resolve_account(ctx, plan.account)
    files = _read(acct.folder)
    if files is None or (_sha(files[0]), _sha(files[1])) != plan.pre:
        return OpOutcome(None, "stale", "The account data changed since you opened this. "
                         "Please check again.", (), None, "n/a", None, "STALE")
    data, old_meta = files
    try:
        new_data, new_meta = _built(plan, data, old_meta, int(ctx.clock()))
    except (ValueError, KeyError, IndexError, struct.error, savepatch.PatchRefused,
            savejson.SaveDecodeError) as exc:
        return refused(None, "ACCOUNT_DATA_UNREADABLE", f"The account data cannot be changed "
                       f"({exc}). Nothing was changed.")
    op_id = new_op_id(ctx.clock)
    ref = {**source_to_json(acct.account)}
    if not acct.account.instance:      # where the folder is, as a slot's write says (R8-4)
        ref["at"] = instance_of(acct.folder)
    shas = {DATA: {"pre": plan.pre[0], "post": _sha(new_data)},
            META: {"pre": plan.pre[1], "post": _sha(new_meta)}}
    try:
        opjournal.append(ctx, op_id, "preparing", {
            "kind": KIND, "app": ctx.app(), "targets": [], "account": ref, "note": plan.what,
            "adds": [list(a) for a in plan.adds], "undo_of": None,
            "confirmed": sorted(confirmed), "plan_digest": ""})
    except (OrganizerBusy, StoreReadOnly) as exc:      # review R22-4
        return refused(None, "BUSY", f"The vault is busy. Try again. ({exc})")
    except OSError as exc:
        return refused(None, "VAULT_WRITE_FAILED", f"The vault could not record the change "
                       f"({exc}). Nothing was changed.")
    try:
        snapshot = ctx.versions.add_from_live(
            acct.folder, platform=acct.account.platform, account_key=acct.account.account_key,
            kind="snapshot", label=f"auto: {KIND}", scope=CaptureScope(), op_id=op_id,
            reason=KIND, app_version=ctx.app())
        from .panic import account_files
        kept = account_files(ctx, snapshot.id)
        if (_sha(kept.get(DATA, b"")), _sha(kept.get(META, b""))) != plan.pre:
            raise StoreError("the snapshot does not hold the account data as it is")
    except (StoreError, OSError) as exc:
        append_quietly(ctx, op_id, "abandoned", {"reason": "snapshot_failed"})
        return refused(op_id, "SNAPSHOT_FAILED", f"The safety snapshot failed ({exc}). Nothing "
                       f"was changed.")
    try:
        opjournal.append(ctx, op_id, "writing", {"targets": [
            {"account": ref, "snapshot_id": snapshot.id, "files": shas}]})
    except (OrganizerBusy, StoreReadOnly) as exc:
        append_quietly(ctx, op_id, "abandoned", {"reason": "not_started"})
        return refused(op_id, "BUSY", f"The vault is busy. Try again. ({exc})")
    except OSError as exc:
        append_quietly(ctx, op_id, "abandoned", {"reason": "not_started"})
        return refused(op_id, "VAULT_WRITE_FAILED", f"The vault could not record the change "
                       f"({exc}). Nothing was changed.")
    opjournal.write_marker(ctx, op_id, [plan.label])
    game = _game_problem(ctx, confirmed)
    if game is not None or _read(acct.folder) != files:
        append_quietly(ctx, op_id, "abandoned",
                       {"reason": "game_started" if game is not None else "stale"})
        opjournal.remove_marker(ctx, op_id)
        if game is not None:
            return refused(op_id, game.code, game.text)
        return OpOutcome(op_id, "stale", "The account data changed since you opened this. "
                         "Please check again.", (), None, "n/a", None, "STALE")
    try:
        safety.atomic_write_bytes(acct.folder / DATA, new_data)
        at(ctx.faults, "account_data_written")
        safety.atomic_write_bytes(acct.folder / META, new_meta)
        at(ctx.faults, "account_meta_written")
        if _read(acct.folder) != (new_data, new_meta):
            raise OSError("the account files do not read back as written")
    except OSError as exc:
        back = put_back(ctx, op_id, snapshot.id, acct.folder, plan.pre)
        return OpOutcome(op_id, "rolled_back" if back else "failed",
                         f"The account data could not be written ({exc}). "
                         + ("It is as it was." if back else "Put it back from the vault."),
                         (), None, "n/a", str(exc), "WRITE_FAILED")
    finish(ctx, op_id)
    n = len(plan.adds)
    return OpOutcome(op_id, "done", f"{n} unlock{'s' if n != 1 else ''} added to {plan.label}'s "
                     f"account data.", (), None, "n/a", None)


def finish(ctx: OpContext, op_id: str, *, validated: bool = False) -> None:
    """The op's end, when both files are as written (``validated``: said so already)."""
    if not validated:
        append_quietly(ctx, op_id, "validated", {"targets": []})
    append_quietly(ctx, op_id, "done", {})
    opjournal.remove_marker(ctx, op_id)


def put_back(ctx: OpContext, op_id: str, snapshot_id: str, folder: Path,
             pre: tuple[str, str]) -> bool:
    """The snapshot's two account files over the live ones; whether they are as they were."""
    from .panic import account_files
    kept = account_files(ctx, snapshot_id)
    try:
        for name in (DATA, META):
            if name in kept:
                safety.atomic_write_bytes(folder / name, kept[name])
    except OSError:
        append_quietly(ctx, op_id, "failed", {"reason": "rollback_failed"})
        return False
    now = _read(folder)
    if now is None or (_sha(now[0]), _sha(now[1])) != pre:
        append_quietly(ctx, op_id, "failed", {"reason": "rollback_failed"})
        return False
    append_quietly(ctx, op_id, "rolled_back", {})
    opjournal.remove_marker(ctx, op_id)
    return True


# --- recovery ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stopped:
    """An account write that stopped part-way, as recovery sees it."""

    folder: Path | None
    snapshot_id: str
    pre: tuple[str, str]
    state: str                          # pre / post / torn / other / unusable / unavailable
    problem: str = ""                   # unavailable: why its folder cannot be written now


def stopped(ctx: OpContext, rec: opjournal.OpRecord) -> Stopped:
    writing = rec.last("writing")
    try:
        (t,) = writing["targets"]
        files = t["files"]
        pre = (files[DATA]["pre"], files[META]["pre"])
        post = (files[DATA]["post"], files[META]["post"])
        snapshot_id = t["snapshot_id"]
        account = _account_of(rec)
        if account is None:
            raise ValueError("no account")
        folder = resolve_account(ctx, account).folder
    except (KeyError, TypeError, ValueError):
        return Stopped(None, "", ("", ""), "unusable")
    except TargetError as exc:          # the folder, for now: it waits (review R22-1)
        return Stopped(None, "", ("", ""), "unavailable", exc.detail)
    now = _read(folder)
    shas = (_sha(now[0]), _sha(now[1])) if now is not None else ("", "")
    if shas == pre:
        state = "pre"
    elif shas == post:
        state = "post"
    elif shas[0] in pre + post and shas[1] in pre + post:
        state = "torn"
    else:
        state = "other"
    return Stopped(folder, snapshot_id, pre, state)
