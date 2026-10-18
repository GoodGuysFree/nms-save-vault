"""The planner (D5 2.3): what "Move to live..." shows and what ``execute`` gets.

``eviction_picker`` lists the fifteen slots of one live account for a chosen version, with
what each holds, what writing there would do, the warnings and the suggested slot.
``plan_place`` and ``plan_promote`` build one operation's plan: the target's two writes
(F4a, F5b), what it evicts, the warnings and confirmations, the blocks, a fingerprint of
everything that may not change before ``execute``, and an estimate.

Read-only: no file is created or changed, not even the journal or the scan cache.
"""
from __future__ import annotations

import dataclasses
import hashlib
import shutil
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path

from .. import aliases, gameversion, msstore
from ..organizer.locator import LiveMember, Location
from ..organizer.model import LiveRef, LiveTarget
from ..store import CaptureScope, vault_mode
from ..store import capture as store_capture
from . import convert, crosssave, opjournal, pairmeta, steamcloud, transfer
from .context import OpContext
from .errors import SourceUnreadable, TargetError
from .model import (
    AccountChoice,
    CloudInfo,
    Estimate,
    EvictionPicker,
    Eviction,
    LiveAccount,
    MemberFacts,
    PairSource,
    PlacePlan,
    Position,
    SlotCandidate,
    SourceSummary,
    TargetPlan,
    Warning,
)
from .slots import AccountNow, SlotNow, read_account, slot_fingerprint
from .source import LoadedSource, load_source
from .steamwrite import file_names
from .targets import (
    ResolvedAccount,
    account_label,
    live_accounts,
    resolve_account,
    resolve_target,
    slot_label,
)

EXPEDITION_MODE = 6
XBOX_SYNC_SECONDS = 60
OWN_WRITE_SLACK = 2.0           # seconds a journal stamp may trail the index mtime
XBOX_QUOTA_WARN = 200 * 1024 * 1024
XBOX_QUOTA_MAX = 256 * 1024 * 1024
SPACE_FACTOR = 1.5
SPACE_MARGIN = 64 * 1024 * 1024
SYNC_PENDING = (msstore.BLOB_SYNC_MODIFIED, msstore.BLOB_SYNC_CREATED)
SYNC_NAMES = {1: "Synced", 2: "Modified", 3: "Deleted", 5: "Created"}

#: D5 2.3.3: severity and text of every warning code. ``{slot}`` is the slot's label.
TEXTS = {
    "NOT_IN_VAULT": ("info", "Not in the vault yet - it will be copied there first."),
    "EMPTY_SLOT": ("info", "Emptied outside the app - checked after the next launch."),
    "PLAY_TIME_AHEAD": ("confirm", "{slot} has {ahead} more play than this version. "
                        "That newer save stays in the vault."),
    "PINNED": ("warn", "You pinned this save."),
    "MIXED_SLOT": ("warn", "This slot holds two different saves. Both will be kept in the vault."),
    "PARTIAL_SLOT": ("warn", "Part of this slot is missing. What is there will be kept in the vault."),
    "UNREADABLE_MEMBER": ("warn", "One file in this slot cannot be read. "
                          "It will be kept in the vault as it is."),
    "PENDING_UPLOAD": ("info", "This slot has changes Xbox has not uploaded yet."),
    "EXPEDITION_ACTIVE": ("warn", "Expedition in progress. If the season ends while it is in "
                          "the vault, it becomes a normal save when you put it back."),
    "EXPEDITION_SOURCE": ("warn", "Expedition save. If its season has ended, the game turns it "
                          "into a normal save when you load it."),
    "TWIN_LIVE": ("confirm", "This save is already in {twin}. A second copy makes two separate "
                  "branches."),
    "LIVE_ELSEWHERE": ("info", "This save is also in {where}."),
    "ALREADY_LIVE": ("block", "This version is already in {slot}."),
    "ALREADY_LOADS": ("block", "That save already loads in {slot}."),
    "NOTHING_TO_PROMOTE": ("block", "{slot} has no readable save in that position."),
    "CROSS_PLATFORM_GATED": ("block", "Moving saves between Steam and Xbox is not available yet."),
    "CROSS_PLATFORM_EXPERIMENTAL": ("confirm", "Experimental - for validation runs. Moving a save "
                                    "between Steam and Xbox has not been proven yet. Use a "
                                    "throwaway save and keep your backups."),
    "CROSS_TARGET_NOT_EMPTY": ("block", "{slot} is not empty. For now a save goes to the other "
                               "platform only into an empty slot."),
    "CROSS_NEEDS_BACKUP": ("block", "Make a full backup of the destination folder first (it must "
                           "be less than 30 minutes old)."),
    "CONVERT_UNSUPPORTED": ("block", "This save cannot be converted for the other platform "
                            "with certainty, so it was not touched. Nothing was changed."),
    "UNSUPPORTED_META": ("block", "This save's file layout is not supported for the other "
                         "platform yet. Nothing was changed."),
    "SOURCE_OLD_FORMAT": ("block", "This save uses an older format. Load and save it once in "
                          "the game first."),
    "SOURCE_NO_ID": ("block", "This save has no readable identity, so it cannot go to the "
                     "other platform."),
    "OTHER_ACCOUNT": ("confirm", "This save comes from another account. Its bases may not be "
                      "editable until the game takes them over when you play it here."),
    "XGS_READ_ONLY": ("block", "This Xbox folder can only be read."),
    "GAME_RUNNING": ("block", "No Man's Sky is running. Close it first."),
    "GAME_STATE_UNKNOWN": ("confirm", "Could not check whether No Man's Sky is running. "
                           "Make sure it is closed."),
    "CROSS_SAVE_ON": ("warn", "Hello Games cross-save is on: the game may ask which copy to "
                      "keep."),
    "STEAM_CLOUD_OTHER_DEVICE": ("info", "If Steam asks at the next launch, choose the copy on "
                                 "this PC."),
    "XBOX_SYNC_BUSY": ("block", "Xbox is still syncing. Try again in a minute."),
    "XBOX_QUOTA_NEAR": ("warn", "Xbox cloud space would be nearly full."),
    "XBOX_QUOTA": ("block", "Xbox cloud space would be over the 256 MB limit."),
    "VAULT_UPGRADE_NEEDED": ("block", "Upgrade the vault first: Tools > Vault upgrade... "
                             "Nothing was changed."),
    "VAULT_UNAVAILABLE": ("block", "The vault is not reachable. Nothing was changed."),
    "VAULT_SPACE": ("block", "Not enough free disk space."),
    "SOURCE_UNREADABLE": ("block", "That version cannot be read from the vault."),
    "RECOVERY_PENDING": ("block", "An earlier move was interrupted. Resolve it first."),
    "RECOVERY_UNKNOWN": ("block", "Cannot tell whether an earlier move is waiting to be "
                         "recovered. Nothing was changed."),
    "STALE_SOURCE_MIXED": ("warn", "The saved slot held two different saves; only the chosen "
                           "one is used."),
    "SWAP_EMPTY_SIDE": ("block", "{slot} is empty, and the app never empties a slot. Use Move "
                        "to live to put a save into it."),
    "UNDO_UNAVAILABLE": ("block", "That move cannot be undone: it did not finish, or its record "
                         "is incomplete."),
    "UNDO_WOULD_EMPTY": ("block", "Undoing this would empty {slot}. The app never empties a "
                         "slot; see the steps to do it yourself."),
    "ALREADY_UNDONE": ("block", "{slot} already holds '{name}' again."),
    "UNDO_STALE": ("block", "{slot} has changed since (it now holds '{name}'). Use Move to "
                   "live to put '{was}' where you want it."),
    "PLAYED_SINCE": ("confirm", "You played {played} in {slot} since the move. That progress "
                     "stays in the vault as the newest version of '{name}'."),
    "NOTHING_TO_KEEP": ("block", "{slot} is empty: there is nothing to keep."),
    "EMPTY_SLOT_MAY_REFILL": ("info", "{refill}"),
    "GAME_NEWER": ("block", gameversion.GAME_NEWER_TEXT),
    "META_LAYOUT": ("block", "The save's file layout cannot be rewritten safely, so it was not "
                    "touched. Nothing was changed."),
    "NO_SNAPSHOT": ("block", "There is no snapshot or full backup of this folder to restore. "
                    "Nothing was changed."),
    "SNAPSHOT_NOT_THIS_FOLDER": ("block", "That snapshot was not taken of this folder. "
                                 "Nothing was changed."),
    "SNAPSHOT_EMPTY": ("block", "That snapshot holds no saves. Nothing was changed."),
    "FOREIGN_ACCOUNT": ("block", "That backup was taken from another Xbox account. Restoring "
                        "it would write that account's save index into this one. "
                        "Nothing was changed."),
    "SLOTS_LEFT": ("info", "Slots {slots} are empty in the snapshot and were left as they are."),
    "ACCOUNTDATA_XBOX": ("block", "Restoring an Xbox account's data is not available."),
    "ACCOUNTDATA_ROLLBACK": ("confirm", "This also restores the account data, which rolls back "
                             "unlocks and settings."),
}


def warning(code: str, **data: object) -> Warning:
    severity, template = TEXTS[code]
    text = template.format(**{k: v for k, v in data.items() if isinstance(v, str)})
    return Warning(code, severity, str(aliases.redact(text)), dict(data))  # type: ignore[arg-type]


def duration(seconds: int) -> str:
    hours, minutes = divmod(max(0, seconds) // 60, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


# --- the picker ------------------------------------------------------------------------


def eviction_picker(ctx: OpContext, source: PairSource,
                    account: LiveAccount | None = None) -> EvictionPicker:
    """What "Move to live..." renders for ``source``: one account's fifteen slots (2.3.1
    chooses which), each a candidate, and every other account with why it is not shown.
    Raises SourceUnreadable when the version cannot be read, TargetError when no live
    account of its platform is configured."""
    src = load_source(ctx, source)
    shown = account or _default_account(ctx, src)
    try:
        acct = resolve_account(ctx, shown)
        now = read_account(ctx, acct)
    except TargetError as exc:
        if exc.code != "XGS_READ_ONLY":
            raise
        acct, now = None, None
    plan_level = _plan_warnings(ctx, src, shown, now)
    blocked = next((w.code for w in plan_level if w.severity == "block"), "")
    candidates = []
    for slot in range(1, 16):
        target = shown.target(slot)
        if acct is None or now is None:
            candidates.append(_blocked_candidate(target, "XGS_READ_ONLY"))
            continue
        cand = _candidate(ctx, src, acct, now, now.slots[slot])
        if blocked and not cand.blocked:
            cand = _replace(cand, blocked=blocked)
        candidates.append(cand)
    slot, reason = suggest(candidates)
    candidates = [_replace(c, suggested=c.target.slot == slot,
                           suggest_reason=reason if c.target.slot == slot else "")
                  for c in candidates]
    cloud = CloudInfo(steam_cloud=None if shown.platform == "xbox" else
                      (acct is not None and acct.cloud_mode == "steam-cloud"),
                      cross_save_on=None if now is None else _cross_save(now),
                      xbox_sync_busy=_sync_busy(ctx, now))
    others = tuple(_account_choice(ctx, src, ref) for ref in live_accounts(ctx) if ref != shown)
    folders = [] if acct is None else [(acct.folder, shown.platform, s) for s in range(1, 16)]
    return EvictionPicker(_summary(src), shown, cloud, tuple(candidates), slot, others,
                          tuple(plan_level), fingerprint(folders, [source]))


def suggest(cands: Sequence[SlotCandidate]) -> tuple[int | None, str]:
    """D5 2.3.4: the line's own slot (most recently played), else the lowest empty slot,
    else the least recently played unpinned slot already in the vault, else any unpinned."""
    usable = [c for c in cands if not c.blocked]
    same = [c for c in usable if c.kind == "restore_older"]
    if same:
        return max(same, key=lambda c: (c.last_played, -c.target.slot)).target.slot, "same_line"
    empty = [c for c in usable if c.kind == "place_into_empty"]
    if empty:
        return min(c.target.slot for c in empty), "empty"
    pool = [c for c in usable if c.kind == "place" and not c.pinned]
    safe = [c for c in pool if c.durable]
    if safe:
        return min(safe, key=lambda c: (c.last_played, c.target.slot)).target.slot, \
            "in_vault_least_recent"
    if pool:
        return min(pool, key=lambda c: (c.last_played, c.target.slot)).target.slot, "least_recent"
    return None, ""


def _default_account(ctx: OpContext, src: LoadedSource) -> LiveRef:
    """D5 2.3.1: where the save is live on its platform, else the account it was captured
    from, else the first live account of its platform."""
    accounts = [r for r in live_accounts(ctx) if r.platform == src.platform]
    if not accounts:
        raise TargetError("UNKNOWN_ACCOUNT", "no live account of that platform is configured")
    line = _line(ctx, src.loading.pid)
    if line is not None:
        for badge in line.live:
            if badge.target.ref in accounts:
                return badge.target.ref
    if src.capture_account is not None:
        for ref in accounts:
            if _same_account(ref, src.capture_account):
                return ref
    return accounts[0]


def _account_choice(ctx: OpContext, src: LoadedSource, ref: LiveRef) -> AccountChoice:
    label = account_label(ctx, ref)
    cross = ref.platform != src.platform
    if cross and not transfer.gate_on(ctx):
        return AccountChoice(ref, label, "CROSS_PLATFORM_GATED", ())
    try:
        resolve_account(ctx, ref)
    except TargetError as exc:
        return AccountChoice(ref, label, exc.code, ())
    other = _other_account(src, ref)
    notes = ((warning("CROSS_PLATFORM_EXPERIMENTAL"),) if cross else ()) +         ((other,) if other else ())
    return AccountChoice(ref, label, "", notes)


def _blocked_candidate(target: LiveTarget, code: str) -> SlotCandidate:
    return SlotCandidate(target, "", "unreadable", None, None, None, None, "", 0, 0, False,
                         "none", False, False, False, "", "noop", "", (), code)


def _replace(cand: SlotCandidate, **changes) -> SlotCandidate:
    return dataclasses.replace(cand, **changes)


# --- one slot -------------------------------------------------------------------------


def _candidate(ctx: OpContext, src: LoadedSource, acct: ResolvedAccount, now: AccountNow,
               slot: SlotNow) -> SlotCandidate:
    label = _slot_label(acct, slot.target.slot)
    kind = classify(src, slot)
    head = slot.head
    kept = {m.vid: durable(ctx, m.vid) for m in slot.present}
    warnings = _slot_warnings(ctx, src, now, slot, kind, label, kept)
    blocked = next((w.code for w in warnings if w.severity == "block"), "")
    line = _line(ctx, head.pid) if head else None
    return SlotCandidate(
        target=slot.target, label=label, state=slot.state,
        a=_facts(slot.a, kept), b=_facts(slot.b, kept), newer=slot.newer,
        pid=head.pid if head else None,
        line_name=(line.name if line else head.meta.name) if head else "",
        play_time=head.meta.play_time if head else 0,
        last_played=head.meta.meta_ts if head else 0,
        durable=bool(slot.present) and all(kept.values()),
        vault_copy=_vault_copy(slot, kept), pinned=bool(line and line.pinned),
        same_line=kind == "restore_older", suggested=False, suggest_reason="",
        kind=kind, outcome=outcome_text(kind, label, src, slot), warnings=tuple(warnings),
        blocked=blocked)


def classify(src: LoadedSource, slot: SlotNow) -> str:
    """noop (the slot holds exactly the source pair), place_into_empty, restore_older (the
    slot's save is the source's playthrough) or place."""
    pair = (src.pair.a.vid if src.pair.a else None, src.pair.b.vid if src.pair.b else None)
    if slot.pair == pair:
        return "noop"
    if not slot.present:
        return "place_into_empty"
    if slot.head is not None and slot.head.pid == src.loading.pid:
        return "restore_older"
    return "place"


def outcome_text(kind: str, label: str, src: LoadedSource, slot: SlotNow) -> str:
    if kind == "place_into_empty":
        return f"Put into empty {label}"
    if kind == "restore_older":
        behind = slot.head is not None and src.play_time < slot.head.meta.play_time
        return f"{'Go back to' if behind else 'Use'} this version in {label}"
    if kind == "noop":
        return f"Already in {label}"
    return f"Swap into {label}"


def newer_source(src: LoadedSource) -> bool:
    """The source (either member) was written by a newer game version than the app knows."""
    return any(m is not None and gameversion.newer_plain(src.platform, m.meta_plain)
               for m in (src.pair.a, src.pair.b))


def newer_slot(slot: SlotNow) -> bool:
    """A save in the slot was written by a newer game version than the app knows."""
    return any(gameversion.newer(slot.target.platform, m.meta.meta_format, m.meta.base_version)
               for m in slot.present)


def _slot_warnings(ctx: OpContext, src: LoadedSource, now: AccountNow, slot: SlotNow,
                   kind: str, label: str, kept: dict) -> list[Warning]:
    out = []
    if newer_slot(slot):
        out.append(warning("GAME_NEWER"))
    head = slot.head
    if slot.target.platform != src.platform and transfer.gate_on(ctx) and (
            slot.present or transfer.slot_has_files(now.account.folder, slot.target.platform,
                                                    slot.target.slot)):
        out.append(warning("CROSS_TARGET_NOT_EMPTY", slot=label))
    if kind == "noop":
        out.append(warning("ALREADY_LIVE", slot=label))
    if any(not d for d in kept.values()):
        out.append(warning("NOT_IN_VAULT"))
    if slot.state == "empty":
        out.append(warning("EMPTY_SLOT"))
    if kind == "restore_older" and head is not None and head.meta.play_time > src.play_time:
        ahead = head.meta.play_time - src.play_time
        out.append(warning("PLAY_TIME_AHEAD", slot=label, ahead=duration(ahead), seconds=ahead))
    line = _line(ctx, head.pid) if head else None
    if line is not None and line.pinned:
        out.append(warning("PINNED"))
    state_code = {"mixed": "MIXED_SLOT", "partial": "PARTIAL_SLOT",
                  "unreadable": "UNREADABLE_MEMBER"}.get(slot.state)
    if state_code:
        out.append(warning(state_code))
    if any(m.sync_state in SYNC_PENDING for m in slot.present) or _steam_pending(ctx, now, slot):
        out.append(warning("PENDING_UPLOAD"))
    if head is not None and head.meta.game_mode == EXPEDITION_MODE:
        out.append(warning("EXPEDITION_ACTIVE"))
    twins = [s for s in now.slots.values() if s.target != slot.target and s.head is not None
             and s.head.pid == src.loading.pid]
    if twins and kind != "noop":
        twin_label = _slot_label(now.account, twins[0].target.slot)
        out.append(warning("TWIN_LIVE", twin=twin_label, slot_number=twins[0].target.slot))
    return out


def _cross_save(now: AccountNow) -> bool | None:
    """The account's CrossSaves flag (D5 2.3.3); None when unknown."""
    return crosssave.cross_save_on(now.account.folder, now.account.account.platform)


def _steam_pending(ctx: OpContext, now: AccountNow, slot: SlotNow) -> bool:
    """Steam Cloud account whose remotecache.vdf says this slot's files are not uploaded
    (D5 2.12.2); False when the manifest is not found."""
    if now.account.cloud_mode != "steam-cloud" or not slot.present:
        return False
    manifest = steamcloud.account_manifest(ctx.host, now.account.account.account_key)
    names = [n for m in slot.present for n in file_names(slot.target.slot, m.position)]
    return steamcloud.upload_state(manifest, now.account.folder, names) == "pending"


def _plan_warnings(ctx: OpContext, src: LoadedSource, account: LiveRef,
                   now: AccountNow | None) -> list[Warning]:
    """The warnings of the operation as a whole (not of one slot)."""
    out = []
    if newer_source(src):
        out.append(warning("GAME_NEWER"))
    vault = vault_problem(ctx)
    if vault:
        out.append(vault)
    if account.platform != src.platform:
        out += _cross_platform_warnings(ctx, src, account)
    if now is None:
        out.append(warning("XGS_READ_ONLY"))
    game = game_warning(ctx)
    if game:
        out.append(game)
    pending = recovery_pending(ctx, [account])
    if pending is not None:
        out.append(pending)
    other = _other_account(src, account)
    if other:
        out.append(other)
    if src.game_mode == EXPEDITION_MODE:
        out.append(warning("EXPEDITION_SOURCE"))
    if src.pair.a and src.pair.b and src.pair.a.pid != src.pair.b.pid:
        out.append(warning("STALE_SOURCE_MIXED"))
    line = _line(ctx, src.loading.pid)
    if line is not None:
        elsewhere = [b.target for b in line.live if b.target.ref != account]
        if elsewhere:
            out.append(warning("LIVE_ELSEWHERE", where=_where(ctx, elsewhere[0]),
                               cross_platform=str(line.cross_platform)))
    if now is not None:
        if _cross_save(now) is True:
            out.append(warning("CROSS_SAVE_ON"))
        if now.account.cloud_mode == "steam-cloud":
            out.append(warning("STEAM_CLOUD_OTHER_DEVICE"))
        if _sync_busy(ctx, now):
            out.append(warning("XBOX_SYNC_BUSY"))
    return out


def _cross_platform_warnings(ctx: OpContext, src: LoadedSource, account: LiveRef) -> list[Warning]:
    """D5 3.5.1: the gate (G1) and the checks of the source (G2-G4). G5-G7 are the ordinary
    OTHER_ACCOUNT, XBOX_QUOTA and CROSS_SAVE_ON warnings of the plan."""
    if not transfer.gate_on(ctx):
        return [warning("CROSS_PLATFORM_GATED")]
    out = [warning("CROSS_PLATFORM_EXPERIMENTAL")]
    out += [warning(code) for code in transfer.source_problems(src)]
    if not transfer.fresh_backup(ctx, account.platform, account.account_key):
        out.append(warning("CROSS_NEEDS_BACKUP"))
    return out


def vault_problem(ctx: OpContext) -> Warning | None:
    """D5's gate: live operations need the vault reachable and in store mode (D3)."""
    root = Path(ctx.vault.root)
    if not root.is_dir():
        return warning("VAULT_UNAVAILABLE")
    if vault_mode(root) != "store" or ctx.versions is None:
        return warning("VAULT_UPGRADE_NEEDED")
    if getattr(ctx.versions, "read_only", False):
        return warning("VAULT_UNAVAILABLE")
    return None


def game_warning(ctx: OpContext) -> Warning | None:
    probe = ctx.game_probe()
    if probe is True:
        return warning("GAME_RUNNING")
    if probe is None:
        return warning("GAME_STATE_UNKNOWN")
    return None


def recovery_pending(ctx: OpContext, accounts: Iterable[LiveRef]) -> Warning | None:
    """Why no new live op may start on one of ``accounts`` (2.10): an open op of this machine
    there, or a journal that cannot be read (unknown is not "nothing pending")."""
    wanted = set(accounts)
    try:
        if any(t.ref in wanted for op in opjournal.open_ops(ctx) for t in op.targets):
            return warning("RECOVERY_PENDING")
    except OSError:
        return warning("RECOVERY_UNKNOWN")
    return None


def _other_account(src: LoadedSource, account: LiveRef) -> Warning | None:
    """F7b: one confirmation whenever the version was captured from another account (no
    ownership id is read); none when the capture account is unknown."""
    if src.capture_account is None or _same_account(src.capture_account, account):
        return None
    return warning("OTHER_ACCOUNT")


def _same_account(a: LiveRef, b: LiveRef) -> bool:
    return (a.platform, a.account_key) == (b.platform, b.account_key)


def _sync_busy(ctx: OpContext, now: AccountNow | None) -> bool:
    """A recent write to the Xbox index that is not this app's own last write."""
    if now is None or now.index_mtime is None:
        return False
    if ctx.clock() - now.index_mtime >= XBOX_SYNC_SECONDS:
        return False
    try:
        own = opjournal.last_activity(ctx, now.account.account)
    except OSError:
        own = None
    return own is None or now.index_mtime > own + OWN_WRITE_SLACK


# --- plans --------------------------------------------------------------------------------


def plan_place(ctx: OpContext, source: PairSource, target: LiveTarget) -> PlacePlan:
    """The plan to put ``source`` into ``target`` (place, place into empty, restore an older
    version). Raises TargetError for a target no write may use."""
    rt = resolve_target(ctx, target)
    acct = resolve_account(ctx, target.ref)
    try:
        src = load_source(ctx, source)
    except SourceUnreadable:
        return _refusal(ctx, rt, "place", warning("SOURCE_UNREADABLE"), source)
    now = read_account(ctx, acct)
    slot = now.slots[target.slot]
    kind = classify(src, slot)
    warnings = _plan_warnings(ctx, src, target.ref, now)
    warnings += _slot_warnings(ctx, src, now, slot, kind, rt.label,
                               {m.vid: durable(ctx, m.vid) for m in slot.present})
    if kind == "noop":
        kind = "place"
    return _plan(ctx, kind, [(rt, now, slot, src, source)], warnings,
                 outcome_text(classify(src, slot), rt.label, src, slot), source=_summary(src))


def plan_promote(ctx: OpContext, target: LiveTarget, position: Position) -> PlacePlan:
    """The plan to make ``position`` of a slot the member the game loads: the slot's own
    pair written back, both stamped, ``position`` now (D5 2.4)."""
    rt = resolve_target(ctx, target)
    acct = resolve_account(ctx, target.ref)
    now = read_account(ctx, acct)
    slot = now.slots[target.slot]
    member = slot.member(position)
    source = PairSource(slot.pair[0], slot.pair[1], position,
                        Location(member.vid if member else "", "live", target.ref,
                                 target.slot, position, target.platform, False))
    if member is None or not member.valid:
        return _refusal(ctx, rt, "promote", warning("NOTHING_TO_PROMOTE", slot=rt.label), source)
    if slot.newer == position:
        return _refusal(ctx, rt, "promote", warning("ALREADY_LOADS", slot=rt.label), source)
    try:
        src = load_source(ctx, source)
    except SourceUnreadable:
        return _refusal(ctx, rt, "promote", warning("SOURCE_UNREADABLE"), source)
    warnings = [w for w in _plan_warnings(ctx, src, target.ref, now)
                if w.code not in ("OTHER_ACCOUNT", "LIVE_ELSEWHERE")]
    if slot.state == "mixed":
        warnings.append(warning("MIXED_SLOT"))
    if any(m.sync_state in SYNC_PENDING for m in slot.present) or _steam_pending(ctx, now, slot):
        warnings.append(warning("PENDING_UPLOAD"))
    return _plan(ctx, "promote", [(rt, now, slot, src, source)], warnings,
                 f"Make the {'auto-save' if position == 'A' else 'restore point'} load in "
                 f"{rt.label}", source=_summary(src))


def plan_swap(ctx: OpContext, first: LiveTarget, second: LiveTarget) -> PlacePlan:
    """The plan to exchange two occupied slots of one platform (D5 2.4): each slot gets the
    other's pair in its captured positions, stamped now. A swap with an empty side would
    vacate a slot, which the app never does (F8): blocked."""
    if first == second:
        raise TargetError("BAD_TARGET", "a slot cannot be swapped with itself")
    rts = [resolve_target(ctx, t) for t in (first, second)]
    nows = [read_account(ctx, resolve_account(ctx, t.ref)) for t in (first, second)]
    slots = [now.slots[t.slot] for now, t in zip(nows, (first, second))]
    sources = [_live_pair(t, s) for t, s in zip((first, second), slots)]
    if first.platform != second.platform:
        return _refusal(ctx, rts[0], "swap", warning("CROSS_PLATFORM_GATED"), sources[1])
    for rt, slot, source in zip(rts, slots, sources):
        if not slot.present:
            return _refusal(ctx, rt, "swap", warning("SWAP_EMPTY_SIDE", slot=rt.label), source)
    try:
        srcs = [load_source(ctx, s) for s in sources]
    except SourceUnreadable:
        return _refusal(ctx, rts[0], "swap", warning("SOURCE_UNREADABLE"), sources[1])
    warnings = []
    for rt, now, src in zip(rts, nows, (srcs[1], srcs[0])):
        warnings += [w for w in _plan_warnings(ctx, src, rt.target.ref, now)
                     if w.code not in ("LIVE_ELSEWHERE", "OTHER_ACCOUNT")]
    if not _same_account(first.ref, second.ref):
        warnings.append(warning("OTHER_ACCOUNT"))
    for slot in slots:
        code = {"mixed": "MIXED_SLOT", "partial": "PARTIAL_SLOT"}.get(slot.state)
        if code:
            warnings.append(warning(code))
    parts = [(rts[0], nows[0], slots[0], srcs[1], sources[1]),
             (rts[1], nows[1], slots[1], srcs[0], sources[0])]
    return _plan(ctx, "swap", parts, warnings, f"Swap {rts[0].label} and {rts[1].label}",
                 source=None)


def plan_keep(ctx: OpContext, target: LiveTarget) -> PlacePlan:
    """The plan to copy one live slot into the vault, writing nothing live (D5 2.4.1). The
    game may run: the store refuses a file that changes while it is read."""
    rt = resolve_target(ctx, target)
    now = read_account(ctx, resolve_account(ctx, target.ref))
    slot = now.slots[target.slot]
    warnings = [warning("NOT_IN_VAULT") for m in slot.present if not durable(ctx, m.vid)][:1]
    blocked = [w for w in (vault_problem(ctx),) if w is not None]
    if not slot.present:
        blocked.append(warning("NOTHING_TO_KEEP", slot=rt.label))
    estimate = _estimate(ctx, [], [], 0)
    try:
        if shutil.disk_usage(ctx.vault.root).free < sum(m.meta.data_size for m in slot.present) \
                * SPACE_FACTOR + SPACE_MARGIN:
            blocked.append(warning("VAULT_SPACE"))
    except OSError:
        blocked.append(warning("VAULT_UNAVAILABLE"))
    evictions = tuple(_eviction(ctx, m) for m in slot.present)
    head = slot.head.meta.name if slot.head else ""
    return PlacePlan(
        plan_id=uuid.uuid4().hex, kind="keep",
        targets=(TargetPlan(target, rt.label, rt.cloud_mode, slot.state, (), evictions),),
        source=None, undo_of=None, warnings=tuple(warnings), confirmations=frozenset(),
        blocked=tuple(_unique(blocked)), outcome=f"Keep '{head or '<unnamed>'}' from {rt.label} "
        "in the vault", fingerprint=fingerprint([(rt.folder, target.platform, target.slot)], []),
        estimate=estimate, created_at=ctx.clock())


def _live_pair(target: LiveTarget, slot: SlotNow) -> PairSource:
    """A live slot's own pair as a source: its members in their positions, the loading one
    loading."""
    head = slot.newer or "A"
    vid = (slot.member(head).vid if slot.member(head) else "") or ""
    return PairSource(slot.pair[0], slot.pair[1], slot.newer,
                      Location(vid, "live", target.ref, target.slot, head, target.platform, False))


def _plan(ctx: OpContext, kind: str, parts, warnings: list[Warning], outcome: str, *,
          source: SourceSummary | None, undo_of: str | None = None) -> PlacePlan:
    """``parts``: per target (resolved target, account now, slot now, loaded source, its
    PairSource). Writes are computed for the plan's time; ``execute`` stamps its own."""
    stamp = int(ctx.clock())
    targets, members, write_bytes, folders = [], [], 0, []
    for rt, now, slot, src, pair_source in parts:
        writes = ()
        if src.platform == rt.target.platform or transfer.gate_on(ctx):
            try:
                written = transfer.adapt(src, rt.target.platform)
                planned = pairmeta.compute(written.pair, rt, stamp,
                                           stamped=kind != "panic_restore")
            except convert.ConvertUnsupported as exc:
                code = transfer.code_of(exc)
                return _refusal(ctx, parts[0][0], kind, warning(code), parts[0][4])
            except ValueError:
                return _refusal(ctx, parts[0][0], kind, warning("META_LAYOUT"), parts[0][4])
            writes = tuple(p.write for p in planned)
            write_bytes += sum(len(src.data[p.write.vid]) + len(p.meta_disk) for p in planned)
        evictions = tuple(_eviction(ctx, m) for m in slot.present)
        targets.append(TargetPlan(rt.target, rt.label, rt.cloud_mode, slot.state, writes,
                                  evictions, pair_source))
        members += [LiveMember(rt.target, m.position, m.vid) for m in slot.present]
        folders.append((rt.folder, rt.target.platform, rt.target.slot))
    estimate = _estimate(ctx, parts, members, write_bytes)
    space = _space_problem(ctx, parts, estimate)
    if space:
        warnings = list(warnings) + [space]
    if any(p[0].target.platform == "xbox" for p in parts):
        quota = _quota(parts, write_bytes)
        if quota:
            warnings = list(warnings) + [quota]
    if any(newer_slot(slot) or (src is not None and newer_source(src))
           for _rt, _now, slot, src, _ps in parts):
        warnings = list(warnings) + [warning("GAME_NEWER")]       # swap, promote, undo, restore
    warnings = _unique(warnings)
    blocked = tuple(w for w in warnings if w.severity == "block")
    confirmations = frozenset(w.code for w in warnings if w.severity == "confirm")
    return PlacePlan(
        plan_id=uuid.uuid4().hex, kind=kind, targets=tuple(targets), source=source,  # type: ignore[arg-type]
        undo_of=undo_of, warnings=tuple(w for w in warnings if w.severity != "block"),
        confirmations=confirmations, blocked=blocked, outcome=outcome,
        fingerprint=fingerprint(folders, [p[4] for p in parts]), estimate=estimate,
        created_at=ctx.clock())


def _refusal(ctx: OpContext, rt, kind: str, why: Warning, source: PairSource) -> PlacePlan:
    """A plan that only says why it cannot run."""
    target = TargetPlan(rt.target, rt.label, rt.cloud_mode, "unreadable", (), (), source)
    return PlacePlan(uuid.uuid4().hex, kind, (target,), None, None, (), frozenset(), (why,),  # type: ignore[arg-type]
                     why.text, fingerprint([(rt.folder, rt.target.platform, rt.target.slot)],
                                           [source]),
                     Estimate(0, 0, 0, 0.0), ctx.clock())


def fingerprint(folders: Sequence[tuple[Path, str, int]], sources: Sequence[PairSource]) -> str:
    """sha256 over every target slot's fingerprint and every source's ids and loading
    position: ``execute`` recomputes it and refuses a plan the world has moved past."""
    h = hashlib.sha256()
    for folder, platform, slot in folders:
        h.update(f"{platform}:{slot}:".encode("ascii"))
        h.update(slot_fingerprint(folder, platform, slot).encode("ascii"))
    for s in sources:
        h.update(f"|{s.vid_a}|{s.vid_b}|{s.load}".encode("ascii"))
    return h.hexdigest()


def _estimate(ctx: OpContext, parts, members: list[LiveMember], write_bytes: int) -> Estimate:
    preserver = getattr(ctx.locator, "preserver", None)
    preserve = 0
    if members and preserver is not None and hasattr(preserver, "space_needed_for_preserve"):
        try:
            preserve = preserver.space_needed_for_preserve(members)
        except Exception:  # noqa: BLE001 - an estimate; preserve itself checks for real
            preserve = 0
    snapshot = 0
    inline = getattr(ctx.versions, "inline_max", 8192)
    for folder in {p[0].folder for p in parts}:
        platform = "xbox" if (folder / "containers.index").is_file() else "steam"
        try:
            plan = store_capture.plan(folder, platform, kind="snapshot", scope=CaptureScope(),
                                      live=True)
            snapshot += plan.object_bytes(inline)
        except Exception:  # noqa: BLE001 - an estimate only
            pass
    return Estimate(preserve, snapshot, write_bytes, 1.0)


def _space_problem(ctx: OpContext, parts, estimate: Estimate) -> Warning | None:
    try:
        vault_free = shutil.disk_usage(ctx.vault.root).free
        live_free = min(shutil.disk_usage(p[0].folder).free for p in parts)
    except OSError:
        return warning("VAULT_UNAVAILABLE")
    need = (estimate.preserve_bytes + estimate.snapshot_bytes) * SPACE_FACTOR + SPACE_MARGIN
    if vault_free < need or live_free < 2 * estimate.write_bytes:
        return warning("VAULT_SPACE", needed=int(need))
    return None


def _quota(parts, write_bytes: int) -> Warning | None:
    for rt, now, slot, _src, _ps in parts:
        if rt.target.platform != "xbox":
            continue
        evicted = sum(m.meta.data_size for m in slot.present)
        after = now.total_bytes - evicted + write_bytes
        if after > XBOX_QUOTA_MAX:
            return warning("XBOX_QUOTA", bytes=after)
        if after > XBOX_QUOTA_WARN:
            return warning("XBOX_QUOTA_NEAR", bytes=after)
    return None


def _eviction(ctx: OpContext, m) -> Eviction:
    return Eviction(m.position, m.vid, m.pid, m.meta.name, m.meta.play_time, m.meta.meta_ts,
                    durable(ctx, m.vid))


def _facts(m, kept: dict) -> MemberFacts | None:
    if m is None:
        return None
    meta = m.meta
    return MemberFacts(m.vid, m.pid, meta.name, meta.play_time, meta.meta_ts, meta.game_mode,
                       meta.difficulty, meta.season, meta.data_size, kept.get(m.vid, False),
                       SYNC_NAMES.get(m.sync_state, "") if m.sync_state is not None else "")


def _vault_copy(slot: SlotNow, kept: dict) -> str:
    if not slot.present:
        return "none"
    n = sum(1 for v in kept.values() if v)
    return "durable" if n == len(kept) else "partial" if n else "none"


def durable(ctx: OpContext, vid: str) -> bool:
    try:
        return bool(ctx.locator.is_durable(vid))
    except Exception:  # noqa: BLE001 - shown as not kept; preserve will keep it first
        return False


def _summary(src: LoadedSource) -> SourceSummary:
    a, b = src.pair.a, src.pair.b
    return SourceSummary(str(aliases.redact(src.name)), src.loading.pid,
                         a.vid if a else None, b.vid if b else None, src.pair.load,
                         src.play_time, src.saved_at, src.game_mode, src.platform,
                         src.where_label)


def _line(ctx: OpContext, pid: str | None):
    if pid is None or ctx.readmodel is None:
        return None
    return ctx.readmodel.line(pid)


def _slot_label(acct: ResolvedAccount, slot: int) -> str:
    return slot_label(acct.account.platform, slot, acct.label)


def _where(ctx: OpContext, target: LiveTarget) -> str:
    try:
        return resolve_target(ctx, target).label
    except TargetError:
        return f"{'Steam' if target.platform == 'steam' else 'Xbox'} slot {target.slot}"


def _unique(warnings: Iterable[Warning]) -> list[Warning]:
    seen, out = set(), []
    for w in warnings:
        if w.code not in seen:
            seen.add(w.code)
            out.append(w)
    return out
