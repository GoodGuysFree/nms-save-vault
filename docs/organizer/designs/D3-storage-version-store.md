# D3 - Storage option B: a content-addressed version store

Status: design for fork F3 option B and fork F9. Author: D3 subagent, 2026-10-16, branch
`design/save-organizer`. No production code, tests or packaging were changed.

Audience: implementing agents (completeness wins) and the parent. Bound by `01-consolidation.md`
sections 2 and 6, by D1's interfaces (`D1-core-model.md` 2.11) and by the old-app rule "legacy
kinds only" shared with D2 (`D2-storage-index-over-folders.md`, the competing option A).

Evidence tags: **VERIFIED** = run or measured by D3 (script named); **SOURCED** = code `file:line`
or a research/design report; **INFERRED** = reasoning from those. Scripts and outputs live in
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\<session>\scratchpad\D3\` (written `<D3>`).
Redaction: `st_<steamid64>`, `<xbox-account>`, `%APPDATA%`, `%LOCALAPPDATA%`.

All real-data work was read-only. A prototype store was built in `<D3>\proto\` by *reading* the
owner's vault; the vault fingerprint (4,517 files, 3,404,610,911 B, `catalog.json` sha256
`a3a58014...a045b9`, `oplog.jsonl` `29b3ece9...598aaa`) was identical before and after
(Appendix A).

## 0. Owner summary

- B stores each save version once; a backup becomes a small list that points at those copies.
- Your vault: 3.4 GB -> 0.5 GB (tried on a copy: every backup came back byte-identical); a new full backup costs ~15 MB, not 50-170 MB.
- Vs A (folder per backup + index, D2): same organizer features; A stays plain folders that every older app version can restore.
- With B, older app versions list your backups but refuse to restore them (tested: nothing touched); "Export to folder" gives any backup back.
- Your calls (my pick): ship B early but deleting nothing (yes); fold old backups in and keep the old folders until you press "Reclaim space" (yes); retention (keep everything).
- Thumbnail cache: keep it deduplicated, +139 MB (my pick), or drop it.

## 1. Scope, non-goals, dependencies

**In scope (F3-B, S6, F9)**

- The store layout inside the vault: objects, entry manifests, trash, state, compat stub.
- What is stored per version and per backup: data, metas (Steam XXTEA, Xbox plaintext), Xbox
  index and containers, account-level files, `cache/`.
- The write path for every backup-like operation (full backup, snapshot, extract, import,
  `preserve`), and how every existing operation maps onto the store.
- `StoreLocator`, D3's implementation of D1's `VersionLocator` (`locate`, `open`, `preserve`,
  `is_durable`), and materialize / export.
- Integrity (verify on write and read, `fsck`), crash safety, references, trash, garbage
  collection, retention on top (C27, F9 variants).
- Migration of the existing vault (fold, verify, reclaim), compatibility with older app versions,
  downgrade, D11 portability, USB / NAS, disk and performance budget.

**Non-goals**

- Identity, captures, lineage, notes, journal, read model, organizer store and lock: D1.
- Live writes (Place / Swap / Evict / undo / verification) and the op record format: D5.
- Content facts: D4. UI: D6/D7.
- Compression of stored bytes (save data is already LZ4; the verbatim rule) and sub-file
  deduplication: measured at 0.4 % extra saving (2.3), rejected.
- Any change to the `catalog.json` v1 schema (C21). B adds only legacy-shaped compat entries.
- Phase-0 defects (P1, P2, P7, P9) are fixed elsewhere; B depends on some of them (table).

**Dependencies**

| From | What D3 uses |
|---|---|
| D1 | `Vid`, `Pid`, `LiveTarget`, `SourceRef` (`LiveRef` / `EntryRef` / `StoreRef(key)`), `MetaSnap` fields, `Location`, `OpenedVersion`, `LiveMember`, `VersionLocator` ABC, `VersionUnavailable`, `PreserveFailed`; the vault lock `safety.exclusive_lock(<vault>/organizer/.lock)`; `protection(vid)` and `locked_vids()` for retention; `vault_id`; the per-machine cache dir |
| D2 | `FolderLocator` over legacy folders and live (B indexes old folders in place through it, F3-B); durability classes for folders (2.4 of D2); `manifests.json` (when A ships first, the fold cross-checks against it); the shared rule "legacy kinds only" |
| D5 | Calls `preserve()` before every live write; adopts the oplog barrier convention (2.9, 4.2) for records whose snapshot exists only in the store |
| D4 | Reads bytes only through `open()` |
| Phase 0 | P1 (Xbox meta parser) for correct Xbox member records; P7 (account-level files) before a full restore is routed through the store; P9 (live folder catalogued as in-place) so the fold skips it |
| New primitive | `safety.fsync_dir(path)`: fsync a directory on POSIX after a rename, no-op on Windows (OS branch, so it lives in `safety.py`) |

## 2. Design

### 2.1 Vocabulary

| Term | Meaning |
|---|---|
| object | one file's bytes stored once under the sha256 of those bytes. For a save data file the object name *is* the `vid` (C4) |
| store entry | one backup, snapshot, extract, import or preserve held by the store: an immutable JSON manifest listing every file of the captured folder or slot, plus, per slot member, its `vid`, meta and meta facts. The store's equivalent of a catalog entry. D1's `StoreRef(key)` uses the entry id as key. One store entry yields one D1 `Capture` per occupied slot |
| inline file | a file of at most 8 KiB kept inside the manifest (base64) instead of as an object |
| legacy entry | a catalog entry backed by a folder (today's layout); read through D2's `FolderLocator` |
| fold | create a store entry from a legacy entry's folder, keeping the entry id |
| reclaim | delete a folded legacy folder (owner-confirmed only, after re-verification) |
| shadow / barrier entry | `catalog.json` v1 entries the new app writes only so that older app versions see the backups and refuse to restore them (4.2) |

"Store entry", not "capture": D1 uses *capture* for one observation of one slot.

### 2.2 On-disk layout

```
<vault>/                           unchanged: catalog.json, oplog.jsonl, backups/, snapshots/,
  |                                extracts/, organizer/ (D1), playthrough-ids.json
  store/
    FORMAT                         {"format": "nmsvault-store", "version": 1, "read_compat": 1,
                                    "write_compat": 1, "store_id": "<uuid4 hex>",
                                    "hash": "sha256", "fanout": 2, "inline_max": 8192,
                                    "created_utc": "..."}
    objects/<aa>/<sha256>          verbatim bytes; name = 64 lower-case hex; <aa> = first 2 hex
    entries/<entry_id>.json        entry manifests; immutable once written
    trash/<entry_id>.json          manifests removed by the owner or by retention; still referenced
    trash/<entry_id>.trashed.json  {"trashed_utc", "by": "owner"|"retention", "reason", "purge_after_utc"}
    quarantine/<sha256>.<n>        objects that failed verification; never deleted automatically
    tmp/                           staging on the same volume; emptied at open (under the lock)
    state/migration.json           fold / reclaim progress per legacy entry
    state/tombstones.json          versions the owner agreed to delete: {vid: {utc, reason, entries}}
    state/damage.json              entries with missing or corrupt objects (fsck); never auto-deleted
    compat/stub/README.txt         the folder that shadow and barrier catalog entries point at (4.2)
```

Rules:

- Every path inside `store/` is relative and derived from a hash or an entry id, so D11 holds by
  construction. The store contains no absolute path and no machine name.
- Names are lower-case hex or ASCII ids: no case-collision issues on case-insensitive volumes. The
  longest path is `<vault>\store\objects\ab\<64 hex>` = vault root + 85 characters (~140 for the
  owner's default vault), under Windows' 260.
- 256 fan-out folders: FAT32's 65,534 entries per folder is never reached (~16 M objects).
- No sqlite in the vault (R5 F6: corruption risk on network shares), no hard links or symlinks
  (absent on FAT/exFAT and many SMB shares; a hard-linked file edited in place changes every
  backup, D2 H2).
- Entry ids keep today's scheme `<kind>-<platform>-<YYYYMMDD-HHMMSS>[-n]` (local time, as
  `Vault.new_id`, `catalog.py:205-215`), unique across catalog **and** store (the generator checks
  both, trash included). A folded entry keeps its legacy id, so every `oplog.jsonl` reference
  stays valid. New kind prefix: `preserve-`.
- Root folders an older app does not know are invisible to it: old apps never enumerate the vault
  root and `import_vault` does not carry unknown root folders (VERIFIED, 4.2 X6; D2 E3).

### 2.3 What is stored

| File | Owner's sizes (VERIFIED) | Stored as | Deduplication (owner's vault) |
|---|---|---|---|
| Steam `saveN.hg` (data) | 41.6 KB - 6.92 MB | object, name = `vid` | 818 copies -> 116 objects |
| Steam `mf_saveN.hg` (meta) | 432 B | inline: the original **encrypted** bytes + the ordinal it decrypts with + `meta_plain_sha256` | 818 copies, 121 distinct raw (0.4 MB total) |
| Xbox data blob | 0.58 - 4.0 MB | object, name = `vid` | 150 -> 38 |
| Xbox meta blob | 296 / 360 B, plaintext | inline, verbatim | 150 -> 38 |
| Xbox `container.N` | 328 B | inline | 155 -> 43 |
| Xbox `containers.index` | 4,353 B | inline; verbatim for full / snapshot, the rebuilt minimal index for slot-scoped entries (as `extract_slot` does today, `operations.py:466-489`) | 5 -> 4 |
| `accountdata.hg` | 95.8 - 105.9 KB | object | 27 -> 8 |
| `mf_accountdata.hg` | 432 B | inline | 27 -> 13 |
| Xbox `AccountData` blob | 36 KB | object | 5 -> 4 |
| `steam_autocloud.vdf` | 51 B | inline | 28 -> 1 |
| `cache/*` (thumbnails, server-feed JSON) | 4 B - 921,600 B | object (policy K1) or not captured (K2/K3, 3.3) | 2,326 -> 184 objects (1,772 -> 139 MB) |

Source: `<D3>\analyze_store.py` over `<D3>\files.jsonl` (every file of every catalog entry and
the live folders, hashed). The `xgs` mirror is not captured, as today (R1 B5).

**Decision: Steam metas are stored encrypted, with their ordinal.** Rejected alternative: store
the decrypted plaintext keyed by its own hash and re-key on every placement. Reasons:

1. Verbatim: an exported or materialized folder is byte-identical to what was captured (VERIFIED:
   0 differences in names, bytes and mtimes on three real entries, 2.7).
2. Self-checking: decrypting with the recorded ordinal must give the header `0xEEEEEEBE`, a
   format-level integrity check on top of sha256.
3. A meta keyed for another ordinal ("moved", `savedir.py:162-169`) is kept exactly as found.
4. Nothing to gain: 156 distinct raw Steam metas vs 151 distinct plaintexts over vault + live +
   in-place (VERIFIED), all inline anyway.
5. Placement re-keys in both designs: `meta.re_key(raw, ordinal, dst)`; one XXTEA pass on 432 B
   costs 0.19 ms (VERIFIED, pure Python).

`meta_plain_sha256` equals D1's `MetaSnap.meta_sha`. In the owner's data every data version has
exactly one meta plaintext (0 of 195 have more; VERIFIED), so data hash and meta plaintext are
1:1 as R7 found.

**Decision: files of at most 8 KiB are inline.** That covers every meta, `container.N`, the vdf,
`mf_accountdata.hg` and `containers.index` (4,353 B). Effect on the owner's vault: 349 objects
instead of 574; on a 128-KiB-cluster exFAT USB drive the store needs ~536 MB instead of ~565 MB;
`open()` finds the meta in the manifest it already has in memory. Inline bytes are not
deduplicated: 0.66 MB over 1,202 inline files (VERIFIED).

**Decision: whole-file content addressing.** Splitting the 195 distinct save data files at their
`0xFEEDA1E5` chunk boundaries gives 3,049 chunks, of which 3,029 are distinct: 0.4 % less than
whole-file deduplication (VERIFIED, `<D3>\chunk_dedupe.py`). A small game change shifts the JSON
and every later compressed chunk changes. Packs, deltas and chunking are rejected.

**Decision: objects are raw bytes.** Never compressed or transformed: the verbatim rule, and an
object can be copied out as is.

### 2.4 Entry manifest

One JSON file per store entry. Example (Steam snapshot, trimmed, values illustrative):

```json
{
 "format": "nmsvault-entry", "schema": 1,
 "id": "snapshot-steam-20261016-141500", "kind": "snapshot",
 "label": "auto: pre-place slot 7", "reason": "pre-place", "op_id": "op-20261016T111500Z-ab12",
 "created_utc": "2026-10-16T11:15:00Z", "created_local": "2026-10-16T14:15:00",
 "app_version": "0.3.0", "platform": "steam", "account_key": "st_<steamid64>",
 "source": {"type": "live", "platform": "steam", "account_key": "st_<steamid64>"},
 "scope": {"slots": "all", "account_files": true, "cache": "none"},
 "origin": null,
 "files": [
  {"path": "accountdata.hg", "size": 105904, "sha256": "07fa...", "mtime_ns": 1791200000000000000},
  {"path": "mf_save13.hg", "size": 432, "sha256": "9b1c...", "mtime_ns": 1791200000000000000,
   "inline": "<base64 of the encrypted meta>"},
  {"path": "save13.hg", "size": 6921201, "sha256": "5e0d...", "mtime_ns": 1791200000000000000}
 ],
 "slots": {
  "7": {
   "A": {"data": "save13.hg", "meta": "mf_save13.hg", "vid": "5e0d...", "ordinal": 14,
         "meta_plain_sha256": "c3a1...", "pid": "DDDAD1BDD39F9C72", "play_time": 1916280,
         "meta_ts": 1791199000, "name": "Main Save (S2)", "summary": "...", "difficulty": 3,
         "game_mode": 1, "season": 0, "base_version": 4220, "meta_format": 2004,
         "size_decompressed": 18800000, "valid": true, "note": ""},
   "B": {"data": "save14.hg", "meta": "mf_save14.hg", "...": "..."}
  }
 },
 "account": {"data": "accountdata.hg", "meta": "mf_accountdata.hg", "autocloud": "steam_autocloud.vdf"},
 "xbox": null,
 "body_sha256": "..."
}
```

| Field | Meaning |
|---|---|
| `format`, `schema` | `nmsvault-entry`, 1. A reader refuses a schema it does not know unless `FORMAT.read_compat` allows it |
| `id`, `kind` | `kind` in `full`, `snapshot`, `extract`, `imported`, `preserve` |
| `label`, `reason`, `op_id` | owner label or "auto: ..."; why the app created it; D5's op id for snapshots and preserves |
| `created_utc`, `created_local` | ISO UTC; and the legacy catalog form (local, naive, seconds) for shadow entries and for sorting next to legacy entries |
| `app_version`, `platform`, `account_key` | `account_key` = live folder name, internal only, always shown through `aliases.redact` |
| `source` | D1 `SourceRef` as a dict: live account, legacy entry id, external folder name (never a path), or another store (`store_id` + entry id) |
| `scope` | `slots`: `"all"` or a list; `account_files`: bool; `cache`: `included` / `omitted` / `none` |
| `origin` | folded entries: `{"legacy_entry": id, "legacy_subdir": "backups"|"snapshots"|"extracts", "folded_utc"}`; merged entries: `{"store_id", "entry_id"}`; else null |
| `files[]` | every file the entry covers: posix relative `path`, `size`, `sha256`, `mtime_ns` (int), `inline` (base64, only when size <= `inline_max`). Sorted by path |
| `slots{}` | per slot `"1"`..`"15"`, per member `"A"`/`"B"`: `data` / `meta` paths (Steam names, or Xbox `<DIRGUID>/<GUID>`), `vid`, `ordinal` (Steam ordinal actually used; null for Xbox), `meta_plain_sha256` (null when unreadable), `pid` (16 hex or null), the `MetaSnap` fields, `size_decompressed`, `valid` (Steam: header ok and SizeDecompressed == chunk total, as `_validate_pair`, `operations.py:925-940`; Xbox: meta parses), `note` (scan diagnostics), Xbox `identifier` (`Slot7Auto`) |
| `account{}` | paths of the account-level files this entry holds; absent keys = not captured |
| `xbox` | `{"index": "containers.index", "index_kind": "verbatim"|"rebuilt"}` or null |
| `body_sha256` | sha256 of the canonical serialization of every other key |

Canonical form: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`
encoded as ASCII. No floats anywhere (times are ints or ISO strings), so a parse and
re-serialization reproduces the bytes exactly; unknown keys written by a newer app are covered by
the checksum and ignored by older readers. Manifests are never rewritten, so no key is ever lost.

Sizes (VERIFIED, prototype): 41 manifests = 1.81 MB; the largest is 72.9 KB (a Steam full listing
180 cache files); a snapshot is ~10-30 KB, a slot-scoped entry ~2-3 KB.

### 2.5 Modules and signatures

Package `nms_save_vault.core.store`, platform-neutral; OS-specific calls only through `safety`.

```
core/store/__init__.py     VersionStore, open_store, errors
core/store/objects.py      ObjectStore
core/store/manifest.py     StoreEntry, FileEntry, MemberRecord, canonical JSON, strict loader
core/store/capture.py      building entries from folders and live accounts (Steam, Xbox)
core/store/locator.py      StoreLocator(VersionLocator)
core/store/materialize.py  materialize, export, unfold
core/store/gc.py           trash, purge, tombstones, gc
core/store/retention.py    retention planner (F9 variants)
core/store/fsck.py         quick / deep / repair
core/store/migrate.py      fold, verify, reclaim, adopt
core/store/compat.py       barrier and shadow catalog entries, stub, path re-basing
```

```python
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Literal, Mapping, Sequence

from ..organizer.ids import Vid, Pid, Platform, Member, LiveTarget, StoreRef      # D1
from ..organizer.locator import (Location, OpenedVersion, LiveMember, VersionLocator,
                                 VersionUnavailable, PreserveFailed)            # D1

EntryKind = Literal["full", "snapshot", "extract", "imported", "preserve"]
STORE_VERSION = 1
INLINE_MAX = 8192
TRASH_GRACE_DAYS = 14


@dataclass(frozen=True)
class FileEntry:
    path: str                    # posix, relative to the captured folder
    size: int
    sha256: str
    mtime_ns: int
    inline: bytes | None = None  # decoded payload when size <= INLINE_MAX


@dataclass(frozen=True)
class MemberRecord:
    slot: int
    member: Member
    data: str                    # FileEntry.path of the data file / blob
    meta: str
    vid: Vid
    ordinal: int | None          # Steam: the ordinal the meta decrypts with; Xbox: None
    meta_plain_sha256: str | None
    pid: Pid | None
    play_time: int
    meta_ts: int
    name: str
    summary: str
    difficulty: int
    game_mode: int
    season: int
    base_version: int
    meta_format: int
    size_decompressed: int
    valid: bool
    note: str = ""
    xbox_identifier: str | None = None


@dataclass(frozen=True)
class StoreEntry:
    id: str
    kind: EntryKind
    label: str
    reason: str
    op_id: str | None
    created_utc: str
    created_local: str
    app_version: str
    platform: Platform
    account_key: str | None
    source: Mapping[str, str]
    scope: Mapping[str, object]
    origin: Mapping[str, str] | None
    files: tuple[FileEntry, ...]
    members: tuple[MemberRecord, ...]
    account: Mapping[str, str]
    xbox: Mapping[str, str] | None
    body_sha256: str

    def file(self, path: str) -> FileEntry: ...
    def member(self, slot: int, member: Member) -> MemberRecord | None: ...
    def object_digests(self) -> frozenset[str]: ...   # non-inline files only
    def to_bytes(self) -> bytes: ...                   # canonical JSON with body_sha256
    @staticmethod
    def from_bytes(raw: bytes) -> "StoreEntry": ...    # schema + checksum; raises EntryCorrupt


@dataclass(frozen=True)
class CaptureScope:
    slots: tuple[int, ...] | None = None    # None = the whole folder
    account_files: bool = True
    cache: Literal["include", "omit"] = "omit"


@dataclass(frozen=True)
class PutResult:
    digest: str
    size: int
    written: bool                # False: deduplicated onto a re-verified existing object
    healed: bool                 # an existing object failed its re-hash and was replaced


class ObjectStore:
    def __init__(self, objects: Path, tmp: Path, quarantine: Path) -> None: ...
    def path_for(self, digest: str) -> Path: ...
    def has(self, digest: str) -> bool: ...
    def put(self, data: bytes, digest: str) -> PutResult: ...
    def get(self, digest: str, size: int | None = None) -> bytes: ...   # verifies sha256
    def digests(self) -> Iterator[str]: ...
    def delete(self, digest: str) -> int: ...                           # GC only, under the lock
    def quarantine(self, digest: str, reason: str) -> Path: ...


class VersionStore:
    root: Path
    store_id: str
    read_only: bool              # FORMAT.write_compat too new, medium read-only, or no lock

    @classmethod
    def open(cls, vault_root: Path, *, lock: Callable[[], "ContextManager[None]"],
             app_version: str, clock: Callable[[], datetime], create: bool = False) -> "VersionStore": ...

    # reading (no lock)
    def entries(self, *, include_trash: bool = False) -> list[StoreEntry]: ...
    def entry(self, entry_id: str) -> StoreEntry: ...
    def has_entry(self, entry_id: str) -> bool: ...
    def vid_refs(self, vid: Vid) -> tuple[tuple[str, int, Member], ...]: ...   # (entry, slot, member)
    def read_file(self, entry_id: str, path: str) -> bytes: ...

    # writing (each call holds the vault lock for its whole duration)
    def add_from_folder(self, folder: Path, *, kind: EntryKind, label: str,
                        source: Mapping[str, str], scope: CaptureScope = CaptureScope(),
                        entry_id: str | None = None, origin: Mapping[str, str] | None = None,
                        created_local: str | None = None, op_id: str | None = None,
                        reason: str = "", cancel: Callable[[], bool] | None = None,
                        progress: Callable[[int, int], None] | None = None) -> StoreEntry: ...
    def add_from_live(self, target_dir: Path, *, platform: Platform, account_key: str,
                      kind: EntryKind, label: str, scope: CaptureScope,
                      op_id: str | None = None, reason: str = "") -> StoreEntry: ...

    # materialize / export
    def materialize(self, entry_id: str, dest: Path, *, slots: Iterable[int] | None = None,
                    account_files: bool = True, cache: bool = True,
                    keep_mtimes: bool = True) -> "MaterializeResult": ...
    @contextmanager
    def materialized(self, entry_id: str, **kw) -> Iterator[Path]: ...    # temp under store/tmp

    # lifecycle (phase 5; absent from the append-only core)
    def trash(self, entry_id: str, *, by: Literal["owner", "retention"], reason: str,
              confirm_unique: frozenset[Vid] = frozenset()) -> None: ...
    def untrash(self, entry_id: str) -> None: ...
    def purge_due(self) -> "PurgeResult": ...
    def tombstone(self, vids: Sequence[Vid], *, owner_confirmed: bool, reason: str) -> None: ...
    def gc(self, *, dry_run: bool = False) -> "GcResult": ...
    def fsck(self, *, deep: bool = False, repair: bool = False,
             fallbacks: Sequence[VersionLocator] = ()) -> "FsckReport": ...
    def space_needed(self, sizes_by_digest: Mapping[str | None, int]) -> int: ...


class StoreLocator(VersionLocator):          # D1 2.11
    def __init__(self, store: VersionStore, *, live_dir: Callable[[LiveTarget], Path],
                 fallbacks: Sequence[VersionLocator] = ()) -> None: ...
    def locate(self, vid: Vid) -> list[Location]: ...
    def open(self, vid: Vid, prefer: Location | None = None) -> OpenedVersion: ...
    def preserve(self, members: Sequence[LiveMember], *, op_id: str, reason: str) -> list[Location]: ...
    def is_durable(self, vid: Vid) -> bool: ...
    def space_needed_for_preserve(self, members: Sequence[LiveMember]) -> int: ...


# errors. StoreError subclasses OperationError so the front ends already show it; to avoid an
# import cycle (operations imports store) OperationError moves to core/errors.py and
# operations.py re-exports it.
class StoreError(OperationError): ...
class StoreUnavailable(StoreError): ...      # vault offline, FORMAT unreadable    -> D5 VAULT_UNAVAILABLE
class StoreTooNew(StoreError): ...           # read_compat / write_compat newer than this app
class InsufficientSpace(StoreError): ...     # -> D5 VAULT_SPACE
class ObjectMissing(StoreError, VersionUnavailable): ...
class ObjectCorrupt(StoreError, VersionUnavailable): ...
class EntryCorrupt(StoreError): ...
class SourceChanged(StoreError): ...         # a source file changed while it was being read
class PreserveMismatch(StoreError, PreserveFailed): ...   # live member != expected_vid
class LastCopyError(StoreError): ...         # would drop versions without the owner's consent
class FoldVerifyError(StoreError): ...
```

The vault-level locator handed to D4 / D5 composes the providers (D1 decides where the composite
lives): `StoreLocator` first, then D2's `FolderLocator` for legacy folders, in-place entries and
live. `locate` concatenates (store locations first, then durable folders, prunable, external,
live, as D1's order); `open` tries `prefer`, then the order above; `preserve` is the store's when
the store is writable; `is_durable` is true when any provider says so.

### 2.6 Write path: adding a store entry

```python
def add_from_folder(folder, *, kind, scope, ...):
    with lock():                                      # D1 vault lock, whole call (2.13)
        require_writable()                            # FORMAT write_compat, medium writable
        files = select_files(folder, scope)           # sorted posix paths; regular files only,
                                                      # symlinks skipped with a warning
        worst = sum(size for f in files if size > INLINE_MAX)
        if free_space(store.root) < worst + 64 * MiB: raise InsufficientSpace
        records = []
        for rel in files:
            data, st = read_stable(folder / rel)      # read; re-stat; size and mtime_ns must be
                                                      # unchanged, else re-read (3 tries) -> SourceChanged
            digest = sha256(data)
            if len(data) <= INLINE_MAX:
                records.append(FileEntry(rel, len(data), digest, st.st_mtime_ns, inline=data))
            else:
                objects.put(data, digest)             # below
                records.append(FileEntry(rel, len(data), digest, st.st_mtime_ns))
            if cancel and cancel(): raise Cancelled   # nothing committed; orphans GC'd
        members = describe_members(platform, folder, records)   # 2.6.1
        entry = StoreEntry(..., files=records, members=members, body_sha256=...)
        write_atomic(store/"entries"/f"{entry.id}.json", entry.to_bytes())   # COMMIT POINT
        safety.fsync_dir(store/"entries")
        check_present(entry)                          # stat every referenced object (size)
        return entry


def ObjectStore.put(data, digest):
    p = path_for(digest)
    if p.is_file():
        if sha256(p.read_bytes()) == digest:
            return PutResult(digest, len(data), written=False, healed=False)
        quarantine(digest, "re-hash mismatch on dedupe")         # keep the bad bytes
        healed = True
    tmp = tmp_dir / f"{digest}.{os.getpid()}.{counter}.part"
    write tmp; flush; os.fsync                                    # safety.atomic_write_bytes pattern
    os.replace(tmp, p); safety.fsync_dir(p.parent)
    if sha256(p.read_bytes()) != digest:                          # read-back verification
        quarantine(digest, "read-back mismatch"); raise ObjectCorrupt(digest)
    return PutResult(digest, len(data), written=True, healed=healed)
```

#### 2.6.1 Describing members

- **Steam**: for each `mf_saveN.hg` with its `saveN.hg` present: decrypt with the mapped ordinal,
  else `meta.decrypt_autodetect` (the logic of `savedir._load_member`, `savedir.py:144-172`);
  record the ordinal used, `meta.parse` fields, `pid = f"{slot_identifier:016X}"`, and
  `valid` = header ok and `SizeDecompressed == lz4_block.stats(data).total_decompressed`. An
  unreadable meta gives `valid: false`, `ordinal: null`, a `note`; the bytes are still stored.
  A data file without its meta (3 such slots in the owner's catalog) is stored as a file and
  listed with no member record; a meta without data likewise.
- **Xbox**: `msstore.parse_containers_index(folder)` maps identifiers to GUID folders and blobs;
  meta fields from the fixed parser (P1: id 0x118, time 0x120, names at 296 B). `ordinal: null`;
  `meta_plain_sha256` = sha256 of the blob.

#### 2.6.2 Scopes per entry kind

| Kind | Steam files | Xbox files |
|---|---|---|
| `full` | the whole folder (today: `copytree`, `operations.py:170-171`); `cache/` per policy (3.3) | the whole `wgs` account folder |
| `snapshot` | `*.hg` + `steam_autocloud.vdf` (as `_copy_save_state`, `operations.py:115-131`); never `cache/` | the whole `wgs` account folder |
| `extract`, `preserve` | the slot's `saveN.hg` / `mf_saveN.hg` at their original numbers (`operations.py:286-295`) | the slot's GUID folders + a rebuilt minimal `containers.index` (`operations.py:466-489`) |
| `imported` (copy into vault) | the whole folder, any files (as `copytree`) | the whole folder |

#### 2.6.3 Crash analysis

| Crash or power loss at | State afterwards | Recovery |
|---|---|---|
| writing an object's temp file | `*.part` in `store/tmp/` | `tmp/` emptied at the next open, under the lock |
| after an object's rename, before the manifest | unreferenced objects | GC deletes them; or the next capture deduplicates onto them after re-hashing |
| writing the manifest's temp file | `*.part` in `tmp/` | as above |
| after the manifest rename | entry complete and visible | none |
| a medium that lies about flushes | an object renamed but its bytes lost | verify-on-read raises `ObjectCorrupt`; quarantine; self-heal from any other location (2.7); else `state/damage.json` |

An entry is visible only after its manifest is renamed into `entries/`, and a manifest is written
only after every object it references was written or re-verified. There is no half-written entry.

#### 2.6.4 Live sources, the game and the UI thread

- Captures only read the live folder. Today's backups and snapshots do not check for the game
  (R1 D2); B keeps that for backups but adds the stability check (2.6) and flags invalid members
  (`valid: false`) rather than dropping them. `preserve` runs only with the game closed (D5 guard).
- No hashing or copying on the Tk thread (C31): a worker thread is fine (`hashlib` releases the
  GIL on large buffers); a capture takes 0.16-0.27 s (VERIFIED, 2.16), a fold several seconds.
- Memory: files are read whole (largest save 6.9 MB, largest cache file 0.9 MB).

### 2.7 Read path

```python
def StoreLocator.open(vid, prefer=None) -> OpenedVersion:
    for loc in candidates(vid, prefer):           # prefer; store refs newest entry first; fallbacks
        try:
            if loc.kind != "store":
                ov = fallback_for(loc).open(vid, loc)
                heal(vid, ov.data)                 # object missing/corrupt here: re-put verified bytes
                return ov
            entry = store.entry(loc.ref.key); m = entry.member(loc.slot, loc.member)
            data = objects.get(vid, size=entry.file(m.data).size)      # sha256 == vid checked
            raw = entry.file(m.meta).inline or objects.get(entry.file(m.meta).sha256)
            plain = meta.decrypt(raw, m.ordinal) if entry.platform == "steam" else raw
            if entry.platform == "steam" and header(plain) != META_HEADER: raise ObjectCorrupt
            if m.meta_plain_sha256 and sha256(plain) != m.meta_plain_sha256: raise ObjectCorrupt
            return OpenedVersion(data, plain, entry.platform, m.meta_format, loc)
        except (ObjectMissing, ObjectCorrupt) as exc:
            record_damage(loc, exc); continue
    raise VersionUnavailable(vid)
```

- **Which meta.** A `vid` can have more than one meta plaintext (promote bumps the timestamp; 0
  cases in the owner's data). `open` returns `prefer`'s meta, else the newest store entry's;
  `OpenedVersion.location` tells the caller which.
- **`locate(vid)`**: every `(entry, slot, member)` holding the vid, as
  `Location(vid, "store", StoreRef(entry_id), slot, member, platform, durable=True,
  prunable=<entry is a snapshot under R-B>)`, then the fallbacks' locations. In memory.
- **`is_durable(vid)`**: the vid is referenced by an entry outside the trash, is not tombstoned,
  and its object exists with the right size. True for every stored vid, because no automatic
  process drops a vid's last entry (2.11).
- **Index**: built at open by parsing every `entries/*.json` (VERIFIED: 41 manifests in 6-7 ms
  warm; 185 ms on the first read after writing them, the virus scanner, as R5 F6 saw). Refreshed
  whenever the lock is taken (other processes may have added entries). A persistent cache is not
  needed below ~2,000 entries; above, it goes in D1's per-machine cache dir keyed by `vault_id`
  with a fingerprint of `entries/` (names and sizes).

**Materialize** (the classic operations' folder-shaped view, and Export):

```python
def materialize(entry_id, dest, *, slots=None, account_files=True, cache=True, keep_mtimes=True):
    refuse if dest exists and is not empty, lies inside store/, or inside any live save root
    for fe in entry.files filtered by (slots, account_files, cache):
        data = fe.inline or objects.get(fe.sha256, fe.size)      # verified
        write dest / fe.path (parents created)
        if keep_mtimes: os.utime(dest / fe.path, ns=(fe.mtime_ns, fe.mtime_ns))
    re-read every written file; sha256 must equal fe.sha256      # catches a bad destination
```

- `materialized(entry_id, ...)` writes to `store/tmp/mat-<entry_id>-<rand>/` (same volume as the
  objects) and removes it on exit, also on an exception; stale `mat-*` folders go at open.
- Xbox slot filter: the entry's `containers.index` plus the GUID folders of the chosen slots.
- VERIFIED on three real entries (`<D3>\proto_store.py`): Steam full 239 files / 187 MB in
  0.24 s, Xbox full 94 files / 65 MB in 0.10 s, extract 4 files in 0.01 s; each identical to its
  original folder in names, bytes **and** mtimes.

### 2.8 `preserve(members)` for D5

D1 contract: `preserve(members: Sequence[LiveMember], *, op_id, reason) -> list[Location]`,
durable, hash-verified, never prunable, one location per member in order; raises
`PreserveFailed`.

1. Group the members by target (platform, account key, slot).
2. For each target, read the slot's files from the live folder (resolved with the `live_dir`
   callable): Steam `saveN.hg` / `mf_saveN.hg` of **both** members; Xbox the slot's GUID folders
   and a rebuilt minimal index. Both members are kept even if only one was asked for (pair
   integrity; D2 rule 5 for the same reason).
3. Hash each data file; a requested member whose hash is not its `expected_vid` raises
   `PreserveMismatch` (the slot changed since D5 planned; D5 re-plans).
4. Add a store entry of kind `preserve`, label `kept: slot N (<name>)`, with `op_id` and
   `reason`. A preserve entry is written **every time**, even when both objects deduplicate:
   it costs one ~3 KB manifest, records exactly what was in the slot (meta bytes, ordinals,
   mtimes), and is the stable, never-prunable `StoreRef` that D1's `OpLineage.preserved` points
   at.
5. Verify before returning: re-read every object the entry references (not only new ones), check
   sha256, decrypt each Steam meta with its recorded ordinal, check `SizeDecompressed`. Only then
   return `Location(vid, "store", StoreRef(entry_id), slot, member, platform, durable=True,
   prunable=False)`.

Cost: two data files plus metas, ~10-30 ms of I/O plus fsyncs (a whole Steam snapshot takes
0.17 s, VERIFIED). `space_needed_for_preserve(members)` is exact: the sizes of the members whose
`expected_vid` is not already stored, plus 64 KiB; D5 blocks with `VAULT_SPACE` on a shortfall.

### 2.9 Mapping of every existing operation

A vault is in **store mode** when `store/FORMAT` exists and this app can write it. Then new
backup-like entries go to the store; otherwise the app behaves as today.

| Operation (`operations.py`) | Today | Store mode |
|---|---|---|
| `create_full_backup` (165) | `copytree` to `backups/<id>`, sha256 verify, catalog entry, oplog `full_backup` | `add_from_live(kind="full", scope=whole folder, cache per 3.3)`; shadow entry (3.5); oplog `full_backup` with `entry_id` = store id |
| `snapshot_live` (134) + `_prune_snapshots` (148) | copy `*.hg` + vdf (Xbox: whole tree) to `snapshots/<id>`, prune keep-20 by age | `add_from_live(kind="snapshot")`; **no** age pruning (retention, 2.11 / 3.2) |
| `extract_slot` (280, 466) | slot files / minimal wgs folder to `extracts/<id>` | `add_from_live(kind="extract", scope=slot)`; shadow entry |
| `preserve` (new, D5) | - | 2.8 |
| `restore_full` (224, 525) | folder -> live | `with store.materialized(id, cache=False) as folder:` the unchanged `restore_full` on `folder` (after P7). Later, optionally, a `CaptureSource` refactor reads `read_file()` directly and skips the temp copy |
| `restore_entry` (191) | extract -> `repopulate_slot` same slot | materialize the one slot, then the same call |
| `repopulate_slot` (306, 492) classic Copy | from a folder | materialize the slot; organizer Place (D5) uses `open(vid)` instead |
| `undo_last` (881) classic | `restore_full` of the newest record's snapshot | resolve the id: `snapshot_store_id` first, else `snapshot_id` in catalog or store; materialize; `restore_full`. Becomes D5's "panic restore" (C24) |
| `promote_member` (367), `clear_slot` (701) | live write after a folder snapshot | live write after a store snapshot (+ D5 `preserve`) |
| `plan_clear_slot` (671) | latest entry with the same slot **number** (P5) | by identity: `is_durable(vid)` of the slot's members |
| `import_backup` copy (766) | `copytree` to `backups/imported-...` | `add_from_folder(kind="imported", source={"type": "folder", "name": <folder name>})` |
| `import_backup` in place | catalog only | unchanged |
| `import_vault` copy (811), legacy source | copy folders | fold each source entry by id (idempotent, deduplicated) |
| `import_vault`, store source | - | merge (4.4) |
| `import_vault` in place, store source | - | refused with a message ("copy it in instead"): a second store is not referenced in place |
| delete a backup (new, owner) | none | `trash(id, by="owner")` with the last-copy check (2.11) |

**Oplog records (coordination with D5).** New-app lines keep the v1 shape; keys are additive (R1
E4). When the snapshot a line refers to exists only in the store, the line carries
`"snapshot_id": "snapshot-needs-newer-app"` (the barrier entry, 4.2) and the real id in
`"snapshot_store_id"`. One helper produces every line, so no call site can forget it. The new
app's undo reads `snapshot_store_id` first. Under B this replaces D5's requirement that
`snapshot_id` names a whole-folder snapshot (D5 conclusion 1): both protect older apps; the
barrier makes an older app's Undo refuse instead of restoring.

### 2.10 Integrity: `fsck`

| Level | Checks | Owner's vault (VERIFIED, prototype) |
|---|---|---|
| quick | `FORMAT` readable; every manifest parses and its `body_sha256` matches; every referenced object exists with the listed size; inline payloads decode to their size and sha256; `tmp/` leftovers; orphan count; compat: barrier entry, stub folder, shadow paths | 83 ms (41 manifests, 353 objects; first read) |
| deep | quick + re-hash every object; Steam members: meta decrypts with the recorded ordinal, `SizeDecompressed` == chunk total, `pid` == id in the first chunk (optional, ~23 ms per version, R7 2.9); Xbox: index and `container.N` parse, GUIDs resolve | 0.25 s for 498 MB (warm), plus ~4.5 s for the optional first-chunk ids of 195 versions |
| repair | missing or corrupt object: search every fallback location (legacy folders, in-place, live, a second vault the owner points at) for bytes with that sha256, re-put them; stale `tmp/`: delete; missing stub or barrier: re-create; otherwise list the entry in `state/damage.json` (shown to the owner, never deleted) | - |

When: quick at open when the vault is on removable or network media and after any crash
recovery; deep after a fold, on demand (`nmsvault store fsck [--deep] [--repair]`, a GUI item)
and suggested monthly. Every read verifies anyway (2.7).

### 2.11 References, trash, tombstones and garbage collection

- **Reference**: an object digest listed (non-inline) by a manifest in `entries/` or `trash/`.
- **Trash** (`trash(id, by, reason)`): write `trash/<id>.trashed.json`, then `os.replace` the
  manifest into `trash/`, then drop its shadow catalog entry. `untrash` reverses. `purge_due`
  deletes manifest and sidecar once `purge_after_utc` (14 days) has passed. A trashed entry keeps
  its objects alive and stays readable during the grace period.
- **Last-copy rule (C27)**: `trash(..., by="retention")` raises `LastCopyError` when the entry
  holds a vid that no other entry outside the trash holds (durable legacy folders count, per D2's
  durability classes). `trash(..., by="owner")` of such an entry requires `confirm_unique` to
  list exactly those vids; the UI shows "N versions exist only in this backup" first, and the
  oplog line records the list.
- **Tombstones**: `tombstone(vids, owner_confirmed=True)` records versions the owner agreed to
  delete (per-line limits, 3.2). GC deletes their objects even when referenced; reads of such a
  member raise `VersionUnavailable` with "deleted by you on <date>"; a whole-folder materialize of
  an entry with a tombstoned member is refused unless the slots exclude it.
- **GC**, under the lock: re-parse every manifest in `entries/` and `trash/` from disk (never the
  in-memory index), collect the referenced digests, subtract tombstoned vids, delete every object
  file not in that set, empty `tmp/`. `dry_run` reports counts and bytes. Idempotent; a crash
  midway leaves orphans for the next run. GC does not exist in the append-only core (3.4).

Invariant: an object is deleted only when no manifest references it, or when its vid carries an
owner-confirmed tombstone.

### 2.12 Retention on top (C27)

Retention decides which **entries** go to the trash and which **versions** get tombstones; GC only
frees what is no longer referenced. Inputs come from D1: `protection(vid)` (`live`, `locked`,
`noted`, `tip`, `fork`, `placed`, `evicted`), `locked_vids()`, the line (`pid`) of each vid, and
the oplog ids inside the undo window.

```python
@dataclass(frozen=True)
class RetentionSettings:
    mode: Literal["keep_all", "thin_snapshots", "per_line_limit"] = "keep_all"
    keep_snapshots_per_account: int = 20
    undo_window_records: int = 50
    per_line_keep: int = 30
    per_line_weekly_after_days: int = 30


@dataclass(frozen=True)
class RetentionPlan:
    trash_entries: tuple[str, ...]
    tombstone_vids: tuple[Vid, ...]
    frees_bytes: int
    needs_owner_confirmation: bool       # True whenever tombstone_vids is not empty
    why: Mapping[str, str]               # entry id or vid -> ASCII reason


def plan_retention(store: VersionStore, settings: RetentionSettings, *,
                   protection: Callable[[Vid], frozenset[str]], pid_of: Mapping[Vid, Pid],
                   legacy_durable: frozenset[Vid], undo_refs: frozenset[str],
                   now: datetime) -> RetentionPlan: ...
def apply_retention(store: VersionStore, plan: RetentionPlan, *, owner_confirmed: bool) -> None: ...
```

The variants are specified in 3.2. In every variant: entries of kind `full`, `extract`,
`imported` and `preserve` are never trashed automatically; no automatic step removes a vid's last
entry; tombstones need the owner.

### 2.13 Concurrency and locking

- One writer at a time: every mutation (add, preserve, trash, purge, tombstone, GC, fold, reclaim,
  compat catalog writes) runs inside D1's `safety.exclusive_lock(<vault>/organizer/.lock)`,
  released by the OS if the process dies. An add holds it for its whole duration: 0.2-0.3 s for a
  capture, about 0.1 s for a preserve, one entry at a time during a fold (D1 expects
  milliseconds; see Deviations). Holding it across the add is what keeps a concurrent GC from
  deleting an orphan object that a capture has just deduplicated onto.
- Readers take no lock: manifests and objects are immutable and appear atomically; GC deletes
  only unreferenced objects; trashed entries stay readable for 14 days.
- Older apps take no lock but never touch `store/`. Compat catalog writes re-read
  `catalog.json` inside our lock immediately before writing; the residual race with an old app
  writing the catalog at the same moment is pre-existing (R1 A9).
- If the lock cannot be taken (a file system without locks), the store opens read-only with a
  visible warning.

### 2.14 Safety invariants

1. Objects are written once and never modified; an object's name is the sha256 of its bytes;
   every read verifies it.
2. An entry exists only once its manifest is committed, and a committed manifest references only
   objects that were written or re-verified before the commit.
3. No code path deletes an object that a manifest in `entries/` or `trash/` references, except an
   owner-confirmed tombstone.
4. No automatic process (retention, GC, fold) removes the last stored copy of a version.
5. Legacy folders are deleted only by reclaim: owner-confirmed, after a fresh byte-for-byte
   verification, only managed folders inside `<vault>/{backups,snapshots,extracts}`; never an
   in-place folder, never a live folder.
6. The store writes only inside `<vault>/store/`, plus `catalog.json` (compat entries, under the
   lock) and materialize / export destinations, which may not be a live save root or the store.
7. Save data is stored and returned verbatim; only placement (D5) re-keys a meta.
8. Captures only read live folders.
9. Every manifest is self-checking (`body_sha256`), every file entry carries its sha256.
10. Standard library only: `hashlib`, `json`, `base64`, `os`, `shutil`, `uuid`.

### 2.15 Errors and what the owner sees

| Error | When | D5 code / UI text (ASCII) |
|---|---|---|
| `StoreUnavailable` | vault folder gone (USB unplugged), `FORMAT` unreadable | `VAULT_UNAVAILABLE` / "The vault is not reachable." |
| `StoreTooNew` | `FORMAT` compat newer than this app | "This vault was upgraded by a newer NMS Save Vault. Update the app." Opens read-only when only `write_compat` is newer |
| `InsufficientSpace` | free space < worst case + 64 MiB | `VAULT_SPACE` / "Not enough free space in the vault drive (needs N MB)." |
| `SourceChanged` | a live file changed three times while read | "A save changed while it was being copied. Is the game running?" |
| `PreserveMismatch` | slot changed since planning | D5 re-plans |
| `ObjectMissing` / `ObjectCorrupt` | read verification failed and no fallback had the bytes | "A stored copy of <name> is damaged. Run Check vault." Entry listed in `damage.json` |
| `LastCopyError` | retention or delete would drop a unique version | the confirmation dialog listing the versions |
| `FoldVerifyError` | a folded entry does not match its folder | the entry stays a legacy folder; reported |

### 2.16 Disk and performance budget

**Owner's vault now** (VERIFIED, `<D3>\analyze_store.py`, `<D3>\proto_store.py`):

| | Files | Size | On a 128-KiB exFAT drive |
|---|---|---|---|
| Today (41 managed folders + catalog/oplog/ids) | 4,517 | 3,405 MB | 3,910 MB |
| Store, everything folded, `cache/` kept (K1) | 349 objects + 41 manifests | 498.2 MB + 1.8 MB = **~500 MB** | ~536 MB |
| Store, `cache/` dropped (K3) | 166 objects + 41 manifests | 358.9 MB + ~1 MB = **~360 MB** | ~374 MB |
| Adopting the 7 in-place folders (optional) | | +73 MB (25 Steam versions 65.4 MB + cache 7.6 + account 0.4) | |
| First snapshot of today's live saves | | +44.5 MB (16 live-only versions: Steam 27.2, Xbox 17.3) | |

**Marginal cost of a new entry** (VERIFIED: the owner's 41 entries replayed in creation order
against a store, bytes not already stored):

| Kind | Folder today (median) | Store: new bytes, median / p90 / max |
|---|---|---|
| snapshot | 49.1 MB | 0.0 / 13.8 / 35.6 MB |
| full (Steam / Xbox) | 170.2 MB | 14.4 / 26.6 / 64.4 MB (median 4 new data versions); with `cache/` 15.2 MB median |
| extract | 1.2 MB | 0.0 / 0.0 / 0.09 MB |
| preserve (INFERRED) | - | 0 when the evicted pair was stored before, else the pair (~2-14 MB); plus a ~3 KB manifest |

`cache/` churn per Steam full: the first full brought 154 files / 116 MB, the next 13 brought
0-8 files / 0-5.6 MB each.

**Growth.** Replaying the record (2026-06-25 .. 10-05, 102 days): the store would hold 498 MB, of
which ~240 MB is the first copy of everything (first snapshot 42 MB, first Steam full with cache
133 MB, first Xbox full 64 MB) and ~258 MB came later: **~76 MB per month, ~0.9 GB per year** at
the owner's habit (~4 Steam fulls, ~1-2 Xbox fulls and ~5 snapshots a month). The folders grew
3.4 GB in the same period.

One-year projection (INFERRED from the rates above):

| Scenario | Assumption | Store after one year | Today's behaviour (folders, cache copied, keep-20) |
|---|---|---|---|
| S1 current habit | as measured | ~0.5 + 0.9 = **~1.4 GB** | ~3.4 + 9.4 = ~13 GB (D2 projects ~6.2 GB for option A with its cache policy) |
| S2 organizer use | S1 + one Place or Swap per play day (~150 a year): a pre-op snapshot (median 0, p90 14 MB) + a preserve (0-14 MB) | ~1.7-2.6 GB | + ~0.6 GB of preserved slots; snapshots stay capped |
| S3 full history (option, not proposed now) | S2 + every new live version the journal sees is captured (~600 a year x 2.4 MB mean) | ~3-4 GB | not offered |

**Time** (VERIFIED on this PC, SSD, warm cache):

| Operation | Measured |
|---|---|
| Fold all 41 entries (read 3,404 MB, write 353 objects / 498 MB, re-verify 2,964 dedupe hits / 2,905 MB, fsync each object and manifest) | 7.55 s |
| Verify every folded entry against its folder (materialize in memory, sha256 each file) | 1.52 s, 0 mismatches |
| Snapshot of the live Steam folder (59 files, 53.8 MB read, 10 new objects / 27.2 MB) | 0.17 s |
| Full of the live Steam folder with `cache/` (240 files, 191 MB read, 1 new object) | 0.27 s |
| Snapshot of the live Xbox folder (94 files, 67.7 MB read, 8 new objects / 17.4 MB) | 0.16 s |
| `open(vid)` (object read + sha256 + meta decrypt), average over 40 vids of 2.1 MB | 1.43 ms |
| Load all manifests and build the vid index | 6-7 ms (+0.2 ms index) |
| fsck quick / deep | 83 ms / 0.25 s |
| Hash every file of vault + live + in-place (4.5 GB) | 8.0 s |

Cold-cache and slow media (INFERRED): the fold is bound by reading 3.4 GB: ~10-20 s on an SSD,
~35-60 s on a hard disk, a USB 3 hard disk or a 1 GbE NAS. After a year (~600-800 entries,
~12-16 MB of manifests) loading the index takes ~40-55 ms (3.3 ms per MB measured); a deep fsck
of ~1.4 GB ~1 s warm.

Budgets for the implementation (CI-checkable on synthetic data scaled to these sizes): snapshot of
a 60 MB Steam folder < 0.5 s warm; preserve of one slot < 0.2 s; `open` of a 7 MB version < 5 ms;
store open with 1,000 entries < 100 ms; fold throughput > 300 MB/s warm with progress per file and
cancel between entries.

## 3. Options

### 3.1 Migration of the existing vault

| | M0 New captures only | M1 Fold, keep folders | M2 Fold, then reclaim (owner-confirmed) |
|---|---|---|---|
| What happens | Store gets new entries only; the 41 folders stay legacy entries, read through D2's `FolderLocator` | Every managed entry gets a store entry with the same id; folders untouched | M1, then the owner deletes verified folders, all or by kind |
| Vault size | 3.4 GB + growth | 3.9 GB (+0.5 GB) | ~0.5 GB |
| Snapshot-only versions (9 per D2, 6 single-folder per R7) | still in prunable folders: B keeps them only if retention never prunes legacy snapshots (today's keep-20 must stay off in store mode) | safe in the store | safe |
| Older apps | unchanged | unchanged | reclaimed entries become shadows: listed, not restorable (4.2) |
| Risk | none | none (adds files only) | deletion; mitigated by re-verification and confirmation |

**Fold, step by step** (per managed entry, oldest first; `state/migration.json` records the
status `pending -> folded -> verified -> reclaiming -> reclaimed`, or `skipped` / `failed` with a
reason):

1. Skip: in-place entries (outside the vault; never moved, never deleted), unmanaged references
   into another vault, any entry whose folder resolves to a live folder or inside a live root (P9:
   the owner's live `wgs` folder is catalogued as `inplace-20260624-182102`), entries whose
   folder is missing.
2. `add_from_folder(folder, kind=entry.kind, entry_id=entry.id, label=entry.label,
   created_local=entry.created, source={"type": "folder", "name": entry.source},
   origin={"legacy_entry": id, "legacy_subdir": ...}, scope=whole folder,
   cache=per policy)`. Member records are computed fresh from the bytes, so Xbox entries get the
   P1-fixed names and ids.
3. Verify: every file of the folder (walked again) is in the manifest with the same size and
   sha256; every referenced object re-hashes; no file of the manifest is missing from the folder.
   When D2's `manifests.json` exists (A shipped first) its hashes must agree too.
4. Idempotent and resumable: an interrupted fold leaves orphans that the re-run deduplicates
   onto; a manifest that already exists with the same id is verified instead of rewritten; same
   id with different content (an old app reused an id) folds under `<id>-L` and is reported.
5. When D2's `thumbs/` exists (A with cache option K3a), its files are folded as objects (they
   are already named by sha256).

**Reclaim, step by step** (owner-confirmed; the UI states the bytes it frees and the number of
entries, and lists any entry that fails):

1. Re-verify exactly as fold step 3, now, immediately before deleting.
2. Guard: the folder is a managed entry folder strictly inside `<vault>/{backups,snapshots,
   extracts}`; never in-place, never live.
3. Under the lock: set status `reclaiming`; turn the catalog entry into a shadow (same id and
   kind, `managed: false`, path = stub, label suffix) and save the catalog; then delete the folder.
   Order matters: the catalog never points at a half-deleted folder.
4. Deletion uses `shutil.rmtree` with an error callback that retries once and records leftovers
   (virus scanner or Explorer handles); never `ignore_errors=True` silently; leftovers are retried
   at the next open. Status `reclaimed`.
5. Crash between 3 and 4: on resume a `reclaiming` entry has its remaining files deleted (the
   store entry is authoritative and was verified).

Every reclaimed entry, of every kind, becomes a shadow: removing a reclaimed snapshot's catalog
entry instead would let an older app's Undo skip it and restore an even older snapshot (4.2).

**Optional: adopt in-place folders.** `adopt_inplace(entry_id)` adds a store entry of kind
`imported` (origin: the in-place entry id, never its path) from the in-place folder as it is now;
the folder and its catalog entry stay. Owner: +73 MB; the 16 versions that exist only in those
folders (R7 2.5) become durable. Never for the live alias (P9).

**Recommendation: M1, then M2 on request.** Offer M1 on the first start in store mode as one
dialog with the numbers ("adds 0.5 GB, deletes nothing, about 10-60 s"), run it in the
background with progress and cancel. Then offer M2 as "Reclaim 2.9 GB" with a per-kind choice
(snapshots first, they are the clutter) and a standing opt-in "reclaim automatically after
verification" the owner may switch on. Adopt in-place: offer it, default off (it is the owner's
call whether PC-A/PC-B are archives or scratch).

### 3.2 Retention (F9)

| | R-A Keep everything | R-B Thin snapshots | R-C Per-line limit |
|---|---|---|---|
| Automatic deletion | none | snapshot entries only, and only entries whose every vid is held by another entry | none without confirmation |
| What it frees | nothing | manifests only (~10-30 KB each); never version bytes | version bytes (tombstones) |
| Rule | - | trash a snapshot entry when: it is not among the newest `keep_snapshots_per_account` (20) of its platform + account; no record in the undo window (newest 50 oplog lines) names it; it holds no locked vid; every vid it holds is held by another entry outside the trash or a durable legacy folder | per line (pid), protected versions are kept (`live`, `locked`, `noted`, `tip`, `fork`, `placed`, `evicted`, D1 2.7.5); of the rest keep the newest `per_line_keep` (30) and one per week older than 30 days; the remainder is proposed as a list ("12 versions of Main Save, 41 MB"); tombstoned only after the owner confirms; GC then frees the bytes |
| Owner-visible | nothing changes | old auto-snapshots disappear from lists; every version stays | a "Free space" preview with the exact list and size |
| Cost / year (owner, S1) | ~0.9 GB | ~0.9 GB (same bytes; fewer entries) | owner-controlled |

**Recommendation: R-A by default** (the parent's lean "keep everything + C27"). With B, keeping
everything costs ~0.9 GB a year, and an auto-snapshot of unchanged saves costs 0 bytes, so age
pruning buys nothing. Offer R-B as a setting when the snapshot list gets long (clutter, not
space), and R-C as a manual "Free space" tool, never scheduled. Today's keep-20 age pruning
(`SNAPSHOT_RETENTION`, `operations.py:31,148-159`) must not run on store entries in any variant,
and must not run on legacy snapshot folders once the vault is in store mode (P2: it can delete the
only copy of a version).

### 3.3 `cache/` handling

Facts (VERIFIED, R7 2.5, D2 2.12): `cache/` holds account-level location thumbnails
(`<hex16>.<mode>.<BaseName>.<steamid64>.DDS`, 921,600 B each) and server-feed JSON; no restore
path copies it back (`restore_full` copies only `*.hg` and the vdf, `operations.py:258-264`).

| | K1 Keep, deduplicated | K2 Stop capturing | K3 Drop on migration |
|---|---|---|---|
| New full backups | capture `cache/` (scope `cache: include`); new bytes per full: 0-5.6 MB (VERIFIED) | `cache: omit`; manifest records it | as K2 |
| Existing fulls (fold) | folded with `cache/`: 139 MB of objects | folded with `cache/` (K2 changes only new captures) | folded without `cache/`; manifest `cache: omitted`; the bytes go when the folder is reclaimed |
| Vault | ~500 MB | ~500 MB, then +0 per full | ~360 MB |
| Export of an old full | byte-identical folder incl. `cache/` | same | without `cache/` |
| Owner value | thumbnails stay available for a later UI (they are keyed by place, R1 3) | none lost yet | gone |

**Recommendation: K1.** Deduplicated, the cache costs 139 MB once and a few MB per full, keeps
every exported full byte-identical, and keeps thumbnails for a possible later UI. K3 only if the
owner says the thumbnails do not matter (D2 made the same call for option A: "keep one each,
139 MB").

### 3.4 When B ships

| | P-early: append-only core with the first live writes | P-late: A first, B in phase 5 (parent's lean, D2's pick) |
|---|---|---|
| Phase 2 | store core (section 6, steps B-1 .. B-11): objects, entries, capture, materialize, `StoreLocator`, `preserve`, compat entries, routed classic ops, fsck. **No deletion of anything**: no GC, no trash, no reclaim, retention = R-A | D2's `FolderLocator`, preserve as `keep-*` extract folders, rescue of snapshot-only versions, version-aware pruning |
| Phase 5 | fold / reclaim (M1/M2), trash, GC, R-B/R-C, store merge, downgrade tools | the whole of B; fold also ingests A's `keep-*` / `rescue-*` / `thumbs/` |
| Disk until phase 5 | new entries ~15 MB per full, snapshots ~0; old folders stay | ~50 MB per full (A drops `cache/`), snapshots pruned with rescue |
| Old apps | cannot restore entries created after the upgrade (shadows, barrier) | can restore everything until phase 5 |
| Code written twice | no | A's preserve, rescue and pruning code becomes legacy at phase 5 (D2 4.4: still useful as the folder provider) |
| Risk | a new on-disk format carries the owner's evicted saves from day one; mitigated by never deleting anything in phase 2, verify on write and read, and the old folders untouched | lowest |

**Recommendation: P-early, if the owner picks B at all.** Its risky parts (deleting bytes) are
all in phase 5, while the phase-2 core is the simplest form of `preserve` (one verified,
deduplicated slot entry instead of folders plus a rescue mechanism). If the owner prefers that
older app versions keep restoring every backup, P-late is the safe choice and loses nothing:
D2's folder provider is B's legacy reader.

### 3.5 What older app versions see of store entries

| | V0 Barrier only | V1 Barrier + shadows for non-snapshot kinds (recommended) | V2 Shadows for everything |
|---|---|---|---|
| Old app's backup list | legacy entries only; store backups invisible | also every store full, extract, preserve, import and every reclaimed entry, with names, slots and play times; restore refused | also every snapshot |
| `catalog.json` growth | none | ~16 KB per full, ~1 KB per extract or preserve | + ~16 KB per snapshot: megabytes a year, rewritten whole on every change |
| Old Undo after a new-app op | refused (barrier) | refused (barrier) | refused |
| Old clear-slot plan | "no copy in the vault" may be wrong (P5 anyway) | sees the shadows' summaries | same |

Shadow entries use legacy kinds only (`preserve` -> `extract`), because an older app restores
an unknown kind with a mirroring full restore that deletes every other live slot (D2 E1, VERIFIED
on v0.0.9-v0.2.8).

## 4. Migration and compatibility

### 4.1 The owner's vault, step by step

1. Upgrade: the app creates `store/` (`FORMAT`, empty folders), the barrier entry and the stub
   (4.2). Nothing else changes; size +0.
2. From now on (store mode) every backup, snapshot, extract, import and preserve is a store entry:
   ~0-15 MB each instead of 1-187 MB.
3. M1 (one click): 41 entries folded in ~8-60 s (SSD warm .. hard disk cold), +498 MB of objects
   and 1.8 MB of manifests; skipped: the 7 PC-A/PC-B in-place entries and
   `inplace-20260624-182102` (the live `wgs` folder, P9). Verified against every folder.
4. Optional adopt of the 7 in-place folders: +73 MB.
5. M2 (owner-confirmed): 41 folders deleted after re-verification: 3,404 MB -> ~0.5 GB; the
   catalog keeps 49 entries (41 shadows, 8 in-place), about today's 771 KB.
6. The organizer reads store entries (vids and meta facts straight from the manifests) plus
   in-place and live through D2's provider. D1's bootstrap uses the store entries' vids instead of
   re-hashing the folded folders.

### 4.2 What an older app version sees

The vault as an older app finds it: `catalog.json` v1 holding legacy entries, the barrier entry
and shadow entries; `oplog.jsonl` whose newest live-write line names the barrier; `store/` and
`organizer/` folders it ignores.

- **Barrier entry**: id `snapshot-needs-newer-app`, kind `snapshot`, `managed: false`, path =
  absolute path of `store/compat/stub` (a folder holding only `README.txt`), `slots: []`, label
  "Undo of changes made by NMS Save Vault <version> or newer - use that version".
- **Shadow entry**: the store entry's id, a legacy kind, `managed: false`, path = the stub,
  `created` = `created_local`, `source` = account key, `slots` = v1 slot summaries built from the
  member records with exactly the 14 v1 member keys (`catalog.py:28-45`; `moved` = ordinal !=
  mapped ordinal), label suffix " [in version store - needs NMS Save Vault <version>+ or Export]".

Why the barrier is required: every released `undo_last` walks the oplog backwards and **skips** a
record whose snapshot entry is missing (v0.1.0+ also when its folder is missing), then restores
the next older snapshot with a mirroring full restore (SOURCED `operations.py:888-907`; v0.0.9
`operations.py:614-626` in D2's archive). A new-app record naming a store-only snapshot would make
an old Undo roll the live folder back to an older state. With the barrier the old Undo stops at
the barrier and refuses.

VERIFIED (`<D3>\compat_store_exp.py`, synthetic sandbox, old sources from D2's `git archive`
trees, run with `python -S`):

| # | Old app action | v0.0.9 | v0.1.0 | v0.2.8 |
|---|---|---|---|---|
| X1 | Undo, Steam live, newest record = barrier | refused: "'snapshot-needs-newer-app' has no Steam-format save*.hg files, so it cannot be restored into a Steam folder."; live and vault unchanged | same | same |
| X2 | Undo, Xbox-shaped live | refused: FeatureNotYetAvailableError (the stub is not a `wgs` folder); live unchanged | same | same |
| X3 | Restore a `full` shadow | refused (no `*.hg`); live unchanged | same | same |
| X4 | Restore an `extract` shadow | refused (no `*.hg`) | refused: "source slot 2 has no saves" | same as v0.1.0 |
| X5 | snapshot + prune with retention 1 | barrier, shadows, stub and `store/` kept; only its own managed snapshot pruned | same | same |
| X6 | `import_vault` (copy) of this vault | shadows re-catalogued as unmanaged references to the source stub; `store/` and `organizer/` not carried | same | same |
| X7 | catalog load + save | barrier and shadows kept, `version: 1` | same | same |
| X8 | clear-slot plan | - | - | uses the shadow's summary ("backed up in extract-...") |

Releases before v0.0.9 already crash on the owner's catalog (D2: `MemberSummary.difficulty`), so
v0.0.9-v0.2.8 is the realistic old set.

What still works in an older app: everything on legacy folders not yet reclaimed; its own new
backups (legacy folders, which the new app folds on its next start when M1/M2 is on). What does
not: restoring or undoing anything that lives only in the store. An older app writing into a
store vault cannot damage the store (it never touches `store/`, never prunes unmanaged entries).

### 4.3 Downgrade and "Export to folder"

- **Export** (`export_entry(id, dest, slots=None, account_files=True, cache=True)`): materialize
  to any folder outside the vault and the live roots; byte-identical names, bytes and mtimes;
  verified after writing. A Steam export is an `st_`-shaped folder that any tool reading plain
  save folders can open (NomNom, goatfungus, a manual copy). An Xbox export is a `wgs`-shaped
  folder (index + GUID folders).
- **Unfold** (`unfold(id)`): materialize into the legacy location `<vault>/<subdir>/<id>`, verify,
  then turn the shadow into a normal managed entry pointing there. Older apps can restore it
  again. The store entry stays (the M1 state for that entry).
- **Downgrade the whole vault**: unfold every store entry (the UI shows the space needed: the sum
  of the manifests' sizes; ~3.4 GB plus everything captured since), then optionally delete
  `store/` (owner-confirmed, refused unless every entry is unfolded and verified). Afterwards the
  vault is a plain v1 vault again; the barrier entry is removed last.
- The oplog keeps its lines; after a downgrade, lines naming the barrier are inert for old apps
  (refused) and `snapshot_store_id` points at unfolded folders with the same ids.

### 4.4 Importing vaults

| Source -> target | Behaviour |
|---|---|
| Legacy vault -> store-mode vault, copy | each source entry folded by id (deduplicated, verified); the source untouched |
| Legacy vault -> store-mode vault, in place | as today: unmanaged references; never folded or reclaimed |
| Store vault -> store-mode vault | **merge**: copy every missing object (verify), copy every manifest whose id is absent; same id + same `body_sha256` = skip; same id + different body = import as `<id>-m<n>` and report; shadows re-synced; `source` keeps the other `store_id` in `origin` |
| Store vault -> legacy app | old `import_vault` copies only legacy entries; shadows come along as unmanaged references (X6) |

Merging is the natural way to mirror a vault onto a USB drive: after the first copy only new
objects move (~76 MB a month for the owner).

### 4.5 D11 portability

- The store holds no absolute path: object paths come from hashes, manifest paths are relative,
  `source` holds folder names, never paths. A vault on a USB drive or NAS opens in place on any
  machine and OS; Xbox entries can be listed and exported anywhere and placed only on Windows (D3
  of DESIGN.md).
- The only absolute paths are those `catalog.json` already has, plus the barrier and shadow paths
  (older apps need absolute paths: a relative path resolves against their working directory,
  D2 E4). When the new app opens a vault whose stub path in the catalog differs from the current
  location, it rewrites the barrier and shadow paths under the lock (together with D2's re-homing
  of managed entries). An older app opening a moved vault before the new app did finds every
  managed entry and the barrier missing; its Undo then has nothing restorable and refuses.

### 4.6 A vault on USB or NAS

- File systems: no hard links, symlinks, sparse files, alternate streams or sqlite; small files
  inline (no cluster waste); names short and case-safe; `os.replace` within one volume only (temp
  files live in `store/tmp/` on the same volume).
- FAT32 / exFAT: rename is not journalled, so a power loss can leave a torn rename; the commit
  protocol plus verify-on-read plus `fsck` covers it. FAT32 mtimes have 2-second resolution: the
  manifest keeps the source's exact `mtime_ns`, so only exports onto FAT lose precision.
- NAS (SMB): `msvcrt.locking` byte-range locks work over SMB; `os.fsync` maps to
  `FlushFileBuffers`, which the SMB redirector honours. Reading 0.5 GB over 1 GbE takes ~5 s
  (fsck deep). If locking is not supported, the store is read-only (2.13).
- Unplugged mid-capture: the live folder is untouched (captures only read it); the store keeps
  orphans and `tmp/` leftovers, cleaned at the next open; the op that needed the capture fails
  with `StoreUnavailable`, and D5 refuses its live write (`VAULT_UNAVAILABLE`).
- Quick fsck runs at open on removable and network media (2.10).

## 5. Test plan

Risk-based; all write tests on `tmp_path` sandboxes with synthetic saves or copies (project
rule). Highest risks first: losing the only copy (GC, retention, reclaim), undetected corruption,
crash consistency, old-app hazards, Xbox fidelity, Steam meta ordinals.

**Fixtures** (in `tests/conftest.py` unless noted):

| Fixture | What |
|---|---|
| `steam_live` | factory building an `st_` folder: N slots of chunked synthetic data with real XXTEA metas (D2's `steam_pair`, promoted from `tests/test_playtime.py:43-63`), `accountdata.hg` + meta, vdf, `cache/` with three dummy DDS files, optional "moved" meta |
| `wgs_account` | `make_wgs_account` fixed to real 296/360-byte metas with id 0x118 and time 0x120 (P1), plus `AccountData` |
| `legacy_vault` | a `Vault` filled by the real ops: Steam full with cache, three snapshots, an extract, an import (copy), an in-place entry, an in-place entry that is the live folder, oplog lines |
| `store` | `VersionStore.open(create=True)` in `tmp_path` with a real `safety.exclusive_lock` |
| `faults` | a `FaultPlan` passed into the store (test-only parameter, default None) raising at named points: `object.after_tmp`, `object.after_replace`, `entry.before_replace`, `entry.after_replace`, `gc.mid`, `reclaim.after_catalog`, `reclaim.mid_rmtree` |
| `old_app` (marked `compat`, slow) | `git archive v0.1.0` / `v0.2.8` into `tmp_path`, run with `python -S` in a subprocess against a sandbox vault, as `<D3>\compat_store_exp.py`; skipped when the tags are absent |
| `real_vault_ro` (optional) | skipped unless `NMSVAULT_REAL_VAULT_TEST=1`: folds the real vault **reading only** into a `tmp_path` store and asserts 0 mismatches and an unchanged vault fingerprint (as `<D3>\proto_store.py`) |

**Tests**

| # | Test | Asserts |
|---|---|---|
| T1 | `test_object_put_get_verifies` | put/get round trip; `get` of a flipped byte raises `ObjectCorrupt` |
| T2 | `test_dedupe_hit_rehashes_and_heals` | a corrupt existing object is quarantined and rewritten on the next put of the same bytes |
| T3 | `test_capture_steam_exact_tree` | materialize == source: names, bytes, mtimes; member records (vid, ordinal, pid, play time) |
| T4 | `test_capture_xbox_exact_tree` | same for `wgs`; `savedir.scan_any(materialized)` equals `scan_any(source)` per member |
| T5 | `test_inline_threshold` | 8,192 B inline, 8,193 B object |
| T6 | `test_moved_meta_keeps_ordinal` | a meta keyed for another slot keeps its bytes and recorded ordinal; `open` decrypts it |
| T7 | `test_manifest_checksum` | editing one byte of a manifest -> `EntryCorrupt`; unknown keys round-trip |
| T8 | `test_crash_points_leave_consistent_store` | for each fault point: no visible half entry; fsck quick clean after the next open; re-run succeeds |
| T9 | `test_gc_never_deletes_referenced` | property test: random adds, trashes, purges, tombstones and GC runs; every entry outside a tombstone stays fully readable |
| T10 | `test_trash_grace_untrash_purge` | trashed entry readable until purge; untrash restores it |
| T11 | `test_retention_never_drops_unique` | R-B never trashes an entry holding a unique vid; R-C tombstones only with `owner_confirmed=True` |
| T12 | `test_preserve_contract` | D1's locator conformance suite (D1 test plan item 10) on `StoreLocator`; `PreserveMismatch` when the live slot changed; preserve writes both members and re-verifies every referenced object |
| T13 | `test_fold_idempotent_resumable` | fault at entry k; re-run completes; same ids; 0 mismatches |
| T14 | `test_reclaim_guards` | needs owner confirmation; refuses a folder changed after the fold; never touches in-place or live folders; leaves a shadow; resumes a `reclaiming` entry |
| T15 | `test_restore_from_store_equals_folder` | `restore_full` / `restore_entry` / classic `undo_last` through `materialized()` give the same live folder as the legacy path |
| T16 | `test_oplog_barrier_helper` | every new-app line whose snapshot is store-only names the barrier and carries `snapshot_store_id`; the new undo uses the latter |
| T17 | `test_old_app_contract` (`compat`) | X1-X7 of 4.2 against v0.1.0 and v0.2.8 |
| T18 | `test_shadow_entries_are_v1` | shadows load in the current `Vault`, have only the 14 v1 member keys, legacy kinds only |
| T19 | `test_export_and_unfold` | export byte-identical; an unfolded entry restores through the classic code path |
| T20 | `test_import_store_vault_merge` | objects and manifests merged once; conflicting id imported as `-m1` |
| T21 | `test_two_process_lock` | a capture in one process and GC in another never produce a dangling reference |
| T22 | `test_store_has_no_absolute_paths` | no drive letter, no leading slash, no user-profile name in any manifest |
| T23 | `test_fsck_reports_and_repairs` | missing, corrupt, orphan, stale tmp, missing stub; repair from a fallback folder copy |
| T24 | `test_source_changed_during_capture` | a file that changes between reads raises `SourceChanged` after three tries |
| T25 | `test_path_lengths` | the deepest store and materialize paths stay under 260 for a vault root of 100 characters |

**Integrity checks in production**: verify on every write and read (2.6, 2.7), `fsck` quick at
open on removable media and after recovery, deep after a fold and on demand (2.10).

## 6. Implementation plan

Sizes: S <= ~150 lines of production code plus tests, M ~150-400, per commit. Each step ends
with `uv run pytest` green and one commit.

| Step | Content | Size | Tests | Phase (P-early / P-late) |
|---|---|---|---|---|
| B-0 | prerequisites outside D3: P1, P7, P9; D1's `safety.exclusive_lock`; this design's `safety.fsync_dir` | S | lock + fsync_dir | 0 / 0 |
| B-1 | `store/FORMAT` create / open / compat checks, `tmp/` sweep | S | open, too-new, read-only | 2 / 5 |
| B-2 | `ObjectStore` put / get / verify / quarantine | S | T1, T2 | 2 / 5 |
| B-3 | `StoreEntry` dataclasses, canonical JSON, strict loader | S | T5, T7 | 2 / 5 |
| B-4 | Steam `add_from_folder` / `add_from_live`, scopes, member records, stability check, crash points | M | T3, T6, T8, T24 | 2 / 5 |
| B-5 | Xbox capture: verbatim index, slot-scoped rebuilt index | M | T4 | 2 / 5 |
| B-6 | materialize / `materialized()` / export | S | T19 (export), T25 | 2 / 5 |
| B-7 | `StoreLocator` locate / open / is_durable, index, fallbacks, self-heal | M | T12 (read part), T23 (repair part) | 2 / 5 |
| B-8 | `preserve()` | M | T12 | 2 / 5 |
| B-9 | compat: barrier, stub, shadows (V1), id uniqueness, path re-basing, oplog helper | M | T16, T17, T18 | 2 / 5 |
| B-10 | route classic ops through the store in store mode; disable age pruning in store mode | M | T15 | 2 / 5 |
| B-11 | `fsck` quick / deep / repair + `nmsvault store fsck` | M | T23 | 2 / 5 |
| - | **end of the append-only core** (no deletion code exists before this line) | | | |
| B-12 | fold (M1), `migration.json`, progress / cancel, `thumbs/` ingest, D2 manifest cross-check | M | T13 | 5 / 5 |
| B-13 | reclaim (M2), shadows for reclaimed entries, leftovers | M | T14 | 5 / 5 |
| B-14 | trash / untrash / purge, tombstones, GC | M | T9, T10, T21 | 5 / 5 |
| B-15 | retention planner R-A / R-B / R-C with confirmation flow | M | T11 | 5 / 5 |
| B-16 | `import_vault` store merge | S | T20 | 5 / 5 |
| B-17 | unfold, downgrade vault, remove store | S | T19 | 5 / 5 |
| B-18 | DESIGN.md (storage model, invariants), README (vault format, older versions, export) | S | - | with B-11 and B-17 |

Rough total: ~2,000-2,600 lines of production code and ~1,500-2,000 lines of tests; the
append-only core (B-1 .. B-11) is about 60 % of it.

## 7. Risks and open questions

**Risks** (most severe first)

1. **One copy per version.** Deduplication removes today's accidental redundancy (a version sits
   in 6 folders on average, R1 2h): a damaged object affects every entry that references it.
   Mitigations: verification on write, on every dedupe hit and on every read; fsck; self-heal from
   legacy, in-place and live copies; quarantine instead of deletion. Residual: a disk failure
   loses the vault, as today. Recommend mirroring the vault to a second drive (4.4 merge; cheap
   now).
2. **A bug in GC, retention or reclaim deletes many versions at once.** Mitigations: no deletion
   code in the append-only core; GC recomputes references from disk; property tests (T9); dry
   runs; 14-day trash; reclaim re-verifies and needs the owner.
3. **Older app versions cannot restore store entries.** Mitigations: barrier and shadows (VERIFIED
   safe on v0.0.9-v0.2.8), export, unfold, downgrade path; README states it.
4. **Coordination with D5.** The oplog barrier helper (2.9) must be the only way lines are
   written; D5's "snapshot_id names a whole-folder snapshot" rule is replaced under B.
5. **Slow or quirky media.** Fold time on hard disks and NAS (~35-60 s), the virus scanner's
   first-read penalty (185 ms vs 6 ms for 41 new manifests), exFAT rename atomicity, SMB locking.
   Mitigations: resumable per-entry fold, progress, read-only fallback, quick fsck at open.
6. **Lock hold time.** An add holds the vault lock for 0.1-0.3 s (seconds for a large fold entry
   on slow media); D1's journal writers wait (timeout 10 s).
7. **Manifest schema evolution.** `FORMAT` `read_compat` / `write_compat`; manifests immutable;
   unknown keys ignored by readers and covered by the checksum.
8. **Folder-reading tools** (NomNom) cannot browse the vault directly; Export covers it.

**Open questions**

| # | Question | Whose | Recommendation |
|---|---|---|---|
| Q1 | B at all, and when (3.4) | owner (F3) | B, P-early append-only; P-late if older-version restore matters |
| Q2 | Fold the old backups (3.1) | owner | M1 on one click, M2 on request |
| Q3 | Retention (3.2, F9) | owner | R-A; R-B as a setting; R-C as a manual tool |
| Q4 | `cache/` (3.3) | owner | K1 |
| Q5 | Adopt in-place folders | owner | offer, default off |
| Q6 | Old-app visibility (3.5) | design | V1 |
| Q7 | Persistent index cache | design | not before ~2,000 entries |
| Q8 | Owner validation run | owner, once | on the real machine after B-10: full backup, export, compare with the live folder; restore the export into a throwaway slot |

## Deviations from 01-consolidation section 6

- **A second vault-resident store.** Section 6 names `<vault>/organizer/` (D1) as the vault's
  store. B adds `<vault>/store/`, owned by D3, holding objects and entry manifests; it shares D1's
  lock file and keys nothing by path.
- **`Location` for the store carries slot and member.** D1's `Location.slot` comment says "None
  for a store object"; a store location here is `(StoreRef(entry_id), slot, member)` of the entry
  that holds the vid, because the meta (and its ordinal) belongs to that entry. `StoreRef.key` =
  entry id.
- **"Capture" naming.** The persisted backup unit is a "store entry"; D1's capture stays one slot
  observation. A store entry yields one D1 capture per occupied slot (`where = StoreRef`,
  `observed_at` = `created_utc`, `time_quality = "exact"`).
- **`open(vid, prefer)` meta choice.** When a vid has more than one meta plaintext, `open` returns
  `prefer`'s, else the newest store entry's; `OpenedVersion.location` reports which.
- **Lock duration.** D1 expects the vault lock for milliseconds; a store add holds it for its
  whole duration (0.1-0.3 s, longer per fold entry) so that GC cannot delete an object a capture
  is about to reference.
- **Oplog convention for D5.** Lines whose snapshot exists only in the store carry
  `snapshot_id = "snapshot-needs-newer-app"` and `snapshot_store_id = <id>` (2.9, 4.2).

## Appendix A. Evidence

All scripts are in `<D3>`, run from the repo root with `PYTHONUTF8=1 uv run python <script>`
(the compat experiment with `python -S`). Every script opens real files with `open('rb')` only
and writes only under `<D3>`.

| Script | Purpose | Output |
|---|---|---|
| `file_inventory.py` | sha256, size, mtime and category of every file of the live folders and of all 49 catalog entries; vault fingerprint before and after | `files.jsonl`, `folders.json`, `inventory_summary.json` (4,508,041,245 B hashed in 8.02 s) |
| `analyze_store.py` | store size per variant, deduplication by category, marginal cost per entry in creation order, live-only and in-place extras, Steam meta raw vs plaintext, cache churn | `store_analysis.json` |
| `chunk_dedupe.py` | chunk-level vs whole-file deduplication over the 195 distinct data files | `chunk_dedupe.json` |
| `proto_store.py` | the prototype: folds the 41 managed entries into `<D3>\proto\store`, verifies all, materializes three, times index load and fsck | `proto_report.json` |
| `proto_ops.py` | live snapshot / full captures into the prototype store, `open(vid)` and XXTEA timings | `proto_ops_report.json` |
| `compat_store_exp.py <old-src> <label>` | X1-X8 of 4.2 against v0.0.9, v0.1.0, v0.2.8 in `<D3>\sbx\<label>\` | `sbx\<label>\result.json` |

Read-only proof (VERIFIED after the prototype runs): the vault still has 4,517 files and
3,404,610,911 B, newest mtime unchanged, `catalog.json` sha256 `a3a58014...a045b9` and
`oplog.jsonl` sha256 `29b3ece9...598aaa` as at R1's baseline.
