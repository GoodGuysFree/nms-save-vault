# R1 - Core model and operations audit

Research wave 1, topic R1 (feeds S1 identity, S5 live-slot operations, S6 vault storage).
Branch `design/save-organizer`; no production code, tests or docs were changed.

Evidence tags: **VERIFIED** = run or measured by R1 (script/command named); **SOURCED** = read
in code or an external source at the cited `file:line` / URL; **INFERRED** = reasoning from
the above. Redaction: `st_<steamid64>`, `<xbox-account>`, `%APPDATA%`, `%LOCALAPPDATA%`.
R1 scripts and raw outputs live in the session scratchpad,
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\be828316-f5a0-4419-bd50-12964a376ba0\scratchpad\R1\`
(written `<R1>` below). All real-data work was read-only; the vault's `catalog.json` and
`oplog.jsonl` hashes, its file count, `playthrough-ids.json`, `state.json` and every live save
file were checked unchanged at the end (VERIFIED, Appendix A).

## 1. TL;DR

- The vault is folder-per-backup plus one flat `catalog.json` (v1, 49 entries, 771 KB, every
  path absolute, so D11 is not done). Its per-member cache holds only meta fields: no
  playthrough id, no content hash. Neither existing `note` field is a user note.
- Identity is the game's u64 playthrough id. It survives renames, slot moves and cross-save,
  but it identifies a playthrough, not a branch. Twins made by copying a slot fold into one
  (VERIFIED). Pre-Waypoint saves fall back to `platform:slot:name`.
- **Defect:** `msstore.parse_ms_meta` checks Xbox meta lengths against the Steam constants.
  Real Xbox metas (296/360 B) carry the id at 0x118 and a timestamp at 0x120, and neither is
  read. The 296-B metas also lose their names. A fix makes Xbox identity free; today it costs
  a 9.7 s pure-Python decode plus a cache file written into the vault.
- Speed does not block a meta-level organizer. Scanning all 52 folders (780 slots, 1237 saves)
  takes ~1.0 s, loading the catalog takes 3.7 ms, and hashing every save's data takes 2.65 s.
  Hashing folds 1177 copies onto 195 distinct versions (6.0x).
- Ops are slot-granular and single-shot. Each has a game guard, a full-folder auto-snapshot,
  atomic writes and post-validation. sha256 verification exists only for full backups.
- "Move to live with eviction" can be done today as `extract_slot` + `repopulate_slot`, but
  the two steps are not linked, not atomic and not identity-checked. A one-member source
  leaves the evicted save's other member in the slot (VERIFIED). On Xbox the game would then
  load the evicted save.
- Undo is single-level, vault-global and ignores which folder it writes to (all VERIFIED).
  A second undo is a no-op. Undo restores into whatever folder it is handed. A write that
  fails post-validation logs nothing, so undo then restores an *older* snapshot and loses
  progress.
- The oplog (42 lines) cannot rebuild lineage. It records no target folder, names the source
  only by folder name, and has no ids, hashes or member detail. `import_vault` drops the source
  vault's oplog. New JSONL keys are safe to add.
- `plan_clear_slot` decides "already backed up" by slot *number*, not identity, so it can
  report a false "safe" (VERIFIED). Organizer eviction must not reuse it as-is.
- Schema-evolution traps (VERIFIED): an unknown member key crashes older app versions
  (`TypeError`). Older versions also drop unknown entry keys and write `version: 1` back.
  There is no lock, so the GUI and CLI running together lose updates.
- The GUI picks write targets itself. Promote on a non-active source's save writes to the
  *active* source (VERIFIED bug, `gui.py:1539`). CLI `discover --add` catalogues live Xbox
  folders as "backups", and the owner's live wgs folder is in the catalog as an in-place
  backup.
- Tests: 412, of which 411 pass. The 1 failure is pre-existing: a Linux-symlink test fails
  because this machine can create symlinks. Synthetic Steam and Xbox builders exist. The
  Xbox one builds metas at the wrong length, which is why the parser defect went unnoticed.

## 2. Findings

### 2a. Vault storage

**A1. On-disk layout per entry kind** - VERIFIED (listing) + SOURCED (`operations.py`)

| Kind | Folder under the vault root | What is in it | Managed | Copy hash-verified | Owner's vault |
|---|---|---|---|---|---|
| `full` | `backups/full-<plat>-<YYYYMMDD-HHMMSS>` | Steam: the whole `st_` folder via `copytree`, including `cache/` (default `include_cache=True`, ops:165-179). Xbox: the whole wgs account folder. | yes | **yes**, sha256 per file (`_verify_copy`, ops:182-188) | 17: 14 Steam at 151-180 MiB, 3 Xbox at 62-63 MiB |
| `snapshot` | `snapshots/snapshot-<plat>-<stamp>` | Steam: `*.hg` + `steam_autocloud.vdf`, no cache. Xbox: the whole wgs account folder (`_copy_save_state`, ops:115-131). | yes | no | 16 (14 Steam at 4-50 MiB, 2 Xbox at 63 MiB); 724 MiB |
| `extract` | `extracts/extract-<plat>-<stamp>` | Steam: the slot's `save*/mf_save*` files at their **original file numbers** (ops:286-295). Xbox: a minimal wgs folder, i.e. a rebuilt `containers.index` + the slot's GUID dirs (ops:466-489). | yes | no | 8 (all Steam), 19 MiB |
| `imported` | `backups/imported-<plat>-<stamp>` | `copytree` of a user folder (ops:771-775) | yes | no | 0 |
| `inplace` | none; `path` points at an external folder | nothing copied (ops:777-778) | no | n/a | 8: 7 Steam folders inside two hand-made backup folders (manual backup A and B) under `%APPDATA%\HelloGames\NMS\`, and **1 that is the live Xbox wgs account folder** |

Totals: 3,404,610,911 B in 4517 files. Of `backups/` (2511 MiB), **1700 MiB is copies of the
Steam `cache/` DDS thumbnails**: 131 of ~180 MiB per Steam full backup (73%), 180 files named
`<hex>.<difficulty>.<place>.<steamid64>.DDS` (VERIFIED, `du`).

**A2. `catalog.json` schema, v1** - VERIFIED on a copy (`<R1>\analyze_catalog.py` -> `catalog_analysis.txt`) + SOURCED `catalog.py:18-147`

- Top level is `{"version": 1, "entries": [...]}`. The whole file is rewritten on every change
  (indent 2, temp file + `os.replace`, catalog.py:176-181): 771,052 B for 49 entries.
- Entry: `id, kind, label, path, created, source, managed, slots, note`.
  - `path` is absolute in all 49 entries.
  - `created` is local ISO time with no time zone.
  - `source` is the origin's folder **name** (`st_<steamid64>`, `<xbox-account>`, or e.g.
    `st_<steamid64> - Copy before graves`).
- Slot: `slot, occupied, name` (the newest member's name), `newest_label` (`"A"`/`"B"`), and
  `members` [A, B]. A full or snapshot entry caches all 15 slots; an extract caches only its
  own slot.
- Member: `label, present, name, summary, game_mode, season, play_time, timestamp,
  base_version, data_size, valid, moved, note, difficulty`. `timestamp` is
  `effective_timestamp`: the Steam meta value at 0x164, or the containers.index FILETIME for
  Xbox (catalog.py:101).
- Real record, redacted and trimmed to one member:

```json
{"id": "full-steam-20260630-092328", "kind": "full", "label": "Steam20260630",
 "path": "%APPDATA%\\HelloGames\\NMS\\_SaveVault\\backups\\full-steam-20260630-092328",
 "created": "2026-06-30T09:23:30", "source": "st_<steamid64>", "managed": true, "note": "",
 "slots": [{"slot": 1, "occupied": true, "name": "Swarm", "newest_label": "B",
   "members": [{"label": "A", "present": true, "name": "Swarm", "summary": "In the Yunakoi system",
     "game_mode": 6, "season": 46, "play_time": 12496, "timestamp": 1781440106, "base_version": 4220,
     "data_size": 518796, "valid": true, "moved": false, "note": "", "difficulty": 0}, "...B..."]},
   "... 14 more slot records"]}
```

- Owner's catalog: 623 slot records, 575 occupied, 1147 present members, 26 distinct names;
  0 `moved`, 0 invalid. Three slots have only one member (`incomplete: missing data file`).

**A3. What the cache does not hold** - SOURCED `catalog.py:28-80`

The fields an organizer needs and the cache lacks:
- the playthrough id (Steam meta 0x15C, Xbox 0x118)
- `size_decompressed`
- the meta format
- any content hash
- the Xbox cloud state
- the member's file name or identifier
- the entry's platform or account

Platform appears only as the tag in the id (`full-steam-...`); the account appears only
inside the `source` folder name.

**A4. Neither `note` field is a user note** - VERIFIED + SOURCED

- `CatalogEntry.note` is never set. `make_entry_from_dir(note="")` (catalog.py:232-245) has
  no caller that passes a note (grep), and no CLI or GUI path edits it. It is `""` in all 49
  entries.
- `MemberSummary.note` copies `MemberView.note`, which holds **scan diagnostics**: in the
  owner's catalog, `"Xbox/Microsoft save"` x178, `"incomplete: missing data file"` x3 and
  `""` x1065. It is regenerated by any re-summary, and the GUI tooltips print it as
  "Note:" (gui.py:1112, 1151).

**A5. Entry lifecycle** - SOURCED `catalog.py:150-245`, `operations.py:148-159`

- `Vault` offers only `load/save/upsert/remove/new_id/append_oplog/read_oplog/make_entry_from_dir`.
- No operation deletes, renames, relabels, annotates or re-summarises an entry. The only
  removal is snapshot pruning.
- Cached summaries are frozen at creation. That is right for immutable managed entries but
  wrong for in-place ones: VERIFIED, the in-place live Xbox entry differs from disk in 14 of
  30 members today (`<R1>\measure_scan.txt`).

**A6. Snapshot retention** - SOURCED + VERIFIED

- `SNAPSHOT_RETENTION = 20` (ops:31). After each snapshot, the oldest *managed* snapshots
  beyond 20 (by `created`, across platforms) are `rmtree`'d and removed from the catalog
  (ops:148-159). Unmanaged snapshots (imported in place from another vault) are never pruned.
- Oplog lines keep pointing at pruned ids, and undo then reports "can no longer be found"
  (ops:897-913).
- The owner has 16 snapshots, so pruning has not run yet.
- Every live write costs one full-folder snapshot: ~41-50 MiB on Steam, 63 MiB on Xbox.

**A7. Import and vault-merge flows** - SOURCED (+ INFERRED where marked)

- `import_backup` (ops:766-781):
  - With copy: creates an `imported` entry, `copytree` into `backups/`, no hash check.
  - Without copy: creates an `inplace` entry. Logs `import` with the full source path in
    `detail`.
- Discover:
  - The GUI walks the NMS roots, excluding live sources and the vault (gui.py:1617-1629 via
    `discover.discover_inplace_backups`).
  - **CLI `discover` also appends `locations.find_microsoft_save_dirs()`** (cli.py:337).
    That list is the *live* Xbox account folders, so `discover --add` catalogues a live
    folder as an in-place "backup". INFERRED: this, or the GUI's old read-only-era
    "Import this Xbox folder as a backup" (gui.py:1227-1231), created
    `inplace-20260624-182102`.
- `import_vault` (ops:811-842):
  - Keyed by entry id, so it is idempotent.
  - Managed entries are either copied to `<kind subdir>/<id>` (`_entry_folder`, ops:845-850)
    or referenced in place with `managed=False`, so this vault never prunes them.
  - External in-place entries are re-catalogued unchanged.
  - **The source vault's `oplog.jsonl` and `playthrough-ids.json` are not merged.**
    VERIFIED: the 10 entries imported on 2026-07-01 from `F:\_SaveVault-old` have no oplog
    history (the log starts 2026-06-30). One of them, `snapshot-steam-20260625-154113`
    ("auto: pre-repopulate slot 13"), proves that a 2026-06-25 repopulate happened which this
    log never saw.

**A8. D11 (relative paths) is not implemented** - VERIFIED

- `CATALOG_VERSION = 1` (catalog.py:18), and all 49 paths are absolute.
- `Vault.load` never reads `version` (catalog.py:167-174), and there is no migration code.
- Only `import_vault` knows the relative layout (kind subdir + id).

**A9. Location and concurrency** - VERIFIED

- The default vault is `%APPDATA%\HelloGames\NMS\_SaveVault` (locations.py:54-66). That is
  inside the NMS root, so every discover must exclude it; both front-ends do.
- There is **no lock and no reload-before-write**. Two `Vault` instances (a GUI held open plus
  a CLI run) lose updates: in a temp test the second `upsert` overwrote the first entry
  (catalog.py:192-197).

### 2b. The savedir model

**B1. Model** - SOURCED `savedir.py`, `slotmap.py`

- `SaveDirView` (savedir.py:126-141): `path`, `slots` (1..15 -> `SlotView`), `account_present`,
  `steam_cloud` (the folder has `steam_autocloud.vdf`), `stray_files` (never filled), and
  `xbox_index` (`XboxIndexInfo` or None).
- `SlotView` (savedir.py:92-123): `a`, `b`, `members`, `present_members`, `occupied`,
  `display_name` (newest's name, else `<unnamed>`/`<empty>`), and `newest` (max
  `effective_timestamp` over valid present members, else over all present members).
- `MemberView` (savedir.py:23-89):
  - Fields: `ref: SaveFileRef(file_no)`, `data_path`, `meta_path`, `exists` (data **and**
    meta present), `data_size`, `data_mtime`, `ordinal_used`, `info: MetaInfo|None`, `note`,
    `xbox: XboxBlobInfo|None`.
  - Properties: `slot`, `label` (A/B), `valid`, `moved`, `save_type_label`
    (Auto-Save/Restore-Point), `cloud_status` (Xbox only), `save_name`, `timestamp`,
    `effective_timestamp` (meta ts, else `int(data_mtime)`).
- Slot mapping: slot k = files 2k-1 (A, auto-save) and 2k (B, restore point); ordinal = file
  + 1. The meta is XXTEA-keyed by that ordinal.

**B2. What `meta.py` decodes (Steam, decrypted)** - SOURCED `meta.py:196-226`, `formats.py:55-68`; VERIFIED (`<R1>\meta_regions.txt`)

- Parsed fields:
  - header 0x00
  - format 0x04
  - SizeDecompressed 0x38
  - SizeDisk 0x3C
  - BaseVersion 0x44
  - GameMode 0x48 / Season 0x4A
  - TotalPlayTime 0x4C
  - SaveName 0x58 (128)
  - SaveSummary 0xD8 (128)
  - Difficulty 0x158
  - **playthrough id 0x15C (u64)**
  - Timestamp 0x164
  - format repeat 0x168
- Not parsed but non-zero on real Worlds II metas:
  - a u32 at **0x54** (meaning unknown, e.g. `0x0055539F`)
  - an ASCII **difficulty tag** at **0x16C..0x1AF** (`"Creative"`, `"Normal"`)
- 0x08-0x37 and 0x40 are zero.

**B3. Steam scan** - SOURCED `savedir.py:144-188`

- Reads every meta and stats the data file; it never reads the data itself.
- Decrypts with the mapped ordinal, otherwise tries all 31 ordinals and sets `moved`.
- A bad member never raises; it gets a `note`.
- `valid` means only that the header check passed. The SizeDecompressed-vs-chunks tie is
  checked only in repopulate's preflight (ops:925-940), not in scans or `nmsvault-cli verify`.
- `DESIGN.md:91-92` lists "SizeDisk == file size" as a validation step, but the code
  deliberately skips it (ops:928-929); the doc is stale.

**B4. Xbox wgs scan** - SOURCED `msstore.py:209-292, 386-442`

- Parses `containers.index`, resolves `container.<n>`, and **reads every data blob in full
  during a scan** (msstore.py:401) to get the decompressed size.
- Synthesises `header = META_HEADER` (msstore.py:408), so `valid` is always True for a present
  Xbox save, and `verify` cannot flag a broken one.
- The effective time is the index FILETIME (msstore.py:404).
- `cloud_status` comes from the per-record sync state; all 30 live saves are "Synced".

**B5. xgs** - SOURCED `msstore.py:458-475, 676-785`; DESIGN.md "The xgs mirror"

- `scan_xgs` gives the same model, read-only: members carry no `xbox`, so the writers refuse
  them via `_require_wgs`.
- Every wgs write and delete is mirrored to xgs, and a full restore re-syncs it
  (`mirror_xgs`).
- Snapshots and backups do not include xgs.

**B6. DEFECT: the Xbox meta parser uses Steam lengths** - VERIFIED (live data, `<R1>\xbox_meta_raw.py/.txt`) + SOURCED (libNOM)

- **The bug.** `parse_ms_meta` (msstore.py:311-339) gates names on
  `L >= formats.META_LEN_WAYPOINT` (360, line 329), and the difficulty/id/timestamp/format
  block on `L in (META_LEN_WORLDS_I, META_LEN_WORLDS_II)` (384/432, line 332). Those are the
  **Steam** lengths, although its own comments say "280+" and "296 / 360".
- **What libNOM says.** Microsoft meta lengths are 0x18/0x118/0x128/0x168 = 24/280/296/360
  ([PlatformMicrosoft.cs:22-25](https://github.com/zencq/libNOM.io/blob/master/libNOM.io/PlatformMicrosoft/PlatformMicrosoft.cs)).
  The layout is name 0x14, summary 0x94, difficulty 0x114, **slot identifier 0x118**,
  **timestamp 0x120** and meta format 0x124 from 296 B up, plus a 64-B difficulty tag at 360 B
  ([PlatformMicrosoft_Initialize.cs:278-310](https://github.com/zencq/libNOM.io/blob/master/libNOM.io/PlatformMicrosoft/PlatformMicrosoft_Initialize.cs)).
- **Real data.** The owner's live Xbox account has 30 saves with metas of 296 and 360 bytes:
  - **Every** meta has a non-zero id at 0x118. For 28 it equals the id decoded from the data
    blob. The 2 "Voyagers" metas have an id that their data lacks.
  - **Every** meta has a non-zero timestamp at 0x120.
  - The parser reads neither field.
  - For the 296-B metas it also skips name and summary: live slots 4, 9 and 14 show
    `<unnamed>`, while their metas say "CrossSaveTest", "Exobiology1" and
    "The Cartographers".
- **Knock-on effects.**
  - `playtime.py`'s premise that "Xbox metas leave that field 0" (playtime.py:13-16) is an
    artefact of this bug.
  - Xbox identity therefore costs a pure-Python LZ4 decode (9.7 s for the vault uncached)
    and a cache file in the vault.
  - Cached Xbox names are blank for Worlds-I-era metas.
  - Xbox "Saved" times come from the index, not the meta.
- **Why tests miss it.** Synthetic Microsoft metas are built with
  `formats.META_LEN_WORLDS_II` (= 432) under a comment saying `# 360` (tests/conftest.py:57,
  tests/test_msstore.py:40). The bug has been present since `fef420e` (2026-06-24).

**B7. Small pitfalls** - SOURCED / INFERRED

- `platform_of` (savedir.py:207-212) returns `"steam"` for anything that is neither wgs nor
  xgs, including a missing folder. `repopulate_slot` checks platforms (ops:321) before it
  checks that the source exists, so a missing Xbox source reports "coming soon" instead of
  "not found" (INFERRED).
- `effective_timestamp` mixes the Steam meta time with the Xbox index FILETIME.

### 2c. Identity (`core/playtime.py`)

**C1. Pipeline** - SOURCED `playtime.py:166-238`

1. For each folder (every live dir + every catalog entry path), call `scan_any`.
2. For each present member that has `info`, choose its key:
   - `f"{info.slot_identifier:016X}"` (Steam meta 0x15C);
   - else decode the first data chunk and regex `"(?:WmU|ActiveSaveSlotId)":"0x..."`
     (playtime.py:32, 95-112). The result goes through `_IdCache`, keyed `path|size|int(mtime)`
     and persisted to `<vault>/playthrough-ids.json` (`use_cache=True` is the default);
   - else `platform:slot:name`, with `identified=False`.
3. Fold all members with the same key into a `Playthrough`:
   - `copies`, `platforms`
   - `sources` (`"live"` or an entry id)
   - `locations` {(folder, slot)}
   - `name_seen`, `newest`
   - `play_time = max`
   - display name = the newest copy's name

**C2. The GUI's "filter backups by save file" (v0.2.7)** - SOURCED `gui.py:843-871, 999-1055`

- The report is computed lazily once per refresh.
- The combo lists playthroughs with `backups > 0`, labelled
  `name - was: old names (platforms, play time, N backups)`.
- Choosing one keeps only the entries whose path is in `p.locations`, and only those slots.
- The right-click "Show this save's history" item uses `report.find(folder, slot)`.

**C3. On the owner's data** - VERIFIED (`<R1>\measure_scan.txt`, `measure_collect_cached.txt`)

- 51 folders, 1207 saves -> 30 playthroughs.
- 1 is unidentified ("Voyagers", Xbox).
- 4 span both platforms: Creative Primary, CrossSaveTest-300qs, Exobiology1-600qs and
  The Cartographers.
- Renames fold correctly: SHIPSTOGIVE-1400QS was ShipsToGive-2400qs, and Exp23 Our Journey
  Continues was Our Journey Continues.
- Cost:
  - 9.70 s with `use_cache=False`.
  - 1.01-1.14 s with the warm cache. That run added 4 new cache keys, so the real GUI would
    have written to the vault file.

**C4. Limits**

- **Twins share one id.** VERIFIED (E8): after copying slot 5 into slot 3, both live slots
  fold into one playthrough. Their later divergence is invisible: max play time wins and the
  newest copy's name wins. Today 0 twins exist in the owner's folders (VERIFIED), but any
  copy creates them.
- **Cross-save is one playthrough by design** (4 cases). Whether that is one line or two is
  a design choice.
- **Pre-Waypoint fallback.** The fallback key contains the slot number, so moving such a save
  splits it. Two different runs with the same name in the same slot number merge.
- **In-place live entries count as backups.** VERIFIED: every Xbox run shows `copies=14`,
  `backups=6`, and one of the six "backups" is the live folder itself.
- **Granularity is playthrough -> (folder, slot).** There is no version (member or content)
  identity, no order between versions, and no notion of a branch.
- **The cache** uses absolute-path keys, so it breaks with D11 or a moved vault. It is never
  pruned and grows on every Xbox GUID rotation: 187 keys for 15 distinct ids.
- **Every report rescans every folder** (~1 s for meta-level data).

### 2d. Operations (`core/operations.py`, `core/msstore.py` writers)

**D1. Inventory** - SOURCED (line numbers in `operations.py` unless noted)

| Function | Writes | Game guard | Snapshot first | Validation | sha256 | Oplog record |
|---|---|---|---|---|---|---|
| `snapshot_live` (134) | `snapshots/<id>` + catalog; prunes | no | - | none | no | none (callers log) |
| `create_full_backup` (165) | `backups/<id>` + catalog | no | no | `_verify_copy` | **yes** | `full_backup`(entry_id) |
| `restore_entry` (191) | dispatch: extract -> `repopulate_slot` same slot; else `restore_full` | callee | callee | callee | no | as callee (an extract restore is logged as `repopulate_slot`) |
| `restore_full` (224) | live, Steam: overwrite `*.hg` (+vdf); `mirror=True` deletes live `*.hg` absent from the backup. Live, Xbox: reconcile the whole tree + `mark_modified` + `mirror_xgs` (525-560). | yes | yes | warnings only (`_validate_dir_metas` 950 / `_validate_xbox_dir` 432) | no | `restore_full`(entry_id, snapshot_id, changed) |
| `extract_slot` (280) | `extracts/<id>` + catalog | no | no | none | no | `extract_slot`(entry_id, "slot N") |
| `repopulate_slot` (306) | live slot: the source's **present** members only | yes | yes | Steam preflight `_validate_pair` (dest-ordinal header, chunks > 0, SizeDecompressed == sum of chunks) + post `_validate_slot` (raises). Xbox post `_validate_xbox_slot` (readable). | no | `repopulate_slot`(snapshot_id, "<src folder name> slot X -> slot Y", changed) |
| `promote_member` (367) | Steam: meta ts = max(A, B) + 1, mtime of meta and data = that ts. Xbox: `write_save` with meta ts and FILETIME bumped. | yes | yes | `is_valid_for` / slot readable | no | `promote_member`(snapshot_id, "slot NM ts->T") |
| `plan_clear_slot` (671) | - (read-only) | - | - | - | - | - |
| `clear_slot` (701) | Steam: unlink whatever slot files exist. Xbox: `delete_save` x2, keeping the records as Deleted. | yes | yes | slot empty afterwards | no | `clear_slot`(snapshot_id) |
| `import_backup` (766) | copy into `backups/`, or catalog only | no | no | none | no | `import`(entry_id, full path) |
| `preview_vault_import` (792) | - (read-only) | - | - | - | - | - |
| `import_vault` (811) | copies or references entries | no | no | none | no | `import_vault`(detail) |
| `undo_last` (881) | `restore_full`(last snapshot) into the *given* live dir | yes | yes (`restore_full`'s) | as `restore_full` | no | `restore_full` + `undo`(snapshot_id) |

The Xbox primitives in `msstore.py` work as follows:
- `write_save` (630-678): writes fresh GUID blobs, then `container.<n+1>`, rewrites
  `containers.index` **last** with Modified/Created states, deletes the superseded blobs, and
  mirrors to xgs.
- `delete_save` (681-714): rewrites the index first with the record flagged Deleted, then
  removes the blob dir and the xgs copy.
- `mark_modified` (717-726) and `mirror_xgs` (757-785).

**D2. Safety mechanics actually in place** - SOURCED / INFERRED

- **Game guard.** `safety.is_game_running()` calls `tasklist` (safety.py:23-38). True raises
  `GameRunningError` unless `allow_game_running`; None adds a warning (ops:80-85). Snapshot,
  backup, extract and import are not guarded. They only read the live folder, but they can
  copy a save mid-write (INFERRED).
- **Atomic writes.** Temp file in the same directory + fsync + `os.replace`
  (safety.py:53-66). `atomic_copy` reads the whole file into memory.
- **Hashes.** sha256 is used only by `create_full_backup` (ops:182-188); `sha256_bytes` is
  never called. DESIGN.md's "sha256 on copies" therefore holds for full backups only.
- **Post-validation.** It runs after the live folder has already changed, and it raises
  without rolling back (ops:360, 947). Recovery is "undo", which has the gap shown in D5.
- **Steam Cloud.**
  - Restore and repopulate stamp the write time when Cloud is on (ops:257, 350).
  - Promote always stamps the older meta time (ops:394-397). INFERRED risk: Steam may judge
    the cloud copy newer and revert a promote.
- **Cross-account copies are unguarded on the same platform** (INFERRED; the owner has one
  account per platform):
  - Steam to Steam copies work between any two `st_` folders.
  - An Xbox full restore writes the *backup's* `containers.index` (its process and account
    ids) into whatever account folder it targets (ops:525-560).

**D3. The cross-platform guard** - SOURCED

- `_guard_same_platform` (ops:97-100) runs only in `restore_full` (241) and
  `repopulate_slot` (321).
- On a mismatch it raises `FeatureNotYetAvailableError`, shown as CLI exit 3 or GUI
  "Coming soon".
- Extract and import across platforms are allowed, so the vault can hold both.

**D4. Clear plan and cloud warnings** - SOURCED + VERIFIED (E10)

- `plan_clear_slot` (671-698) compares the live slot's play time with `_latest_vault_copy`
  (647-668). That is the most recently created entry holding the **same slot number**,
  skipping the live folder itself and missing folders.
- **VERIFIED false "safe":** live slot 3 held "Save X", which is in no backup. The plan
  reported it backed up because the newest entry's slot 3 holds "Save Y" (more play time),
  and the warning was `""`.
- `cloud_warning` gives the Steam Cloud manual procedure (618-634). Xbox deletions propagate
  through the Deleted record.

**D5. Undo** - VERIFIED (E2, E3, E11; `<R1>\experiments.txt`)

- **Single level.** The `undo` line carries the snapshot it restored, so the next undo restores
  that same snapshot again (it reports "undid undo"). Earlier operations can never be undone.
- **Ignores the target folder.** `undo_last(vault, other_folder)` mirrored the last snapshot
  into a *different* same-platform folder. The oplog never records a target (ops:958-966).
  The GUI passes the active source (gui.py:1637), so after an Xbox operation with Steam
  active, undo says "coming soon" (SOURCED).
- **Failure gap.** The snapshot is catalogued before the write (ops:347), but the oplog line
  is appended only after validation passes (ops:361). After a write that failed validation,
  undo restored the **previous** operation's snapshot and discarded the progress made since;
  the failed write's own snapshot was ignored.

**D6. A one-member source leaves a mixed slot** - VERIFIED (E1)

- `repopulate_slot` writes only the members present in the source and leaves the
  destination's other member untouched (Steam ops:327-345, Xbox 499-509).
- Steam: destination B kept the evicted save byte-for-byte.
- Xbox: the evicted "Old Manual" stayed in the slot and, being newer, **is what the game
  would load**.
- The owner's catalog has 3 single-member slots.

**D7. Composing the organizer's flows from today's ops** - VERIFIED (E6) + INFERRED

| Flow | Today | What works | What is missing |
|---|---|---|---|
| F1 Move vault version V (entry E, slot s) into live slot S, evicting S to the vault | `extract_slot(vault, L, S)` then `repopulate_slot(vault, E.path, s, L, S)` | Re-key handles s != S. The extract is a durable, unpruned copy. Undo reverts the placement (the extract stays). | One compound op with one id and one oplog record linking evicted extract, source version and target. A check that eviction is needed (an identical version may already be in the vault) and that it succeeded (no hash). Handling of one-member sources (D6). An explicit target source id. A game guard before the eviction copy. A rollback path. |
| F2 Use an older version of a live save | Find the playthrough's live slot by id, then `repopulate_slot` from the older entry | Works. | The current live version is kept only in a prunable snapshot unless extracted. **Nothing records that the slot now descends from V**, so the history tree cannot be drawn later. |
| F3 Use a specific member (A or B) of a version | `repopulate_slot` + `promote_member` | Works in 2 ops with 2 snapshots. | A member-level operation. |
| F4 Swap two live slots / move across accounts | extract + `repopulate_slot` x2 | Same platform only. | Cross-platform is gated (R8). |
| F5 Free a slot | `extract_slot` + `clear_slot` | Works. | The clear plan's safety check is by slot number (D4). |

### 2e. `oplog.jsonl`

**E1. Format** - VERIFIED (copy) + SOURCED (ops:958-966)

- JSONL, one line per operation:
  `{"ts": local ISO seconds, "op", "entry_id", "snapshot_id", "detail": free text, "changed": [...]}`.
  `changed` holds file names, Xbox identifiers or GUID dirs; a leading `-` means deleted.
- The owner's log has 42 lines (2026-06-30 to 2026-10-05):
  - `full_backup` 17
  - `extract_slot` 7
  - `repopulate_slot` 7
  - `restore_full` 4
  - `clear_slot` 3
  - `undo` 2
  - `import_vault` 1
  - `promote_member` 1
- Every id the log references is still in the catalog. Example:

```json
{"ts": "2026-09-20T21:13:59", "op": "repopulate_slot", "entry_id": null,
 "snapshot_id": "snapshot-steam-20260920-211359",
 "detail": "snapshot-steam-20260917-171206 slot 15 -> slot 1",
 "changed": ["save.hg", "mf_save.hg", "save2.hg", "mf_save2.hg"]}
```

**E2. What can be rebuilt today** - VERIFIED / INFERRED

- From each repopulate line:
  - the time;
  - the source folder **name**: an entry id for managed sources, but for in-place sources
    only a folder name (`st_<steamid64> - b4 v6.4`). That is ambiguous in general, because an
    in-place folder can itself be named exactly `st_<steamid64>` (`inplace-20260624-172901-5`
    is);
  - the slot numbers (inside the text);
  - the pre-op snapshot.
- The target folder or account is not recorded.
- Versions can be recovered only by re-reading the source entry (immutable if managed) and
  matching ids or hashes.
- An extract restore is logged as `repopulate_slot`.
- The 2026-08-22 accident is visible:
  - `restore_full` of an *extract* deleted 14 slots plus the account data (58 files).
  - `snapshot-steam-20260822-152010` holds 1 slot.
  - The owner recovered 18 s later.
  - This is the case `restore_entry` now prevents (ops:191-221).

**E3. What lineage needs that is missing**

- an op id, and a group id for compound ops
- the target: source id, platform, folder and slot
- the source: entry id or live source id, plus slot and member
- per member moved and per member evicted:
  - file or identifier
  - data sha256
  - meta hash (plaintext; Steam meta bytes change with the slot key)
  - playthrough id, play time, meta timestamp, name
- where the evicted save went (an entry id)
- `undo_of`
- a failure record
- the app version
- optionally a user note

**E4. Compatibility and privacy**

- `read_oplog` returns dicts and `undo_last` uses `.get` (catalog.py:222-230,
  ops:893-905), so **additive keys are safe for old and new readers**. VERIFIED by reading;
  no migration is needed.
- `detail` embeds folder names that contain account ids, and for `import` the full path.
  Redact both on any export.

### 2f. Front-end/core boundary

**F1. CLI** - SOURCED `cli.py:473-556`

- Commands: `status, list, playtime, sources, show, backup, restore, extract, clear, repopulate,
  promote, import, discover, undo, accounts, verify`.
- The target is `--live <dir>`, otherwise the first writable source in `state.json`
  (cli.py:110-122). There is no way to select a source by id, and no history/organizer
  listing or notes.
- Read-like commands with write side effects:
  - `playtime` writes `playthrough-ids.json` (cli.py:229).
  - `sources` bootstraps and writes `state.json` when it is absent (cli.py:401-405).
  - `discover --add` catalogues live Xbox folders (cli.py:337).

**F2. GUI logic that belongs in core** - SOURCED; item 1 VERIFIED (E9, `<R1>\gui_promote_target.py/.txt`)

1. **Write target choice.** `on_promote` and `on_undo` act on `self.live_dir` (the active
   source), not the folder of the row clicked (gui.py:1539, 1637). **VERIFIED bug:**
   right-click Promote on a save in the *non-active* writable source called `promote_member`
   on the *active* source's same slot and member. With Steam active and an Xbox row clicked,
   that is a wrong-target live write (snapshot-backed).
2. The discover-and-add loop exists twice with different semantics (gui.py:1617-1629 vs
   cli.py:324-348).
3. The "save file history" query is assembled in the GUI on top of `playtime.collect`
   (gui.py:843-871, 999-1055).
4. Copy-source and destination enumeration (gui.py:1860-1885, 1999-2015).
5. The extract-ness test (`_extract_slot_number`, gui.py:211) and the restore labels.
6. Writes on startup: `vault.ensure()` (gui.py:301) and first-run `state.json`
   (gui.py:328-336). `_save_state_quietly` and theme changes write the real `state.json`
   (gui.py:509-539).

**F3. On the core side**

- `msstore.microsoft_root()` reads `os.environ` at the point of use (msstore.py:80), which
  goes against D7.
- `aliases` is a process-global redaction filter, which is by design.

### 2g. Tests

**G1. Result** - VERIFIED (`<R1>\pytest2.txt`)

- `uv run pytest` gives **1 failed, 411 passed, 0 skipped, 19.8 s**.
- The failure is `tests/test_platform.py::test_linux_symlinked_steam_roots_are_not_double_counted`.
  This machine can create symlinks, so the test runs instead of skipping. `_existing_dirs`
  keeps the first spelling (`.steam/steam`) while the test expects `.local/share/Steam`
  (platform.py:248-269). It is pre-existing and unrelated; the README says 412 tests.
- Plain `uv run pytest -q` stacks with `addopts = "-q"` and hides the count line.

**G2. Coverage map** (319 test functions -> 412 collected)

| Area | Tests | Covered | Gaps that matter for the organizer |
|---|---|---|---|
| formats/slotmap/meta | test_slotmap 7, test_meta 9 (real saves, read-only), test_savetype 12 | mapping, XXTEA round-trip, re-key, timestamp edit, roles, difficulty, old-catalog compatibility | - |
| lz4_block | test_lz4_block 6 | framing, bare blocks, trailing bytes | throughput |
| msstore / xgs | test_msstore 16, test_xgs 8 | index parse/serialise, write/rotate/create/undelete, xgs mirror, xgs-only read-only | **real meta lengths 296/360; id and timestamp at 0x118/0x120** |
| catalog | indirect only | - | load/save round-trip, version handling, forward compatibility, concurrent writers |
| operations | test_operations 16, test_operations_xbox 12, test_clear_slot 16, test_restore_extract 14 | every op once, one undo, Cloud mtimes, pruning order, vault import, extract restore safety | one-member source into an occupied slot (E1); multi-level undo (E2); undo target (E3); failed-write undo (E11); clear plan by identity (E10); cross-account |
| playtime | test_playtime 10 | id from data, cache, renames, cross-platform, name fallback | twins (E8); Xbox meta id |
| discover/state/platform | test_discover 9, test_state 5, test_platform 20 | discovery, config, per-OS roots | CLI discover adding live Xbox folders |
| GUI | test_gui_actions 25, test_gui_panes 20, test_ui_text 1, test_theme 28 | menus invoked, panes, sort, history filter, tooltips, ASCII-only | promote/undo target with two writable sources (E9) |
| other | test_aliases 21, test_updates 24, test_selfupdate 40 | - | - |

**G3. Fixtures and helpers**

- `live_save_dir`: session scope, the real Steam folder read-only, skipped when absent;
  located through `APPDATA` (conftest.py:15-33).
- Per-file `sandbox` fixtures copy the live `*.hg` files into `tmp_path` (test_operations.py:20-31,
  test_restore_extract.py:22, test_clear_slot.py:137).
- `make_wgs_account`: a synthetic wgs builder (conftest.py:70-116). Its meta length is wrong
  (B6), and its meta id and timestamp are 0.
- `steam_save(folder, slot, member, name, play, ident, ts)`: a fully synthetic Steam pair. It
  is a local helper in test_playtime.py:43-63, not a shared fixture.
- `gui_app`: the single session Tk root with patched config and a hand-written catalog
  (conftest.py:121-187).

**G4. How organizer code would be tested** - INFERRED

- Core index and queries: build synthetic vaults from `steam_save` + `make_wgs_account`, after
  promoting the former to a conftest factory and fixing the latter to real lengths with a
  non-zero 0x118/0x120. Use `Vault(tmp_path/...)` and the real ops to create entries.
- Compound ops: the same sandboxes. Assert the E1/E2/E3/E10/E11 properties as regression
  tests.
- Real-data smoke tests: an optional read-only `real_vault_dir` fixture (skip when absent),
  holding any cache in temp, never in the vault.
- GUI: only through the single `gui_app` root.
- **Windows MAX_PATH:** a synthetic wgs tree under a deep `--basetemp` passed 260 characters
  (`FileNotFoundError` in E1 Xbox). Keep fixture names short.

### 2h. Measurements

VERIFIED, read-only, warm OS file cache; cold-disk times will be higher.

| What | Result | Script |
|---|---|---|
| Load `catalog.json` | 771,052 B; `json.loads` 1.6 ms; `Vault.load()` + `.entries` 3.7 ms (min of 5) | `measure_scan.py` |
| `scan_any` over everything | 52 folders (2 live + the xgs twin + 49 entries): **0.98-0.99 s**; 780 slot positions, 620 occupied, 1237 present members | `measure_scan.py` (2 passes) |
| Per folder | Steam full 18-21 ms; Steam extract ~3.4 ms; wgs 28-43 ms (reads 461 MB of blobs); xgs 27 ms | same |
| Identity, no cache | 9.70 s (51 folders, 1207 saves, 30 playthroughs) | same |
| Identity, warm cache | 1.01-1.14 s | `measure_collect_cached.py` (cache copied to temp) |
| sha256 of every member data file | 1177 files, 1987 MB, **2.65 s** including scans; 195 distinct contents (6.0x); 3 versions occur in more than one slot number; none occurs as both A and B | `measure_hash.py` |
| Test suite | 412 tests, 19.8 s | `uv run pytest` |

### 2i. Friction list: where a playthrough-centric model plugs in

| # | Friction | Where | Needs a schema bump or migration? |
|---|---|---|---|
| 1 | Xbox id, timestamp and names missing (parser) | msstore.py:311-339 | No (code fix; re-summarise Xbox entries) |
| 2 | No per-member id, content hash or `size_decompressed` in the cache | catalog.py:28-55 | **Yes**: member keys crash older apps (catalog.py:133; VERIFIED). Bump `CATALOG_VERSION`, add a version check plus tolerant member loading, or keep this data in a sidecar or index file |
| 3 | No home for notes (per save-file / per version) | catalog.py:42, 76 | Yes if stored in the catalog (an older app drops unknown entry keys, VERIFIED). No if stored in a sidecar keyed by id/hash |
| 4 | No lineage records | ops:958-966 | No: oplog keys are additive and safe. Inferring lineage for the existing vault is R7's job |
| 5 | Absolute paths (D11) | catalog.py:239 | Yes. Do it in the same bump as #2 |
| 6 | Live folders catalogued as in-place "backups" | cli.py:337; the owner's `inplace-20260624-182102` | Data clean-up, or a rule: an entry whose path is a live source is treated as live |
| 7 | No entry mutation ops (delete, rename, label, note, re-summarise) | catalog.py:150-245 | No (new API) |
| 8 | No lock or reload-before-write | catalog.py:192-197 | No (lock file or optimistic reload) |
| 9 | Id cache keyed by absolute path, never pruned | playtime.py:122-163 | Largely obsolete after #1 (only pre-Waypoint Steam needs it) |
| 10 | Ops are slot-granular and single-shot; undo is single-level and target-blind; failure gap | D5-D7 | New core layer: transaction/compound op, op ids, per-source undo |
| 11 | Front-ends choose write targets | gui.py:1539, 1637 | No (core target type: source id + slot) |
| 12 | Platform and account not first-class on entries | catalog.py:66-77 | Additive entry keys are tolerated on read but dropped by older writers |
| 13 | Snapshot retention of 20 versus organizer churn | ops:31 | No |
| 14 | Storage duplication: 6x for save data, 1.7 GB of DDS copies | A1, 2h | S6 decision |
| 15 | Mixed time bases (`created` local naive vs unix stamps) | catalog.py:240 | Normalise when bumping |

## 3. Implications for the design

**Constraints that hold whatever is chosen**

- Standard-library-only runtime, a Tk GUI and ASCII-only UI text.
- Save data is copied verbatim; every write goes stage -> validate -> swap with a snapshot
  first; no writes while the game runs.
- A slot is a pair with fixed roles, and the game loads the newer member.
- A Steam meta is bound to its slot (re-key on every move). Xbox identity lives in
  `containers.index`, mirrored to xgs.
- Steam<->Xbox transfer is gated.
- `sqlite3` ships in the Windows kit: `packaging/build_portable.ps1:41-44, 119` copies
  `sqlite3.dll` and does not skip `_sqlite3.pyd`, and both are present in the local
  `dist/NMSSaveVault/_runtime/DLLs/` (VERIFIED). An index store can therefore stay
  stdlib-only. The POSIX kit was not checked.

**Risks, most severe first**

1. **Wrong-target writes.** The organizer manages up to 30 live slots across two platforms,
   and today the target is chosen by the front-end (E9) or not recorded at all (E3). Every
   organizer op needs an explicit (source id, slot) target, validated in core.
2. **Mixed slots after a move (E1).** A version placed into a live slot must replace the pair
   completely: write both members, and delete or refuse a member the source lacks.
3. **Eviction safety by slot number (E10).** "Is the evicted save safe in the vault?" must be
   answered by identity plus content hash.
4. **Undo for compound ops (E2, E11).** One grouped record, a correct snapshot reference even
   on failure, and undo per live source rather than per vault.
5. **Schema evolution.** New member data cannot go into the v1 catalog without breaking older
   app versions, and concurrent writers already lose updates.
6. **In-place live folders masquerade as versions** (the owner's live Xbox account).
7. **Snapshot churn.** 20 snapshots is about 1 GB of space but only ~20 single-slot moves of
   history (fewer for swaps). Snapshots of different sources compete for the same retention.
8. **Xbox metadata quality** (names, times, ids) is wrong until B6 is fixed. Some names that
   look like `<unnamed>` are parser losses, not real blanks.

**Opportunities**

- A meta-level index is cheap: a full rescan takes ~1 s, so the main organizer table can be
  computed live from `scan_any` plus identity. Only content facts (S2) need a persistent
  cache.
- Managed entries never change after creation. Ids, hashes and summaries can be computed once,
  at creation or in a one-time migration (~3 s for the whole vault), and never again.
- The data file's sha256 is a natural **version id**. Every op copies data verbatim, so the
  hash survives backup, extract, restore and move; it folds 1177 copies to 195 versions.
  Steam metas are re-keyed per slot, so hash the decrypted plaintext if the meta must be
  part of the key.
- The oplog can carry lineage from the next release on, with no migration.
- 1.7 GB of the 3.4 GB vault is copied thumbnail cache. Excluding it from full backups, or a
  content-addressed store (S6), halves the vault. The DDS files are named by place, which
  might be usable as thumbnails (UI topic).
- The building blocks are solid: re-key, wgs write/delete with xgs mirroring, atomic writes,
  snapshot-before-write. A compound "move with eviction" can live entirely inside core.

## 4. Open questions and candidate options

These are owner choices; the parent forks designs on them.

**O1. What is one line ("save-file")?**
- (a) The game id: twins and cross-saves merge. Simplest, and it matches today's history
  filter.
- (b) Game id + platform: cross-saves split; twins still merge.
- (c) Game id + branch, from recorded or inferred lineage: twins and restored-old-version
  branches become distinct lines. Needs S3.
- (d) Lines anchored to live slots (one per live slot plus vault-only runs): intuitive for
  "where is it live", but weak for history.

**O2. What is one version?**
- (a) One member's data, by sha256: exact; A and B are separate versions (0 overlaps seen).
- (b) A slot pair (A+B) as one version: matches slot-granular ops, but two moments in time
  become one item.
- (c) Meta tuple (id, play time, meta timestamp): needs no hashing, but is not
  collision-proof after edits.

**O3. Where do notes live?**
- (a) Catalog v2 fields: one file, but needs the version bump and old-app refusal.
- (b) A vault sidecar such as `notes.json`, keyed by playthrough id plus version hash: no
  schema risk; notes follow the bytes across backups automatically. Not carried by
  `import_vault` unless taught to.
- (c) A file inside each managed entry folder: travels with copies and vault imports, but is
  duplicated per backup and absent for in-place entries.
- (d) A `sqlite3` index holding notes, versions and lineage: queryable; a new store to back up
  and migrate.

**O4. How is lineage recorded?**
- (a) Richer oplog lines only.
- (b) A separate edge log of version -> version moves.
- (c) Either of those, plus inference for the existing vault (R7).

**O5. What is the undo model?**
- (a) Keep snapshot-restore undo but group compound ops, record the target, close the failure
  gap, and keep a stack per source.
- (b) "Everything is a version": undo is a reverse move from the vault, and snapshots become
  a safety net only.
- (c) Both: (b) in the organizer, (a) for the classic ops.

**O6. What does eviction do?**
- (a) Always extract (duplicate bytes; simple).
- (b) Extract only when no identical version (hash) is already in the vault.
- (c) A distinct "parked" entry kind so evicted saves are not mixed with user extracts.

**O7. How are members handled in a move?**
- (a) Pair only: always replace both members, and delete a destination member the source
  lacks.
- (b) Let the user pick a member as "the version". Write it and drop or keep the other
  (needs a member-level op).
- (c) Refuse incomplete sources.

**O8. How does the catalog evolve?**
- (a) v2 in one migration: version check, forward-tolerant member loading, D11 relative
  paths, ids and hashes per member, normalised times.
- (b) Freeze v1 and put everything new in sidecar or index files.
- (c) A `sqlite3` index rebuilt from the folders, with the catalog kept as-is.

**O9. What happens to in-place entries that are live folders?**
- (a) Mark them live and hide them from the version lists.
- (b) Convert them: copy into the vault, then drop the in-place reference.
- (c) Block them at discover and import time (the CLI `discover` fix).

**Pre-work candidates.** These are defects, so the parent's call rather than the owner's:
- the B6 Xbox meta parser and its test fixture lengths
- the E9 GUI promote/undo target
- the E1 one-member source into an occupied slot
- the E3/E11 undo target and failure gap
- the E10 clear plan by identity
- CLI `discover` adding live Xbox folders
- the stale "SizeDisk == file size" line in DESIGN.md:91
- `msstore.microsoft_root` reading `os.environ` at the point of use

**Questions recorded for the owner** (not asked, per instructions):
1. Should a cross-saved run (same id on Steam and Xbox) be one line or two?
2. Should two live slots holding copies of one run be separate lines?
3. On "move to live", should the evicted save always be copied into the vault, or only when
   no identical copy exists?
4. Should snapshot retention stay at 20 once organizer moves are routine?
5. Should full Steam backups keep copying `cache/` thumbnails (1.7 GB so far)?
6. Should the live Xbox folder's in-place catalog entry be removed or converted?

## Appendix A. Reproduction and read-only proof

All scripts are in `<R1>` and are run from the repo root with `PYTHONUTF8=1 uv run python <script>`.

| Script | Purpose | Output |
|---|---|---|
| `analyze_catalog.py <R1>\vaultcopy` | schema, counts and examples from **copies** of `catalog.json`, `oplog.jsonl` and `playthrough-ids.json` | `catalog_analysis.txt` |
| `measure_scan.py` | catalog load and `scan_any` timing, staleness, uncached identity, before/after fingerprint of vault + live + xgs | `measure_scan.txt` |
| `measure_collect_cached.py` | identity with the id cache **copied into a temp root** | `measure_collect_cached.txt` |
| `measure_hash.py` | sha256 of every member data file, distinct versions | `measure_hash.txt` |
| `meta_regions.py`, `xbox_meta_raw.py` | unparsed Steam meta regions; raw Xbox meta bytes vs parser output | `meta_regions.txt`, `xbox_meta_raw.txt` |
| `test_r1_experiments.py` | E1, E1x, E2, E3, E6, E8, E10, E11 in sandboxes (small real extracts **copied** out of the vault, plus synthetic saves). Run with `PYTHONPATH=<repo> uv run pytest -p tests.conftest <file> --basetemp <R1>\pt -s`: **8 passed** | `experiments.txt` |
| `gui_promote_target.py` | E9: a GUI App over two sandbox Steam folders; config writes patched to raise; promote replaced by a recorder | `gui_promote_target.txt` |

**Read-only proof** (VERIFIED at the end):
- `catalog.json` sha256 `a3a58014...a045b9` and `oplog.jsonl` sha256 `29b3ece9...598aaa` are
  identical to the values taken at the start.
- `playthrough-ids.json` is unchanged at 41,480 B, mtime 2026-10-05 17:41.
- The vault still has 4517 files and 3,404,610,911 B.
- 0 files changed today in the live Steam folder and under the Xbox `SystemAppData` tree.
- `state.json` is unchanged (2026-08-21).
- The `measure_scan.py` fingerprint (files, bytes, newest mtime) of the vault, live Steam, wgs
  and xgs is equal before and after.
