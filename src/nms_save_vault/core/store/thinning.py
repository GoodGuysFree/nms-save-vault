"""Thinning the vault (docs/thinning.md): old backups deleted by a schedule, only when the owner
asks, after the list of what goes was shown. Deleting is the one thing the vault never did
before, so every rule here errs toward keeping.

A *version* is one save of one slot at one moment (a member's data, by its sha256) or an
account's data file. Its playthrough is the save's id, else its slot of its account. Per
playthrough the schedule keeps every version of the last 7 days, then one per day until 30
days, one per ISO week until about six months, one per month after: a pinned or noted version
when the period has one, else the newest; and always the newest of the playthrough. Pinned,
noted and named entries, what an operation or the whole-folder Undo still needs, and old-layout
folders still in the vault stay whatever their age. A version that stays keeps the newest
entry holding it, unless an entry that stays anyway holds it. Whole entries only: an entry goes
when nothing keeps it and none of its versions needs it.

Planning reads only. The run re-plans under the vault lock and deletes entry by entry: the
record first (``store/state/thinning.json``: thinning), the manifest, the objects no manifest
names any more, the catalog entry when the catalog lists one, then the record (thinned). An
interrupted run is finished by the next run or by :func:`finish_thinning`.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

from .. import catalog, safety
from ..catalog import CatalogEntry, Vault
from ..formats import ACCOUNT_DATA_NAME, CACHE_DIR
from ..organizer import store as orgstore
from ..organizer.model import is_pid, unknown_pid
from ..organizer.notes import Notes, NotesState, read_notes
from .errors import StoreBusy, StoreTooNew, StoreUnavailable, ThinError
from .listing import THINNING_JSON, catalog_vault_entry, epoch_utc, newest_record
from .manifest import ENTRY_SCHEMA, StoreEntry, is_entry_id
from .objects import is_digest
from .upgrade import upgrade_lock_path
from .versionstore import (
    STATE,
    STORE_VERSION,
    VersionStore,
    iso_utc,
    store_root,
    utc_now,
    vault_lock,
    vault_mode,
)

log = logging.getLogger(__name__)

DAY = 86400
RECENT = 7 * DAY            # every version
DAILY = 30 * DAY            # then one per day
WEEKLY = 183 * DAY          # then one per ISO week; after that one per month
THINNING_FORMAT = "nmsvault-thinning"
THINNING_SCHEMA = 1
ThinFault = Callable[[str], None]     # tests: raise at a named point to simulate a crash
#: How long a deletion waits out a file another program holds open (Windows) before the run
#: stops; the entry is then finished at the next start.
BUSY_WAIT_SECONDS = 2.0

# --- why an entry stays, in the owner's words -------------------------------------------------

NEWEST = "it holds the newest save of a playthrough"
RECENT_SAVE = "it holds a save from the last 7 days"
PERIOD = {"day": "it holds the save kept for that day",
          "week": "it holds the save kept for that week",
          "month": "it holds the save kept for that month"}
#: The schedule's own reasons; ``ThinPlan.kept`` lists the entries kept for any other.
SCHEDULE = frozenset({NEWEST, RECENT_SAVE, *PERIOD.values()})
UNKNOWN_TIME = "it holds a save whose date is not known"
PINNED = "you pinned it"
NAMED = "you named it"
NOTED = "you wrote a note on it"
VERSION_PINNED = "it holds a save you pinned"
VERSION_NOTED = "it holds a save you wrote a note on"
#: How long a change to the live saves that went through keeps what its undo needs; after that
#: the schedule decides about it like any other save (GGF 2026-11-20, thinning question 2:
#: "B"). A copy the owner asked the vault to keep stays however old.
OP_KEEP_DAYS = 30
OP_UNFINISHED = "an unfinished change to your live saves still needs it"
OP_UNREADABLE = "a change to your live saves whose record cannot be read names it"
OP_RESTORE = "the Undo of a whole-folder restore puts it back"
OP_PRESERVE = "it keeps what a change to your live saves replaced"
OP_KEEP = "you asked the vault to keep this copy"
OP_UNDO = "the Undo of a change to your live saves needs a save in it"
OP_ACCOUNT = "it holds the account data as it was before an app changed it"
UNDO_NEXT = "Undo of the whole folder would put it back next"
OLD_FOLDER = "its old backup folder is still in the vault (free the old folders first)"
OWN_FOLDER = "it is the vault's copy of a folder you keep yourself"
NEWER_APP = "a newer NMS Save Vault made it"
OTHER_FILE = ("it holds the copy kept of a file that is not a whole save (a save kept without "
              "its partner, or another file of the folder)")
_ORDER = (PINNED, NAMED, NOTED, VERSION_PINNED, VERSION_NOTED, OP_UNFINISHED, OP_UNREADABLE,
          OP_RESTORE, OP_PRESERVE, OP_KEEP, OP_UNDO, OP_ACCOUNT, UNDO_NEXT, OLD_FOLDER,
          OWN_FOLDER, NEWER_APP, OTHER_FILE, UNKNOWN_TIME, NEWEST, RECENT_SAVE,
          *PERIOD.values())
#: Entries whose versions count as the owner's own pick for their period.
_MARKS = frozenset({PINNED, NAMED, NOTED})

NOT_LISTED = "it was not in the list of what may go"
GONE = "it is no longer in the vault"

# --- the labels the apps write: never the owner's ---------------------------------------------
#
# Listed exactly, each from the code that writes it; any other label is the owner's.

_SLOT = r"(?:[1-9]|1[0-5])"
_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]*"


@functools.cache
def _app_labels() -> tuple[re.Pattern, ...]:
    from ..liveops.account import KIND as ACCOUNT_KIND
    from ..liveops.execute import SNAPSHOT_LABELS
    from ..liveops.model import PlanKind

    kinds = [k for k in get_args(PlanKind) if k not in SNAPSHOT_LABELS] + [ACCOUNT_KIND]
    return tuple(re.compile(p, re.S) for p in (
        # liveops.execute._snapshot ("auto: <plan kind>"), liveops.account ("auto: account_edit")
        "auto: (?:" + "|".join(map(re.escape, kinds)) + ")",
        # operations.snapshot_live as the releases before the live operations called it
        rf"auto: pre-restore of {_ID}", rf"auto: pre-promote slot {_SLOT}[AB]",
        rf"auto: pre-repopulate slot {_SLOT}", rf"auto: pre-clear slot {_SLOT}",
        *map(re.escape, SNAPSHOT_LABELS.values()),          # liveops.execute: a whole restore
        rf"kept: slot {_SLOT}",                             # store.locator: a preserve
        rf"kept: (?:Steam|GOG|Xbox) slot {_SLOT}(?: \(.*\))?",   # liveops.execute: a keep
    ))


def user_label(label: str, *, platform: str = "steam", names: Iterable[str | None] = (),
               slots: Iterable[int] = (), origin: Mapping[str, Any] | None = None) -> bool:
    """The owner wrote ``label``: it is not empty and not one an app writes for an entry of
    ``platform`` taken of a folder called one of ``names`` (its slots ``slots``, its
    ``origin``)."""
    from ..organizer.model import platform_name

    text = label.strip()
    if not text:
        return False
    names = [n for n in names if isinstance(n, str) and n]
    if text in names:            # operations.import_backup, and the GUI's and CLI's discover
        return False
    if any(p.fullmatch(text) for p in _app_labels()):
        return False
    if any(text == f"slot {s} from {n}" for s in slots for n in names):   # extract_slot
        return False
    if any(re.fullmatch(re.escape(platform_name(platform, n)) + r"\d{8}-\d{4}", text)
           for n in [*names, ""]):                  # gui: the label a backup is offered
        return False
    while isinstance(origin, Mapping):              # liveops.edit, also merged from elsewhere
        made_by, what = origin.get("made_by"), origin.get("what")
        if isinstance(made_by, str) and isinstance(what, str) and made_by and (
                text == f"edited by {made_by.split(' ')[0]}: {what}"[:120].strip()):
            return False
        origin = origin.get("source_origin")
    return True


def _entry_label_is_users(e: StoreEntry) -> bool:
    wanted = e.scope.get("slots")
    return user_label(e.label, platform=e.platform,
                      names=(e.source.get("name"), e.source.get("account_key"), e.account_key),
                      slots=wanted if isinstance(wanted, list) else e.slots, origin=e.origin)


def _catalog_label_is_users(c: CatalogEntry) -> bool:
    platform = catalog.Vault.tag_of(c.id)
    return user_label(c.label, platform=platform if platform in ("steam", "xbox") else "steam",
                      names=(c.source,), slots=[s.slot for s in c.slots])


# --- what the plan hands out ------------------------------------------------------------------


@dataclass(frozen=True)
class ThinSave:
    slot: int
    name: str


@dataclass(frozen=True)
class ThinCandidate:
    """An entry that may go, as the list shows it."""

    id: str
    kind: str
    created: str                       # local time, as the Backups list shows it
    created_utc: int
    label: str
    saves: tuple[ThinSave, ...]
    bytes: int                         # what deleting it gives back (alone)
    reason: str                        # why it may go, in plain words


@dataclass(frozen=True)
class ThinKept:
    """An entry the schedule alone would let go that stays, and why."""

    id: str
    kind: str
    created: str
    created_utc: int
    label: str
    saves: tuple[ThinSave, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ThinPlan:
    vault_root: Path
    now: datetime
    candidates: tuple[ThinCandidate, ...]     # oldest first
    kept: tuple[ThinKept, ...]
    entries: int                              # entries in the store
    bytes: int                                # freed when every candidate goes
    blocked: tuple[str, ...]                  # why it cannot run now; empty when it can
    objects: Mapping[str, frozenset[str]] = field(default_factory=dict, repr=False,
                                                  compare=False)
    object_sizes: Mapping[str, int] = field(default_factory=dict, repr=False, compare=False)
    manifest_sizes: Mapping[str, int] = field(default_factory=dict, repr=False, compare=False)

    def freed_bytes(self, entry_ids: Iterable[str]) -> int:
        """What deleting the candidates ``entry_ids`` gives back (the others stay): for the
        list's total as rows are ticked and unticked."""
        listed = {c.id for c in self.candidates}
        chosen = {i for i in entry_ids if i in listed}
        rest: set[str] = set()
        for entry_id, digests in self.objects.items():
            if entry_id not in chosen:
                rest |= digests
        gone = set().union(*(self.objects.get(i, frozenset()) for i in chosen)) - rest
        return (sum(self.object_sizes[d] for d in gone)
                + sum(self.manifest_sizes.get(i, 0) for i in chosen))


@dataclass(frozen=True)
class ThinProgress:
    done: int
    total: int
    current: str


@dataclass(frozen=True)
class ThinResult:
    thinned: tuple[str, ...]
    freed_bytes: int
    left: tuple[tuple[str, str], ...]         # (entry id, why it stayed)
    cancelled: bool
    message: str


@dataclass(frozen=True)
class ThinnedEntry:
    """An entry the owner thinned, as ``store/state/thinning.json`` records it."""

    id: str
    when: str                                 # UTC, ISO seconds
    why: str
    bytes: int
    kind: str
    label: str
    created_utc: str
    op_id: str | None
    done: bool                                # False: its deletion is still to be finished


# --- reading the vault (no writes) -------------------------------------------------------------

Key = tuple[str, str]            # ("m", member vid) or ("a", account data sha256)


@dataclass
class _Facts:
    entries: dict[str, StoreEntry]
    catalog: dict[str, CatalogEntry]
    vault: Vault
    notes: NotesState
    ops: dict[str, Any]
    blocked: list[str]


def _read_facts(root: Path, store: VersionStore, *, check: bool) -> _Facts:
    from ..liveops import opjournal

    blocked: list[str] = []
    if check:
        report = store.fsck()
        if not report.ok:
            blocked.append(f"The check of the vault found {len(report.problems)} problem(s); "
                           "nothing is thinned until it is repaired (nmsvault-cli store fsck "
                           "--repair).")
    if store.format.write_compat > STORE_VERSION:
        blocked.append("A newer NMS Save Vault upgraded this vault: update the app to thin it.")
    unseen = _unseen_manifests(store)
    if unseen:
        blocked.append(unseen)
    entries = {e.id: e for e in store.entries()}
    vault = Vault(root, read_only=True)
    known = {e.id: e for e in vault.entries}
    org = orgstore.OrganizerStore(root, create=False, read_only=True)
    notes = read_notes(org)
    if org.newer_schema():
        blocked.append("A newer NMS Save Vault wrote this vault's notes: update the app to thin "
                       "it.")
    elif any(orgstore.NOTES in p for p in org.problems):
        blocked.append("The vault's notes (organizer/notes.json) cannot be read, so what you "
                       "pinned is not known; nothing is thinned until they can.")
    elif notes.damaged:
        blocked.append(f"{len(notes.damaged)} of the vault's notes or pins (organizer/notes.json) "
                       "do not read, so what you meant to keep is not known; nothing is thinned "
                       f"until they are fixed or removed ({notes.damaged[0]}).")
    path = orgstore.store_root(root) / opjournal.OPS
    ops = dict(opjournal.read_ops_at(path))
    if opjournal.newer_records_at(path):
        blocked.append("A newer NMS Save Vault recorded changes to live saves here: update the "
                       "app to thin the vault.")
    return _Facts(entries, known, vault, notes, ops, blocked)


def _unseen_manifests(store: VersionStore, *, part_way: bool = False) -> str:
    """Why nothing may be deleted when a manifest the store lists is not one it could read
    (damaged, or not readable just now: a cloud file not on this machine, a file held open):
    what it names is not known. "" when every one was read. ``part_way``: said during a run,
    which may have deleted entries before this one."""
    unseen = sorted({*store.corrupt_manifests, *store.unreadable_manifests()})
    if not unseen:
        return ""
    names = ", ".join(n[:-5] for n in unseen[:3]) + (" ..." if len(unseen) > 3 else "")
    stop = "the rest is not thinned" if part_way else "nothing is thinned"
    return (f"{len(unseen)} backup(s) in the store cannot be read ({names}), so what they hold "
            f"is not known; {stop} until they can be (nmsvault-cli store fsck).")


def _require_every_manifest_read(store: VersionStore) -> None:
    unseen = _unseen_manifests(store, part_way=True)
    if unseen:
        raise ThinError("damaged", unseen)


# --- deciding what stays ----------------------------------------------------------------------


@dataclass
class _Decision:
    stay: dict[str, set[str]]           # entry id -> why it stays
    go: dict[str, str]                  # entry id -> why it may go
    catalog_listed: set[str]            # entries that may go and that the catalog lists
    schedule_alone: set[str]            # what the schedule alone keeps


def _decide(f: _Facts, now: datetime) -> _Decision:
    now_ts = now.timestamp()
    forced: dict[str, set[str]] = defaultdict(set)
    kept: dict[Key, set[str]] = defaultdict(set)
    holders: dict[Key, set[str]] = defaultdict(set)
    versions: dict[tuple, dict[Key, int]] = defaultdict(dict)
    keys_of: dict[str, set[Key]] = {}
    for e in f.entries.values():
        keys_of[e.id] = set()
        for m in e.members:
            key = ("m", m.vid)
            group = ("pid", m.pid) if m.pid else ("slot", e.platform, _account(e), m.slot)
            _version(versions[group], holders, keys_of[e.id], key, e.id, m.meta_ts)
        data = e.account.get("data")
        if isinstance(data, str) and e.has_file(data):
            fe = e.file(data)
            _version(versions[("account", e.platform, _account(e))], holders, keys_of[e.id],
                     ("a", fe.sha256), e.id, max(fe.mtime_ns, 0) // 1_000_000_000)
        for key in _other_files(e):        # each stays while it is in an entry that stays
            holders[key].add(e.id)
            keys_of[e.id].add(key)
            kept[key].add(OTHER_FILE)

    pins = f.notes.pinned_entries()
    for e in f.entries.values():
        if e.id in pins:
            forced[e.id].add(PINNED)
        if _entry_label_is_users(e):
            forced[e.id].add(NAMED)
        if e.schema > ENTRY_SCHEMA:
            forced[e.id].add(NEWER_APP)
    catalog_listed = _catalog_rules(f, forced)
    _op_rules(f, forced, kept, now_ts)
    _undo_walk(f, forced)

    marked: set[Key] = set()
    for note in f.notes.versions.values():
        why = (VERSION_PINNED if note.locked
               else VERSION_NOTED if note.label.strip() or note.comment.strip() else None)
        if why is not None:
            for vid in {note.head, *note.pair} - {None}:
                kept[("m", vid)].add(why)
                marked.add(("m", vid))
    for entry_id, why in forced.items():
        if why & _MARKS and entry_id in keys_of:
            marked |= keys_of[entry_id]

    # When the vault first had a version: of two saved at the same time (an edit keeps the
    # meta's time), the one kept later is the newer one.
    first_kept = {key: min(epoch_utc(f.entries[i].created_utc) for i in found)
                  for key, found in holders.items()}
    dropped: dict[Key, str] = {}        # key -> the period a newer (or chosen) one is kept for
    for times in versions.values():
        _schedule(times, first_kept, now_ts, now, marked, kept, dropped)

    def newest(ids: Iterable[str]) -> str:
        return max(ids, key=lambda i: (f.entries[i].created_utc, i))

    stay: dict[str, set[str]] = {i: set(why) for i, why in forced.items() if i in f.entries}
    held = set(stay)
    for key, why in kept.items():
        found = holders.get(key)
        if found:
            stay.setdefault(newest(found & held or found), set()).update(why)
    schedule_alone = {newest(holders[k]) for k, why in kept.items()
                      if why & SCHEDULE and holders.get(k)}
    go = {}
    for entry_id, keys in keys_of.items():
        if entry_id not in stay:
            go[entry_id] = _go_reason(keys, kept, dropped)
    return _Decision(stay, go, catalog_listed & set(go), schedule_alone)


def _version(times: dict[Key, int], holders: dict[Key, set[str]], keys: set[Key], key: Key,
             entry_id: str, when: int) -> None:
    times[key] = max(times.get(key, 0), when)
    holders[key].add(entry_id)
    keys.add(key)


def _other_files(e: StoreEntry) -> set[Key]:
    """The files of an entry that are neither a save nor the account's data: a save file kept
    without its partner, a file of the folder the game does not know. Each counts on its own: it
    may be the vault's only copy. A save's own files (data, meta, an Xbox save's folder), the
    account's, the Xbox index made of the saves and the game's thumbnail cache do not."""
    own: set[str] = set()
    folders: set[str] = set()
    paths = [p for m in e.members for p in (m.data, m.meta)]
    paths += [p for role, p in e.account.items() if role in ("data", "meta")]
    for path in paths:
        own.add(path)
        if "/" in path:
            folders.add(path.rsplit("/", 1)[0])
    if e.xbox is not None:
        own.add(str(e.xbox.get("index")))
    out: set[Key] = set()
    for fe in e.files:
        if (fe.path in own or fe.path.startswith(CACHE_DIR + "/")
                or ("/" in fe.path and fe.path.rsplit("/", 1)[0] in folders)):
            continue
        out.add(("f", fe.sha256))
    return out


def _account(e: StoreEntry) -> str:
    """The account a version without a playthrough id belongs to."""
    return str(e.account_key or e.source.get("account_key") or e.source.get("name")
               or e.source.get("folder") or "")


def _schedule(times: Mapping[Key, int], first_kept: Mapping[Key, int], now_ts: float,
              now: datetime, marked: set[Key], kept: dict[Key, set[str]],
              dropped: dict[Key, str]) -> None:
    """One playthrough's versions by the schedule. The newest is the latest saved, of those
    the one the vault kept last; versions that cannot be told apart so all count as newest."""
    known = {k: t for k, t in times.items() if t > 0}
    for key in times.keys() - known.keys():
        kept[key].add(UNKNOWN_TIME)            # no date: no period to thin it in
    if not known:
        return

    def newest(keys: Iterable[Key]) -> set[Key]:
        keys = list(keys)
        top = max((known[k], first_kept.get(k, 0)) for k in keys)
        return {k for k in keys if (known[k], first_kept.get(k, 0)) == top}

    for key in newest(known):
        kept[key].add(NEWEST)
    buckets: dict[tuple, list[Key]] = defaultdict(list)
    for key, when in known.items():
        age = now_ts - when
        if age < RECENT:
            kept[key].add(RECENT_SAVE)
            continue
        day = datetime.fromtimestamp(when, now.tzinfo).date()
        if age < DAILY:
            buckets[("day", day)].append(key)
        elif age < WEEKLY:
            buckets[("week", tuple(day.isocalendar())[:2])].append(key)
        else:
            buckets[("month", day.year, day.month)].append(key)
    for bucket, keys in buckets.items():
        tier = bucket[0]
        chosen: set[Key] = set()
        if not any(k in marked for k in keys):          # the owner's pick is that period's one
            chosen = newest(keys)
            for key in chosen:
                kept[key].add(PERIOD[tier])
        for key in keys:
            if key not in chosen:
                dropped.setdefault(key, tier)


_TIER_WORDS = {"day": "days", "week": "weeks", "month": "months"}


def _go_reason(keys: set[Key], kept: Mapping[Key, set[str]], dropped: Mapping[Key, str]) -> str:
    if not keys:
        return "it holds no saves"
    tiers = [t for t in ("day", "week", "month")
             if any(k not in kept and dropped.get(k) == t for k in keys)]
    parts = []
    if len(tiers) == 1:
        parts.append(f"a newer save from that {tiers[0]} is kept")
    elif tiers:
        parts.append("newer saves from those " + " and ".join(_TIER_WORDS[t] for t in tiers)
                     + " are kept")
    if any(k in kept for k in keys):
        parts.append(("every other save in it is also in a backup that stays" if parts
                      else "every save in it is also in a backup that stays"))
    return "; ".join(parts)


def _catalog_rules(f: _Facts, forced: dict[str, set[str]]) -> set[str]:
    """A store entry the catalog lists too (a fold not freed yet, a copy-in, a note on the old
    entry) stays unless its catalog entry is only a leftover; those are returned (their catalog
    entry goes with them)."""
    listed = set()
    for c in f.catalog.values():
        if c.id not in f.entries:
            continue
        if c.note.strip():
            forced[c.id].add(NOTED)
        if _catalog_label_is_users(c):
            forced[c.id].add(NAMED)
        if not c.managed or c.kind == catalog.KIND_INPLACE:
            forced[c.id].add(OWN_FOLDER)
        elif _old_folder(f.vault, c).exists():
            forced[c.id].add(OLD_FOLDER)
        else:
            listed.add(c.id)
    return listed


def _old_folder(vault: Vault, c: CatalogEntry) -> Path:
    subdir = {catalog.KIND_SNAPSHOT: vault.snapshots_dir,
              catalog.KIND_EXTRACT: vault.extracts_dir}.get(c.kind, vault.backups_dir)
    return subdir / c.id


def _op_time(rec) -> float | None:
    """When an op ended (its last event's time), None when that does not read."""
    event = rec.last("done") or (rec.events[-1] if rec.events else None)
    utc = event.get("utc") if isinstance(event, dict) else None
    try:
        return datetime.fromisoformat(str(utc).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _op_rules(f: _Facts, forced: dict[str, set[str]], kept: dict[Key, set[str]],
              now_ts: float) -> None:
    """What the operations of ``ops.jsonl`` still need: everything an unfinished one names;
    of one that went through in the last :data:`OP_KEEP_DAYS` days (or whose time does not
    read), the snapshot its undo restores, its preserves, the versions it evicted (its undo
    puts them back) and the account data it replaced; a keep op's copy, however old."""
    by_op: dict[str, set[str]] = defaultdict(set)
    for e in f.entries.values():
        if e.op_id:
            by_op[e.op_id].add(e.id)
    for rec in f.ops.values():
        status = rec.status
        left = (rec.last("abandoned") or {}).get("reason") == "user_left"
        if rec.open or status == "recovered" or (status == "abandoned" and left):
            for entry_id in _named(rec.events) | by_op.get(rec.op_id, set()):
                forced[entry_id].add(OP_UNFINISHED)
            for vid in _named_vids(rec.events):
                kept[("m", vid)].add(OP_UNDO)
        elif status == "done":
            ended = _op_time(rec)
            recent = ended is None or now_ts - ended <= OP_KEEP_DAYS * 86400
            try:
                _done_op(rec, forced, kept, recent=recent)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                log.warning("thinning: the record of op %s does not read (%s); what it names "
                            "stays", rec.op_id, exc)
                for entry_id in _named(rec.events) | by_op.get(rec.op_id, set()):
                    forced[entry_id].add(OP_UNREADABLE)


def _done_op(rec, forced: dict[str, set[str]], kept: dict[Key, set[str]], *,
             recent: bool = True) -> None:
    from ..liveops.account import KIND as ACCOUNT_KIND

    if rec.kind == "keep":
        entry_id = (rec.last("done") or {}).get("entry_id")
        if isinstance(entry_id, str):
            forced[entry_id].add(OP_KEEP)
        return
    if not recent:                      # older than OP_KEEP_DAYS: the schedule decides
        return
    writing = rec.last("writing")
    if writing is None:
        return
    for t in writing["targets"]:
        if rec.kind == ACCOUNT_KIND:
            pre = t["files"][ACCOUNT_DATA_NAME]["pre"]
            if is_digest(pre):
                kept[("a", pre)].add(OP_ACCOUNT)
            continue
        if rec.kind == "panic_restore" and isinstance(t.get("snapshot_id"), str):
            forced[t["snapshot_id"]].add(OP_RESTORE)
        for copy in t.get("preserved") or ():
            forced[copy["location"]["entry"]].add(OP_PRESERVE)
        for position in ("A", "B"):
            member = (t.get("pre") or {}).get(position)
            if member and is_digest(member["vid"]):
                kept[("m", member["vid"])].add(OP_UNDO)


def _named(value: object) -> set[str]:
    """Every entry id an op's events name: snapshots, kept entries and locations."""
    out: set[str] = set()
    if isinstance(value, dict):
        for key in ("snapshot_id", "entry_id"):
            if isinstance(value.get(key), str):
                out.add(value[key])
        if value.get("kind") in ("store", "entry") and isinstance(value.get("entry"), str):
            out.add(value["entry"])
        for item in value.values():
            out |= _named(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            out |= _named(item)
    return out


def _named_vids(value: object) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for key in ("vid", "vid_a", "vid_b"):
            if is_digest(value.get(key)):
                out.add(value[key])
        for item in value.values():
            out |= _named_vids(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            out |= _named_vids(item)
    return out


def _undo_walk(f: _Facts, forced: dict[str, set[str]]) -> None:
    """The snapshot NMS Save Vault's whole-folder Undo lands on next, for every live folder
    (liveops.panic.candidates): never deleted."""
    from ..liveops.panic import undo_skip_set

    skip = undo_skip_set(f.ops)
    newest: dict[tuple, tuple[tuple[int, str], str | None]] = {}

    def offer(group: tuple, when: int, entry_id: str, in_store: bool) -> None:
        if group not in newest or (when, entry_id) > newest[group][0]:
            newest[group] = ((when, entry_id), entry_id if in_store else None)

    for e in f.entries.values():
        if (e.kind != "snapshot" or e.id in skip or (e.op_id and e.op_id in skip)
                or (e.origin or {}).get("store_id")):
            continue
        folder = e.source.get("folder")
        group = (("folder", e.platform, folder) if folder else
                 ("key", e.platform, e.source.get("account_key") or e.source.get("name")))
        offer(group, epoch_utc(e.created_utc), e.id, True)
    for c in f.catalog.values():
        if c.id in f.entries or c.kind != catalog.KIND_SNAPSHOT or c.id in skip:
            continue
        listed = catalog_vault_entry(f.vault, c)
        offer(("key", listed.platform, listed.source), listed.created_utc, c.id, False)
    for _when, entry_id in newest.values():
        if entry_id is not None:
            forced[entry_id].add(UNDO_NEXT)


# --- planning ---------------------------------------------------------------------------------


def plan_thin(vault_root: str | Path, *, now: datetime | None = None) -> ThinPlan:
    """What thinning would delete now, why, and what stays that the schedule alone would let
    go. Nothing is written. ``now`` (default: now) sets the ages; its time zone the days,
    weeks and months (a naive one is local time)."""
    root = Path(vault_root)
    store = _open(root, read_only=True)
    when = _when(now)
    facts = _read_facts(root, store, check=True)
    return _plan(root, store, when, facts, _decide(facts, when))


def _when(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    return now if now.tzinfo is not None else now.astimezone()


def _open(root: Path, *, read_only: bool) -> VersionStore:
    return VersionStore.open(root, lock=vault_lock(root), live_roots=(), read_only=read_only)


def _plan(root: Path, store: VersionStore, now: datetime, f: _Facts, d: _Decision) -> ThinPlan:
    objects = {i: e.object_digests() for i, e in f.entries.items()}
    sizes = {fe.sha256: fe.size for e in f.entries.values() for fe in e.files
             if fe.inline is None}
    refs: dict[str, int] = defaultdict(int)
    for digests in objects.values():
        for digest in digests:
            refs[digest] += 1
    manifests = {i: store.manifest_size(i) for i in d.go}
    candidates = []
    for e in sorted((f.entries[i] for i in d.go), key=lambda e: (e.created_utc, e.id)):
        alone = sum(sizes[x] for x in objects[e.id] if refs[x] == 1) + manifests[e.id]
        candidates.append(ThinCandidate(e.id, e.kind, e.created_local, epoch_utc(e.created_utc),
                                        e.label, _saves(e), alone, d.go[e.id]))
    kept = []
    for e in sorted((f.entries[i] for i in set(d.stay) - d.schedule_alone),
                    key=lambda e: (e.created_utc, e.id)):
        reasons = tuple(r for r in _ORDER if r in d.stay[e.id] and r not in SCHEDULE)
        if reasons:
            kept.append(ThinKept(e.id, e.kind, e.created_local, epoch_utc(e.created_utc),
                                 e.label, _saves(e), reasons))
    plan = ThinPlan(root, now, tuple(candidates), tuple(kept), len(f.entries), 0,
                    tuple(f.blocked), objects, sizes, manifests)
    return _with_bytes(plan)


def _with_bytes(plan: ThinPlan) -> ThinPlan:
    from dataclasses import replace

    return replace(plan, bytes=plan.freed_bytes(c.id for c in plan.candidates))


def _saves(e: StoreEntry) -> tuple[ThinSave, ...]:
    out = []
    for slot in e.slots:
        record = newest_record(e, slot)
        out.append(ThinSave(slot, (record.name if record is not None else "") or "<unnamed>"))
    return tuple(out)


# --- running ----------------------------------------------------------------------------------


def run_thin(vault_root: str | Path, plan: ThinPlan, chosen_ids: Iterable[str], *,
             progress: Callable[[ThinProgress], None] | None = None,
             cancel=None, faults: ThinFault | None = None) -> ThinResult:
    """Delete the entries ``chosen_ids`` of ``plan`` (the rows the owner left ticked) that may
    still go when re-planned under the vault lock; any other is left and reported. An
    interrupted earlier run is finished first. ``cancel`` (an Event) stops between entries.
    Raises ThinError (busy, blocked, damaged), StoreTooNew, StoreUnavailable."""
    root = Path(vault_root)
    if os.path.normcase(os.path.abspath(plan.vault_root)) != os.path.normcase(
            os.path.abspath(root)):
        raise ThinError("other-vault", "That list was made for another vault.")
    chosen = list(dict.fromkeys(chosen_ids))
    store = _open(root, read_only=False)
    store._require_writable()
    with _held(root):
        _require_sound(store)
        before = _finish(root, store, faults)
        result = _run(root, store, plan, chosen, progress, cancel, faults)
    if before is None:
        return result
    thinned = before.thinned + result.thinned
    freed = before.freed_bytes + result.freed_bytes
    # an entry the earlier run's finish deleted is also listed (the plan was made while it was
    # pending): it reads as deleted only, never also as "no longer in the vault"; one kept
    # twice is said once
    left, seen = [], set(thinned)
    for entry_id, why in (*before.left, *result.left):
        if entry_id not in seen:
            seen.add(entry_id)
            left.append((entry_id, why))
    return ThinResult(thinned, freed, tuple(left), result.cancelled,
                      _message(thinned, freed, left, result.cancelled))


def _require_sound(store: VersionStore) -> None:
    """The quick check of the store, before anything is deleted or an interrupted run is
    finished."""
    report = store.fsck()
    if not report.ok:
        raise ThinError("damaged", f"The check of the vault found {len(report.problems)} "
                                   "problem(s); nothing was deleted. Repair it first "
                                   "(nmsvault-cli store fsck --repair).")


@contextmanager
def _held(root: Path) -> Iterator[None]:
    """No live write, no upgrade, undo of it, fold, freeing or other thinning meanwhile: the
    live-op lock (when the vault has an organizer), then the upgrade lock, both at once or not
    at all."""
    with ExitStack() as stack:
        live = orgstore.liveop_lock_path(root)
        try:
            if live.parent.is_dir():
                stack.enter_context(safety.exclusive_lock(live, timeout=0))
            stack.enter_context(safety.exclusive_lock(upgrade_lock_path(root), timeout=0))
        except (safety.LockTimeout, RuntimeError) as exc:
            raise ThinError("busy", "Another NMS Save Vault window (or Savecraft) is changing "
                                    "the vault or the live saves; try again when it is "
                                    "done.") from exc
        yield


@contextmanager
def _vault_locked(store: VersionStore) -> Iterator[None]:
    try:
        with store._lock():
            if vault_mode(store.vault_root) != "store":      # never recreate store/state/
                raise ThinError("blocked", "The vault is not in store mode any more.")
            yield
    except safety.LockTimeout as exc:
        raise StoreBusy() from exc


def _run(root: Path, store: VersionStore, plan: ThinPlan, chosen: list[str], progress, cancel,
         faults) -> ThinResult:
    listed = {c.id for c in plan.candidates}
    left = [(i, NOT_LISTED) for i in chosen if i not in listed]
    todo = [i for i in chosen if i in listed]
    statuses = _read_record(root)
    with _vault_locked(store):
        facts = _read_facts(root, store, check=False)
        if facts.blocked:
            raise ThinError("blocked", facts.blocked[0])
        decision = _decide(facts, plan.now)
        seen = _inputs(root)
        if todo:
            _write_record(root, statuses, "running")
    thinned: list[str] = []
    freed, cancelled = 0, False
    for n, entry_id in enumerate(todo):
        if cancel is not None and cancel.is_set():
            cancelled = True
            left += [(i, "not deleted: you stopped") for i in todo[n:]]
            break
        if progress is not None:
            progress(ThinProgress(n, len(todo), entry_id))
        with _vault_locked(store):
            if _inputs(root) != seen:        # a pin, a note, an operation since: decide again
                facts = _read_facts(root, store, check=False)
                if facts.blocked:
                    left += [(i, facts.blocked[0]) for i in todo[n:]]
                    break
                decision, seen = _decide(facts, plan.now), _inputs(root)
            if entry_id not in decision.go:
                left.append((entry_id, _why_kept(entry_id, decision)))
                continue
            freed += _thin_one(root, store, facts, facts.entries[entry_id],
                               decision.go[entry_id], entry_id in decision.catalog_listed,
                               statuses, faults)
            del facts.entries[entry_id]
            thinned.append(entry_id)
    if todo:
        with _vault_locked(store):
            _write_record(root, statuses, "done")
    if progress is not None:
        progress(ThinProgress(len(todo), len(todo), ""))
    return ThinResult(tuple(thinned), freed, tuple(left), cancelled,
                      _message(thinned, freed, left, cancelled))


def _why_kept(entry_id: str, decision: _Decision) -> str:
    why = decision.stay.get(entry_id)
    if why is None:
        return GONE
    return next((r for r in _ORDER if r in why), "it stays")


def _inputs(root: Path) -> tuple:
    """What the keep rules read besides the entries: a pin, a note, an operation or a catalog
    change since shows here (a few stats)."""
    org = orgstore.store_root(root)
    paths = (org / orgstore.NOTES, org / orgstore.HEADER, org / "ops.jsonl",
             root / "catalog.json", store_root(root) / STATE / "migration.json")
    out = []
    for path in paths:
        try:
            st = os.stat(path)
            out.append((st.st_size, st.st_mtime_ns, st.st_ino))
        except OSError:
            out.append(None)
    return tuple(out)


def _thin_one(root: Path, store: VersionStore, facts: _Facts, entry: StoreEntry, why: str,
              in_catalog: bool, statuses: dict, faults: ThinFault | None) -> int:
    """Delete one entry (the caller holds the vault lock): recorded first, then its manifest,
    its objects no other manifest names, its catalog entry. Returns the bytes freed."""
    _require_every_manifest_read(store)
    digests = sorted(entry.object_digests())
    elsewhere = set().union(*(e.object_digests() for e in facts.entries.values()
                              if e.id != entry.id))
    sizes = {fe.sha256: fe.size for fe in entry.files if fe.inline is None}
    freed = store.manifest_size(entry.id) + sum(sizes[d] for d in digests if d not in elsewhere)
    statuses[entry.id] = {"status": "thinning", "when": iso_utc(utc_now()), "why": why,
                          "bytes": freed, "kind": entry.kind, "label": entry.label,
                          "created_utc": entry.created_utc, "op_id": entry.op_id,
                          "objects": digests, "catalog": in_catalog}
    _write_record(root, statuses, "running")
    _fault(faults, "thin.after_record")
    _finish_one(root, store, entry.id, statuses, faults)
    return freed


def _finish_one(root: Path, store: VersionStore, entry_id: str, statuses: dict,
                faults: ThinFault | None) -> None:
    """The deletion of an entry recorded as thinning, from wherever it stopped."""
    record = statuses[entry_id]
    store._drop_manifest(entry_id, remove=_unlink_when_free)
    _fault(faults, "thin.after_manifest")
    _require_every_manifest_read(store)        # an unread manifest may name any of them
    named = set().union(*(e.object_digests() for e in store.entries()))
    for digest in record.get("objects") or ():
        if is_digest(digest) and digest not in named:
            _delete_object(store, digest)
    _fault(faults, "thin.after_objects")
    if record.get("catalog"):
        _drop_catalog_entry(root, entry_id)
    _fault(faults, "thin.after_catalog")
    record.pop("objects", None)
    record.pop("catalog", None)
    record["status"] = "thinned"
    _write_record(root, statuses, "running")


def _delete_object(store: VersionStore, digest: str) -> None:
    """The one deletion of an object anywhere (objects.py): the caller holds the vault lock and
    found that no manifest names it."""
    path = store.objects.path_for(digest)
    if _unlink_when_free(path):
        safety.fsync_dir(path.parent)


def _unlink_when_free(path: Path) -> bool:
    """Delete a file of the store, waiting out a sharing violation (another program holds it
    open) for BUSY_WAIT_SECONDS; True when it was there. Still held, or any other failure:
    ThinError, and the entry, recorded as thinning, is finished at the next start."""
    deadline = time.monotonic() + BUSY_WAIT_SECONDS
    pause = 0.005
    while True:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise ThinError("busy", "A file of the vault is in use by another program "
                                        f"({exc.strerror or exc}). Close it and start the app "
                                        "again: the deletion is finished then.") from exc
            time.sleep(pause)
            pause = min(pause * 2, 0.2)
        except OSError as exc:
            raise ThinError("failed", f"A file of the vault could not be deleted "
                                      f"({exc.strerror or exc}); the deletion is finished at "
                                      "the next start.") from exc


def _drop_catalog_entry(root: Path, entry_id: str) -> None:
    """The catalog's leftover of an entry thinned: removed, unless it carries a note or its
    folder came back meanwhile."""
    vault = Vault(root)
    entry = vault.get(entry_id)
    if entry is None or entry.note.strip() or _old_folder(vault, entry).exists():
        return
    vault.remove(entry_id)


def _finish(root: Path, store: VersionStore, faults: ThinFault | None) -> ThinResult | None:
    """Finish an interrupted run: each entry recorded as thinning whose manifest is gone is
    finished; one whose manifest is still there is decided again (it may stay now)."""
    statuses = _read_record(root)
    pending = [i for i, r in statuses.items() if r.get("status") == "thinning"]
    if not pending:
        return None
    thinned, left, freed = [], [], 0
    for entry_id in pending:
        with _vault_locked(store):
            if store.has_entry(entry_id):
                facts = _read_facts(root, store, check=False)
                if facts.blocked or entry_id not in _decide(facts, _when(None)).go:
                    del statuses[entry_id]
                    _write_record(root, statuses, "running")
                    left.append((entry_id, facts.blocked[0] if facts.blocked
                                 else "it may no longer go; it was not deleted"))
                    continue
            _finish_one(root, store, entry_id, statuses, faults)
            thinned.append(entry_id)
            freed += _uint(statuses[entry_id].get("bytes"))
    with _vault_locked(store):
        _write_record(root, statuses, "done")
    return ThinResult(tuple(thinned), freed, tuple(left), False,
                      _message(thinned, freed, left, False))


def finish_thinning(vault_root: str | Path, *,
                    faults: ThinFault | None = None) -> ThinResult | None:
    """At the start of an app: finish a run that was interrupted (None when none was)."""
    root = Path(vault_root)
    if not thinning_pending(root):
        return None
    store = _open(root, read_only=False)
    store._require_writable()
    with _held(root):
        _require_sound(store)
        return _finish(root, store, faults)


def thinning_pending(vault_root: str | Path) -> bool:
    """For the front ends: a run of thinning stopped before its end. Reads only."""
    try:
        statuses = _read_record(Path(vault_root))
    except ThinError:
        return False
    return any(r.get("status") == "thinning" for r in statuses.values())


# --- the record -------------------------------------------------------------------------------


def _record_path(root: Path) -> Path:
    return safety.long_path(store_root(root)) / STATE / THINNING_JSON


def _read_record(root: Path) -> dict[str, dict]:
    """The entries recorded so far; ThinError when the record cannot be read (it is never
    written over then)."""
    try:
        body = json.loads(_record_path(root).read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ThinError("damaged", f"The record of earlier thinning (store/state/"
                                   f"{THINNING_JSON}) cannot be read ({exc}); nothing was "
                                   "deleted.") from exc
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, dict):
        raise ThinError("damaged", f"The record of earlier thinning (store/state/"
                                   f"{THINNING_JSON}) is not in the expected form; nothing was "
                                   "deleted.")
    return {k: dict(v) for k, v in entries.items() if isinstance(v, dict) and is_entry_id(k)}


def _write_record(root: Path, statuses: Mapping[str, dict], state: str) -> None:
    body = {"format": THINNING_FORMAT, "schema": THINNING_SCHEMA, "state": state,
            "entries": dict(statuses)}
    raw = json.dumps(body, indent=1, sort_keys=True, ensure_ascii=True).encode("ascii")
    safety.atomic_write_bytes(_record_path(root), raw, temp_prefix=".thinning.tmp_")


def thinned_entries(vault_root: str | Path) -> dict[str, ThinnedEntry]:
    """The entries the owner thinned, by id: for the History and Backups lists ("thinned on
    <date>"). An unreadable record gives none (and is logged). Reads only."""
    try:
        statuses = _read_record(Path(vault_root))
    except ThinError as exc:
        log.warning("%s", exc)
        return {}
    out = {}
    for entry_id, r in statuses.items():
        out[entry_id] = ThinnedEntry(
            entry_id, str(r.get("when", "")), str(r.get("why", "")), _uint(r.get("bytes")),
            str(r.get("kind", "")), str(r.get("label", "")), str(r.get("created_utc", "")),
            r.get("op_id") if isinstance(r.get("op_id"), str) else None,
            r.get("status") == "thinned")
    return out


def _uint(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _fault(faults: ThinFault | None, point: str) -> None:
    if faults is not None:
        faults(point)


def _message(thinned, freed: int, left, cancelled: bool) -> str:
    size = (f"{freed / (1 << 30):.1f} GB" if freed >= (1 << 27)
            else f"{freed / (1 << 20):.1f} MB")
    noun = "backup" if len(thinned) == 1 else "backups"
    if cancelled:
        text = f"Stopped: {len(thinned)} {noun} deleted so far, {size} freed."
    else:
        text = f"{len(thinned)} {noun} deleted, {size} freed."
    if left:
        text += f" {len(left)} kept: " + "; ".join(f"{k} ({why})" for k, why in left)
    return text


# --- the reminder -----------------------------------------------------------------------------


def vault_size(vault_root: str | Path) -> int:
    """What the vault's store takes, from its manifests (no object is looked at): what the
    status bar compares with the size the owner set (``AppState.thin_reminder_bytes``).
    Raises StoreError like opening the store does."""
    return _open(Path(vault_root), read_only=True).stored_bytes()


# --- pins -------------------------------------------------------------------------------------


def _notes_to_write(root: Path) -> Notes:
    """The notes, to change: refused (StoreTooNew, StoreUnavailable) while the vault is
    read-only, as every vault write is."""
    if not root.is_dir():
        raise StoreUnavailable("The vault is not reachable.")
    if vault_mode(root) == "store":
        store = _open(root, read_only=False)
        if store.read_only_reason == "too-new":
            raise StoreTooNew()
        if store.read_only:
            raise StoreUnavailable(store.warnings[0] if store.warnings
                                   else "The vault is open read-only.")
    org = orgstore.OrganizerStore(root, create=False)
    if org.newer_schema():
        raise StoreTooNew("A newer NMS Save Vault wrote this vault's notes. Update the app.")
    if org.read_only:
        raise StoreUnavailable("The vault's notes cannot be written: " + "; ".join(org.problems))
    return Notes(org)


@contextmanager
def _writing() -> Iterator[None]:
    try:
        yield
    except orgstore.StoreReadOnly as exc:
        raise StoreUnavailable(f"The vault's notes cannot be written: {exc}") from exc
    except orgstore.StoreBusy as exc:
        raise StoreBusy() from exc


def pin_entry(vault_root: str | Path, entry_id: str) -> None:
    """Pin a whole entry (a backup, snapshot or extract): thinning never deletes it. KeyError
    when the vault holds no such entry."""
    root = Path(vault_root)
    if not is_entry_id(entry_id):
        raise ValueError(f"not an entry id: {entry_id!r}")
    notes = _notes_to_write(root)
    in_store = vault_mode(root) == "store" and _open(root, read_only=True).has_entry(entry_id)
    if not in_store and Vault(root, read_only=True).get(entry_id) is None:
        raise KeyError(f"the vault holds no entry {entry_id!r}")
    with _writing():
        notes.set_entry_pinned(entry_id, True)


def unpin_entry(vault_root: str | Path, entry_id: str) -> None:
    if not is_entry_id(entry_id):
        raise ValueError(f"not an entry id: {entry_id!r}")
    notes = _notes_to_write(Path(vault_root))
    with _writing():
        notes.set_entry_pinned(entry_id, False)


def pinned_entries(vault_root: str | Path) -> frozenset[str]:
    return read_notes(orgstore.OrganizerStore(Path(vault_root), create=False,
                                              read_only=True)).pinned_entries()


def pin_version(vault_root: str | Path, vid: str, *, pair: tuple[str | None, str | None] | None
                = None, pid: str | None = None) -> None:
    """Pin one version (the sha256 of a save's data): the version-note lock, "the owner's keep
    mark" (organizer/notes.py), on ``vid``'s own note - the one it heads, never another row's
    that holds it as a partner. Its own note, when there is one, is locked with its words and
    pair kept; else one is made for the slot ``pair`` (A, B) and playthrough ``pid`` it was
    kept with (default: as the newest entry holding it says). KeyError when no pair is given
    and the vault holds no such version."""
    root = Path(vault_root)
    if not is_digest(vid):
        raise ValueError("a version is the sha256 of a save's data")
    notes = _notes_to_write(root)
    own = notes.state().versions.get(vid)
    with _writing():
        if own is not None and vid in own.pair:
            notes.set_version_note(vid, pair=own.pair, pid=own.pid, locked=True)
            return
        if pair is None or pid is None:
            found_pair, found_pid = _kept_as(root, vid)
            pair, pid = pair or found_pair, pid or found_pid
        if not is_pid(pid):
            raise ValueError(f"not a playthrough id: {pid!r}")
        notes.set_version_note(vid, pair=pair, pid=pid, locked=True)


def _kept_as(root: Path, vid: str) -> tuple[tuple[str | None, str | None], str]:
    store = _open(root, read_only=True) if vault_mode(root) == "store" else None
    refs = store.vid_refs(vid) if store is not None else ()
    if not refs:
        raise KeyError("the vault holds no such version")
    entry_id, slot, member = refs[0]
    entry = store.entry(entry_id)
    a, b = entry.member(slot, "A"), entry.member(slot, "B")
    record = entry.member(slot, member)
    return ((a.vid if a else None, b.vid if b else None),
            (record.pid if record is not None and record.pid else unknown_pid(vid)))


def unpin_version(vault_root: str | Path, vid: str) -> None:
    """Take the lock off ``vid``'s own version note (the one it heads); its text stays, and the
    pin of any other version - another save of its slot that holds ``vid`` as its partner - is
    left as it is. Nothing to do when ``vid`` is not pinned. ValueError when ``vid`` is kept
    only by such another version's pin: unpinning ``vid`` cannot take that pin off."""
    if not is_digest(vid):
        raise ValueError("a version is the sha256 of a save's data")
    notes = _notes_to_write(Path(vault_root))
    state = notes.state()
    own = state.versions.get(vid)
    if own is None or not own.locked:
        if vid in state.locked_vids():
            raise ValueError("this save stays pinned: the other save of its slot at that moment "
                             "is pinned, and its pin keeps both. Unpin that one.")
        return
    with _writing():
        notes.set_version_note(vid, pair=own.pair, pid=own.pid, locked=False)


def pinned_versions(vault_root: str | Path) -> frozenset[str]:
    """Every version pinned: each locked version note's head and partner."""
    return read_notes(orgstore.OrganizerStore(Path(vault_root), create=False,
                                              read_only=True)).locked_vids()
