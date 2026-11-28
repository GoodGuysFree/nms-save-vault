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
#: A restore's account files written whole (the roll-back the owner asked for, or the backup's
#: given to a folder with none): journaled as an account write is, found and settled alike.
RESTORE_KIND = "account_restore"
#: Every kind of account-data op recovery settles.
KINDS = (KIND, RESTORE_KIND)
DATA, META = formats.ACCOUNT_DATA_NAME, formats.ACCOUNT_META_NAME
#: The account's lists an app may add to (UserSettingsData).
UNLOCK_LISTS = ("UnlockedSeasonRewards", "UnlockedTwitchRewards", "UnlockedPlatformRewards",
                "UnlockedSpecials", "UnlockedTitles")
#: A reward id as the game keeps one: letters, digits and _ - . after an optional ``^``.
_ID = re.compile(r"\^?[A-Za-z0-9_.\-]{1,63}")
_SIZE_AGAIN = 0x54                      # the size the game repeats (saveedit)


@dataclass(frozen=True)
class AccountPlan:
    """The plan to add ``adds`` ((list, id) pairs) to an account's data, or, undoing the
    unlock ``undo_of``, to take ``removes`` out. ``blocked`` says why it cannot be written;
    ``pre`` is the SHA-256 of each file it was planned from."""

    account: LiveRef
    label: str
    what: str
    adds: tuple[tuple[str, str], ...]
    pre: tuple[str, str]
    blocked: tuple[Warning, ...]
    removes: tuple[tuple[str, str], ...] = ()
    undo_of: str | None = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _shas_now(folder: Path) -> tuple[str, str]:
    """Each account file's sha as it is now; "" for one that is not there (or not read)."""
    out = []
    for name in (DATA, META):
        try:
            out.append(_sha((folder / name).read_bytes()))
        except OSError:
            out.append("")
    return out[0], out[1]


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


def unlock_of(ctx: OpContext, op_id: str) -> opjournal.OpRecord | None:
    """The finished unlock ``op_id`` (an account write that added ids, not an undo); None
    for any other op or one the journal does not hold."""
    rec = opjournal.read_ops(ctx).get(op_id)
    preparing = rec.last("preparing") if rec is not None else None
    if rec is None or rec.kind != KIND or rec.status != "done" or not isinstance(preparing, dict) \
            or preparing.get("undo_of") or not preparing.get("adds"):
        return None
    return rec


def plan_unlock_undo(ctx: OpContext, op_id: str) -> AccountPlan:
    """The plan to undo the unlock ``op_id`` (Savecraft, GGF 2026-11-19): the ids it added
    that the account still holds are taken out; whatever else the account holds, what it had
    before and what came since, stays. Raises ``TargetError`` for an account no write may
    use."""
    rec = unlock_of(ctx, op_id)
    account = opjournal.account_ref(rec) if rec is not None else None
    preparing = rec.last("preparing") if rec is not None else {}
    try:
        added = check_unlocks(_grouped(preparing.get("adds"))) if account is not None else None
    except ValueError:
        added = None
    if account is None or not added:
        blocked = (warning("UNLOCK_UNDO_UNKNOWN"),)
        return AccountPlan(account, "", "", (), ("", ""), blocked, undo_of=op_id)
    if undone(ctx, op_id):              # an id it added that is back since is not its (R34-2)
        return AccountPlan(account, "", "", (), ("", ""), (warning("UNLOCK_UNDONE"),),
                           undo_of=op_id)
    acct = resolve_account(ctx, account)
    label = account_label(ctx, account)
    what = f"undo: {preparing.get('note') or 'an unlock'}"
    files = _read(acct.folder)
    if files is None:
        return AccountPlan(acct.account, label, what, (), ("", ""),
                           (warning("NO_ACCOUNT_DATA"),), undo_of=op_id)
    data, _meta = files
    pre = (_sha(data), _sha(_meta))
    try:
        settings = _settings(data) if _meta_ok(_meta) else None
    except (savejson.SaveDecodeError, ValueError):
        settings = None
    if settings is None:
        return AccountPlan(acct.account, label, what, (), pre,
                           (warning("ACCOUNT_DATA_UNREADABLE"),), undo_of=op_id)
    removes = tuple((name, rid) for name, rid in added
                    if isinstance(settings.get(savepatch._k(name)), list)
                    and rid in settings[savepatch._k(name)])
    blocked = () if removes else (warning("NOTHING_TO_UNDO_UNLOCK"),)
    return AccountPlan(acct.account, label, what, (), pre, blocked, removes=removes,
                       undo_of=op_id)


@dataclass(frozen=True)
class AccountChange:
    """One account write of an app that went through: an unlock (``adds``, (list, id)
    pairs), or the undo of the unlock ``undo_of`` (``removes``)."""

    op_id: str
    at: int                             # UTC unix, when it was done
    app: str                            # its stamp, "<name> <version>"
    platform: str
    account_key: str                    # the folder's name: never shown raw
    note: str                           # what the app called it
    adds: tuple[tuple[str, str], ...] = ()
    removes: tuple[tuple[str, str], ...] = ()
    undo_of: str | None = None


def changes_by(ops: Mapping[str, opjournal.OpRecord], app: str) -> list[AccountChange]:
    """Every account write ``app`` made that went through, oldest first (review R34-3: an
    app whose own record of one was lost tells from these that it went through)."""
    from ..appinfo import app_of
    out = []
    for rec in ops.values():
        preparing = rec.last("preparing") or {}
        done = rec.last("done")
        account = opjournal.account_ref(rec)
        if rec.kind != KIND or rec.status != "done" or done is None or account is None \
                or app_of(preparing.get("app")) != app:
            continue
        adds, removes = _pairs(preparing.get("adds")), _pairs(preparing.get("removes"))
        undo_of = preparing.get("undo_of")
        if adds is None or removes is None or not isinstance(undo_of, (str, type(None))):
            continue                    # a record this core does not read: never guessed
        note = preparing.get("note")
        out.append(AccountChange(rec.op_id, int(opjournal.parse_utc(done["utc"]) or 0),
                                 preparing.get("app") or "", account.platform,
                                 account.account_key, note if isinstance(note, str) else "",
                                 adds, removes, undo_of or None))
    return out


def _pairs(raw: object) -> tuple[tuple[str, str], ...] | None:
    """The journal's (list, id) pairs (none recorded: none); None for any other shape."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
            isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p)
            for p in raw):
        return None
    return tuple((p[0], p[1]) for p in raw)


def undone(ctx: OpContext, op_id: str) -> bool:
    """Whether an account write that went through undid the unlock ``op_id``."""
    return any(rec.kind == KIND and rec.status == "done"
               and (rec.last("preparing") or {}).get("undo_of") == op_id
               for rec in opjournal.read_ops(ctx).values())


def _grouped(pairs: object) -> dict[str, list[str]]:
    """The journal's (list, id) pairs as list -> ids; ValueError for any other shape."""
    if not isinstance(pairs, list):
        raise ValueError("no adds")
    out: dict[str, list[str]] = {}
    for pair in pairs:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)):
            raise ValueError("not a (list, id) pair")
        out.setdefault(pair[0], []).append(pair[1])
    return out


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
    held = {name: list(settings[savepatch._k(name)]) for name, _rid in plan.removes}
    steps = []
    for name, rid in plan.adds:
        steps.append([savepatch.Insert(("UserSettingsData", name), sizes[name],
                                       json.dumps(rid).encode("ascii"))])
        sizes[name] += 1
    for name, rid in plan.removes:      # an undo: each id out where it is now
        index = held[name].index(rid)
        steps.append([savepatch.Remove(("UserSettingsData", name, index))])
        del held[name][index]
    new_text = savepatch.apply_steps(text, savepatch.Steps(steps))
    new_data = new_text + nul + tail
    after = _settings(new_data)
    for name, rid in plan.adds:
        if after is None or rid not in after.get(savepatch._k(name), ()):
            raise ValueError("the account data does not read back with the unlocks")
    for name, rid in plan.removes:
        if after is None or after.get(savepatch._k(name)) != held[name]:
            raise ValueError("the account data does not read back with the unlocks taken out")
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
            "adds": [list(a) for a in plan.adds], "removes": [list(r) for r in plan.removes],
            "undo_of": plan.undo_of,
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
    if plan.removes:
        n = len(plan.removes)
        return OpOutcome(op_id, "done", f"{n} unlock{'s' if n != 1 else ''} taken out of "
                         f"{plan.label}'s account data.", (), None, "n/a", None)
    n = len(plan.adds)
    return OpOutcome(op_id, "done", f"{n} unlock{'s' if n != 1 else ''} added to {plan.label}'s "
                     f"account data.", (), None, "n/a", None)


def write_files(ctx: OpContext, account: LiveRef, files: Mapping[str, bytes],
                what: str) -> OpOutcome:
    """Write a restore's account files whole into ``account``'s folder (:data:`RESTORE_KIND`),
    under the live-op lock and journaled as an account write: a safety snapshot of the folder
    first (it holds the files as they are, or that there are none), the record of both files'
    before and after, then each file, and both checked. A write cut off part-way is found at
    the next start and finished or put back (:func:`stopped`); a failure puts it back now.
    Files ``files`` does not name are left as they are."""
    try:
        with opjournal.liveop_lock(ctx):
            return _write_files(ctx, account, files, what)
    except opjournal.LiveOpBusy:
        return refused(None, "BUSY", "Another operation is still running. Try again in a moment.")


def _write_files(ctx: OpContext, account: LiveRef, files: Mapping[str, bytes],
                 what: str) -> OpOutcome:
    acct = resolve_account(ctx, account)
    pre = _shas_now(acct.folder)
    post = tuple(_sha(files[n]) if n in files else was for n, was in zip((DATA, META), pre))
    if post == pre:
        return OpOutcome(None, "done", "The account data is already as wanted.", (), None,
                         "n/a", None)
    op_id = new_op_id(ctx.clock)
    ref = {**source_to_json(acct.account)}
    if not acct.account.instance:
        ref["at"] = instance_of(acct.folder)
    shas = {DATA: {"pre": pre[0], "post": post[0]}, META: {"pre": pre[1], "post": post[1]}}
    try:
        opjournal.append(ctx, op_id, "preparing", {
            "kind": RESTORE_KIND, "app": ctx.app(), "targets": [], "account": ref, "note": what,
            "undo_of": None, "confirmed": [], "plan_digest": ""})
    except (OrganizerBusy, StoreReadOnly) as exc:
        return refused(None, "BUSY", f"The vault is busy. Try again. ({exc})")
    except OSError as exc:
        return refused(None, "VAULT_WRITE_FAILED", f"The vault could not record the change "
                       f"({exc}). Nothing was changed.")
    try:
        snapshot = ctx.versions.add_from_live(
            acct.folder, platform=acct.account.platform, account_key=acct.account.account_key,
            kind="snapshot", label=f"auto: {RESTORE_KIND}", scope=CaptureScope(), op_id=op_id,
            reason=RESTORE_KIND, app_version=ctx.app())
        from .panic import account_files
        kept = account_files(ctx, snapshot.id)
        if tuple(_sha(kept[n]) if n in kept else "" for n in (DATA, META)) != pre:
            raise StoreError("the snapshot does not hold the account data as it is")
    except (StoreError, OSError) as exc:
        append_quietly(ctx, op_id, "abandoned", {"reason": "snapshot_failed"})
        return refused(op_id, "SNAPSHOT_FAILED", f"The safety snapshot failed ({exc}). Nothing "
                       f"was changed.")
    try:
        opjournal.append(ctx, op_id, "writing", {"targets": [
            {"account": ref, "snapshot_id": snapshot.id, "files": shas}]})
    except (OrganizerBusy, StoreReadOnly, OSError) as exc:
        append_quietly(ctx, op_id, "abandoned", {"reason": "not_started"})
        return refused(op_id, "VAULT_WRITE_FAILED", f"The vault could not record the change "
                       f"({exc}). Nothing was changed.")
    opjournal.write_marker(ctx, op_id, [account_label(ctx, account)])
    try:
        for name, point in ((DATA, "account_data_written"), (META, "account_meta_written")):
            if name in files:
                safety.atomic_write_bytes(acct.folder / name, files[name])
            at(ctx.faults, point)
        if _shas_now(acct.folder) != post:
            raise OSError("the account files do not read back as written")
    except OSError as exc:
        back = put_back(ctx, op_id, snapshot.id, acct.folder, pre)
        return OpOutcome(op_id, "rolled_back" if back else "failed",
                         f"The account data could not be written ({exc}). "
                         + ("It is as it was." if back else "Put it back from the vault."),
                         (), None, "n/a", str(exc), "WRITE_FAILED")
    finish(ctx, op_id)
    return OpOutcome(op_id, "done", f"{what}.", (), None, "n/a", None)


def finish(ctx: OpContext, op_id: str, *, validated: bool = False) -> None:
    """The op's end, when both files are as written (``validated``: said so already)."""
    if not validated:
        append_quietly(ctx, op_id, "validated", {"targets": []})
    append_quietly(ctx, op_id, "done", {})
    opjournal.remove_marker(ctx, op_id)


def put_back(ctx: OpContext, op_id: str, snapshot_id: str, folder: Path,
             pre: tuple[str, str]) -> bool:
    """The snapshot's two account files over the live ones; whether they are as they were. A
    file that was not there before (``pre`` ""), which the op wrote, is taken out again."""
    from .panic import account_files
    kept = account_files(ctx, snapshot_id)
    try:
        for name, was in zip((DATA, META), pre):
            if name in kept:
                safety.atomic_write_bytes(folder / name, kept[name])
            elif not was and (folder / name).is_file():
                (folder / name).unlink()
    except OSError:
        append_quietly(ctx, op_id, "failed", {"reason": "rollback_failed"})
        return False
    if _shas_now(folder) != pre:
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
    shas = _shas_now(folder)
    if shas == pre:
        state = "pre"
    elif shas == post:
        state = "post"
    elif shas[0] in pre + post and shas[1] in pre + post:
        state = "torn"
    else:
        state = "other"
    return Stopped(folder, snapshot_id, pre, state)
