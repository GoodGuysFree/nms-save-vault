"""The panic restore (D5 4.3): today's whole-folder undo and restore, as a live operation.

A snapshot or full backup of one live account goes back slot by slot, byte for byte (metas
included: it restores a recorded state, so nothing is stamped), as one op of kind
``panic_restore``. Every slot is preserved first and a fresh store snapshot is taken, so the
restore is itself undoable (the newest snapshot of the folder is that fresh one). Slots the
source does not hold are left alone and named in the outcome; nothing is ever deleted. The
account data stays the live account's own, and ``steam_autocloud.vdf`` is never touched.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .. import formats, msstore, safety, savedir
from ..formats import MAX_SAVE_SLOTS
from ..organizer.locator import Location
from ..organizer.model import EntryRef, LiveRef, StoreRef
from ..store import listing
from .context import OpContext
from .errors import SourceUnreadable
from .execute import execute
from .model import OpOutcome, PairSource, PlacePlan, Warning
from .planner import _plan, _refusal, durable, warning
from .slots import read_account
from .source import load_source
from .targets import resolve_account, resolve_target

SNAPSHOT_KINDS = ("snapshot", "full")
WHOLE_KINDS = ("snapshot", "full", "imported", "inplace")


@dataclass(frozen=True)
class Candidate:
    """A snapshot or full backup that may be restored into one live account."""

    id: str
    kind: str
    label: str
    created: str


def candidates(ctx: OpContext, account: LiveRef,
               kinds: tuple[str, ...] = ("snapshot",)) -> list[Candidate]:
    """The entries of ``kinds`` (default: the safety snapshots) taken from ``account``'s
    folder, newest first (P4: never another folder's, never another platform's)."""
    store = ctx.versions
    out = []
    for e in listing.list_vault_entries(ctx.vault, store, live_roots=[]):
        if e.kind in kinds and e.platform == account.platform and \
                e.source == account.account_key:
            out.append(Candidate(e.id, e.kind, e.label, e.created))
    return list(reversed(out))


def plan_panic_restore(ctx: OpContext, account: LiveRef, snapshot_id: str | None = None, *,
                       restore_accountdata: bool = False) -> PlacePlan:
    """The plan to put a whole snapshot or full backup back: ``snapshot_id``, else the newest
    snapshot or full backup taken of this folder. Raises TargetError for an account no write
    may use."""
    acct = resolve_account(ctx, account)
    rt1 = resolve_target(ctx, account.target(1))
    none = PairSource(None, None)
    if snapshot_id is None:
        found = candidates(ctx, account)
        if not found:
            return _refusal(ctx, rt1, "panic_restore", warning("NO_SNAPSHOT"), none)
        snapshot_id = found[0].id
    entry = listing.get_vault_entry(ctx.vault, ctx.versions, snapshot_id, live_roots=[])
    if entry is None or entry.kind not in WHOLE_KINDS or entry.platform != account.platform:
        return _refusal(ctx, rt1, "panic_restore", warning("SNAPSHOT_NOT_THIS_FOLDER"), none)
    foreign = account_mismatch(ctx, entry, account)
    if foreign is not None:
        return _refusal(ctx, rt1, "panic_restore", foreign, none)
    if restore_accountdata and account.platform == "xbox":
        return _refusal(ctx, rt1, "panic_restore", warning("ACCOUNTDATA_XBOX"), none)
    now = read_account(ctx, acct)
    sources = entry_pairs(ctx, snapshot_id, account)
    if not sources:
        return _refusal(ctx, rt1, "panic_restore", warning("SNAPSHOT_EMPTY"), none)
    parts, warnings, left = [], [], []
    for slot_no in range(1, MAX_SAVE_SLOTS + 1):
        source = sources.get(slot_no)
        if source is None:
            if now.slots[slot_no].present:
                left.append(slot_no)
            continue
        rt = resolve_target(ctx, account.target(slot_no))
        slot = now.slots[slot_no]
        try:
            src = load_source(ctx, source)
        except SourceUnreadable:
            return _refusal(ctx, rt, "panic_restore", warning("SOURCE_UNREADABLE"), source)
        if any(not durable(ctx, m.vid) for m in slot.present):
            warnings.append(warning("NOT_IN_VAULT"))
        parts.append((rt, now, slot, src, source))
    if left:
        warnings.append(warning("SLOTS_LEFT", slots=", ".join(str(n) for n in left)))
    if restore_accountdata:
        warnings.append(warning("ACCOUNTDATA_ROLLBACK"))
    outcome = f"Restore the whole folder from '{entry.label or entry.id}'"
    return _plan(ctx, "panic_restore", parts, warnings, outcome, source=None)


def panic_restore(ctx: OpContext, account: LiveRef, snapshot_id: str | None = None, *,
                  restore_accountdata: bool = False, confirmed: Iterable[str] = (),
                  progress=None) -> tuple[PlacePlan, OpOutcome | None]:
    """Plan and run the restore. Returns the plan and the outcome (None when the plan is
    blocked: nothing was started). The caller names the confirmations it answered."""
    plan = plan_panic_restore(ctx, account, snapshot_id, restore_accountdata=restore_accountdata)
    if plan.blocked:
        return plan, None
    outcome = execute(ctx, plan, frozenset(confirmed), progress)
    if outcome.status == "done" and restore_accountdata:
        _restore_accountdata(ctx, plan, account, snapshot_id)
    return plan, outcome


def _restore_accountdata(ctx: OpContext, plan: PlacePlan, account: LiveRef,
                         snapshot_id: str | None) -> None:
    """Steam: the snapshot's account data over the live account's (the owner asked)."""
    folder = resolve_account(ctx, account).folder
    for name, data in account_files(ctx, snapshot_id or "").items():
        safety.atomic_write_bytes(folder / name, data)


def account_files(ctx: OpContext, entry_id: str) -> dict[str, bytes]:
    """The account data files an entry holds (Steam), by name."""
    out: dict[str, bytes] = {}
    names = (formats.ACCOUNT_DATA_NAME, formats.ACCOUNT_META_NAME)
    entry = listing.get_vault_entry(ctx.vault, ctx.versions, entry_id, live_roots=[])
    if entry is None:
        return out
    with listing.open_entry_folder(ctx.vault, ctx.versions, entry, slots=[]) as folder:
        for name in names:
            if (Path(folder) / name).is_file():
                out[name] = (Path(folder) / name).read_bytes()
    return out


def entry_pairs(ctx: OpContext, entry_id: str, account: LiveRef) -> dict[int, PairSource]:
    """The pair each slot of a snapshot holds, readable later through the locator (so a
    crashed restore can be finished): by version store entry or by catalog entry."""
    store = ctx.versions
    out: dict[int, PairSource] = {}
    if store is not None and store.has_entry(entry_id):
        entry = store.entry(entry_id)
        for slot in entry.slots:
            vids, where = {}, None
            for pos in ("A", "B"):
                m = entry.member(slot, pos)
                if m is not None and m.valid:
                    vids[pos] = m.vid
                    where = where or Location(m.vid, "store", StoreRef(entry_id), slot, pos,
                                              entry.platform, True)
            if vids:
                out[slot] = PairSource(vids.get("A"), vids.get("B"), None, where)
        return out
    entry = ctx.vault.get(entry_id)
    if entry is None or not entry.path:
        return out
    view = savedir.scan_any(Path(entry.path))
    platform = "xbox" if savedir.platform_of(Path(entry.path)) == "xbox" else "steam"
    for slot, sv in view.slots.items():
        vids, where = {}, None
        for pos, mv in zip(("A", "B"), sv.members):
            if mv.exists and mv.valid:
                vid = hashlib.sha256(Path(mv.data_path).read_bytes()).hexdigest()
                vids[pos] = vid
                where = where or Location(vid, "entry", EntryRef(entry_id), slot, pos,
                                          platform, True)
        if vids:
            out[slot] = PairSource(vids.get("A"), vids.get("B"), None, where)
    return out


def account_mismatch(ctx: OpContext, entry, account: LiveRef) -> Warning | None:
    """P7: an Xbox backup of another account's index is refused. A live index that cannot be
    read gives no verdict (a restore is how a damaged folder is repaired)."""
    if account.platform != "xbox":
        return None
    try:
        live = msstore.parse_containers_index(resolve_account(ctx, account).folder)[0].account_id
        with listing.open_entry_folder(ctx.vault, ctx.versions, entry, slots=[]) as folder:
            theirs = msstore.parse_containers_index(Path(folder))[0].account_id
    except (OSError, ValueError):
        return None
    return warning("FOREIGN_ACCOUNT") if theirs != live else None
