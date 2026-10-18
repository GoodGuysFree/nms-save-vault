# D3 - Storage: the content-addressed version store

Status: **chosen design** (owner decision F3-B, `01-consolidation.md` 4a), updated 2026-10-17 for
the owner's answers to the design review (`03-design-review.md` Q1, Q2, Q7). The store starts
with phase 2; the vault is copied aside first; older app versions are not supported on an
upgraded vault; everything is kept. No production code, tests or packaging were changed.

Audience: implementing agents (completeness wins) and the parent. Bound by `01-consolidation.md`
sections 2, 4a and 6 and by D1's interfaces (`D1-core-model.md` 2.11). D2 (option A, not chosen)
survives only as the legacy-folder reader B uses until the old folders are freed (D2 sections
2-4).

Evidence tags: **VERIFIED** = run or measured (script named); **SOURCED** = code `file:line` or a
research/design report; **INFERRED** = reasoning from those. Scripts and outputs live in
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\<session>\scratchpad\D3\` (written `<D3>`) and
`...\scratchpad\U2\` (written `<U2>`). Redaction: `st_<steamid64>`, `<xbox-account>`,
`%APPDATA%`, `%LOCALAPPDATA%`.

All real-data work was read-only. A prototype store was built in `<D3>\proto\` and a prototype
copy-aside in `<U2>\copyout\` (deleted after the run) by *reading* the owner's vault; the vault
fingerprint (4,517 files, 3,404,610,911 B, `catalog.json` sha256 `a3a58014...a045b9`,
`oplog.jsonl` `29b3ece9...598aaa`) was identical before and after (Appendix A).

## 0. Owner summary

- The vault keeps each save version once; a backup becomes a small list that points at those copies. Your vault: 3.4 GB -> ~0.5 GB once the old folders are freed.
- Upgrade, at the start of phase 2: first a full copy of the vault is made next to it (3.4 GB, about one minute, checked file by file). Nothing in the vault is changed before that copy is verified.
- Then new backups, auto-snapshots and the copies kept when you move a save out of a slot go to the store, and your old backups are read into it automatically (+0.5 GB; nothing is deleted).
- "Undo the vault upgrade" uses that copy and keeps everything made since. The old folders are freed only when you ask, after a full check (phase 5).
- Thumbnails are kept, once each (139 MB). Your hand-made backup folders outside the vault stay listed where they are; one action copies them in (+73 MB, off by default).
- Older app versions are not supported on an upgraded vault (your decision).

## 1. Scope, non-goals, dependencies

**In scope**

- The store layout inside the vault: objects, entry manifests, quarantine, state.
- What is stored per version and per backup: data, metas (Steam XXTEA, Xbox plaintext), Xbox
  index and containers, account-level files, `cache/`.
- The write path for every backup-like operation (full backup, snapshot, extract, import,
  `preserve`) and how every existing operation maps onto the store.
- `StoreLocator`, D3's implementation of D1's `VersionLocator` (`locate`, `open`, `preserve`,
  `is_durable`), materialize and export.
- One listing of catalog and store entries for D1's scanner, the classic tab and the CLI.
- Integrity (verify on write and read, `fsck`), crash safety, locking.
- The upgrade: copy-aside, store creation, fold of the old backups, copy-in of hand-made in-place
  backup folders, rollback; freeing old folders (phase 5); importing vaults; D11, USB / NAS;
  disk and performance budget.

**Non-goals**

- Identity, captures, lineage, notes, journal, read model, organizer store and lock: D1.
- Live writes (Place / Swap / undo / verification) and the op record format: D5.
- Content facts: D4. UI: D6.
- Compression of stored bytes (save data is already LZ4; the verbatim rule) and sub-file
  deduplication: measured at 0.4 % extra saving (2.3), rejected.
- Any deletion of versions, entries or objects (F9 keep everything). The only deletions are the
  app's own temporary files, its own unfinished copy after a failed upgrade, and, in phase 5, old
  folders the owner asks to free.
- Anything for older app versions (owner decision Q2, 3.2).
- Changes to the `catalog.json` v1 schema (C21).

**Dependencies**

| From | What D3 uses |
|---|---|
| D1 | `Vid`, `Pid`, `LiveTarget`, `SourceRef` (`LiveRef` / `EntryRef` / `StoreRef(key)`), `MetaSnap` fields, `Location`, `OpenedVersion`, `LiveMember`, `VersionLocator` ABC, `VersionUnavailable`, `PreserveFailed`; the vault lock `safety.exclusive_lock(<vault>/organizer/.lock)`; the per-machine cache dir |
| D2 sections 3-4 (kept parts) | `FolderLocator` (read side) over unfolded legacy folders, hand-made in-place folders and live; its durability classes; `Vault.entry_folder()` to resolve legacy folders of a moved vault |
| D5 | Calls `preserve()` before every live write; makes its pre-op snapshots with `add_from_live(kind="snapshot")`; reads rollback bytes with `read_file()` / `open()`; refuses organizer live operations while the vault is not in store mode |
| D4 | Reads bytes only through `open()` |
| Phase 0 | P1 (Xbox meta parser) for correct Xbox member records; P2 (snapshot pruning removed, D2 section 2); P7 (account-level files) before a full restore is routed through the store; P9 (live folder catalogued as in-place) so fold and listing skip it |
| New primitives | `safety.fsync_dir(path)`: fsync a directory on POSIX after a rename, no-op on Windows (OS branch, so it lives in `safety.py`) |

## 2. Design

### 2.1 Vocabulary

| Term | Meaning |
|---|---|
| object | one file's bytes stored once under the sha256 of those bytes. For a save data file the object name *is* the `vid` (C4) |
| store entry | one backup, snapshot, extract, import or preserve held by the store: an immutable JSON manifest listing every file of the captured folder or slot, plus, per slot member, its `vid`, meta and meta facts. The store's equivalent of a catalog entry. D1's `StoreRef(key)` uses the entry id as key. One store entry yields one D1 `Capture` per occupied slot |
| inline file | a file of at most 8 KiB kept inside the manifest (base64) instead of as an object |
| legacy entry | a catalog entry backed by a folder (today's layout); read through D2's `FolderLocator` |
| store mode | the vault has `store/FORMAT` and this app can write it: new entries go to the store |
| copy-aside | the verified copy of the whole vault made before the upgrade (4.1) |
| fold | create a store entry from a legacy entry's folder, keeping the entry id (4.2) |
| copy-in | fold of a hand-made in-place backup folder, on the owner's request (4.3) |
| free | delete a folded legacy folder: phase 5, on the owner's request, after a deep check (4.5) |

"Store entry", not "capture": D1 uses *capture* for one observation of one slot.

### 2.2 On-disk layout

```
<vault>/                           catalog.json, oplog.jsonl, backups/, snapshots/, extracts/
  |                                (legacy; the store never writes them, 4.4 and 4.5 aside),
  |                                organizer/ (D1)
  store/
    FORMAT                         {"format": "nmsvault-store", "version": 1, "read_compat": 1,
                                    "write_compat": 1, "store_id": "<uuid4 hex>",
                                    "hash": "sha256", "fanout": 2, "inline_max": 8192,
                                    "created_utc": "..."}
    objects/<aa>/<sha256>          verbatim bytes; name = 64 lower-case hex; <aa> = first 2 hex
    entries/<entry_id>.json        entry manifests; immutable once written
    quarantine/<sha256>.<n>        objects that failed verification; never deleted automatically
    tmp/                           staging on the same volume; own files older than 1 h swept at open
    state/upgrade.lock             OS lock target held for the whole upgrade or rollback (4.1, 4.4)
    state/upgrade.json             present only while an upgrade or rollback runs
    state/migration.json           copy-aside record; fold, copy-in and free status per legacy entry
    state/damage.json              entries with missing or corrupt objects (fsck); never auto-deleted
<vault parent>/<vault name>.pre-store-<UTC>/   the copy-aside (4.1); only the owner deletes it
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
  backup).
- Entry ids keep today's scheme `<kind>-<platform>-<YYYYMMDD-HHMMSS>[-n]` (local time, as
  `Vault.new_id`, `catalog.py:205-215`), unique across catalog **and** store; the id is assigned
  under the vault lock at commit time (2.12). Folded and copied-in entries keep their legacy id, so
  every `oplog.jsonl` and journal reference stays valid. New kind prefix: `preserve-`.

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
| `cache/*` (thumbnails, server-feed JSON) | 4 B - 921,600 B | object; captured by full backups and the fold (owner decision, 3.5) | 2,326 -> 184 objects (1,772 -> 139 MB) |

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
5. Placement re-keys anyway: `meta.re_key(raw, ordinal, dst)`; one XXTEA pass on 432 B costs
   0.19 ms (VERIFIED, pure Python).

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
         "meta_plain_sha256": "c3a1...", "pid": "0123456789ABCDEF", "play_time": 1916280,
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
| `created_utc`, `created_local` | ISO UTC; and the catalog v1 form (local, naive, seconds) for display and for sorting next to legacy entries |
| `app_version`, `platform`, `account_key` | `account_key` = live folder name, internal only, always shown through `aliases.redact` |
| `source` | D1 `SourceRef` as a dict: live account, legacy entry id, external folder name (never a path), or another store (`store_id` + entry id) |
| `scope` | `slots`: `"all"` or a list; `account_files`: bool; `cache`: `included` / `omitted` / `none` |
| `origin` | folded: `{"legacy_entry": id, "legacy_subdir": "backups"|"snapshots"|"extracts", "folded_utc"}`; copied-in: `{"inplace_entry": id, "copied_utc"}`; merged (phase 5): `{"store_id", "entry_id"}`; else null |
| `files[]` | every file the entry covers: posix relative `path`, `size`, `sha256`, `mtime_ns` (int), `inline` (base64, only when size <= `inline_max`). Sorted by path |
| `slots{}` | per slot `"1"`..`"15"`, per member `"A"`/`"B"`: `data` / `meta` paths (Steam names, or Xbox `<DIRGUID>/<GUID>`), `vid`, `ordinal` (Steam ordinal actually used; null for Xbox), `meta_plain_sha256` (null when unreadable), `pid` (16 hex from the meta, or null), the `MetaSnap` fields, `size_decompressed`, `valid` (Steam: header ok and SizeDecompressed == chunk total, as `_validate_pair`, `operations.py:925-940`; Xbox: meta parses), `note` (scan diagnostics), Xbox `identifier` (`Slot7Auto`) |
| `account{}` | paths of the account-level files this entry holds; absent keys = not captured |
| `xbox` | `{"index": "containers.index", "index_kind": "verbatim"|"rebuilt"}` or null |
| `body_sha256` | sha256 of the canonical serialization of every other key |

Canonical form: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`
encoded as ASCII. No floats anywhere (times are ints or ISO strings), so a parse and
re-serialization reproduces the bytes exactly; unknown keys written by a newer app are covered by
the checksum and ignored by readers. Manifests are never rewritten, so no key is ever lost.

Sizes (VERIFIED, prototype): 41 manifests = 1.81 MB; the largest is 72.9 KB (a Steam full listing
180 cache files); a snapshot is ~10-30 KB, a slot-scoped entry ~2-3 KB.

### 2.5 Modules and signatures

Package `nms_save_vault.core.store`, platform-neutral; OS-specific calls only through `safety`.

```
core/store/__init__.py     VersionStore, open_store, vault_mode, errors
core/store/objects.py      ObjectStore
core/store/manifest.py     StoreEntry, FileEntry, MemberRecord, canonical JSON, strict loader
core/store/capture.py      building entries from folders and live accounts (Steam, Xbox)
core/store/locator.py      StoreLocator(VersionLocator)
core/store/materialize.py  materialize, materialized, export
core/store/listing.py      VaultEntry, list_vault_entries, open_entry_folder, store_status (2.9)
core/store/fsck.py         quick / deep / repair
core/store/upgrade.py      copy-aside, store creation, interrupted-upgrade cleanup, rollback (4.1, 4.4)
core/store/migrate.py      fold, copy-in (4.2, 4.3); free old folders (4.5, phase 5)
```

```python
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, ContextManager, Iterable, Iterator, Literal, Mapping, Sequence

from ..organizer.model import Vid, Pid, Platform, Member, LiveTarget, EntryRef, StoreRef   # D1
from ..organizer.locator import (Location, OpenedVersion, LiveMember, VersionLocator,
                                 VersionUnavailable, PreserveFailed)                   # D1

EntryKind = Literal["full", "snapshot", "extract", "imported", "preserve"]
VaultMode = Literal["legacy", "upgrading", "store"]
STORE_VERSION = 1
INLINE_MAX = 8192


def vault_mode(vault_root: Path) -> VaultMode:
    """"upgrading" when store/state/upgrade.json exists (an upgrade or a rollback is running or
    was interrupted); else "store" when store/FORMAT exists; else "legacy". A few stat calls;
    no lock."""


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
    pid: Pid | None              # from the meta; D1 applies its fallback chain when None
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
    def quarantine(self, digest: str, reason: str) -> Path: ...         # rename aside, never delete
    # no delete(): no code path removes an object (2.13 invariant 3)


class VersionStore:
    root: Path
    store_id: str
    read_only: bool              # FORMAT.write_compat too new, medium read-only, or no lock support

    @classmethod
    def open(cls, vault_root: Path, *, lock: Callable[[], ContextManager[None]],
             app_version: str, clock: Callable[[], datetime]) -> "VersionStore": ...
             # raises StoreNotUpgraded unless vault_mode(vault_root) == "store"

    # reading (no lock)
    def entries(self) -> list[StoreEntry]: ...
    def entry(self, entry_id: str) -> StoreEntry: ...
    def has_entry(self, entry_id: str) -> bool: ...
    def vid_refs(self, vid: Vid) -> tuple[tuple[str, int, Member], ...]: ...   # (entry, slot, member)
    def read_file(self, entry_id: str, path: str) -> bytes: ...               # verified

    # writing (the vault lock only for the manifest commit, 2.12)
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
    def export_entry(self, entry_id: str, dest: Path, **kw) -> "MaterializeResult": ...

    # integrity
    def fsck(self, *, deep: bool = False, repair: bool = False,
             fallbacks: Sequence[Path] = ()) -> "FsckReport": ...
    def space_needed(self, sizes_by_digest: Mapping[str | None, int]) -> int: ...


class StoreLocator(VersionLocator):          # D1 2.11
    def __init__(self, store: VersionStore, *, live_dir: Callable[[LiveTarget], Path]) -> None: ...
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
class StoreBusy(StoreError): ...             # LockTimeout on a commit: this one write fails, retry
class StoreNotUpgraded(StoreError): ...      # vault_mode is "legacy" or "upgrading"
class StoreTooNew(StoreError): ...           # read_compat / write_compat newer than this app
class InsufficientSpace(StoreError): ...     # -> D5 VAULT_SPACE
class ObjectMissing(StoreError, VersionUnavailable): ...
class ObjectCorrupt(StoreError, VersionUnavailable): ...
class EntryCorrupt(StoreError): ...
class SourceChanged(StoreError): ...         # a source file changed while it was being read
class PreserveMismatch(StoreError, PreserveFailed): ...   # live member != expected_vid
class FoldVerifyError(StoreError): ...
class UpgradeError(StoreError):              # 4.1; code: space, path-too-long, bad-destination,
    code: str                                # in-progress, vault-changing, copy-failed,
                                             # copy-mismatch, cancelled
class RollbackError(StoreError):             # 4.4; code: copy-missing, copy-mismatch, space,
    code: str                                # in-progress, export-failed, cancelled
```

The vault-level locator handed to D4 / D5 is D1's composite
`VaultLocator(readers=[StoreLocator, FolderLocator], preserver=StoreLocator)` (D1 2.11):
`StoreLocator` first, then D2's `FolderLocator` for unfolded legacy folders, hand-made in-place
folders and live. `locate` concatenates (store locations first, then durable folders, external,
live, as D1's order); `open` tries `prefer`, then the readers in order; `preserve` is the store's
(there is no folder preserve; before the upgrade D1 raises `PreserveFailed("store-unavailable")`);
`is_durable` is true when any reader says so.

### 2.6 Write path: adding a store entry

```python
def add_from_folder(folder, *, kind, scope, entry_id=None, ...):
    require_store_mode_and_writable()                 # StoreNotUpgraded / StoreTooNew
    files = select_files(folder, scope)               # sorted posix paths; regular files only,
                                                      # symlinks skipped with a warning
    worst = sum(size for f in files if size > INLINE_MAX)
    if free_space(store.root) < worst + 64 * MiB: raise InsufficientSpace
    records = []
    for rel in files:                                 # no lock held while reading and writing objects
        data, st = read_stable(folder / rel)          # read; re-stat; size and mtime_ns must be
                                                      # unchanged, else re-read (3 tries) -> SourceChanged
        digest = sha256(data)
        if len(data) <= INLINE_MAX:
            records.append(FileEntry(rel, len(data), digest, st.st_mtime_ns, inline=data))
        else:
            objects.put(data, digest)                 # below
            records.append(FileEntry(rel, len(data), digest, st.st_mtime_ns))
        if cancel and cancel(): raise Cancelled       # nothing committed; written objects stay
    members = describe_members(platform, folder, records)   # 2.6.1
    with lock():                                      # D1 vault lock: the commit only (2.12)
        require_store_mode()                          # re-checked inside the lock
        entry_id = entry_id or new_id(kind, platform) # unique across catalog and store
        if exists(entries / f"{entry_id}.json"):      # fold re-run: verify instead of rewriting
            return verify_same(entry_id, records)     # different content -> <id>-L, reported
        entry = StoreEntry(id=entry_id, ..., files=records, members=members, body_sha256=...)
        write_atomic(store/"entries"/f"{entry.id}.json", entry.to_bytes())   # COMMIT POINT
        safety.fsync_dir(store/"entries")
    check_present(entry)                              # stat every referenced object (size)
    return entry


def ObjectStore.put(data, digest):
    p = path_for(digest)
    if p.is_file():
        if sha256(p.read_bytes()) == digest:
            return PutResult(digest, len(data), written=False, healed=False)
        quarantine(digest, "re-hash mismatch on dedupe")         # keep the bad bytes
        healed = True
    tmp = tmp_dir / f"{digest}.{os.getpid()}.{token}.part"      # token: random per process
    write tmp; flush; os.fsync                                    # safety.atomic_write_bytes pattern
    try:
        os.replace(tmp, p); safety.fsync_dir(p.parent)
    except PermissionError:                                       # another process holds p open
        if p.is_file() and sha256(p.read_bytes()) == digest:      # it wrote the same bytes
            remove tmp; return PutResult(digest, len(data), written=False, healed=healed)
        raise
    if sha256(p.read_bytes()) != digest:                          # read-back verification
        quarantine(digest, "read-back mismatch"); raise ObjectCorrupt(digest)
    return PutResult(digest, len(data), written=True, healed=healed)
```

#### 2.6.1 Describing members

- **Steam**: for each `mf_saveN.hg` with its `saveN.hg` present: decrypt with the mapped ordinal,
  else `meta.decrypt_autodetect` (the logic of `savedir._load_member`, `savedir.py:144-172`);
  record the ordinal used, `meta.parse` fields, `pid = f"{slot_identifier:016X}"` (null when 0),
  and `valid` = header ok and `SizeDecompressed == lz4_block.stats(data).total_decompressed`. An
  unreadable meta gives `valid: false`, `ordinal: null`, a `note`; the bytes are still stored.
  A data file without its meta (3 such slots in the owner's catalog) is stored as a file and
  listed with no member record; a meta without data likewise.
- **Xbox**: `msstore.parse_containers_index(folder)` maps identifiers to GUID folders and blobs;
  meta fields from the fixed parser (P1: id 0x118, time 0x120, names at 296 B). `ordinal: null`;
  `meta_plain_sha256` = sha256 of the blob.

#### 2.6.2 Scopes per entry kind

| Kind | Steam files | Xbox files |
|---|---|---|
| `full` | the whole folder including `cache/` (today: `copytree`, `operations.py:170-171`) | the whole `wgs` account folder |
| `snapshot` | `*.hg` + `steam_autocloud.vdf` (as `_copy_save_state`, `operations.py:115-131`); never `cache/` | the whole `wgs` account folder |
| `extract`, `preserve` | the slot's `saveN.hg` / `mf_saveN.hg` at their original numbers (`operations.py:286-295`) | the slot's GUID folders + a rebuilt minimal `containers.index` (`operations.py:466-489`) |
| `imported` (copy into vault, fold, copy-in) | the whole folder, any files (as `copytree`) | the whole folder |

#### 2.6.3 Crash analysis

| Crash or power loss at | State afterwards | Recovery |
|---|---|---|
| writing an object's temp file | `*.part` in `store/tmp/` | swept at a later open once older than 1 hour |
| after an object's rename, before the manifest | unreferenced objects | kept; the next capture of the same bytes deduplicates onto them after re-hashing; fsck reports the count. Each costs disk only, bounded by one interrupted capture |
| writing the manifest's temp file | `*.part` in `tmp/` | as above |
| after the manifest rename | entry complete and visible | none |
| a medium that lies about flushes | an object renamed but its bytes lost | verify-on-read raises `ObjectCorrupt` and records `state/damage.json`; another reader serves the read (2.7); `fsck --repair` re-puts the bytes from any other copy (2.11) |

An entry is visible only after its manifest is renamed into `entries/`, and a manifest is written
only after every object it references was written or re-verified. There is no half-written entry.

#### 2.6.4 Live sources, the game and the UI thread

- Captures only read the live folder. Today's backups and snapshots do not check for the game
  (R1 D2); B keeps that for backups but adds the stability check (2.6) and flags invalid members
  (`valid: false`) rather than dropping them. `preserve` runs only with the game closed (D5 guard).
- No hashing or copying on the Tk thread (C31): a worker thread is fine (`hashlib` releases the
  GIL on large buffers); a capture takes 0.16-0.27 s (VERIFIED, 2.15), a fold several seconds.
- Memory: files are read whole (largest save 6.9 MB, largest cache file 0.9 MB).

### 2.7 Read path

```python
def StoreLocator.open(vid, prefer=None) -> OpenedVersion:
    for loc in candidates(vid, prefer):           # prefer (if a store location); store refs, newest entry first
        try:
            entry = store.entry(loc.ref.key); m = entry.member(loc.slot, loc.member)
            data = objects.get(vid, size=entry.file(m.data).size)      # sha256 == vid checked
            raw = entry.file(m.meta).inline or objects.get(entry.file(m.meta).sha256)
            plain = meta.decrypt(raw, m.ordinal) if entry.platform == "steam" else raw
            if entry.platform == "steam" and header(plain) != META_HEADER: raise ObjectCorrupt
            if m.meta_plain_sha256 and sha256(plain) != m.meta_plain_sha256: raise ObjectCorrupt
            return OpenedVersion(data, plain, entry.platform, m.meta_format, loc)
        except (ObjectMissing, ObjectCorrupt) as exc:
            record_damage(loc, exc); continue      # state/damage.json; never deleted
    raise VersionUnavailable(vid)                  # D1's VaultLocator then tries the next reader
```

When a store object fails verification and D1's `VaultLocator` finds the bytes through another
reader (an unfolded or not yet freed legacy folder, a hand-made folder, live), `fsck --repair`
re-puts them (2.11); reads never write.

- **Which meta.** A `vid` can have more than one meta plaintext (promote bumps the timestamp; 0
  cases in the owner's data). `open` returns `prefer`'s meta, else the newest store entry's;
  `OpenedVersion.location` tells the caller which.
- **`locate(vid)`**: every `(entry, slot, member)` holding the vid, as
  `Location(vid, "store", StoreRef(entry_id), slot, member, platform, durable=True)`. In
  memory.
- **`is_durable(vid)`**: the vid is referenced by a manifest in `entries/` and its object exists
  with the right size. Every stored vid is durable: nothing removes entries or objects.
- **Index**: built at open by parsing every `entries/*.json` (VERIFIED: 41 manifests in 6-7 ms
  warm; 185 ms on the first read after writing them, the virus scanner, as R5 F6 saw). Refreshed
  when `entries/` changed (directory mtime and file count) and before every commit. A persistent
  cache is not needed below ~2,000 entries; above, it goes in D1's per-machine cache dir keyed by
  `vault_id` with a fingerprint of `entries/` (names and sizes).

**Materialize** (the classic operations' folder-shaped view, Export, rollback):

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
  objects) and removes it on exit, also on an exception; stale `mat-*` folders older than 1 hour
  go at open.
- Xbox slot filter: the GUID folders of the chosen slots plus a `containers.index` rebuilt from
  the entry's index with just their records (and `AccountData`'s when the account files are
  written), each record verbatim and the trailing bytes dropped, as the classic extract does; so
  the folder never indexes a save it lacks.
- `export_entry(id, dest)`: materialize to any folder outside the vault and the live roots. A Steam
  export is an `st_`-shaped folder that any tool reading plain save folders can open (NomNom,
  goatfungus, a manual copy); an Xbox export is a `wgs`-shaped folder (index + GUID folders).
- VERIFIED on three real entries (`<D3>\proto_store.py`): Steam full 239 files / 187 MB in
  0.24 s, Xbox full 94 files / 65 MB in 0.10 s, extract 4 files in 0.01 s; each identical to its
  original folder in names, bytes **and** mtimes.

### 2.8 `preserve(members)` for D5

D1 contract: `preserve(members: Sequence[LiveMember], *, op_id, reason) -> list[Location]`,
durable, hash-verified, one location per member in order; raises
`PreserveFailed`. It is the only preserve provider (owner decision Q1): D5 refuses organizer live
operations while `vault_mode` is not `"store"`.

1. Group the members by target (platform, account key, slot).
2. For each target, read the slot's files from the live folder (resolved with the `live_dir`
   callable): Steam `saveN.hg` / `mf_saveN.hg` of **both** members; Xbox the slot's GUID folders
   and a rebuilt minimal index. Both members are kept even if only one was asked for (pair
   integrity: a one-member copy restored later leaves a mixed slot, P6).
3. Hash each data file; a requested member whose hash is not its `expected_vid` raises
   `PreserveMismatch` (the slot changed since D5 planned; D5 re-plans).
4. Add a store entry of kind `preserve`, id `preserve-<platform>-<stamp>`, label
   `kept: slot N (<name>)`, with `op_id` and `reason`. A preserve entry is written **every time**,
   even when both objects deduplicate: it costs one ~3 KB manifest, records exactly what was in the
   slot (meta bytes, ordinals, mtimes), and is the stable `StoreRef` that D1's
   `OpLineage.preserved` points at.
5. Verify before returning: re-read every object the entry references (not only new ones), check
   sha256, decrypt each Steam meta with its recorded ordinal, check `SizeDecompressed`. Only then
   return `Location(vid, "store", StoreRef(entry_id), slot, member, platform, durable=True)`.

Cost: two data files plus metas, ~10-30 ms of I/O plus fsyncs (a whole Steam snapshot takes
0.17 s, VERIFIED). `space_needed_for_preserve(members)` is exact: the sizes of the members whose
`expected_vid` is not already stored, plus 64 KiB; D5 blocks with `VAULT_SPACE` on a shortfall.

Other D5 needs, all served by the store: pre-op snapshots = `add_from_live(kind="snapshot",
scope=CaptureScope(), op_id=..., reason=...)` (durable, never pruned; the oplog's `snapshot_id`
is that entry's id); rollback pre-bytes = `read_file(entry_id, path)` or `open(vid, prefer=loc)`;
"keep a copy in the vault now" (no live write) = `add_from_live(kind="extract",
scope=CaptureScope(slots=(n,)))`; panic restore = `open_entry_folder()` (2.9) and the classic
`restore_full`.

### 2.9 Listing: one view of catalog and store entries

D1's scanner, the classic Backups tab and the CLI (`list`, `restore`, `undo`, `clear` planning)
read entries only through this listing, never `catalog.json` directly, so store entries are never
invisible (03 V-09).

```python
VaultEntryKind = Literal["full", "snapshot", "extract", "imported", "preserve", "inplace"]

@dataclass(frozen=True)
class VaultEntry:
    id: str
    kind: VaultEntryKind
    ref: EntryRef | StoreRef          # D1 SourceRef: where the bytes are
    origin: Literal["catalog", "store", "folded", "copied-in"]
    platform: Platform
    label: str
    created: str                      # catalog v1 form (local, naive, seconds): shown as today
    created_utc: int
    source: str | None                # live folder name or external folder name; redact for display
    managed: bool                     # False: hand-made in-place folder or a reference into another vault
    slots: tuple[catalog.SlotSummary, ...]   # the classic pane's row type, unchanged
    op_id: str | None                 # store entries: D5's op id (snapshots, preserves)
    reason: str                       # store entries: why the app made it; "" for catalog entries


def list_vault_entries(vault: catalog.Vault, store: VersionStore | None, *,
                       live_roots: Sequence[Path] = ()) -> list[VaultEntry]: ...
def get_vault_entry(vault: catalog.Vault, store: VersionStore | None,
                    entry_id: str) -> VaultEntry | None: ...

@contextmanager
def open_entry_folder(vault: catalog.Vault, store: VersionStore | None, entry: VaultEntry, *,
                      slots: Iterable[int] | None = None, cache: bool = False) -> Iterator[Path]:
    """A folder holding the entry's files, for reading only: a catalog entry's own folder
    (resolved by Vault.entry_folder, D2 section 4), or a verified temporary materialization of a
    store entry (removed on exit). The classic restore, repopulate and undo code runs unchanged
    on it."""


@dataclass(frozen=True)
class CopyAsideInfo:
    name: str                          # folder name, e.g. "_SaveVault.pre-store-20261017T101500Z"
    sibling: bool                      # made next to the vault (else the owner chose a folder)
    files: int
    bytes: int
    made_utc: int
    found: bool                        # present at the sibling path with the recorded file count

@dataclass(frozen=True)
class StoreStatus:
    mode: VaultMode
    copy_aside: CopyAsideInfo | None
    fold_total: int
    fold_done: int
    fold_failed: tuple[tuple[str, str], ...]     # (entry id, ASCII reason)
    fold_waiting_for_space: int                  # bytes still needed on the vault drive, 0 if none
    inplace_not_copied: tuple[str, ...]          # hand-made in-place entry ids (copy-in offer)
    freeable_bytes: int                          # folded legacy folders still on disk (phase 5 action)
    damage: tuple["DamageRecord", ...]           # store/state/damage.json
    entries: int
    store_bytes: int

def store_status(vault_root: Path, store: VersionStore | None) -> StoreStatus: ...
```

Rules:

1. Every store entry is listed (`ref = StoreRef(id)`); `origin` = `folded` / `copied-in` when its
   manifest `origin` names a legacy or in-place entry, else `store`.
2. Every catalog entry is listed (`ref = EntryRef(id)`, `origin = "catalog"`) unless a store entry
   has the same id (fold and copy-in keep the id; ids are otherwise unique across both) or its
   resolved folder is a live folder or lies inside one of `live_roots` (P9: the live `wgs` folder
   catalogued as in-place).
3. A store entry's `slots` are built from its member records exactly as `catalog.summarize` builds
   them from a folder (the 14 v1 `MemberSummary` fields with the same values), so the classic tab
   and the CLI show the same text for a folder and for its folded entry (test T11).
4. Sorted by `(created_utc, id)`. In memory from the store index and the catalog: ~5 ms for the
   owner's vault (INFERRED from 2.15).
5. Restore of a `preserve` entry follows the `extract` path (slot-scoped); no listing kind ever
   falls back to a whole-folder restore by accident (the P13 trap).

**For D1** (scanner 2.5, bootstrap 2.12): the scanner takes its set of entries from
`list_vault_entries`, not from `vault.entries` plus `store.entries()` separately, so that a folded
legacy folder is never scanned as a second copy of its store entry and the live alias stays out.
For an entry with `ref = EntryRef` it scans the folder as today; for `ref = StoreRef` it reads
`store.entry(id)`: one `Capture` per occupied slot, `where = StoreRef(entry_id)`,
`observed_at = created_utc`, `time_quality = "exact"`, `src = "entry"`; each `MemberObs` comes
straight from the `MemberRecord`: `vid`, `pid` (meta id, or D1's fallback chain when null), and
the `MetaSnap` fields 1:1 (`data_size` = the data file's `FileEntry.size`, `ts_src = "meta"`,
`meta_sha = meta_plain_sha256 or ""`). No object reads, no hashing, no `scan_any`; the entry
fingerprint for D1's capture cache is `body_sha256`. `RawLocation.entry_kind` gains
`"preserve"`.

### 2.10 Mapping of every existing operation

A vault is in **store mode** when `vault_mode()` is `"store"`. Then new backup-like entries go to
the store; in `"legacy"` mode the app behaves as in phase 1; in `"upgrading"` mode every vault
write is refused ("The vault is being upgraded."). From phase 2 on, every vault write, classic or
organizer, takes D1's vault lock for its commit and re-checks the mode inside the lock.

| Operation (`operations.py`) | Today | Store mode |
|---|---|---|
| `create_full_backup` (165) | `copytree` to `backups/<id>`, sha256 verify, catalog entry, oplog `full_backup` | `add_from_live(kind="full", scope=whole folder, cache="include")`; oplog `full_backup` with `entry_id` = store id |
| `snapshot_live` (134) | copy `*.hg` + vdf (Xbox: whole tree) to `snapshots/<id>` (pruning removed in phase 0, P2) | `add_from_live(kind="snapshot")`; never pruned |
| `extract_slot` (280, 466) | slot files / minimal wgs folder to `extracts/<id>` | from live: `add_from_live(kind="extract", scope=slot)`; from a listed entry: `add_from_folder` on `open_entry_folder(entry, slots=[n])` (deduplicates to ~0 bytes) |
| `preserve` (new, D5) | - | 2.8 |
| `restore_full` (224, 525) | folder -> live | `with open_entry_folder(entry, cache=False) as folder:` the unchanged `restore_full` on `folder` (after P7) |
| `restore_entry` (191) | extract -> `repopulate_slot` same slot | `open_entry_folder(entry, slots=[n])`, then the same call; `preserve` entries take this path |
| `repopulate_slot` (306, 492) classic Copy | from a folder | `open_entry_folder(entry, slots=[n])`; organizer Place (D5) uses `open(vid)` instead |
| `undo_last` (881) classic | `restore_full` of the newest record's snapshot | resolve `snapshot_id` with `get_vault_entry` (catalog or store); `open_entry_folder`; `restore_full`. Becomes D5's "panic restore" (C24) |
| `promote_member` (367), `clear_slot` (701) | live write after a folder snapshot | live write after a store snapshot |
| `plan_clear_slot` (671) | latest entry with the same slot **number** (P5) | by identity: `is_durable(vid)` of the slot's members |
| `import_backup` copy (766) | `copytree` to `backups/imported-...` | `add_from_folder(kind="imported", source={"type": "folder", "name": <folder name>})` |
| `import_backup` in place | catalog only | unchanged (an in-place catalog entry; copy-in offered, 4.3) |
| `import_vault` (811) | copy or reference folders | 4.8 |
| delete a backup | none | none (F9 keep everything) |

**Oplog records (coordination with D5).** New lines keep the v1 shape; keys are additive (R1 E4).
`snapshot_id` names the pre-op snapshot's id, a store entry in store mode; undo resolves it
through the listing. No line is written for older app versions.

### 2.11 Integrity: `fsck`

| Level | Checks | Owner's vault (VERIFIED, prototype) |
|---|---|---|
| quick | `FORMAT` readable; every manifest parses and its `body_sha256` matches; every referenced object exists with the listed size; inline payloads decode to their size and sha256; `tmp/` leftovers; orphan object count; `state/migration.json` consistent with `entries/` | 83 ms (41 manifests, 353 objects; first read) |
| deep | quick + re-hash every object; Steam members: meta decrypts with the recorded ordinal, `SizeDecompressed` == chunk total, `pid` == id in the first chunk (optional, ~23 ms per version, R7 2.9); Xbox: index and `container.N` parse, GUIDs resolve | 0.25 s for 498 MB (warm), plus ~4.5 s for the optional first-chunk ids of 195 versions |
| repair | missing or corrupt object: search candidate files of the right size in legacy folders, hand-made in-place folders, live folders, the copy-aside and a second vault the owner points at; re-put bytes whose sha256 matches; own stale temp files: delete; otherwise list the entry in `state/damage.json` (shown to the owner, never deleted) | - |

When: quick at open when the vault is on removable or network media and after any crash
recovery; deep after the fold, **before freeing old folders (required, 4.5)**, on demand
(`nmsvault store fsck [--deep] [--repair]`, a GUI item) and suggested monthly. Every read
verifies anyway (2.7).

### 2.12 Concurrency and locking

- **Commits.** Every manifest commit runs inside D1's vault lock
  (`safety.exclusive_lock(<vault>/organizer/.lock)`) and holds it for milliseconds: re-check
  `vault_mode`, assign the entry id (unique across catalog and store), write and rename the
  manifest, fsync the folder. Objects are written before, outside the lock: they are immutable and
  content-named, and no code path deletes an object, so a concurrent writer can at worst write the
  same object twice (2.6 handles a busy rename). A fold holds the lock only for each entry's
  commit; nothing in the store holds it for longer.
- **`LockTimeout`** (another process holds the lock for more than D1's 10 s) fails that one
  write with `StoreBusy` ("The vault is busy. Try again."); it never switches the session to
  read-only (03 V-13). D1's lock is not re-entrant: D3 never calls D1 while holding it, and
  callers do not call the store from inside a D1 transaction.
- **Lock order**: D5's live-op lock, then `store/state/upgrade.lock`, then D1's vault lock; never
  the reverse.
- **Readers** take no lock: manifests and objects are immutable and appear atomically.
- **Temp files** are named `<digest>.<pid>.<token>.part` and `mat-<id>-<rand>`; the sweep at open
  deletes only files and folders older than 1 hour, so another process's work in progress is never
  removed.
- **Upgrade and rollback** take `store/state/upgrade.lock` (the same `safety.exclusive_lock`
  primitive, a different file) for their whole run, so only one runs at a time, and D1's lock only
  for their short commit steps (4.1, 4.4). A held `upgrade.lock` tells other processes "in
  progress"; a free one with `upgrade.json` present means an interrupted run.
- If the file system does not support locking, the store opens read-only with a visible warning.

### 2.13 Safety invariants

1. Objects are written once and never modified; an object's name is the sha256 of its bytes;
   every read verifies it.
2. An entry exists only once its manifest is committed, and a committed manifest references only
   objects that were written or re-verified before the commit.
3. No code path deletes an object or a manifest. Quarantine renames a bad object aside and keeps
   it.
4. Legacy folders are deleted only by "free old folders" (phase 5): on the owner's request, after
   a deep fsck passed, after a fresh byte-for-byte re-verification against the folded store entry,
   only managed folders strictly inside `<vault>/{backups,snapshots,extracts}`; never a hand-made
   in-place folder, never a live folder, never the copy-aside.
5. The upgrade changes nothing in the vault before the copy-aside is verified, except creating
   `store/state/`; a failed upgrade removes only its own unfinished copy (the `.partial` folder
   recorded in `upgrade.json`) and `store/`.
6. The store writes only inside `<vault>/store/`, plus: the copy-aside folder (upgrade);
   materialize / export destinations (never a live save root or the store); during rollback,
   legacy folders, `<vault>/.rollback/` staging, `catalog.json` and the move of `store/` aside;
   `catalog.json` when old folders are freed.
7. Save data is stored and returned verbatim; only placement (D5) re-keys a meta.
8. Captures only read live folders.
9. Every manifest is self-checking (`body_sha256`), every file entry carries its sha256.
10. Standard library only: `hashlib`, `json`, `base64`, `os`, `shutil`, `uuid`.

### 2.14 Errors and what the owner sees

| Error | When | D5 code / UI text (ASCII) |
|---|---|---|
| `StoreUnavailable` | vault folder gone (USB unplugged), `FORMAT` unreadable | `VAULT_UNAVAILABLE` / "The vault is not reachable." |
| `StoreBusy` | lock timeout on a commit | "The vault is busy. Try again." |
| `StoreNotUpgraded` | organizer live op or store write in a legacy or upgrading vault | `VAULT_UPGRADE_NEEDED` / "Upgrade the vault first (Tools > Vault upgrade...)." / "The vault is being upgraded." |
| `StoreTooNew` | `FORMAT` compat newer than this app | "This vault was upgraded by a newer NMS Save Vault. Update the app." Opens read-only when only `write_compat` is newer |
| `InsufficientSpace` | free space < worst case + 64 MiB | `VAULT_SPACE` / "Not enough free space in the vault drive (needs N MB)." |
| `SourceChanged` | a live file changed three times while read | "A save changed while it was being copied. Is the game running?" |
| `PreserveMismatch` | slot changed since planning | D5 re-plans |
| `ObjectMissing` / `ObjectCorrupt` | read verification failed and no other reader had the bytes | "A stored copy of <name> is damaged. Run Check vault." Entry listed in `damage.json` |
| `FoldVerifyError` | a folded entry does not match its folder | the entry stays a legacy folder; reported |
| `UpgradeError`, `RollbackError` | 4.1, 4.4 | the texts in 4.1 and 4.4 |

### 2.15 Disk and performance budget

**Owner's vault now** (VERIFIED, `<D3>\analyze_store.py`, `<D3>\proto_store.py`,
`<U2>\vault_size.py`):

| | Files | Size | On a 128-KiB exFAT drive |
|---|---|---|---|
| Today (41 managed folders + catalog/oplog/ids; 49 catalog entries) | 4,517 | 3,405 MB | 3,910 MB |
| Store after the fold, `cache/` kept | 349 objects + 41 manifests | 498.2 MB + 1.8 MB = **~500 MB** | ~536 MB |
| Copy-in of the 7 hand-made in-place folders (optional) | | +73 MB (25 Steam versions 65.4 MB + cache 7.6 + account 0.4) | |
| First snapshot of today's live saves | | +44.5 MB (16 live-only versions: Steam 27.2, Xbox 17.3) | |
| Copy-aside (outside the vault, until the owner deletes it) | 4,517 | 3,405 MB | |

During phase 2 the vault drive holds the old folders (3.4 GB), the store (~0.5 GB) and, next to
the vault, the copy-aside (3.4 GB): ~7.3 GB until the owner deletes the copy and frees the folders
(phase 5), then ~0.5 GB.

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
3.4 GB in the same period. With organizer use (one Place or Swap per play day, ~150 a year: a
pre-op snapshot, median 0 / p90 14 MB, plus a preserve, 0-14 MB) the store reaches ~1.7-2.6 GB
after a year (INFERRED). Keeping folders as today would add ~9.4 GB a year.

**Time** (VERIFIED on this PC, SSD, warm cache unless noted):

| Operation | Measured |
|---|---|
| Copy-aside of the whole vault (4,517 files, 3,405 MB): walk + stat | 0.34 s (before and after) |
| Copy-aside: read, sha256, write, fsync every file | 30.85 s |
| Copy-aside: re-read and verify every copied file | 20.04 s, 0 mismatches (`<U2>\copy_aside_proto.py`) |
| Fold all 41 entries (read 3,404 MB, write 353 objects / 498 MB, re-verify 2,964 dedupe hits / 2,905 MB, fsync each object and manifest) | 7.55 s |
| Verify every folded entry against its folder (materialize in memory, sha256 each file) | 1.52 s, 0 mismatches |
| Snapshot of the live Steam folder (59 files, 53.8 MB read, 10 new objects / 27.2 MB) | 0.17 s |
| Full of the live Steam folder with `cache/` (240 files, 191 MB read, 1 new object) | 0.27 s |
| Snapshot of the live Xbox folder (94 files, 67.7 MB read, 8 new objects / 17.4 MB) | 0.16 s |
| `open(vid)` (object read + sha256 + meta decrypt), average over 40 vids of 2.1 MB | 1.43 ms |
| Load all manifests and build the vid index | 6-7 ms (+0.2 ms index) |
| fsck quick / deep | 83 ms / 0.25 s |

So the whole upgrade (copy-aside, verify, fold, verify) takes ~1 minute on the owner's SSD.
Slow media (INFERRED): reading and writing 3.4 GB on a hard disk, a USB 3 hard disk or a 1 GbE
NAS takes ~2-4 minutes for the copy-aside and ~35-60 s for the fold. After a year (~600-800
entries, ~12-16 MB of manifests) loading the index takes ~40-55 ms (3.3 ms per MB measured); a
deep fsck of ~1.4 GB ~1 s warm.

Budgets for the implementation (CI-checkable on synthetic data scaled to these sizes): snapshot of
a 60 MB Steam folder < 0.5 s warm; preserve of one slot < 0.2 s; `open` of a 7 MB version < 5 ms;
store open with 1,000 entries < 100 ms; fold and copy-aside throughput > 100 MB/s warm with
progress per file and cancel between files (copy-aside) or entries (fold).

## 3. Decisions (owner) and rejected alternatives

| # | Decision | Rejected alternative (why) |
|---|---|---|
| 3.1 When | The store starts with phase 2 (owner Q1): preserve, snapshots and new backups use it from the first live write; the upgrade (4.1) and the fold (4.2) run at the start of phase 2 | A first, store in phase 5 (preserve, rescue and pruning code written twice) |
| 3.2 Older app versions | Not supported on an upgraded vault (owner Q2: users cannot and should not go back). Nothing is written for them: no barrier or shadow catalog entries, no stub, no oplog convention, no unfold or downgrade. Known and accepted: an older version's Undo on an upgraded vault can restore an older whole-folder snapshot (03 V-06); the README says older versions must not be used after the upgrade | A guard catalog entry so an older Undo refuses (owner declined) |
| 3.3 Old backups | Folded automatically right after the copy-aside (deletes nothing, +0.5 GB); old folders freed only on the owner's request after a deep check (phase 5) | Never folding (two readers forever, no deduplication); freeing automatically (deletes without the owner) |
| 3.4 Retention (F9) | Keep everything. No trash, garbage collection, tombstones or retention planner exist; an auto-snapshot of unchanged saves costs 0 bytes | Thinning snapshots or per-save limits (deletion code that keep-everything does not need) |
| 3.5 Thumbnails `cache/` | Kept, deduplicated: full backups and the fold capture `cache/` (139 MB once, 0-5.6 MB per new full) | Stop capturing or drop on migration (thumbnails lost for a later UI) |
| 3.6 Hand-made in-place backup folders | Listed in place (outside the vault) until the owner copies them in with one action (4.3), offered after the fold, default off (+73 MB) | Copying them in automatically (the owner decides whether they are archives or scratch) |
| 3.7 Copy-aside location | Next to the vault (`<vault name>.pre-store-<UTC>`); the owner may choose another folder | Inside the vault (it would be copied by every later copy of the vault and lost with it) |

## 4. Upgrade and migration

### 4.1 The upgrade: copy-aside, then store mode (03 V-01)

**When.** The first time a phase-2 app opens a writable vault whose `vault_mode` is `"legacy"`:
the GUI shows the upgrade dialog (default button "Upgrade"); the CLI prints one line and
`nmsvault store upgrade [--copy-to FOLDER]` runs it. "Not now" keeps the vault exactly as in
phase 1 (classic operations and the organizer view work; organizer live operations stay
disabled with "Upgrade the vault first", because their preserve provider is the store); the dialog
returns at the next start. A vault created by a phase-2 app starts in store mode; a vault with no
catalog entries is upgraded without a copy (`"copy_aside": null`).

**Plan** (read-only; 0.34 s walk for the owner's vault):

```python
@dataclass(frozen=True)
class UpgradePlan:
    vault_root: Path
    files: int
    bytes: int                        # the whole vault tree, store/ excluded
    fingerprint: str                  # sha256 of the sorted "relpath|size|mtime_ns" lines
    dest: Path                        # default <vault parent>/<vault name>.pre-store-<UTC yyyymmddThhmmssZ>
    dest_free: int                    # free bytes on dest's volume
    needed: int                       # bytes + 1 GiB
    longest_dest_path: int
    problems: tuple[tuple[str, str], ...]   # (UpgradeError code, ASCII text); empty = can run

def plan_upgrade(vault_root: Path, *, dest_parent: Path | None = None,
                 live_roots: Sequence[Path] = (),
                 now: Callable[[], datetime]) -> UpgradePlan: ...
def run_upgrade(plan: UpgradePlan, *, vault_lock: Callable[[], ContextManager[None]],
                progress: Callable[["UpgradeProgress"], None],
                cancel: threading.Event) -> "UpgradeResult": ...
def cleanup_interrupted_upgrade(vault_root: Path) -> "UpgradeResult | None": ...
```

Plan checks (each failure is a `problems` row; nothing is written):

| Check | Rule | Code |
|---|---|---|
| space | `dest_free >= bytes + 1 GiB` (owner: 4.4 GB on C:, 1,760 GB free) | `space` |
| path length | deepest destination path < 260 (owner: 205 for the sibling, VERIFIED) | `path-too-long` |
| destination | not inside the vault, not inside a live save root, does not exist | `bad-destination` |
| one at a time | `store/state/upgrade.lock` can be taken | `in-progress` |

**Run** (owner's vault: ~52 s on SSD, VERIFIED by the prototype):

```
1  take store/state/upgrade.lock for the whole run (released by the OS on a crash)
2  under D1's vault lock: create store/state/upgrade.json
     {"state": "copying", "dest": "<absolute path of <dest>.partial>", "started_utc": ...,
      "fingerprint": ...}   (transient: the only absolute path the store writes; deleted at step 7)
3  copy every file of the vault (store/ excluded) to <dest>.partial: read in 1 MiB blocks, sha256
   while reading, write, flush, fsync, set mtime_ns; progress per file (bytes, files); cancel
   between files
4  verify: re-read every copied file; size and sha256 must equal step 3's; the file set must
   equal the fingerprint's
5  re-walk the vault: files added or changed since step 1 are copied and verified again, files gone
   are removed from <dest>.partial; up to 3 rounds, else UpgradeError("vault-changing")
6  fold space: from step 3's hashes, the distinct bytes of files > 8 KiB in managed folders
   + 64 MiB = what the fold adds to the vault drive (owner: ~500 MB)
7  under D1's vault lock: final walk equals the last fingerprint (else back to 5);
   rename <dest>.partial -> <dest> (retry 5 x 200 ms on PermissionError);
   write <dest>/PRE-UPGRADE-COPY.txt (ASCII: what the folder is, when, from which vault name,
   how to undo the upgrade; not part of the tree hash);
   write store/state/migration.json {"copy_aside": {"name", "sibling", "files", "bytes",
     "tree_sha256", "verified_utc"}, "fold": {"state": "pending" | "waiting-for-space", ...}};
   create store/FORMAT, objects/, entries/, quarantine/, tmp/    <- COMMIT POINT: store mode on
   delete store/state/upgrade.json
8  release upgrade.lock; start the fold (4.2) in the background unless step 6 found too little
   free space on the vault drive
```

`tree_sha256` = sha256 of the sorted lines `relpath|size|sha256` of the copied files. The vault
records only the copy's folder name and tree hash, never its absolute path (D11); rollback finds
the copy next to the vault or asks the owner for it and checks it against `tree_sha256`.

The copy-aside is never treated as a vault or a backup: `catalog.discover_save_dirs` does not
descend into a folder holding `catalog.json` or `PRE-UPGRADE-COPY.txt` (today it would list the
copy's 41 backup folders as in-place backups, because the sibling sits under the NMS root that
discovery walks to depth 3); "Open vault" and `import_vault` refuse a folder holding
`PRE-UPGRADE-COPY.txt` ("This is the pre-upgrade copy of a vault; use Undo the vault upgrade.").

**Failure handling.** Before the commit point the vault is unchanged except `store/state/`:

| Failure | Detected | What happens | Owner is told |
|---|---|---|---|
| not enough space for the copy | plan | nothing written | "The upgrade needs 4.4 GB free on drive C: for a copy of the vault; there are 2.1 GB. Free some space or choose another folder for the copy." |
| path too long | plan | nothing written | "The copy would need paths longer than Windows allows. Choose a folder with a shorter path." |
| another window is upgrading | plan | nothing written | "The vault is being upgraded by another NMS Save Vault window." |
| read or write error | step 3-5 | `<dest>.partial` and `store/` removed | "The upgrade stopped: could not copy <file> (<error>). Your vault was not changed." |
| copy differs from the vault | step 4 | same | "The upgrade stopped: the copy did not match the vault (<file>). Your vault was not changed. Check the drive." |
| vault keeps changing | step 5 | same | "The upgrade stopped: the vault kept changing while it was copied. Close other NMS Save Vault windows and try again." |
| cancel | step 3-5 | same | "Upgrade cancelled. Your vault was not changed." |
| crash or power loss | next start: `upgrade.json` in state `copying`, `upgrade.lock` free | `cleanup_interrupted_upgrade` removes the folder recorded in `upgrade.json` (only when its name ends in `.partial`) and `store/` | "The last upgrade was interrupted and has been cleaned up. Your vault was not changed." Then the dialog again |
| too little space for the fold | step 6 | store mode on; fold waits | "Vault upgraded. Your old backups were not read into the store yet: needs 0.5 GB more free space on drive C:. Vault > Read old backups into the store retries." |

**Success** (after step 7): "Vault upgraded. A copy of the vault as it was is in
_SaveVault.pre-store-20261017T101500Z next to it (3.4 GB). Keep it until you are happy with the
new version and delete it yourself when you no longer need it; Vault > Undo the vault upgrade
uses it." The vault status (D6) shows the copy's name, size, date and whether it is still found.

### 4.2 Fold

Runs automatically in the background right after the upgrade, and resumes by itself at the next
start when it was cancelled, interrupted or waiting for space. The organizer works meanwhile; new
entries go to the store. Per managed legacy entry, oldest first; `state/migration.json` records
`pending -> folded -> verified`, or `skipped` / `failed` with a reason:

1. Skip: in-place entries (outside the vault; offered for copy-in, 4.3), unmanaged references into
   another vault, any entry whose folder resolves to a live folder or inside a live root (P9: the
   owner's `inplace-20260624-182102` is the live `wgs` folder), entries whose folder is missing
   (`Vault.entry_folder()` resolves moved vaults, D2 section 4).
2. `add_from_folder(folder, kind=entry.kind, entry_id=entry.id, label=entry.label,
   created_local=entry.created, source={"type": "folder", "name": entry.source},
   origin={"legacy_entry": id, "legacy_subdir": ...}, scope=whole folder, cache="include")`.
   Member records are computed fresh from the bytes, so Xbox entries get the P1-fixed names and
   ids.
3. Verify: every file of the folder (walked again) is in the manifest with the same size and
   sha256; every referenced object re-hashes; no file of the manifest is missing from the folder.
   A mismatch raises `FoldVerifyError`; the entry stays a legacy folder (listed from the catalog,
   durable through D2's reader) and is reported.
4. Idempotent and resumable: an interrupted fold leaves unreferenced objects that the re-run
   deduplicates onto; a manifest that already exists with the same id is verified instead of
   rewritten; when the folder changed after it was folded, its new content folds under `<id>-L`
   and is reported, so both states are kept.

Progress: entries done / total, bytes read, current entry; cancel between entries. Owner's vault:
41 entries, 7.55 s warm, +498 MB of objects and 1.8 MB of manifests (VERIFIED). Report: "Your 41
old backups are now in the store (0.5 GB). The old folders stay where they are." plus any entry
that stayed a folder, with its reason. A deep fsck runs after the fold (0.25 s for the owner).

### 4.3 Copy-in of hand-made in-place backup folders

Offered once after the fold ("7 backup folders you made by hand are outside the vault. Copy them
into the vault (+73 MB)? The folders themselves are not changed." - default "Not now"), and any
time from the vault status, per folder or all. `copy_in(entry_id)` is a fold of the in-place
entry: `add_from_folder(kind="imported", entry_id=<the in-place id>, origin={"inplace_entry":
id, "copied_utc"}, source={"type": "folder", "name": <folder name>})` plus the fold's
verification. The hand-made folder and its catalog entry are never changed; the listing then shows
the store entry under the same id, so the folder is no longer listed (2.9 rule 2). Later changes
to that folder are not seen; adding it again (Import, in place) lists it as a new entry. Never for
the live alias (P9). Owner: +73 MB; the 16 versions that exist only in those folders (R7 2.5)
become durable.

Until copied in, a hand-made folder stays listed in place, shown "outside the vault" (4.7).

### 4.4 Rollback: "Undo the vault upgrade"

Offered from the vault menu and `nmsvault store rollback [--copy FOLDER]` while the copy-aside
exists. It returns the vault to the old layout with everything it held before the upgrade plus
everything made since, as plain folders; the copy-aside is only read.

```python
@dataclass(frozen=True)
class RollbackPlan:
    copy: Path
    copy_ok: bool                         # re-hashed, equals migration.json tree_sha256
    export: tuple[str, ...]               # store entries whose id is not in the copy's catalog
    restore: tuple[str, ...]              # copy entries whose vault folder is missing or differs
    bytes_needed: int                     # materialized sizes of export + restore
    free: int
    problems: tuple[tuple[str, str], ...] # (RollbackError code, ASCII text)

def plan_rollback(vault_root: Path, store: VersionStore, *, copy: Path | None = None,
                  progress=None, cancel=None) -> RollbackPlan: ...
def run_rollback(plan: RollbackPlan, *, vault_lock, progress, cancel) -> "RollbackResult": ...
```

Steps:

1. Take `store/state/upgrade.lock`; write `store/state/upgrade.json` `{"state": "rolling-back"}`,
   so `vault_mode` is `"upgrading"` and every other vault write is refused.
2. Check the copy: found next to the vault by its recorded name, or the folder the owner gives;
   re-hash it and compare with `tree_sha256`, and hash the vault's legacy entry folders to find
   the ones that differ (`plan_rollback` does both: ~1 minute for the owner, INFERRED from the
   4.1 rates). Mismatch or missing copy: `RollbackError("copy-missing" | "copy-mismatch")`,
   nothing changed.
3. Export every store entry whose id is not in the copy's `catalog.json` (backups, snapshots,
   extracts, imports and preserves made since the upgrade, and entries imported since): materialize
   into `<vault>/.rollback/<id>`, verify, rename into `<vault>/<subdir>/<id>` and build its catalog
   entry with `make_entry_from_dir` (kind `preserve` -> `extract`, the slot-scoped legacy kind). A
   copied-in entry is exported (as kind `imported`, id `<id>-copy`) only when its hand-made folder
   is missing or no longer matches.
4. Restore from the copy every legacy entry folder of the copy's catalog that is missing in the
   vault (freed in phase 5) or differs from the copy: copy into `<vault>/.rollback/<id>`, verify,
   rename into place; a differing folder is first moved to `<vault>/.rollback/replaced-<id>`, not
   deleted.
5. Under D1's vault lock: `catalog.json` = the current catalog + the copy's entries missing from it
   + the exported entries; `oplog.jsonl`, `organizer/` and `playthrough-ids.json` stay as they are
   (oplog lines written in store mode name ids that now exist as folders).
6. Rename `store/` to `<vault parent>/<vault name>.store-rolled-back-<UTC>` (outside the vault;
   never deleted by the app). COMMIT POINT: `vault_mode` is `"legacy"`; organizer live operations
   are disabled until a new upgrade.

Space: `bytes_needed` is checked before step 3 (exports are not deduplicated: each export is the
full folder; restores are the copy's folder sizes). A crash before step 6 leaves `upgrade.json` in
the rolling-back state; the next start asks "Finish undoing the vault upgrade? [Finish] [Keep
the upgrade]". Finish re-runs the steps (exported or restored folders that already verify are
kept); Keep removes `upgrade.json` and `.rollback/` staging, and folders already exported stay as
catalog entries hidden by their same-id store entries.

Owner is told: "Upgrade undone. Your vault is back in the old layout with everything made since
(12 backups added as folders). The version store was moved to
_SaveVault.store-rolled-back-20261020T091500Z (0.5 GB); delete it when you no longer need it. The
pre-upgrade copy was not changed."

### 4.5 Free old folders (phase 5)

Owner-requested only: "Free 2.9 GB: delete the 41 old backup folders that are now in the store?
A full check of the store runs first. Every backup stays listed. The pre-upgrade copy
_SaveVault.pre-store-... is still next to the vault (Undo the vault upgrade keeps working while it
exists)." - or "is no longer found" when it is gone.

```python
def plan_free(vault: catalog.Vault, store: VersionStore) -> "FreePlan": ...   # entries, bytes
def run_free(plan: "FreePlan", *, vault_lock, progress, cancel) -> "FreeResult": ...
```

1. A deep fsck of the store runs first (0.25 s for the owner's store, ~1 s per GB); any damage
   stops the run and nothing is deleted.
2. Per folded entry (`migration.json` status `verified`): re-verify the folder against its store
   entry, now (every file's name, size and sha256; no extra file). A folder that changed or holds
   an extra file is skipped and reported (it may hold data the store lacks).
3. Guard: the folder is a managed entry folder strictly inside `<vault>/{backups,snapshots,
   extracts}`; never in-place, never live, never the copy-aside.
4. Status `freeing`; delete the folder (`shutil.rmtree` with an error callback that clears the
   read-only attribute and retries once, recording leftovers; never `ignore_errors=True`); under
   D1's lock remove the catalog entry; status `freed`. A crash in between resumes at the next start
   (the listing already shows the store entry under that id, so a half-deleted folder is never
   read).

Owner's vault: 41 folders, 3,404 MB -> ~0.5 GB; the catalog keeps the hand-made in-place entries
not copied in (and the live alias until P9 removes it). Result text: "Freed 2.9 GB. 41 old
folders deleted; every backup is still listed."

### 4.6 The owner's vault, step by step

| Phase | Step | Effect |
|---|---|---|
| 0 | P2: snapshot pruning removed (D2 section 2) | no version is deleted any more; each live write adds a 41-63 MB snapshot folder (R1 A6) until phase 2 |
| 1 | organizer view reads the folders through D2's reader | nothing changes on disk except `organizer/` (D1) |
| 2 | upgrade dialog -> copy-aside | `_SaveVault.pre-store-<UTC>` next to the vault: 4,517 files, 3.4 GB, ~52 s (VERIFIED prototype); store mode on |
| 2 | fold, automatic | 41 entries in ~8-60 s, +498 MB of objects and 1.8 MB of manifests; skipped: the 7 hand-made in-place entries and `inplace-20260624-182102` (P9); every entry verified against its folder |
| 2 | copy-in offer | default off; +73 MB when accepted |
| 2+ | daily use | backups, snapshots and preserves are store entries (~0-15 MB each instead of 1-187 MB) |
| 2+ | owner deletes the copy-aside when satisfied | -3.4 GB on the drive; "Undo the vault upgrade" no longer offered |
| 5 | free old folders, on request after a deep check | vault 3.9 GB -> ~0.5 GB |

### 4.7 What the organizer shows before and after the fold (03 V-17)

| When | Copies | Shown as |
|---|---|---|
| phase 0 onward | managed entry folders inside the vault, every kind including auto-snapshots (pruning is gone) | in the vault (durable) |
| any phase, until copied in | hand-made in-place backup folders; references into another vault | "outside the vault" (not durable), with "Copy into the vault" for hand-made folders |
| any phase | a version only in a live slot | not in the vault |
| phase 2, upgrade not done | as phase 1 | Move to live disabled: "Upgrade the vault first" |
| phase 2, fold running | folded entries from the store, the rest from their folders | in the vault; no difference shown |
| phase 2, fold failed for an entry | that entry's folder | in the vault; the vault status lists it |

No "may be pruned" or "only in auto-snapshots" state exists after phase 0.

### 4.8 Importing vaults

| Source -> target | Behaviour | Phase |
|---|---|---|
| Legacy vault -> store-mode vault, copy | each source entry folded (id kept unless taken: `<id>-i<n>`), deduplicated, verified; the source untouched | 2 |
| Legacy vault -> store-mode vault, in place | as today: unmanaged references (listed, outside the vault, never folded or freed) | 2 |
| Store-mode vault -> store-mode vault, copy | **merge**: copy every missing object (verify), copy every manifest whose id is absent; same id + same `body_sha256` = skip; same id + different body = import as `<id>-m<n>` and report; `origin` keeps the other `store_id` | 5; refused before ("Importing an upgraded vault comes with a later version.") |
| Store-mode vault, in place | refused: a second store is not referenced in place ("copy it in instead") | 2 |
| A pre-upgrade copy (`PRE-UPGRADE-COPY.txt`) | refused (4.1) | 2 |

Merging is the natural way to mirror a vault onto a USB drive: after the first copy only new
objects move (~76 MB a month for the owner).

### 4.9 D11 portability; USB and NAS

- The store holds no absolute path: object paths come from hashes, manifest paths are relative,
  `source` holds folder names, never paths; the copy-aside is recorded by name and tree hash. A
  vault on a USB drive or NAS opens in place on any machine and OS; Xbox entries can be listed and
  exported anywhere and placed only on Windows (D3 of DESIGN.md). The only absolute paths left are
  those `catalog.json` already has (legacy and in-place entries), resolved by `Vault.entry_folder()`
  until the old folders are freed. Moving the vault does not move the copy-aside; rollback then asks
  for it.
- File systems: no hard links, symlinks, sparse files, alternate streams or sqlite; small files
  inline (no cluster waste); names short and case-safe; `os.replace` within one volume only (temp
  files live in `store/tmp/` on the same volume).
- FAT32 / exFAT: rename is not journalled, so a power loss can leave a torn rename; the commit
  protocol plus verify-on-read plus `fsck` covers it. FAT32 mtimes have 2-second resolution: the
  manifest keeps the source's exact `mtime_ns`, so only exports onto FAT lose precision.
- NAS (SMB): `msvcrt.locking` byte-range locks work over SMB; `os.fsync` maps to
  `FlushFileBuffers`, which the SMB redirector honours. Reading 0.5 GB over 1 GbE takes ~5 s
  (fsck deep). If locking is not supported, the store is read-only (2.12).
- Unplugged mid-capture: the live folder is untouched (captures only read it); the store keeps
  unreferenced objects and `tmp/` leftovers; the op that needed the capture fails with
  `StoreUnavailable`, and D5 refuses its live write (`VAULT_UNAVAILABLE`). Unplugged mid-upgrade:
  the copy fails, the vault is unchanged, the next start cleans up (4.1).
- Quick fsck runs at open on removable and network media (2.11).

## 5. Test plan

Risk-based; all write tests on `tmp_path` sandboxes with synthetic saves or copies (project
rule). Highest risks first: losing data in the upgrade or rollback, undetected corruption, crash
consistency, the free step, Xbox fidelity, Steam meta ordinals. D3 also provides the real store to
the cross-design integration suite (01, 03 V-11).

**Fixtures** (in `tests/conftest.py` unless noted):

| Fixture | What |
|---|---|
| `steam_live` | factory building an `st_` folder: N slots of chunked synthetic data with real XXTEA metas (promoted from `tests/test_playtime.py:43-63`), `accountdata.hg` + meta, vdf, `cache/` with three dummy DDS files, optional "moved" meta |
| `wgs_account` | `make_wgs_account` fixed to real 296/360-byte metas with id 0x118 and time 0x120 (P1), plus `AccountData` |
| `legacy_vault` | a `Vault` filled by the real ops: Steam full with cache, three snapshots, an extract, an import (copy), a hand-made in-place entry, an in-place entry that is the live folder, oplog lines |
| `store` | a `legacy_vault` upgraded by `run_upgrade` in `tmp_path` (copy-aside next to it) with a real `safety.exclusive_lock` |
| `faults` | a `FaultPlan` passed into the store (test-only parameter, default None) raising at named points: `object.after_tmp`, `object.after_replace`, `entry.before_replace`, `entry.after_replace`, `upgrade.mid_copy`, `upgrade.after_verify`, `upgrade.before_format`, `fold.after_entry`, `rollback.after_export`, `rollback.before_store_move`, `free.mid_rmtree`, `free.after_rmtree` |
| `fs_fingerprint` | (relpath, size, mtime_ns, sha256) of a tree, for "unchanged" assertions |
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
| T8 | `test_crash_points_leave_consistent_store` | for each store fault point: no visible half entry; fsck quick clean after the next open; re-run succeeds |
| T9 | `test_nothing_is_ever_deleted` | `ObjectStore` and `VersionStore` expose no delete; random sequences of adds, preserves, folds, copy-ins and fsck repairs with injected faults keep every committed entry fully readable and every object present |
| T10 | `test_preserve_contract` | D1's locator conformance suite (D1 test plan item 10) on `StoreLocator`; `PreserveMismatch` when the live slot changed; preserve writes both members and re-verifies every referenced object |
| T11 | `test_listing` | store + catalog entries; a same-id store entry hides the catalog entry; live alias excluded; store slots equal `catalog.summarize` of the materialized folder; sort order |
| T12 | `test_classic_ops_through_open_entry_folder` | `restore_full` / `restore_entry` / `repopulate_slot` / classic `undo_last` on store entries give the same live folder as on the legacy folder; a `preserve` entry restores only its slot |
| T13 | `test_upgrade_copy_aside_verified` | copy equals the vault (names, bytes, mtimes); `tree_sha256` recorded; `FORMAT` written last; `upgrade.json` removed; `PRE-UPGRADE-COPY.txt` present; vault fingerprint unchanged apart from `store/` |
| T14 | `test_upgrade_refusals` | low space (patched `shutil.disk_usage`), path too long, destination inside the vault or a live root, upgrade lock held by another process: nothing written |
| T15 | `test_upgrade_crash_and_cancel` | each upgrade fault point and cancel: vault unchanged; `.partial` and `store/` removed by `cleanup_interrupted_upgrade`; upgrade runs again |
| T16 | `test_upgrade_vault_changes_during_copy` | a file added mid-copy is copied and verified in round 2; a file changing every round -> `UpgradeError("vault-changing")` |
| T17 | `test_fold_idempotent_resumable` | fault after entry k; re-run completes; same ids; 0 mismatches; a corrupted folder stays a legacy entry and stays listed; waiting-for-space resumes when space appears |
| T18 | `test_copy_in` | store entry under the in-place id; hand-made folder fingerprint unchanged; live alias refused; the listing shows one entry |
| T19 | `test_rollback_round_trip` | upgrade, fold, then a full, a snapshot and a preserve in store mode (and, phase 5, a free); rollback: every pre-upgrade folder equals the copy, store-only entries exist as folders with the same ids, the catalog is the union, every oplog `snapshot_id` resolves, `store/` moved out, copy-aside unchanged |
| T20 | `test_rollback_refusals_and_resume` | copy missing, copy tree mismatch, low space: nothing changed; crash before the store move -> Finish completes, Keep leaves store mode intact |
| T21 | `test_free_old_folders` | a damaged object found by the opening deep fsck stops the run before any deletion; skips a folder changed or extended after the fold; never touches in-place, live or the copy-aside; resumes `freeing`; listing unchanged |
| T22 | `test_import_vault_modes` | legacy source folds by id; store source refused in phase 2 and merged once in phase 5 (`-m1` on conflict); a pre-upgrade copy is refused |
| T23 | `test_store_has_no_absolute_paths` | no drive letter, no leading slash, no user-profile name in any manifest or `migration.json` |
| T24 | `test_fsck_reports_and_repairs` | missing, corrupt, unreferenced, stale tmp; repair from a legacy folder and from the copy-aside |
| T25 | `test_source_changed_during_capture` | a file that changes between reads raises `SourceChanged` after three tries |
| T26 | `test_path_lengths` | the deepest store and materialize paths stay under 260 for a vault root of 100 characters |
| T27 | `test_two_process_commits` | concurrent adds of the same bytes from two processes: both entries committed with unique ids, objects verified, no lock held while writing objects |
| T28 | `test_discovery_skips_vaults_and_copies` | `discover_save_dirs` does not descend into folders with `catalog.json` or `PRE-UPGRADE-COPY.txt` |
| T29 | `test_export_byte_identical` | export equals the source folder in names, bytes and mtimes |

**Integrity checks in production**: verify on every write and read (2.6, 2.7), `fsck` quick at
open on removable media and after recovery, deep after the fold and before freeing (2.11).

## 6. Implementation plan

Sizes: S <= ~150 lines of production code plus tests, M ~150-400, per commit. Each step ends
with `uv run pytest` green and one commit.

| Step | Phase | Content | Size | Tests |
|---|---|---|---|---|
| B-0 | 0-1 | prerequisites outside D3: P1, P2 (D2 section 2), P7, P9; D1's `safety.exclusive_lock`; this design's `safety.fsync_dir` | S | lock + fsync_dir |
| B-1 | 2 | `store/FORMAT` create / open / compat checks, `vault_mode`, age-based `tmp/` sweep | S | open, too-new, read-only |
| B-2 | 2 | `ObjectStore` put / get / verify / quarantine | S | T1, T2 |
| B-3 | 2 | `StoreEntry` dataclasses, canonical JSON, strict loader | S | T5, T7 |
| B-4 | 2 | Steam `add_from_folder` / `add_from_live`, scopes, member records, stability check, commit under the lock, crash points | M | T3, T6, T8, T25, T27 |
| B-5 | 2 | Xbox capture: verbatim index, slot-scoped rebuilt index | M | T4 |
| B-6 | 2 | materialize / `materialized()` / export | S | T26, T29 |
| B-7 | 2 | `StoreLocator` locate / open / is_durable, damage records; wired into D1's `VaultLocator` with D2's `FolderLocator` | M | T10 (read part) |
| B-8 | 2 | `preserve()` | M | T10 |
| B-9 | 2 | listing: `VaultEntry`, `list_vault_entries`, `open_entry_folder`, `store_status` | M | T11 |
| B-10 | 2 | classic ops in store mode; mode check + lock on every vault write; `import_vault` legacy-source fold, store source refused | M | T12, T22 (phase-2 part) |
| B-11 | 2 | `fsck` quick / deep / repair + `nmsvault store fsck` | M | T24 |
| B-12 | 2 | upgrade: plan, copy-aside, verify, commit, interrupted cleanup, `PRE-UPGRADE-COPY.txt`, discovery and open/import refusals; GUI dialog hook + `nmsvault store upgrade` | M | T13-T16, T28 |
| B-13 | 2 | fold: automatic after the upgrade, `migration.json`, progress / cancel / resume, waiting for space | M | T9, T17 |
| B-14 | 2 | copy-in of hand-made in-place folders + `nmsvault store copy-in` | S | T18 |
| B-15 | 2 | rollback + `nmsvault store rollback` | M | T19, T20 |
| B-16 | 2 | DESIGN.md (storage model, invariants, upgrade) and README (vault format, upgrade, older versions unsupported after it) | S | - |
| - | | **end of phase 2**: no code deletes a vault file except the app's own temp files and its own unfinished copy | | |
| B-17 | 5 | free old folders after a deep fsck + `nmsvault store free-old-folders` | M | T21, T19 (free part) |
| B-18 | 5 | `import_vault` merge of a store-mode source | S | T22 (merge part) |
| B-19 | 5 | DESIGN.md / README: freeing old folders, merge | S | - |

Rough total (INFERRED from the per-step sizes): ~1,900-2,500 lines of production code and
~1,500-2,000 lines of tests; phase 5 (B-17 .. B-19) is about 15 % of it.

## 7. Risks and open questions

**Risks** (most severe first)

1. **One copy per version.** Deduplication removes today's accidental redundancy (a version sits
   in 6 folders on average, R1 2h): a damaged object affects every entry that references it.
   Mitigations: verification on write, on every dedupe hit and on every read; fsck; repair from
   legacy, in-place, live and copy-aside copies; quarantine instead of deletion; the old folders
   stay until the owner frees them. Residual: a disk failure loses the vault, as today. Recommend
   mirroring the vault to a second drive (4.8 merge, phase 5).
2. **A bug in "free old folders" deletes many versions at once.** Mitigations: phase 5 only,
   owner-requested, a clean deep fsck first, a byte-for-byte re-verification of each folder right
   before its deletion, never in-place, live or the copy-aside; the copy-aside while it exists.
3. **The upgrade on a full or slow drive.** It needs the vault's size again in free space for the
   copy (3.4 GB for the owner) and takes minutes on hard disks and NAS. Mitigations: plan-time
   checks with exact numbers, progress, cancel, another destination folder, the vault unchanged on
   every failure.
4. **Rollback after a long store-mode period** writes every store-only entry as a full folder
   (not deduplicated). Mitigation: exact space check before anything is written; the store is moved
   aside, never deleted.
5. **Older app versions on an upgraded vault** (accepted by the owner, Q2): an older version's
   Undo can restore an older whole-folder snapshot. Mitigation: README and release notes.
6. **Coordination with D5.** Organizer live operations must refuse outside store mode, and the
   store's snapshot ids go into the oplog's `snapshot_id`.
7. **Slow or quirky media.** The virus scanner's first-read penalty (185 ms vs 6 ms for 41 new
   manifests), exFAT rename atomicity, SMB locking. Mitigations: resumable per-entry fold,
   progress, read-only fallback, quick fsck at open.
8. **Manifest schema evolution.** `FORMAT` `read_compat` / `write_compat`; manifests immutable;
   unknown keys ignored by readers and covered by the checksum.
9. **Folder-reading tools** (NomNom) cannot browse the store directly; Export covers it.

**Open questions**

| # | Question | Whose | Recommendation |
|---|---|---|---|
| Q1 | Persistent index cache | design | not before ~2,000 entries |
| Q2 | Owner validation run | owner, once | on the real machine after B-15: upgrade, read the fold report, make a full backup, export it and compare with the live folder; try "Undo the vault upgrade" on a copy of the vault, never on the real one |

## Deviations from 01-consolidation section 6

- **A second vault-resident store.** Section 6 names `<vault>/organizer/` (D1) as the vault's
  store. B adds `<vault>/store/`, owned by D3, holding objects and entry manifests; it shares D1's
  lock file and keys nothing by path. The copy-aside and a rolled-back store live next to the
  vault, outside it.
- **`Location` for the store carries slot and member.** A store location is
  `(StoreRef(entry_id), slot, member)` of the entry that holds the vid, because the meta (and its
  ordinal) belongs to that entry. `StoreRef.key` = entry id. `Location` has no `prunable` field
  (D1): nothing is ever pruned.
- **"Capture" naming.** The persisted backup unit is a "store entry"; D1's capture stays one slot
  observation. A store entry yields one D1 capture per occupied slot (2.9).
- **One listing of vault entries.** D1's scanner, the classic tab and the CLI read entries through
  `list_vault_entries` (catalog + store) instead of `vault.entries` (2.9).
- **`open(vid, prefer)` meta choice.** When a vid has more than one meta plaintext, `open` returns
  `prefer`'s, else the newest store entry's; `OpenedVersion.location` reports which.
- **`preserve` signature.** D1's refinement `preserve(members, *, op_id, reason) -> list[Location]`;
  both members of a slot are always stored.
- **Oplog.** `snapshot_id` may name a store entry; nothing else changes in the oplog line shape.

## Appendix A. Evidence

All scripts are in `<D3>` or `<U2>`, run from the repo root with `PYTHONUTF8=1 uv run python
<script>` (U2's with `python -I`). Every script opens real files read-only and writes only under
its own folder.

| Script | Purpose | Output |
|---|---|---|
| `<D3>\file_inventory.py` | sha256, size, mtime and category of every file of the live folders and of all 49 catalog entries; vault fingerprint before and after | `files.jsonl`, `folders.json`, `inventory_summary.json` (4,508,041,245 B hashed in 8.02 s) |
| `<D3>\analyze_store.py` | store size per variant, deduplication by category, marginal cost per entry in creation order, live-only and in-place extras, Steam meta raw vs plaintext, cache churn | `store_analysis.json` |
| `<D3>\chunk_dedupe.py` | chunk-level vs whole-file deduplication over the 195 distinct data files | `chunk_dedupe.json` |
| `<D3>\proto_store.py` | the prototype: folds the 41 managed entries into `<D3>\proto\store`, verifies all, materializes three, times index load and fsck | `proto_report.json` |
| `<D3>\proto_ops.py` | live snapshot / full captures into the prototype store, `open(vid)` and XXTEA timings | `proto_ops_report.json` |
| `<U2>\vault_size.py` | 2026-10-17 re-check of the vault size per top folder and free space | stdout: 4,517 files, 3,404,610,911 B, newest file 2026-10-05 |
| `<U2>\copy_aside_proto.py` | the copy-aside algorithm of 4.1 (walk, copy with sha256 + fsync, re-read verify, re-walk) into a scratch folder, deleted afterwards | `copy_aside_proto.out.txt`: 30.85 s copy, 20.04 s verify, 0 mismatches, source unchanged |

Read-only proof (VERIFIED after the D3 prototype runs and again on 2026-10-17 after the U2
runs): the vault still has 4,517 files and 3,404,610,911 B, newest mtime unchanged,
`catalog.json` sha256 `a3a58014...a045b9` and `oplog.jsonl` sha256 `29b3ece9...598aaa` as at R1's
baseline.
