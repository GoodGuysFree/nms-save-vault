# D2 - Storage option A: index over folders

Status: design for fork F3-A and fork F9. Author: D2 subagent, 2026-10-16, branch
`design/save-organizer`. No production code, tests or packaging were changed. Experiments ran in
synthetic sandboxes and over research inventories that were already computed; the owner's vault
and live folders were only read.

Audience: implementing agents (completeness wins) and the parent. Bound by
`01-consolidation.md` sections 2 and 6 and by D1's published interfaces
(`findings.D1-interfaces` v1; final text in `D1-core-model.md`).

Evidence tags: **VERIFIED** = run by D2 (script named, under `<D2>`); **SOURCED** = code
`file:line` or a research/design report; **INFERRED** = reasoning from those.
`<D2>` = `%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\<session>\scratchpad\D2\`.
Redaction: `st_<steamid64>`, `<xbox-account>`, `%APPDATA%`, `%LOCALAPPDATA%`.

## 0. Owner summary

- A keeps today's vault (a folder per backup, readable by any app version) plus an index; a version's last copy is never deleted.
- "Move to live" keeps the evicted save as a ~5 MB slot backup, skipped when a re-checked identical copy exists.
- Fixes today's risk: 9 versions that exist only in auto-snapshots are copied out before any snapshot is deleted.
- Stop copying the thumbnail `cache/` (no restore uses it) + one-time clean-up: 3.4 GB -> ~1.7 GB.
- Vs B (one copy per version, D3): no migration, but ~4.5 GB/year growth at your backup rate vs ~1 GB.
- Your calls (my pick): A or B (A now); retention (keep everything); old thumbnails (keep one each, 139 MB); snapshots per account (10).

## 1. Scope, non-goals, dependencies

**In scope**

- `FolderLocator`: the implementation of D1's `VersionLocator` over catalog entries and live
  folders (`locate`, `open`, `preserve`, `is_durable`), plus `details()` for storage-specific facts.
- Durability classes for every kind of copy, including in-place entries and the live folder that
  is catalogued as a backup (P9).
- `preserve()` for evicted saves: storage shape, dedupe, verification, catalog record, what older
  app versions see.
- Version-aware retention (C27, fixes P2): snapshot pruning with rescue, exemptions, "delete
  backup" and "delete version" rules, the fate of `SNAPSHOT_RETENTION = 20`.
- The Steam `cache/` policy and the F9 retention variants (section 3).
- The integrity ledger (manifests), housekeeping and crash recovery, D11 portability for
  option A, disk and performance budget.

**Non-goals**

- Identity, captures, lineage, notes, journal, read model, store header and lock: D1.
- Content facts: D4. Live writes, op journal, undo, post-launch verification: D5. UI: D6/D7.
- The content-addressed store and its migration: D3.
- Save-data deduplication across folders. Hard links were measured and rejected for option A
  (section 3.2); one stored copy per version is option B's job.
- Any change to the `catalog.json` v1 schema (C21). Option A writes only legacy kinds.

**Dependencies**

| From | What D2 uses |
|---|---|
| D1 | `organizer.ids` (`LiveTarget`, `SourceRef` = `LiveRef`/`EntryRef`), `organizer.locator` (ABC, `Location`, `OpenedVersion`, `LiveMember`), `organizer.store` (`<vault>/organizer/`, `safety.exclusive_lock`, atomic JSON with unknown keys preserved), scanner `ScanResult` (captures with member vids, `MetaSnap.meta_sha` = sha256 of the meta plaintext, `entry-is-live` problems), live-source resolution, `protection(vid)` reasons (D1 2.7.5) |
| D5 | Calls `preserve()` before every live write; owns `ops.jsonl` and the per-machine open-op markers, which name the snapshot each open op relies on; passes the returned `Location`s to D1's `record_op` |
| D3 | Competing option. Shares the retention policy semantics (3.1) and the old-app rule "legacy kinds only" |
| D4 | Reads bytes only through `open()` |
| Phase-0 defects | P2 is fixed by this design (2.9). P9 is classified here (2.4); removing that catalog entry is the P9 fix's call. P1 is not needed for hashing or `preserve()`; the thinning modes (3.1) need correct Xbox meta times, so they need P1 |

## 2. Design

### 2.1 Principles

- **A1 Folders stay.** Every vault copy remains a save-folder-shaped catalog entry that app
  versions v0.0.9 to v0.2.8 can list, restore and import (4.2).
- **A2 Index, don't move.** The version index is derived in memory from D1's captures. The only
  new persistent data are an integrity ledger (manifests) and role records, both under
  `<vault>/organizer/`.
- **A3 A snapshot is never the last vault copy of a version** once housekeeping has run: the
  slot is rescued into a durable entry first.
- **A4 Legacy kinds only.** Preserved and rescued copies are catalogued as kind `extract`.
- **A5 Verify every new copy** by re-reading and hashing it before it counts as durable.

### 2.2 Safety invariants

| # | Invariant | Enforced by |
|---|---|---|
| I1 | D2 never writes a live folder. It reads live files only in `preserve()` | code review; tests T3, T19 |
| I2 | Save data is copied verbatim; a copy counts only after its re-read sha256 equals the source bytes' sha256 (and the expected vid when given) | slot-entry writer 2.8 |
| I3 | No automatic path deletes the last durable vault copy of a version. Deletions plan from a fresh index under the lock, rescue first, and re-check right before deleting | 2.9, 2.10 |
| I4 | `catalog.json` stays v1: only existing kinds, absolute paths, no new keys | 2.5, 4.2 |
| I5 | Nothing is ever written inside an entry folder after creation. New vault-level paths are `organizer/`, `thumbs/`, `.staging/`, `trash/` | 2.3 |
| I6 | Deletion is two-phase: catalog record removed under the lock, folder renamed into `trash/`, then deleted. A crash leaves a catalogued folder or an uncatalogued folder in `trash/`, never a record pointing at a half-deleted folder | 2.11 |
| I7 | Entry folders are built in `.staging/<id>/`, verified, then renamed into place. A crash leaves only a staging folder, which is swept | 2.8, 2.11 |
| I8 | Every vault mutation by the new app holds the vault lock and reloads `catalog.json` right before writing it | 2.14 |
| I9 | `preserve()` fails closed: changed live bytes, a failed verification, a missing or full vault, or a lock timeout raise `PreserveFailed` before any live write | 2.7 |
| I10 | A catalog entry that is a live folder (P9) is never a copy, never durable, never deleted | 2.4 |
| I11 | Folders outside the vault (in-place entries) are never modified or deleted; removing one from the list is catalog-only | 2.10 |
| I12 | Vault files are never hard-linked or marked read-only (VERIFIED H2, H6: shared blast radius; old-app `rmtree(ignore_errors=True)` leaves read-only residue) | 3.2 |

### 2.3 What changes on disk

```
<vault>/
  catalog.json          v1, schema unchanged; D2 adds kind "extract" entries keep-* and rescue-*
  oplog.jsonl           unchanged by D2 (D5 extends its lines)
  backups/<id>/         full and imported entries (new Steam fulls carry no cache/ under K1)
  snapshots/<id>/       auto-snapshots, pruned version-aware (2.9)
  extracts/<id>/        user extracts, keep-<plat>-<stamp> (preserved), rescue-<plat>-<stamp> (rescued)
  organizer/            D1's store; D2 owns storage.json in it (reserved by D1)
  thumbs/               only with cache option K3a or K2: <sha[:2]>/<sha>.<ext> + index.json
  .staging/<id>/        transient: an entry folder being built
  trash/<id>/           transient: an entry folder being deleted
```

Old apps never enumerate the vault root and `import_vault` does not carry unknown root folders
(VERIFIED E3), so the new root folders are invisible to them. VERIFIED on the owner's catalog
(`<D2>\catalog_layout.py`): all 41 managed entries sit exactly at
`<vault>/<subdir>/<id>` (`snapshots` for snapshot, `extracts` for extract, else `backups`), the
same rule `_entry_folder` uses (`operations.py:845-850`). Option A relies on that convention for
D11 (2.13).

### 2.4 Durability classes

| Copy | `Location.kind` | `durable` | `prunable` | Why |
|---|---|---|---|---|
| Live member (Steam `st_<steamid64>`, Xbox `wgs`) | live | no | no | The game overwrites it. The `xgs` mirror is ignored (`wgs` is authoritative) |
| Catalog entry whose resolved path equals a live folder or lies inside a live root (P9: `inplace-20260624-182102` = the live `wgs` folder) | not reported | - | - | A live alias, not a copy. Its captures are dropped so live saves are not counted twice |
| Managed `full` / `extract` / `imported` at `<vault>/<subdir>/<id>`, manifest ok | entry | **yes** | no | App-created, immutable, inside the vault, hash-recorded. Includes `keep-*` and `rescue-*` |
| Managed `snapshot` | entry | no | yes | Crash recovery and panic restore only; pruned by policy (C27: "auto-snapshots: no") |
| In-place folder outside the vault (PC-A, PC-B) | entry, `external=True` | no | no | **External**, see below |
| Unmanaged reference into another vault (`import_vault` in place) | entry, `external=True` | no | no | External: the other vault prunes or deletes it |
| Managed entry with a manifest mismatch or a missing file | entry | no | no | Damaged: reported to the owner, never counted |

**Why in-place folders are not durable** (decision):

1. The app does not control their lifecycle. They are ordinary folders under
   `%APPDATA%\HelloGames\NMS\` that the owner, save editors and other tools can change or delete
   at any time, and the catalog never re-reads them: cached summaries are frozen at creation and
   go stale (R1 A5).
2. They are machine-specific by D11 (absolute paths) and do not travel with the vault.
3. Nothing hash-verified them when they were catalogued (`import_backup` copies nothing).
4. C23 and R8 C4 require the preserved copy to be durable, hash-verified and in the vault.

Consequences: `preserve()` copies a version even when an in-place copy exists; retention never
counts an in-place copy as "another copy"; the organizer shows an "outside the vault" badge and
offers "Copy into vault" (2.10, optional, about 73 MB for the owner per D3's measure).

Owner's numbers (VERIFIED, `<D2>\budget.py` over R7's inventory): 195 versions = 145 with a
durable copy, **9 only in snapshots** (none live), 25 only outside the vault (some also live),
16 only live. R7's "6 only in snapshots" counts versions held by exactly one folder; 3 more sit
in two or more snapshots. D1's bootstrap prototype also finds 9.

### 2.5 Data

D2 persists one file, `<vault>/organizer/storage.json`, the name D1 reserved for D2 ("manifests,
roles, retention", D1 2.3). It follows D1's store conventions: header
`schema` / `kind` / `read_compat` / `write_compat`, `indent=1`, `ensure_ascii=True` (pure ASCII
on disk), written by temp + fsync + `os.replace` only while holding the store lock, unknown keys
preserved on rewrite.

#### 2.5.1 Settings, roles, intents (`storage.json`, mutable part)

```json
{
  "schema": 1, "kind": "nmsvault.organizer.storage", "read_compat": 1, "write_compat": 1,
  "settings": {
    "cache_policy": "skip",
    "retention": {
      "mode": "keep-all",
      "snapshots_per_source": 10,
      "thin": {"preserved_grace_days": 90,
               "density": [[7, 0], [30, 1], [365, 7], [1000000, 30]]},
      "per_line": {"max_versions": 30, "max_age_days": null}
    }
  },
  "roles": {
    "keep-steam-20261016-101500": {
      "role": "preserved", "op_id": "op-20261016T071500Z-3f2a",
      "reason": "evicted by Place", "created": 1792131300,
      "target": {"platform": "steam", "account_key": "st_<steamid64>", "slot": 7}},
    "rescue-steam-20260625-154113": {
      "role": "rescued", "from_entry": "snapshot-steam-20260625-154113", "slot": 13,
      "created": 1792131310}
  },
  "pending": [
    {"id": "pi-20261016T071500Z-9c1d", "action": "create-entry",
     "entry_id": "keep-steam-20261016-101500", "op_id": "op-20261016T071500Z-3f2a",
     "stage": "staging", "started": 1792131300,
     "expect": {"save13.hg": "<sha256>", "mf_save13.hg": "<sha256>"}}
  ],
  "compaction": {"variant": null, "done_entries": []},
  "manifests": {"...": "2.5.2"}
}
```

- Times are UTC unix seconds.
- `settings` is vault-level on purpose: two machines sharing one vault behave the same.
- `roles` has records only for entries D2 created. Every other entry's role is derived:
  `user` for full/imported/extract, `auto` for snapshot, `external` / `live-alias` for in-place.
- `pending` holds D2's own intents (2.11). D5's op journal is separate (`ops.jsonl`).

#### 2.5.2 Manifests (`storage.json` key `manifests`; integrity ledger, write-once per entry)

```json
{
  "manifests": {
    "full-steam-20260630-092328": {
      "kind": "full", "platform": "steam", "folder": "backups/full-steam-20260630-092328",
      "verified": "bootstrap", "verified_at": 1792131300, "status": "ok",
      "members": [
        {"slot": 1, "m": "A",
         "data": ["save.hg", 518796, "<sha256>"],
         "meta": ["mf_save.hg", 432, "<sha256 raw>"], "meta_plain": "<sha256>"}
      ],
      "other": [["accountdata.hg", 98765, "<sha256>"], ["mf_accountdata.hg", 432, "<sha256>"],
                ["steam_autocloud.vdf", 52, "<sha256>"]],
      "cache": {"files": 180, "bytes": 131072000}
    }
  }
}
```

- Only **durable-kind** entries get a manifest (full, extract, imported, `keep-*`, `rescue-*`).
  Snapshots come and go; their vids come from D1's scan.
- Paths are relative to the entry folder (POSIX separators). Xbox members use
  `"<DIRGUID>/<BLOBGUID>"`; `other` holds `containers.index` and `container.<n>`.
- `verified`: `creation` (hashed from the source bytes while copying), `bootstrap` (first hash of
  an existing entry), `first-seen` (an entry made by an older app or imported, hashed by
  housekeeping). `status`: `ok`, `damaged` (a file no longer hashes as recorded), `missing`.
- `cache` files are counted, not hashed (they are not save data; K3 hashes them when it runs).
- This is not a cache. D1 keeps its hash caches per machine and outside the vault (D1 decision
  6); the manifest is the vault's own record of what each backup held when it was made. It lets
  any machine detect "this backup changed after it was made" (an editor saving into the vault,
  bit rot) and report it instead of silently treating the changed file as a new version.
- Size (INFERRED from the inventory): about 260 B per member; the 522 member copies in the 25
  durable-kind entries today (VERIFIED budget.py) give ~0.14 MB; +0.2-0.3 MB a year. One file
  keeps D1's reservation; a rewrite of ~0.2-0.5 MB costs ~10 ms (R5 F6), and a `preserve()`
  rewrites it 3-4 times (intent stages), so splitting the ledger out is not worth a second file.

#### 2.5.3 Python types (module `nms_save_vault.core.organizer.folders`)

`FileRec` and `MemberRec` are defined in `core/slotentry.py` (Phase 0, 2.8), which must not
import the organizer package; `organizer.folders` re-exports them.

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from .ids import EntryRef, LiveRef, LiveTarget           # D1
from .locator import LiveMember, Location, OpenedVersion, VersionLocator  # D1

Durability = Literal["durable", "transient", "live", "external", "damaged"]
Role = Literal["preserved", "rescued", "user", "auto", "external", "live-alias"]


@dataclass(frozen=True)
class FileRec:
    rel: str            # relative to the entry folder, POSIX separators
    size: int
    sha256: str


@dataclass(frozen=True)
class MemberRec:
    slot: int
    member: Literal["A", "B"]
    data: FileRec
    meta: FileRec
    meta_plain: str | None   # sha256 of the meta plaintext (D1 MetaSnap.meta_sha)


@dataclass
class EntryManifest:
    entry_id: str
    kind: str
    platform: Literal["steam", "xbox"]
    folder: str              # "<subdir>/<id>", relative to the vault root
    members: list[MemberRec]
    other: list[FileRec]
    cache_files: int = 0
    cache_bytes: int = 0
    verified: Literal["creation", "bootstrap", "first-seen"] = "creation"
    verified_at: int = 0
    status: Literal["ok", "damaged", "missing"] = "ok"


@dataclass(frozen=True)
class FolderLocationInfo:
    """Storage facts about one Location that D1's Location does not carry."""
    folder: Path             # resolved entry or live folder on this machine
    data_rel: str
    meta_rel: str
    meta_plain: str | None
    durability: Durability
    entry_kind: str | None   # catalog kind; None for live
    role: Role | None
    status: Literal["ok", "unverified", "damaged", "missing"]


@dataclass(frozen=True)
class LiveSourceInfo:
    """How D2 sees a live folder. Built by D1 from state.json + discovery (D7: injected)."""
    platform: Literal["steam", "xbox"]
    account_key: str         # live folder name
    folder: Path
    roots: tuple[Path, ...]  # folders whose subtree is "live" (the wgs and xgs account folders)
```

### 2.6 The locator

#### 2.6.1 Building the index

```python
class FolderLocator(VersionLocator):
    def __init__(
        self,
        vault_root: Path,
        store: "OrganizerStore",                       # D1
        live_sources: Callable[[], Sequence[LiveSourceInfo]],
        *,
        clock: Callable[[], int],
        hash_file: Callable[[Path], str] = safety.sha256_file,
    ) -> None: ...

    def refresh(self, scan: "ScanResult") -> None:
        """Rebuild the in-memory index from D1's latest ScanResult (entries, captures,
        problems). No I/O beyond reading catalog.json and storage.json; cheap enough to run on
        every scan."""
```

Algorithm:

1. Load `catalog.json` and `storage.json` (one read each; ~4 ms for the catalog, R1 2h).
2. For each catalog entry, compute `resolved = vault.entry_folder(entry)` (the catalog API of
   2.13, the same call D1's scanner uses) and classify it with the table in 2.4:
   - `is_live_alias(resolved)`: `os.path.normcase(os.path.realpath(p))` equals a live folder or
     starts with one of its `roots`. D1's scanner already reports such an entry as
     `entry-is-live` and yields no captures for it (D1 2.5 step 2); D2 checks again so that its
     own delete and retention paths can never treat it as a vault copy.
   - managed and `resolved` inside the vault root: durable-kind or snapshot;
   - otherwise external.
3. For each capture in `scan.captures`, map `(where, slot, A/B)` to D1's
   `Location(vid, kind, ref, slot, member, platform, durable, prunable, external)`; `external` is
   True for in-place folders and references into other vaults:
   - durable requires a manifest member whose `data.sha256` equals the captured vid. A mismatch
     marks the manifest member `damaged` (written by housekeeping, not by `refresh`), and that
     location stays `durable=False` until the owner resolves it.
   - An entry without a manifest is `unverified`: durable after housekeeping writes its
     first-seen manifest; until then `durable=False` (fail safe).
4. Index structures: `by_vid: dict[str, list[Location]]`, `by_entry: dict[str, list[Location]]`,
   `info: dict[Location, FolderLocationInfo]`, plus a `generation` counter that every D2 write
   bumps (callers holding old Locations re-query).

Cost: 1,237 member copies today (R7 2.2) give about 1,200 Locations; the mapping is a dict pass
of a few milliseconds (INFERRED). The expensive part, hashing, is D1's per-machine cache (cold:
1.2-2.7 s for every copy, R7 2.9 and R1 2h; warm: stat checks only).

#### 2.6.2 Queries

```python
    def locate(self, vid: str) -> list[Location]:
        """All known copies in D1's order: durable (newest verification first), prunable
        (snapshots), external, live. Damaged copies are excluded (details() / housekeeping)."""

    def is_durable(self, vid: str) -> bool:
        """True when at least one durable Location exists (2.4)."""

    def details(self, loc: Location) -> FolderLocationInfo: ...

    def entry_role(self, entry_id: str) -> Role: ...

    def open(self, vid: str, prefer: Location | None = None) -> OpenedVersion:
        """Read one copy's bytes. Order: prefer (if it still holds vid), then locate() order.
        Each candidate's data is re-hashed; a mismatch skips it and queues a 'damaged' mark.
        Steam meta: decrypt with the ordinal of the data file number (file + 1); if that fails,
        meta.decrypt_autodetect (moved copies). Xbox meta: plaintext as stored.
        Raises VersionUnavailable(vid, tried=[...]) when no copy verifies."""
```

`open()` returns D1's `OpenedVersion(data, meta_plain, platform, meta_format, location)`.
Re-hashing costs about 1 ms per MB (R7 2.9: 2,122 MB in 1.24 s); the largest data file is
6.9 MB (VERIFIED budget.py).

### 2.7 `preserve()`

Signature (D1's form, adopted unchanged):

```python
    def preserve(
        self,
        members: Sequence[LiveMember],     # LiveMember(target: LiveTarget, member: "A"|"B", expected_vid: str)
        *,
        op_id: str,
        reason: str,
    ) -> list[Location]:                   # aligned with `members`; every Location durable=True
```

Algorithm:

```
with store.lock(timeout=10 s):                          # D1 lock; LockTimeout -> PreserveFailed("lock-timeout")
    reload catalog.json and storage.json
    for each target T among members (grouped; a Swap gives two groups):
        folder = live folder of T                        # PreserveFailed("live-missing")
        snap = read_slot(folder, T.slot)                 # 2.8: bytes of every present member's data + meta
                                                         # (Xbox: + container.<n>, + index record)
        for each requested member m:
            got = snap[m.member]                         # PreserveFailed("member-missing")
            if sha256(got.data) != m.expected_vid:       # the game or cloud wrote since D5 planned
                raise PreserveFailed("changed-since-plan")
        reuse = {}
        for each requested member m:
            for loc in locate(m.expected_vid) where durable:
                if rehash(loc.data) == m.expected_vid and meta_plain(loc) == got.meta_plain:
                    reuse[m] = loc; break
                else: queue damaged mark for loc
        if every requested member is in reuse:
            result += reuse; continue                    # nothing written
        check free space >= 2 x slot bytes + 64 MB       # PreserveFailed("vault-full")
        intent = begin_intent("create-entry", op_id, expect=hashes of every file to write)
        entry, recs = write_slot_entry(vault, snap, id_prefix="keep", source_name=T.account_key,
                                       label=f"kept: slot {T.slot} before {op_id}",
                                       expected={m.member: m.expected_vid for m in requested},
                                       lock=already_held)
        write manifest(entry, verified="creation"); write role("preserved", op_id, reason, T)
        end_intent(intent)
        result += Locations in entry for the requested members
bump generation; return result in input order
```

Rules and why:

- **Dedupe key is the exact member state**: data vid **and** meta plaintext hash. A promote, or
  an editor that touches only the meta, gives the same vid with a different meta; skipping the
  copy would lose that meta state. In the owner's vault this never happened (195 vids = 195
  (vid, meta plaintext) states, VERIFIED budget.py), so dedupe loses nothing in practice.
- **An existing copy is trusted only after re-hashing it** (and decrypting its meta). Cost
  <= 10 ms per member.
- **Both members of the slot go into the new entry** whenever at least one requested member
  needs a copy, including a partner D5 did not ask for. The slot pair is the restorable unit
  (R7 2.4), and a one-member extract restored by any app version leaves a mixed slot (P6,
  R1 D6). Cost: at most one extra member, 2.4 MB on average.
- **The bytes written are the bytes hashed.** The live slot is read into memory once, checked
  against `expected_vid`, and written from memory, so no write can slip between check and copy.
  Live reads never take a lock on live files and never write there (I1).
- **Shape on disk** = today's extract shape, so every app version restores it into the same
  slot: Steam keeps the original file numbers and the meta stays keyed for them
  (`operations.py:286-295`); Xbox gets a minimal `wgs` folder, the slot's GUID folders verbatim
  plus a rebuilt `containers.index` (`operations.py:466-489`).
- **Catalog record**: `kind "extract"`, `managed true`, `id keep-<plat>-<YYYYMMDD-HHMMSS>` (via
  `Vault.new_id("keep", tag=plat)`), `source` = the live folder name, `created` = now (local
  ISO, as v1 expects), `label` ASCII `kept: slot 7 before op-20261016T071500Z-3f2a`, `slots`
  from `summarize(scan_any(folder), [slot])`. No new keys (I4).
- **Never prunable** in any retention mode while the version is protected (3.1); under
  keep-everything never.

What older app versions see (VERIFIED E2, E3): an ordinary extract in the Backups pane, labelled
"kept: ...". Restore puts that slot back into the same live slot and leaves every other slot and
`accountdata` alone (v0.1.0 and later). Snapshot pruning never touches it. `import_vault` copies
it to `extracts/<id>`. v0.0.9 full-restores every extract (the pre-existing 2026-08-22 accident,
`1e086c5`); that is not new with D2, and a new kind would be worse on every version (E1).

`PreserveFailed.code` values: `live-missing`, `member-missing`, `changed-since-plan`,
`vault-unavailable`, `vault-full`, `verify-failed`, `lock-timeout`, `io-error`. D5 aborts the
operation before its first live write on any of them (R8 4.7 "vault drive missing or full ->
refuse before any live write").

### 2.8 The slot-entry writer (shared by extract, preserve, rescue)

One core routine replaces the unverified copying in `extract_slot` and serves `preserve()` and
rescue. It lives in a new core module (`core/slotentry.py`) so that Phase 0 can use it without
the organizer.

```python
@dataclass(frozen=True)
class MemberBytes:
    slot: int
    member: Literal["A", "B"]
    files: Mapping[str, bytes]     # relative path -> bytes (Steam: data + meta;
                                   # Xbox: "<DIR>/container.<n>", "<DIR>/<BLOB>" x2)
    data_rel: str
    meta_rel: str


@dataclass(frozen=True)
class SlotSnapshot:
    platform: Literal["steam", "xbox"]
    slot: int
    members: Mapping[str, MemberBytes]     # "A"/"B" -> bytes; absent members missing
    xbox_index: "msstore.XboxIndexInfo | None"
    xbox_records: Sequence["msstore._Container"]   # the slot's records, for the rebuilt index


def read_slot(folder: Path, slot: int) -> SlotSnapshot:
    """Read one slot of a Steam or wgs folder (live or an entry) into memory. Never writes."""


def write_slot_entry(
    vault: catalog.Vault,
    snap: SlotSnapshot,
    *,
    id_prefix: Literal["extract", "keep", "rescue"],
    label: str,
    source_name: str,
    created: str | None = None,          # rescue: the source snapshot's created
    expected: Mapping[str, str] | None = None,   # member -> vid that must match
    lock: "ContextManager[None]",
) -> tuple[catalog.CatalogEntry, list[MemberRec]]:
    """Build <vault>/.staging/<id>/ from the in-memory bytes, fsync, re-read and hash every
    file, check `expected`, check scan_any(staging) reads the slot, os.replace the folder to
    <vault>/extracts/<id>, then upsert the catalog entry (reload-before-write, under `lock`).
    Raises SlotEntryError and removes the staging folder on any failure."""
```

Details:

- Staging and the final folder are on the same volume (both under the vault root), so the
  directory rename is atomic on NTFS, ext4 and APFS (INFERRED). On Windows an antivirus or
  indexer handle can make the rename fail with `PermissionError`: retry 5 times at 200 ms,
  then fail and clean up.
- The catalog upsert is the last step. A crash before it leaves an uncatalogued folder that the
  intent (2.11) either adopts (verified) or removes.
- Xbox: the rebuilt index comes from `msstore.build_containers_index(info, picked)` over the
  slot's records (as `_extract_slot_xbox` does today). Validation: `savedir.scan_any(staging)`
  must show the slot's members with the expected sizes and the data hashes must match.
- `extract_slot` keeps its name and behaviour but gains verification and a manifest; the user
  sees no change except that a failed copy now raises instead of cataloguing a bad folder.

### 2.9 Retention: snapshot pruning, rescue, exemptions, keep-20

#### 2.9.1 Today and the defect

`snapshot_live` calls `_prune_snapshots`, which deletes the oldest managed snapshots beyond 20
by `created`, across platforms, whole folders (`operations.py:31`, `148-159`). It deletes the only
copy of 9 versions once their snapshots age out (P2), and Steam and Xbox snapshots compete for
one budget (R1 risk 7).

#### 2.9.2 The policy

```python
@dataclass(frozen=True)
class RetentionPolicy:
    mode: Literal["keep-all", "thin", "per-line"] = "keep-all"     # F9, section 3.1
    snapshots_per_source: int = 10          # >= 1; per live folder (entry.source)
    preserved_grace_days: int = 90          # thin / per-line only
    density: tuple[tuple[int, int], ...] = ((7, 0), (30, 1), (365, 7), (10**6, 30))
    per_line_max: int = 30                  # per-line only
    per_line_max_age_days: int | None = None
```

`SNAPSHOT_RETENTION = 20` is removed. The policy lives in `storage.json` (vault-level); Phase 0,
which has no organizer store, uses the dataclass defaults.

#### 2.9.3 Inputs (pure planner, module `core/retention.py`)

```python
@dataclass(frozen=True)
class CopyRef:
    entry_id: str
    slot: int
    member: Literal["A", "B"]
    vid: str


@dataclass(frozen=True)
class Container:
    entry_id: str
    kind: str                     # catalog kind
    role: Role
    source: str | None            # live folder name; snapshots are counted per source
    created_utc: int              # from the catalog's local naive `created`; only order matters
    durable: bool                 # 2.4
    bytes: int                    # folder size, for the preview
    copies: tuple[CopyRef, ...]


@dataclass(frozen=True)
class Protection:
    vids: frozenset[str]                          # never forgotten in any mode
    reasons: Mapping[str, tuple[str, ...]]        # vid -> ("live", "locked", "noted", "tip", ...)
    pinned_entries: frozenset[str]                # never deleted (see exemptions)


@dataclass(frozen=True)
class Rescue:
    from_entry: str
    slot: int
    vids: tuple[str, ...]          # every member of the slot (pair kept whole)
    bytes: int
    why: Literal["prune", "at-risk", "delete-entry", "protected"]


@dataclass(frozen=True)
class RetentionPlan:
    rescues: tuple[Rescue, ...]               # executed first
    deletes: tuple[str, ...]                  # entry ids, oldest first, executed after rescues
    forgets: tuple[str, ...]                  # vids left with no vault copy (empty in keep-all)
    kept: Mapping[str, str]                   # entry id -> why it stays (for the preview)
    bytes_freed: int
    bytes_written: int


def plan_retention(
    containers: Sequence[Container],
    protection: Protection,
    policy: RetentionPolicy,
    *,
    now_utc: int,
    live_vids: frozenset[str],
    version_time: Mapping[str, int] | None = None,   # vid -> meta timestamp (thin modes)
    line_of: Mapping[str, str] | None = None,        # vid -> pid (per-line mode)
) -> RetentionPlan: ...
```

Pure function: no I/O, deterministic, testable with tables.

#### 2.9.4 Algorithm

```
1  snapshots = managed snapshot containers, grouped by source (live folder name)
2  for each group: sort by created_utc; keep the newest N (N = snapshots_per_source >= 1)
3  candidates = the rest, minus protection.pinned_entries, oldest first
4  durable_set = { vid : some durable container holds it }
5  for C in candidates:
6      for each occupied slot s of C:
7          missing = [vid in slot s of C if vid not in durable_set]
8          if not missing: continue
9          if mode == "keep-all" or any(v in protection.vids for v in missing) or not thinnable(missing):
10             add Rescue(C, s, all vids of slot s, why="prune");  durable_set |= those vids
11         else:
12             forgets += missing                      # thin / per-line only
13     deletes += C
14 at-risk pass (housekeeping only): for each vid held only by snapshots and not in live_vids,
       not yet in durable_set: rescue its slot from the newest snapshot holding it (why="at-risk")
       - keep-all: always; thin modes: only protected or not thinnable vids
15 dedupe rescues by the slot's (vid_A, vid_B) pair
```

- `thinnable(vids)` is defined per F9 mode in 3.1; it is never true for a protected vid.
- Step 14 makes the vault independent of snapshot age: a version that left live and sits only
  in snapshots is copied out at the next housekeeping, so even an **older app's** keep-20 pruning
  (which does not rescue) cannot lose it after the new app has run once (4.2).
- Execution (2.11): rescues first, each verified; then, per delete, re-plan that one container
  against a fresh index **under the lock** and delete only if its vids are all durable elsewhere
  (or listed in `forgets`). A failed rescue keeps its container.

Exemptions (`protection.pinned_entries`), each with its reason:

| Never deleted | Why |
|---|---|
| The snapshot named by the newest oplog record that carries a `snapshot_id` | Old apps' `undo_last` silently falls through to an older snapshot when that one is missing (VERIFIED by D5, experiment E) |
| The snapshots referenced by open D5 operations (`ops.jsonl` without completion, per-machine open-op markers) | Rollback pre-image (D5) |
| The newest snapshot of each source | Panic restore of the last change on each account |
| Any entry named in D2's own `pending` intents | Crash recovery |

Exemptions by version (`protection.vids`) come from D1's `protection(vid)` reasons (D1 2.7.5;
the partner of a protected head gets the same reasons). What each reason allows is D2's call:

| D1 reason | Meaning | keep-all | thin / per-line |
|---|---|---|---|
| `live` | in a live slot now | protected | protected |
| `locked` | owner lock (C20) | protected | protected |
| `noted` | version label or comment | protected | protected |
| `tip` | newest version of a branch | protected | protected |
| `fork` | branch point (two or more children) | protected | protected |
| `placed` | put live by an operation | protected | protected |
| `evicted` | preserved by an operation | protected | protected for `preserved_grace_days` (90), then thinnable |

In keep-all they never matter for snapshots (everything is rescued); they matter for thinning
and for the delete previews.

Measured on the owner's data (VERIFIED, `<D2>\prune_sim.py`): the first prune with N = 10 removes
the 4 oldest Steam snapshots (138.8 MB of save data) and rescues 1 slot pair (11.0 MB, the June
"Main Save" capture); the Xbox pair of snapshots stays. The at-risk pass then rescues the other
5 pairs (about 40 MB). Total rescue for the 9 snapshot-only versions: 6 pairs, 50.7 MB.

#### 2.9.5 Where it runs

- After every write operation that created a snapshot (classic ops and D5 ops): the planner
  runs once the operation finished, not inside `snapshot_live`, so the op itself is not slowed.
  Today's call inside `snapshot_live` becomes a call at the end of each op (or a post-op hook in
  the GUI/CLI runner).
- At housekeeping (2.11): at-risk rescue only; deletion happens only on the op trigger, so
  opening the app never deletes anything.
- Phase 0 (P2 fix, before D1 exists): the same planner, fed by a minimal adapter: candidate
  copies come from the catalog's cached member summaries (`name`, `play_time`, `timestamp`,
  `data_size`); a durable copy is confirmed by hashing both files; no match means rescue. A false
  negative only causes an extra rescue, never a loss. Cost: hashing one snapshot (~50 MB) and its
  matching files, about 0.1 s warm (INFERRED from R7 2.9 rates).

### 2.10 Deleting: backups and versions

| Action | Allowed on | Rule | What happens |
|---|---|---|---|
| **Delete backup** | managed full / imported / extract / keep / rescue / snapshot | Refuses to delete the last durable copy of any version | Plan lists unique versions; the default path rescues their slots into `rescue-*` entries (verified) and then deletes the folder (two-phase). Pinned entries refuse with the reason |
| **Remove from list** | in-place entries, live alias (P9), unmanaged references | Catalog only; files untouched (I11) | One catalog removal under the lock. Old apps forget it too |
| **Delete version** (user-facing) | a version | Never removes its last durable copy | Removes the version's copies that sit in single-slot app entries (`keep-*`, `rescue-*`, extracts) whose other contents stay durable elsewhere; refuses when that would leave zero durable copies, and says which copies remain (inside full backups or snapshots, which option A cannot edit) |
| **Forget version** | thin / per-line modes only, unprotected versions | Explicit owner action with a preview | As "Delete version" but may remove the last copy in single-slot app entries; copies inside full backups stay until those backups are deleted (option A cannot cut a slot out of a multi-slot folder without changing what a full restore of it does) |
| **Copy into vault** (optional) | external-only versions | - | Rescue-style slot copy from the in-place folder into `rescue-*`; owner total about 73 MB (D3's measure) |

```python
def plan_delete_entry(self, entry_id: str) -> RetentionPlan: ...
def delete_entry(self, plan: RetentionPlan) -> "ApplyReport": ...
def remove_from_list(self, entry_id: str) -> None: ...
def plan_delete_version(self, vid: str) -> RetentionPlan: ...
def plan_copy_into_vault(self, vids: Sequence[str]) -> RetentionPlan: ...
```

Why full backups are never edited: `restore_full` mirrors (`operations.py:266-270`). A full
backup with one slot cut out would, when restored by any app version, delete that live slot.

### 2.11 Housekeeping and crash recovery

```python
@dataclass
class HousekeepingReport:
    resolved_intents: list[str]
    swept: list[str]                 # staging / trash folders removed
    manifests_written: list[str]     # first-seen or bootstrap
    damaged: list[tuple[str, str]]   # (entry id, relative file) that no longer hash as recorded
    orphans: list[str]               # folders in backups/snapshots/extracts not in the catalog
    rescued: list[Rescue]
    deleted: list[str]
    bytes_freed: int
    bytes_written: int


def housekeep(self, protection: Protection, *, allow_delete: bool) -> HousekeepingReport: ...
```

Runs on GUI start (after D1's scan, in a worker; C31), after every write operation
(`allow_delete=True`), and on the explicit `nmsvault vault check` command. Read-only CLI commands
never run it (D1 decision 7). It writes only inside the vault, so it needs no game guard.

Steps, in order:

1. **Intents** (`storage.json` `pending`):

   | Action | Stage at crash | Recovery |
   |---|---|---|
   | create-entry | `staging` | if `extracts/<id>` exists, continue as `placed`; else delete `.staging/<id>` and drop the intent ("an interrupted copy was discarded") |
   | create-entry | `placed` | re-hash the folder against `expect`; ok: catalog upsert, manifest, role; mismatch: move to `trash/` |
   | create-entry | `catalogued` | write manifest and role |
   | delete-entry | `planned` | drop the intent; the next op trigger re-plans |
   | delete-entry | `uncatalogued` | move the folder to `trash/`, delete |
   | delete-entry | `trashed` | delete `trash/<id>` |

   Each stage change is one atomic `storage.json` rewrite (~ms). An interrupted `preserve()` that
   reached `placed` is completed, not discarded: it is a valid copy of what was live, and D5's
   own journal decides the operation's fate.
2. **Sweep**: delete `.staging/*` older than one hour and everything in `trash/` (Windows: clear
   the read-only attribute and retry; failures are reported and retried next time).
3. **Manifests**: write first-seen manifests for durable-kind entries that lack one (entries made
   by older apps or by `import_vault`); compare existing manifests with D1's current vids and
   mark `damaged` / `missing` members.
4. **At-risk rescue** (2.9.4 step 14). Runs only after step 3 has a manifest for every
   durable-kind entry; otherwise an entry still waiting for its first hash would look
   non-durable and trigger needless rescues (fail safe, but wasteful).
5. **Retention deletes** only when `allow_delete`.
6. **Orphans**: report folders under `backups/`, `snapshots/`, `extracts/` that no catalog entry
   names. Never deleted automatically. `keep-*` / `rescue-*` orphans with a verifiable content
   can be adopted (catalogued) by an explicit action.

### 2.12 `cache/` mechanics

Facts (VERIFIED, read-only listing of the live folder; R7 2.5; D3's store analysis):

- The live `st_<steamid64>\cache\` holds 181 files, 132 MB: 148 base thumbnails named
  `<hex16>.<difficulty>.<base name>.<steamid64>.DDS` (921,600 B each, dated 2024-09 to now,
  about 1-5 new per week), 29 small DDS (22,000 B) and the game's intro/season feed caches
  (`*.JSON`, `*.MFT`). It is an account-level game cache, not save data.
- **No restore uses it.** Steam `restore_full` copies only `*.hg` and `steam_autocloud.vdf`
  (`operations.py:258-264`); repopulate copies slot files; Xbox has no cache.
- The vault holds 2,326 cache files, 1,772 MB = 52 % of the vault, but only 184 distinct files,
  139 MB. Each Steam full backup copied 116-136 MB of it; after the first, each adds only 0-8 new
  files (0-5.6 MB).
- `create_full_backup(include_cache=True)` is the default (`operations.py:165-173`), the GUI
  never passes the flag (`gui.py:1398`), and the CLI offers `--no-cache` (`cli.py:495`).

Mechanics of the options (choices in 3.2):

- **Skip** (K1): `create_full_backup` defaults to `_copy_save_state` for Steam (all `*.hg` +
  `steam_autocloud.vdf`), verified by sha256 as today (`_verify_copy(only_hg=True)`); the setting
  is `storage.json settings.cache_policy` (`"skip"` | `"copy"`); the CLI gains `--with-cache`
  and keeps `--no-cache` as an accepted no-op.
- **Thumbs store** (K2 / K3a): `<vault>/thumbs/<sha[:2]>/<sha>.DDS` (or `.JSON`), plus
  `thumbs/index.json`:
  `{"<sha>": {"names": ["<hex16>.Normal.<base>.<steamid64>.DDS"], "size": 921600, "first_seen": "<entry id>"}}`.
  Names contain the account id and must be shown through `aliases.redact`.
- **Compaction** (K3): plan, then per Steam full/imported entry with `cache/`: hash every cache
  file; copy each distinct file once into `thumbs/` (verified) when K3a; rename `cache/` into
  `trash/<entry>-cache`; delete; record the entry in `storage.json compaction.done_entries`
  (resumable, idempotent). The manifest's `cache` counters drop to 0 and the entry's manifest
  notes `"cache_moved": "thumbs"` or `"cache_dropped": true`.

### 2.13 D11 portability without a catalog bump

Option A does **not** need the catalog version bump. Writing relative paths into `catalog.json`
v1 breaks every older app (VERIFIED E4: the entry loads, its folder resolves against the current
directory, the scan shows nothing, restore fails "backup folder not found", and pruning removes
the record while leaving the folder orphaned); and older apps never read `version` (R1 A8, E5).

Instead, "re-home on read" in the catalog API, so that D1's scanner (which resolves entry
folders only through the catalog API, D1 2.5 step 2 and section 4) and D2's locator agree:

```python
# core/catalog.py (platform-neutral; Phase 1)
SUBDIR = {KIND_SNAPSHOT: "snapshots", KIND_EXTRACT: "extracts"}   # else "backups"

class Vault:
    def entry_folder(self, entry: CatalogEntry) -> Path | None:
        """Where an entry's files are on this machine. Managed entries resolve by convention
        when their stored path is stale (vault moved, other machine, other OS); in-place
        entries stay absolute per D11. None when the folder is missing."""
        stored = Path(entry.path)
        if entry.managed:
            if stored.is_absolute() and stored.is_dir() and _is_within(stored, self.root):
                return stored
            conventional = self.root / SUBDIR.get(entry.kind, "backups") / entry.id
            if conventional.is_dir():
                return conventional
        return stored if stored.is_dir() else None   # managed outside the vault -> external
```

`_is_within` compares `os.path.normcase(os.path.realpath(...))` prefixes on path-part
boundaries. Classic operations keep using `entry.path` until a later step switches them to
`entry_folder()` (then a moved vault also works for classic restore in the new app).

- Everything D2 persists is keyed by entry id and relative paths (`roles`, `manifests`), and D1
  keys the organizer store by ids, so `<vault>/organizer/` travels with the vault.
- **Repair paths** (explicit action, `nmsvault vault repair-paths`): rewrites the `path` of
  managed entries whose stored path is stale to the conventional absolute path on this machine,
  after copying `catalog.json` to `catalog.json.bak-<stamp>`. This makes a moved vault work for
  older apps too. In-place entries are left alone and show as missing elsewhere (D11).
- A future catalog v2 (if another design needs it) must use a new file name or D3's barrier
  technique, because old apps cannot be made to refuse a v2 `catalog.json`.

### 2.14 Concurrency

- All D2 writes (catalog, `storage.json`, folder rename, deletion) run inside
  D1's lock (`safety.exclusive_lock` on `<vault>/organizer/.lock`, per read-modify-write
  transaction, D1 decision 5) and reload `catalog.json` immediately before writing it. This
  closes the lost-update hole between a GUI and a CLI of the new app (R1 A9).
- Older app versions take no lock. A concurrent old app can still lose a catalog update, as
  today; documented, not solvable without changing old apps.
- Long work (hashing, compaction, bootstrap manifests) runs outside the lock; the lock is held
  only for the short commit steps, and every commit re-validates what it relies on.
- GUI: D2 work runs in a worker, never on the Tk thread (C31); hashing is I/O-bound and needs no
  process pool.

### 2.15 Errors

```python
class StorageError(OperationError): ...              # core.operations.OperationError

class PreserveFailed(StorageError):
    code: Literal["live-missing", "member-missing", "changed-since-plan", "vault-unavailable",
                  "vault-full", "verify-failed", "lock-timeout", "io-error"]

class SlotEntryError(StorageError): ...               # writer failure; staging removed
class DeleteRefused(StorageError):
    unique_vids: tuple[str, ...]
    pinned_reason: str | None
class VersionUnavailable(StorageError):
    vid: str
    tried: tuple[Location, ...]
```

- Messages are plain ASCII (DESIGN.md "The UI is ASCII-only") and carry entry ids, slot and
  member, never full user paths.
- Damaged copies are not exceptions: they go into `HousekeepingReport.damaged` and are shown
  ("Backup full-steam-20260809-...: slot 7 A changed after it was made"), per the project rule
  that anomalies are surfaced, not silently recovered.
- A failed rescue keeps its source snapshot and is reported; the op that triggered pruning still
  succeeds.

### 2.16 API summary

```python
# core/slotentry.py (Phase 0)
def read_slot(folder: Path, slot: int) -> SlotSnapshot
def write_slot_entry(vault, snap, *, id_prefix, label, source_name, created=None, expected=None, lock) -> tuple[CatalogEntry, list[MemberRec]]

# core/retention.py (Phase 0, pure)
@dataclass RetentionPolicy, CopyRef, Container, Protection, Rescue, RetentionPlan
def plan_retention(containers, protection, policy, *, now_utc, live_vids, version_time=None, line_of=None) -> RetentionPlan
def legacy_containers(vault: Vault, hash_file) -> list[Container]     # Phase-0 adapter (2.9.5)

# core/organizer/folders.py (Phase 1-2)
class FolderLocator(VersionLocator):
    def refresh(self, scan: ScanResult) -> None
    def locate(self, vid) -> list[Location]
    def is_durable(self, vid) -> bool
    def open(self, vid, prefer=None) -> OpenedVersion
    def preserve(self, members, *, op_id, reason) -> list[Location]
    def details(self, loc) -> FolderLocationInfo
    def entry_role(self, entry_id) -> Role
    def housekeep(self, protection, *, allow_delete) -> HousekeepingReport
    def plan_retention(self, protection) -> RetentionPlan
    def apply(self, plan: RetentionPlan) -> ApplyReport
    def plan_delete_entry(self, entry_id) -> RetentionPlan
    def remove_from_list(self, entry_id) -> None
    def plan_delete_version(self, vid) -> RetentionPlan
    def plan_copy_into_vault(self, vids) -> RetentionPlan
    def verify_vault(self, progress=None) -> HousekeepingReport        # full re-hash vs manifests
    def plan_cache_compaction(self, variant) -> "CompactionPlan"
    def compact_cache(self, plan) -> ApplyReport
    def repair_paths(self) -> list[str]

# cli.py (thin): nmsvault vault check | verify | repair-paths | compact-cache [--variant a|b|c] [--dry-run]
#                nmsvault backup --with-cache
```

### 2.17 Disk and performance budget

**Per "Place" (move to live)**: one preserved slot = 2 members. Steam data: mean 2.42 MB, median
1.67, p90 6.39, max 6.92 MB per member; Xbox mean 2.32, max 4.00 MB (VERIFIED budget.py). So a
preserved slot is about 4.8 MB on average and 13.8 MB at most, or 0 MB when both members already
have an exact durable copy. Each op also makes a whole-folder snapshot (Steam 41-52 MB, Xbox
63 MB, R1 A6), which ages out after 10 more snapshots of that account.

**Now and after the one-off steps** (owner's vault):

| State | Size | Basis |
|---|---|---|
| Today | 3,405 MB (49 entries) | R7 2.5 |
| + at-risk rescue (6 pairs) | +51 MB | VERIFIED budget.py |
| + first prune, N = 10 | -139 MB (4 snapshots; -184 MB if the new op's snapshot counts and a 5th goes) | VERIFIED prune_sim.py |
| + cache compaction K3a (keep each distinct thumbnail once) | -1,633 MB | 1,772 -> 139 MB |
| **Result** | **~1.68 GB** (K3b drop all: ~1.54 GB) | |

**After a year** (keep-all, cache skipped; owner's habit: 14 Steam + 3 Xbox full backups in
3.5 months, R7/D3):

| Item | Assumption | MB / year |
|---|---|---|
| Steam full backups | 4 / month x ~52 MB (save data + accountdata, no cache) | ~2,500 |
| Xbox full backups | ~0.9 / month x 65 MB | ~700 |
| Preserved slots | 100-150 Places / year, ~60 % need a copy, 4.8 MB | 290-430 (max 720) |
| Rescued slots | ~3.2 MB per pruned snapshot (6 pairs over 16 snapshots), 100-150 / year | 320-480 |
| Snapshots, steady state | 10 per account: Steam ~500 + Xbox ~650 vs ~720 today | +430 once |
| Organizer store (notes, lineage, journal, manifests) | | < 20 |
| **Total** | | **~4.3-4.8 GB / year** -> ~6.2 GB after a year |

For comparison (INFERRED from the same rates): unchanged behaviour (cache copied, keep-20)
adds ~9.5 GB / year (~13 GB after a year) and still loses snapshot-only versions; option B adds
~0.9 GB / year (D3, `findings.D3-chunk-dedupe`). A's growth is dominated by full backups
duplicating unchanged slots (71 % of a Steam full backup's save data is already in the vault,
VERIFIED budget.py).

**Time** (warm cache; cold disk is slower):

| Action | Cost | Basis |
|---|---|---|
| `refresh()` from D1's `ScanResult` | < 10 ms | INFERRED (dict pass over ~1,200 copies) |
| Bootstrap manifests, 25 durable-kind entries (~870 MB) | ~0.6-1.5 s (cold HDD ~10 s), in a worker | R7 2.9 hash rate |
| `preserve()` Steam slot (<= 14 MB) | ~30-80 ms; dedupe check <= 10 ms per member | INFERRED |
| Rescue one slot | same as preserve | |
| Delete a snapshot (rename + ~35 files) | ~50-200 ms | INFERRED |
| First prune on the owner's vault (N = 10) | < 1 s | prune_sim.py |
| Compaction K3 (2,326 files, 1.77 GB) | ~2-4 s | hash rate + deletes |
| `verify_vault()` | ~2-3 s | R1 2h |
| `storage.json` read / write | ~5 / ~10 ms at 0.2-0.5 MB | R5 F6 JSON rates |

## 3. Options

### 3.1 F9 retention

All three modes keep C27's floor: **no automatic path deletes a protected version**, and
snapshots are pruned per account with the exemptions of 2.9.4.

| | R1 Keep everything | R2 Version-aware thinning | R3 Per-save-file limits |
|---|---|---|---|
| Versions ever forgotten automatically | never | unprotected versions that the density schedule does not select | unprotected versions beyond K per line (and optional max age) |
| Snapshot pruning | rescue every version first | rescue protected or selected versions; others go with the snapshot | rescue protected or within-limit versions |
| App-created single-slot entries (`keep-*`, `rescue-*`) | kept | deleted when all their versions are unprotected, unselected and past the preserved grace (90 days) | deleted when all their versions are beyond the limit |
| Owner's own full backups | never touched | only with an explicit opt-in "thin my full backups": rescue the protected or selected versions they hold alone, delete the folder | same opt-in |
| Runs | on op trigger | op trigger (snapshots only) + "Thin now" with a preview | same |
| Growth (owner, INFERRED) | ~4.5 GB / year | ~3.5 GB / year (only app-made copies thin; full backups dominate); ~1-2 GB / year with the full-backup opt-in | full backups as R2; preserved/rescued slots capped near K x 4.8 MB per line (30 lines x 30 = ~4.3 GB ceiling) |
| Risk | disk only | wrong guess about what matters; mitigated by preview, locks and notes | a busy line loses history fast; quiet lines keep everything |

**Protected set** (all modes): every vid with a D1 protection reason (`live`, `locked`, `noted`,
`tip`, `fork`, `placed`; `evicted` within the grace period; table in 2.9.4), plus the newest
version of each line and versions named by pending operations or intents.

**R2 density schedule** (default `((7, 0), (30, 1), (365, 7), (inf, 30))`): keep everything from
the last 7 days; then one version per line per day up to 30 days; one per week up to a year; one
per 30 days after that. The kept one in a bucket is the most progressed (play time, then meta
time). Ages use the version's meta timestamp, not the capture time.

**R3**: per line keep the newest K = 30 versions plus the protected set; optional maximum age.

**Recommendation: R1 keep everything**, now. It fixes P2, never surprises the owner, and the
space problem is mostly `cache/` (3.2). Offer R2's "Thin now" later as a manual, previewed tool if
the vault grows past the owner's comfort (~10 GB). R3 is the simplest to explain but the most
likely to delete something that mattered on the most-played line (Main Save has 38 member
versions, R7 2.2). This matches the parent's lean (keep everything + version-aware).

### 3.2 `cache/` policy

| | K0 Keep copying | K1 Stop copying | K2 Store once (ongoing) | K3 One-time compaction of old backups |
|---|---|---|---|---|
| New Steam full backups | copy `cache/` (~131 MB each) | `*.hg` + vdf only (~50 MB) | K1 + copy only new distinct cache files into `thumbs/` (~0.1-0.25 GB / year) | - |
| Existing backups | unchanged | unchanged | unchanged | **a**: keep each distinct file once in `thumbs/` (139 MB), remove `cache/` from backups; **b**: remove `cache/` from backups, keep nothing; **c**: hard-link duplicates in place |
| Space | +6.3 GB / year | saves ~131 MB per backup | ~K1 | a: -1,633 MB; b: -1,772 MB; c: -1,633 MB while the vault stays on that NTFS volume |
| Restore impact | none (never restored) | none | none | none |
| Old-app impact | none | none | `thumbs/` ignored | none: old apps never read `cache/` from a backup |
| Value kept | full folder copy | none needed | old base thumbnails for a future UI | a: all thumbnails once; b: none; c: all |

**Hard links considered and rejected** (VERIFIED `<D2>\hardlink_exp.py`, C: NTFS, Python 3.12):
they work (one `os.link` per file, 0.036 s for 180 files vs 0.134 s to copy), refcounted deletes
are safe (H5) and atomic replacement breaks only one name (H3). But:

1. an in-place edit through one name changes every name (H2): a save editor that writes into a
   vault file damages all its "copies" at once;
2. any copy of the vault expands again (H4: `shutil.copytree`, so also `import_vault`; Explorer
   and most sync tools behave the same), which defeats D11's "vault on a USB drive";
3. FAT32 and exFAT have no hard links, SMB support varies, links never cross volumes, so the
   same vault behaves differently per filesystem;
4. the read-only attribute is shared by all names (H6), and old apps' `rmtree(ignore_errors=True)`
   leaves read-only files behind.

Variant K3c is therefore offered only for completeness and not recommended.

**Recommendation: K1 now** (Phase 0; one default flips), **plus K3a as a one-time owner action**
with a dry-run preview (frees 1.6 GB, keeps every thumbnail ever seen once, 139 MB). K2 only if a
UI design wants base thumbnails over time. Release notes must say that a full backup no longer
copies `cache/` and that `--with-cache` restores the old behaviour.

### 3.3 Snapshots kept per account

`snapshots_per_source` (default **10**): each live folder (Steam account, Xbox account) keeps its
newest 10 auto-snapshots, plus the exemptions. Today's global 20 lets one platform's snapshots
push out the other's (R1 risk 7). With rescue, snapshots hold nothing unique, so N only sets the
panic-restore window: 10 is ~10 ops back per account at a steady cost of ~0.5 GB (Steam) and
~0.65 GB (Xbox). 5 halves that; 20 doubles it.

## 4. Migration and compatibility

### 4.1 The owner's existing vault

No migration: nothing moves, nothing is renamed, `catalog.json` keeps v1. One-time steps, all
reversible or additive:

| Step | When | Effect on the owner's vault |
|---|---|---|
| Bootstrap manifests | first start of the organizer build (worker, with progress) | 25 durable-kind entries (17 full + 8 extract) hashed (~870 MB), manifests in `storage.json` ~0.14 MB, `verified: bootstrap` |
| Classify | every refresh | 17 full + 8 extract durable; 16 snapshots transient; 7 in-place external; `inplace-20260624-182102` = live alias (P9) |
| At-risk rescue | first housekeeping | 6 slot pairs, 50.7 MB, into `rescue-steam-*` entries (`created` = the source snapshot's time); the 9 snapshot-only versions become durable |
| First prune | next op on the Steam account | 4 (N = 10) or 5 (if the new snapshot counts) oldest Steam snapshots removed, ~139-180 MB |
| Cache compaction | only if the owner runs it | -1.63 GB (K3a) |

Phase 0 ships the P2 fix without the organizer: the first prune rescues before deleting (2.9.5),
and the at-risk pass arrives with Phase 1.

### 4.2 Older app versions reading the vault

Realistic old versions are **v0.0.9 to v0.2.8**: v0.0.1-v0.0.8 already crash on the owner's
catalog, because `MemberSummary.difficulty` (added in `0636886`, first released in v0.0.9) is an
unknown member key for them (INFERRED: R1 2a VERIFIED that an unknown member key raises
`TypeError`; git history dates the key).

VERIFIED (`<D2>\compat_exp.py`, sandboxes with synthetic Steam saves; v0.0.9 = `edc2414`,
v0.1.0 = `7d2793f`, v0.2.8 = `5cf8785`, sources from `git archive`, run with `python -S` so the
editable install does not shadow them; output `<D2>\compat_exp.out.txt`):

| # | What D2 could put in the vault | v0.0.9 | v0.1.0 | v0.2.8 |
|---|---|---|---|---|
| E1 | single-slot entry with an **unknown kind** (`preserved`) -> Restore | full restore with mirror: **deletes live slots 1 and 3 and accountdata** | same (unknown kind is not an extract, `restore_entry` -> `restore_full`) | same |
| E2 | same slot as kind `extract`, id `keep-steam-...` -> Restore | full restore, same wipe (pre-existing for every extract before `1e086c5`) | only slot 2 written; slots 1, 3 and accountdata intact | same as v0.1.0 |
| E3 | `import_vault` (copy) of a vault with: unknown kind in `preserved/<id>`; `extract` in `extracts/keep-...`; root folders `organizer/`, `trash/` | unknown kind skipped ("data folder not found ... backups/<id>"); extract imported to `extracts/<id>`; root folders not carried | same | same |
| E4 | entry with a **relative path** (D11 in v1) | loads; scan shows 0 slots; restore "backup folder not found"; prune removes the record and **orphans the folder** | same | same |
| E5 | unknown top-level catalog key and unknown entry key, then any write | both dropped, `version: 1` written | same | same |

Consequences built into the design:

- Preserved and rescued copies use kind `extract` (E1 vs E2). A new kind would hand every old
  app a one-click wipe of the other 14 slots and the account data.
- No relative paths and no new keys in `catalog.json` (E4, E5); D2's data lives in
  `organizer/`.
- New root folders are safe (E3); they are simply not carried by an old `import_vault`. The
  importing new app then rebuilds first-seen manifests, and the `keep-` / `rescue-` id prefixes
  let it re-derive roles it lost.
- Old apps' pruning (keep 20 by age, managed snapshots only) never touches extracts and, once the
  new app has run its at-risk pass, cannot remove the last copy of a version. The only exposure is
  a snapshot taken by an old app after a downgrade and pruned by it before the new app runs again
  (the risk that exists today).
- Old apps' Clear-slot check by slot number (P5) and their undo are unchanged by D2; D5 keeps the
  newest oplog snapshot alive for them (2.9.4).

### 4.3 D11

Covered in 2.13: no catalog bump; managed entries resolve by convention; D2's records use ids
and relative paths; `repair-paths` updates the catalog for old apps after a move; in-place
entries stay absolute and show as missing on another machine. Vault on exFAT or a NAS: nothing in
option A needs hard links, sqlite or rename-over-directory semantics beyond a same-volume rename;
the D1 lock on a network share is D1's risk.

### 4.4 Moving to option B later

If the owner picks B later, D2's code stays useful: B indexes old folders in place (F3-B), and
`FolderLocator` is that legacy provider. The manifests in `storage.json` give D3's migration verified hashes
for every durable folder; roles tell it which entries were preserved or rescued; the retention
policy block keeps its meaning. Nothing in A has to be undone.

## 5. Test plan

Risk-based; all write tests run on `tmp_path` copies or synthetic saves (project rule).

**Fixtures** (new or promoted, in `tests/conftest.py`):

| Fixture | What |
|---|---|
| `steam_pair` | D1's shared factory `steam_pair(folder, slot, *, pid, play, ts, name, payload, member="both")`, promoted from `tests/test_playtime.py:43-63` (distinct payloads give distinct vids) |
| `wgs_account` | `make_wgs_account` fixed to real 296/360-B metas with id and time (needs P1) |
| `vault_builder` | builds a `Vault` in `tmp_path` with `.full()`, `.snapshot(source=, created=)`, `.extract()`, `.inplace()`, `.keep()` using the real ops and the slot writer |
| `live_sources` | `LiveSourceInfo` list for a sandbox Steam and Xbox folder (D7 injection) |
| `fake_store` | a D1 `OrganizerStore` in `tmp_path` (or D1's real one once it exists) |
| `fake_clock` | deterministic UTC seconds |
| `fs_fingerprint` | (files, bytes, newest mtime) of a tree, for read-only assertions |
| `real_vault_dir` | the owner's vault, **read-only**, skipped when absent |

**Tests** (most important first):

| # | Test | Asserts |
|---|---|---|
| T0 | D1's locator conformance suite (D1 test plan item 10) run against `FolderLocator` | `open` verifies the hash, `locate` order, `preserve` returns verified durable non-prunable Locations, `is_durable` |
| T1 | `test_prune_never_loses_a_version` (property-style over generated vaults) | the set of vids with a vault copy before == after, for keep-all, any N >= 1 |
| T2 | `test_prune_rescues_snapshot_only_slot` | the snapshot-only pair lands in `rescue-*` (kind extract), hashes equal, then the snapshot is deleted |
| T3 | `test_preserve_copies_pair_and_verifies` | new `keep-*` extract holds both members, manifest `creation`, role `preserved`, Locations durable; live folder fingerprint unchanged |
| T4 | `test_preserve_dedupes_exact_state` / `..._rehashes_existing` | no new entry when both exact states are durable; a byte-flipped durable copy is detected, marked damaged, and a new copy is written |
| T5 | `test_preserve_meta_only_difference_copies` | same vid, different meta plaintext -> new copy |
| T6 | `test_preserve_changed_since_plan_fails_closed` | `PreserveFailed("changed-since-plan")`, no entry, no staging left |
| T7 | `test_preserve_vault_full_fails_closed` | patched `shutil.disk_usage` -> `PreserveFailed("vault-full")` before anything is written |
| T8 | `test_old_app_contract_for_keep_entries` | the current classic `restore_entry` on a `keep-*` entry writes only that slot; `import_vault` copies it to `extracts/<id>`; `_prune` paths never touch it |
| T9 | `test_newest_oplog_snapshot_is_never_pruned` + `test_open_op_snapshot_is_pinned` | exemptions hold even beyond N |
| T10 | `test_snapshots_are_counted_per_source` | replaces `test_prune_snapshots_is_chronological_across_platforms` (spec change: per account) |
| T11 | `test_unmanaged_snapshots_untouched` | keeps today's `test_prune_leaves_unmanaged_snapshots_untouched` guarantee |
| T12 | `test_crash_points_create_entry` | exceptions injected after staging / after rename / after catalog -> housekeeping completes or discards per 2.11; never a half entry |
| T13 | `test_crash_points_delete_entry` | after uncatalogue / after trash rename -> swept; never a catalog record without a folder |
| T14 | `test_classification` | live, live alias (in-place path == live folder), snapshot, in-place external, other-vault reference, managed-outside-vault, damaged |
| T15 | `test_rehome_after_vault_move` + `test_repair_paths_backs_up_catalog` | moved vault resolves; repair rewrites managed paths only, writes `.bak-<stamp>` |
| T16 | `test_delete_entry_refuses_last_copy` / `..._rescue_then_delete` | DeleteRefused lists unique vids; rescue path leaves all vids durable |
| T17 | `test_delete_version_keeps_last_copy` | removes extra single-slot copies only |
| T18 | `test_full_backup_skips_cache_by_default` / `..._with_cache` | Steam full has no `cache/`, `*.hg` verified; flag restores old behaviour |
| T19 | `test_xbox_preserve_minimal_wgs` | rebuilt `containers.index` scans; blob hashes equal live; live untouched |
| T20 | `test_compaction_k3a_idempotent` | distinct files once in `thumbs/`, `cache/` gone, re-run is a no-op, a hash mismatch aborts that entry |
| T21 | `test_thin_mode_respects_protection` | protected vids never in `forgets`; preview lists the rest |
| T22 | `test_manifest_first_seen_for_old_app_entries` | an entry created by classic code without manifest gets one at housekeeping |
| T23 | `test_real_vault_plan_is_read_only` (real data, skip if absent) | plan reports 9 snapshot-only vids, 6 rescue pairs, 41 managed at conventional paths, 1 live alias; fingerprint before == after |

Not in CI: the old-app experiments E1-E5 need released sources. Keep them as a dev-only script
(`tools/compat_old_apps.py`, may use git) and run it before any release that changes the vault
layout.

## 6. Implementation plan

Sizes: S < 150 lines with tests, M 150-400, L > 400.

| Step | Phase | Size | Content | Tests |
|---|---|---|---|---|
| 0.1 | 0 | M | `core/slotentry.py`: `read_slot`, `write_slot_entry` (staging, verify, rename, catalog upsert); `extract_slot` uses it | T12 (writer part), extract round trip Steam + Xbox |
| 0.2 | 0 | M | `core/retention.py` pure planner (keep-all, per-source N, pinned newest-oplog snapshot) + Phase-0 adapter (summary prefilter + hash confirm); post-op call replaces `_prune_snapshots`; remove `SNAPSHOT_RETENTION` and rewrite the two tests that use it (`tests/test_operations.py:236-283`; per-account counting is a spec change) | T1, T2, T9 (oplog part), T10, T11 |
| 0.3 | 0 | S | two-phase delete via `trash/` + sweep of `.staging/` and `trash/` at vault open | T13 |
| 0.4 | 0 | S | full backup skips `cache/` by default; CLI `--with-cache`; README and DESIGN.md ("keep-last-N" text) updated | T18 |
| 1.1 | 1 | S | `storage.json` read/write through D1's store and lock (header, compat gates) | schema round trip, unknown keys kept, newer `write_compat` opens read-only |
| 1.2 | 1 | M | manifests at creation (full, extract, slot writer) + bootstrap (worker) + first-seen in housekeeping | T22, damaged detection |
| 1.3 | 1 | M | `FolderLocator.refresh/locate/is_durable/details/entry_role` with classification | T14 |
| 1.4 | 1 | S | `open(vid, prefer)` with verify-on-read | open from durable / live / fallback on damage |
| 1.5 | 1 | S | `Vault.entry_folder` (catalog API, also used by D1's scanner) + `repair_paths` + CLI `vault repair-paths` | T15 |
| 1.6 | 1 | M | `housekeep`: intents, sweep, manifests, at-risk rescue, orphans; GUI start hook in a worker; CLI `vault check` | T12, T23 |
| 1.7 | 1 (or later, owner's choice) | M | cache compaction K3 (a, b; c behind a flag) + `vault compact-cache --dry-run` | T20 |
| 2.1 | 2 | M | `preserve()` Steam + Xbox, dedupe, intents, error codes; D5 integration test | T3-T7, T19 |
| 2.2 | 2 | M | delete backup (plan + rescue-then-delete), remove from list, delete version, copy into vault | T16, T17 |
| 2.3 | 2 | S | pinned snapshots of open D5 ops fed into `Protection` | T9 (open-op part) |
| 5.1 | 5 | M-L | thinning modes R2 / R3 with "Thin now" preview | T21 |

Every step is one commit with its tests; Phase 0 steps need nothing from D1-D5. README's test
count and DESIGN.md's storage section move with the steps that change them.

## 7. Risks and open questions

**Risks**

| # | Risk | Mitigation |
|---|---|---|
| 1 | Growth of ~4.5 GB / year may outrun the owner's patience | K1 + K3 first; R2 "Thin now"; option B later reuses this work (4.4) |
| 2 | An old app running at the same time as the new one loses a catalog update (no lock in old apps) | documented; unchanged from today |
| 3 | Downgrade window: an old app's keep-20 prune before the new app's at-risk pass | only versions created after the last new-app run are exposed; same as today |
| 4 | A save editor writing into a vault file | detected through manifests (damaged), not prevented; the original version is still rescued from any other copy |
| 5 | Antivirus or indexer handles block the staging rename on Windows | retries, then fail closed and sweep |
| 6 | Vault on a removable or network drive is absent at "Move to live" | `preserve()` fails closed ("vault-unavailable"); D5 must say so before any write |
| 7 | Xbox app syncing while `preserve()` reads `wgs` (uploads 10-30 s after the game exits, R8 2e) | `expected_vid` check catches a changed blob; D5 may also wait for "Synced" |
| 8 | Bootstrap hashing on a slow disk (~870 MB) | worker with progress; the organizer works meanwhile with `unverified` entries treated as not durable |
| 9 | Preserving a whole pair duplicates a member that was already durable | bounded (<= 6.9 MB per Place); keeps old apps safe from mixed slots |

**Open questions** (owner's, designed as options; none blocks implementation)

1. F3: A now and B later, or B directly (D3)?
2. F9: R1 keep everything (recommended), R2 or R3.
3. `cache/`: K1 (recommended) and which compaction, K3a (recommended), K3b or none.
4. Snapshots per account: 10 (recommended), 5 or 20.
5. Should "Copy into vault" for the ~25 versions that exist only in PC-A/PC-B run once
   automatically (~73 MB) or stay a button? Recommended: a button, shown on those versions.

**Open for the parent / sibling designs**

- D1: `storage.json` is already reserved for D2 (D1 2.3); D2 keeps its manifests in it. Confirm
  that this in-vault integrity ledger is acceptable next to "hash caches never in the vault" (it
  is a record of what each backup held when made, not a cache). Adopt `Vault.entry_folder()`
  (2.13) as the catalog API the scanner resolves entries with. Protection inputs: D1's
  `protection(vid)` reasons (2.9.4).
- D5: confirm the `PreserveFailed.code` values, the post-op retention hook, and how open ops
  name their snapshot (for `pinned_entries`).
- P9 fix: whether to remove `inplace-20260624-182102` from the catalog; D2 treats it as a live
  alias either way.

## Deviations from 01-consolidation section 6

- **`preserve` signature.** Section 6 lists `preserve(member) -> Location`. D2 adopts D1's
  refinement `preserve(members: Sequence[LiveMember], *, op_id, reason) -> list[Location]` and
  adds one rule: when any requested member needs a copy, the slot's partner is stored with it
  (no one-member entries).
- **Location detail.** D1's final `Location` carries `durable`, `prunable` and `external`; the
  remaining storage facts (folder, file names, meta plaintext hash, status, role) are exposed by
  `FolderLocator.details(location)` instead of more fields on `Location`.
- **Organizer store and vault root.** D2 writes only `storage.json` in `<vault>/organizer/`
  (reserved for D2 by D1; it holds settings, roles, intents and the manifests) and adds the
  vault-root folders `thumbs/` (only with cache options K2/K3a), `.staging/` and `trash/`.
- **D11 seam.** D11 is met without a catalog bump: managed entries resolve by convention through
  a new catalog API `Vault.entry_folder()`, which D1's scanner and D2's locator both use.
- **"Preserved" is a role, not a catalog kind.** Preserved and rescued copies are catalogued as
  kind `extract`, because every old app version full-restores an unknown kind with mirror and
  wipes the other slots (VERIFIED E1).
