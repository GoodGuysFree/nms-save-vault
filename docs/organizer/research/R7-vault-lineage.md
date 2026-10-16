# R7 - The real vault: identities, versions, duplication, branching, lineage signals

Research wave 1, topic R7 (feeds S1 identity and versions, S3 lineage / history tree, S6 vault
storage). Branch `design/save-organizer`; no production code, tests or docs were changed.

Evidence tags: **VERIFIED** = measured by R7 on the real data (script named); **SOURCED** = read
in code (`file:line`) or another wave-1 report; **INFERRED** = reasoning from the above.
Redaction: `st_<steamid64>`, `<xbox-account>`, `%APPDATA%`, `%LOCALAPPDATA%`. Save names and
game content are shown as-is; playthrough ids are shown as their first 8 hex digits.

All real-data work was **read-only** (files opened with `read_bytes()`/`open("rb")`, core
readers `savedir.scan_any`, `Vault(root).load()`, meta decrypt, `lz4_block`); nothing was
written to the vault, the live folders, in-place backup folders or app config. Scripts and raw
outputs live in the session scratchpad,
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\be828316-f5a0-4419-bd50-12964a376ba0\scratchpad\R7\`
(written `<R7>` below; Appendix A lists every script). Times: save timestamps are UTC; entry
ids and oplog times are local time (UTC+3 in this period).

## 1. TL;DR

- **30 playthroughs, 195 versions, 1,237 copies.** Live Steam + live Xbox + 49 catalog entries
  hold 1,237 slot-member copies (1,177 after dropping the `xgs` mirror and an in-place entry
  that *is* the live `wgs` folder). They fold onto 30 identities (29 by the game's
  `SaveUniversalId`, 1 pre-Waypoint Xbox save "Voyagers" with no id) and 195 distinct data
  files (Steam 151, Xbox 44) = 195 distinct decompressed contents. 16 live versions are not in
  the vault yet.
- **One organizer line per identity is safe.** A and B held the same identity in 587/587 slot
  captures; no identity was ever in two slots of one capture (twins = 0). 4 identities are
  cross-platform and 3 are live on Steam *and* Xbox right now, at different versions.
- **The user-facing version is the captured slot pair.** 195 member files form 106 distinct
  (A,B) pair snapshots = 105 distinct "current" members; the newer member by meta timestamp
  always had >= play time (581/581). Store members, show pairs.
- **Xbox metas carry the id (0x118) and a real save time (0x120); the app reads neither.** It
  orders Xbox A/B by the `containers.index` time, which picks a different "newest" member than
  the meta time in 37 of 84 captured pairs (5 of 14 live slots); meta time agrees with play
  time 77/77. 296-B metas also lose name and difficulty.
- **3.41 GB vault, ~0.5 GB of distinct content.** 1.77 GB is the thumbnail `cache` copied by
  every Steam full backup (139 MB distinct); save data is 1.63 GB in-vault, 358 MB distinct
  (78 % duplicate bytes).
- **73 versions exist in exactly one folder** (16 live-only, 6 only in snapshots that the
  age-based keep-20 pruning will delete) - deletion and pruning must become version-aware.
- **Branching in the record is rare and mostly invisible to meta.** A 2026-10-05 rollback of
  "Main Save (S2)" is visible only in the oplog; the one timestamp/play-time inversion in the
  vault is a cross-platform fork of "Creative Primary". No same-platform play-time regression
  in capture order.
- **Saves change outside the game and the app.** Three save-editor sessions (2026-06-30,
  09-23, 09-29) rewrote 7 member files - ships upgraded, an item added, inventory stacks maxed -
  keeping meta timestamp and play time; three vault writes (two clears, one repopulate) did not
  stick, consistent with Steam Cloud. (timestamp, play time) cannot identify a version - only a
  hash can.
- **Measured lineage signals (179 known ancestor pairs):** meta play time and timestamp never
  decrease; 31 core global stats never decrease; the VisitedSystems log (ignoring its mutable
  last entry) extended 261/261 times. Combined: 179/179 accepted, and of all 1,340
  intra-identity pairs only 6 come out incomparable - exactly the real fork.
- **Discovery records and base objects are not containment tests** (store capped at ~3,200
  records with non-age eviction; fresh records vanish), but the discovery timestamps bound
  *when* a fork happened.
- **Content cannot see "touch" forks** (load/quit, editor edits; 11 % of chain steps leave no
  trace). Here meta-only inference gives the same tree as meta+content; content matters once
  both branches progress (it rejects 95 % of unrelated but meta-consistent pairs). Exact
  lineage needs recording at write time plus observation of live slots.
- **Costs are small.** Hash all 1,237 copies 1.24 s; pure-Python first-chunk decode ~23 ms per
  version; full decode + `json.loads` ~0.45 s per version (88 s for all 195). A full content
  index is a one-off of ~1.5-2.5 min, then ~1 s per new version.

## 2. Findings

### 2.1 Scope, method and ground rules

| Item | Value | Tag / source |
|---|---|---|
| Folders scanned | 52 = live Steam (`st_<steamid64>`), live Xbox `wgs\<xbox-account>`, its `xgs` mirror, 49 catalog entries (17 full, 16 snapshot, 8 extract, 8 in-place) | VERIFIED `<R7>\inventory.py` |
| Live Steam | 14 occupied slots (2-15), slot 1 empty, Steam Cloud on; a second `st_` folder holds only a `cache` | VERIFIED |
| Live Xbox | 15/15 slots; `xgs` mirror byte-identical to `wgs` for 30/30 members (data and meta) | VERIFIED |
| In-place entry that is live | `inplace-20260624-182102` has the same path as the live `wgs` folder; counting it as a backup double-counts every live Xbox save | VERIFIED; R1 notes its cached summary is stale |
| Members read | 1,237 (Steam 997, Xbox 240); 3 more incomplete (meta without data, slot 15B of three PC-A folders) | VERIFIED |
| Per member | slot, A/B, platform, entry kind/date, meta fields (name, summary, play time, timestamp, difficulty, mode, season, sizes, id), sha256 of data (full file and valid-chunk region), sha256 of meta (raw and decrypted), id from the first data chunk | VERIFIED `<R7>\inventory.json` |
| Distinct versions decoded fully | 195 (lz4 C package for research speed), de-obfuscated with R5's mapping (0 unknown keys), features per version | VERIFIED `<R7>\features.py`, `<R7>\features\*.json` |
| Dedup scope used below | 1,177 members in 50 folders (live Steam, live `wgs`, 48 entries; `xgs` mirror and the live alias dropped) | VERIFIED |

No Steam data file had stale trailing bytes, so the full-file hash and the valid-region hash
agree everywhere (195 = 195). The 16 old Xbox blobs are bare LZ4 blocks (no chunk framing).

### 2.2 Inventory and identities (question a)

**Identity sources** (VERIFIED, `<R7>\inventory.py` + Xbox meta probe):

| Source | Copies | Result |
|---|---|---|
| Steam meta u64 @0x15C vs id in data | 997 | equal in 997/997 |
| Xbox meta u64 @0x118 (Worlds-era 296/360-B metas, u32 @0x124 = 2003/2004) vs id in data | 224 | equal in 224/224 - but the app never reads it (2.3) |
| Xbox pre-Waypoint (bare LZ4, 360-B meta, format 0) | 16 | no id anywhere -> fallback key `xbox:13:Voyagers` |

The id in the data is `CommonStateData.SaveUniversalId` (obfuscated key `WmU`; R5 mapping), the
game's own per-save id that survives renames, slot moves and cross-save (VERIFIED on R5's
decoded samples: `0xDDDAD1BDD39F9C72` = Main Save (S2)). `CommonStateData.UsedPlatforms` is
`["Win"]` for Steam and Xbox alike, so it cannot tell platforms apart (VERIFIED, 195 versions).

**The 30 identities** (VERIFIED, `<R7>\identity_table.md`; dedup scope; "pair snapshots" =
distinct (A,B) hash pairs; "live now" S = Steam slot, X = Xbox slot):

| id | latest name | platforms | live now | member versions | pair snapshots | copies | folders | play time | last saved (UTC) | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| DDDAD1BD | Main Save (S2) | steam | S9 | 38 | 20 | 70 | 35 | 532.3 h | 2026-10-15 20:39 | 10-05 rollback (2.6) |
| 598350F8 | Main Save XBox (X4) | xbox | X2 | 8 | 4 | 12 | 6 | 296.4 h | 2026-10-10 17:47 | |
| 95897B51 | (unnamed) | xbox | X5 | 2 | 1 | 12 | 6 | 119.5 h | 2026-06-30 18:04 | |
| C8AEF0EB | Egg Gifting Xbox | xbox | X3 | 2 | 1 | 12 | 6 | 61.1 h | 2025-09-18 10:04 | |
| AF0281E2 | All Parts Building-11200qs | steam | S12 | 11 | 6 | 64 | 32 | 54.1 h | 2026-09-29 13:36 | expedition started inside it 09-26 |
| 4D4695F7 | ShipsToGive | xbox | X1 | 4 | 2 | 12 | 6 | 51.1 h | 2026-10-10 18:02 | |
| 0CC4C25E | Creative Primary | steam+xbox | S13, X8 | 22 | 11 | 74 | 37 | 45.2 h | 2026-10-08 13:23 | cross-platform fork; Steam slots 1 -> 13 |
| 733F23BB | SHIPSTOGIVE-1400QS | steam | S11 | 12 | 6 | 70 | 35 | 43.9 h | 2026-10-10 14:15 | was ShipsToGive-2400qs |
| 5FC8FFCB | Egg Gifting | steam | S14 | 13 | 8 | 70 | 35 | 32.3 h | 2026-09-29 13:39 | editor-edited 09-23 |
| F1E6D7E5 | PhoenixesToGive-3200qs | steam | S2 | 5 | 3 | 64 | 32 | 29.0 h | 2026-10-15 18:24 | editor-edited 09-29 |
| 49960A04 | (unnamed) | xbox | X7 | 2 | 1 | 12 | 6 | 28.1 h | 2025-11-12 18:54 | |
| 282A09E0 | Leviathan-80qs-SHIPS | steam | S6 | 5 | 3 | 64 | 32 | 19.5 h | 2026-07-16 05:57 | editor-edited 09-29 |
| F35246E3 | (unnamed) | xbox | X6 | 2 | 1 | 12 | 6 | 18.9 h | 2024-12-13 07:16 | |
| 73581337 | Exobiology-385qs | steam | S4 | 3 | 2 | 66 | 33 | 17.2 h | 2026-10-04 13:56 | |
| 0E27D347 | Polestar-5200qs | steam | S8 | 5 | 3 | 64 | 32 | 17.0 h | 2026-09-29 13:39 | |
| 77AA047F | Light No Sky season 2 | xbox | X10 | 2 | 1 | 12 | 6 | 13.3 h | 2026-07-16 05:04 | |
| FB7AA9BC | Artemis-936qs | steam | S7 | 4 | 2 | 64 | 32 | 12.7 h | 2026-06-30 20:00 | editor-edited 06-30 (ships) |
| 2407C94B | CrossSaveTest-300qs | steam+xbox | X4 | 4 | 2 | 48 | 24 | 11.2 h | 2026-02-22 16:05 | Steam copy evicted 08-22; was CrossSaveTest |
| A379EC9F | (unnamed) | xbox | X15 | 2 | 1 | 12 | 6 | 10.1 h | 2026-07-16 05:05 | |
| 9E8385F0 | Exp23 Our Journey Continues | steam | S15 | 10 | 5 | 22 | 11 | 8.7 h | 2026-10-09 14:29 | expedition -> normal; renamed 10-04 |
| xbox:13: | Voyagers | xbox | X13 | 2 | 1 | 12 | 6 | 8.6 h | 2025-02-06 20:35 | **no id** (pre-Waypoint, game Version 4658) |
| 93E6EB04 | Exobiology1-600qs | steam+xbox | S10, X9 | 5 | 3 | 76 | 38 | 8.4 h | 2026-02-22 16:04 | was Exobiology1 on Xbox; editor-edited 09-29 |
| DECA9E50 | The Cartographers | steam+xbox | S3, X14 | 7 | 4 | 62 | 31 | 8.3 h | 2026-04-28 05:20 | Steam slots 13 -> parked -> 3; editor-edited 09-29 |
| 10EE7280 | Ex-Beachhead-160qs | steam | S5 | 3 | 2 | 66 | 33 | 7.3 h | 2026-02-22 16:02 | editor-edited 09-29 |
| A83A5C36 | Singularity | xbox | X11 | 2 | 1 | 12 | 6 | 6.0 h | 2025-11-12 18:30 | |
| AF246F0C | (unnamed; meta said "Swarm" until 07-07) | steam | - | 4 | 2 | 50 | 25 | 3.7 h | 2026-07-07 18:59 | vault only |
| 79B6E6C5 | FreshStart | steam | - | 3 | 3 | 43 | 23 | 2.1 h | 2026-04-28 05:16 | vault only |
| 4418B58A | Our Journey Continues | xbox | X12 | 8 | 4 | 12 | 6 | 1.7 h | 2026-10-04 14:25 | same name as 9E8385F0, different id |
| 893FC5AF | (unnamed) | steam | - | 3 | 2 | 6 | 3 | 0.4 h | 2026-09-20 18:14 | Permadeath; slots 15 -> 1; vault only |
| 2EE1A949 | (unnamed) | steam | - | 2 | 1 | 2 | 1 | 0.2 h | 2026-09-13 13:19 | Relaxed; vault only |

Totals: 26 identities live (Steam 14 + Xbox 15 - 3 on both), 4 vault-only. Steam play-time of
the largest is ~532 h; versions per identity range 1-38 member files / 1-20 pair snapshots.

**Names are not identity, and the owner already uses them as notes** (VERIFIED,
`<R7>\features\*.json`):
- Same name, different id: "Our Journey Continues" (Steam 9E8385F0 vs Xbox 4418B58A). Near
  names on different ids: "Egg Gifting" / "Egg Gifting Xbox", "ShipsToGive" (Xbox) /
  "SHIPSTOGIVE-1400QS" (Steam).
- Same id, several names: 733F23BB "ShipsToGive-2400qs" -> "SHIPSTOGIVE-1400QS" (2026-07-27);
  2407C94B "CrossSaveTest" (Xbox) -> "CrossSaveTest-300qs" (Steam); 93E6EB04 "Exobiology1" ->
  "Exobiology1-600qs"; 9E8385F0 auto expedition name "Our Journey Continues" (data name empty)
  -> "Exp23 Our Journey Continues" (2026-10-04); AF246F0C meta "Swarm" -> empty.
- The `-NNNqs` suffix is the quicksilver balance: it equals `PlayerStateData.Specials` in 10
  named saves and goes stale (Artemis-936qs holds 1,086; ShipsToGive-2400qs held 1,400 for
  months before the rename). The owner is hand-maintaining an index fact in the name.
- Expedition saves get an automatic meta name (season title) while the data name is empty.

**Difficulty / mode are per-version attributes** (VERIFIED on Steam metas): the preset changes
inside one playthrough (DDDAD1BD switches Creative/Normal five times; AF0281E2 goes Normal ->
Creative -> Custom -> Seasonal -> Custom -> Creative), and expeditions show Seasonal with the
season number in the meta (45, 46, 47). Xbox 296-B metas are mislabelled by the app (2.3).

### 2.3 Xbox metas carry identity and time; the app does not read them

VERIFIED (raw meta bytes of all 240 Xbox members; `<R7>\inventory.json` fields
`xbox_meta_id/xbox_meta_ts`): Worlds-era Xbox metas (296 B with format 2003/2004, 360 B with
2004) hold the playthrough id at u64 0x118 (224/224 equal to the data id), a save timestamp at
u32 0x120 and the format at u32 0x124. SOURCED: `msstore.parse_ms_meta` gates these fields on
`len in (META_LEN_WORLDS_I, META_LEN_WORLDS_II)` = the *Steam* lengths 384/432
(`src/nms_save_vault/core/msstore.py:332-336`) and names on `len >= 360`
(`msstore.py:329-331`), so for real Xbox metas `slot_identifier = 0`, `timestamp = 0`, and
296-B metas also lose name and difficulty. R1 and R8 report the same defect.

Consequences, all VERIFIED:
- `playtime.py` decodes a data chunk per Xbox save to find an id that is sitting in the meta
  (its docstring "Xbox metas leave that field 0", `playtime.py:12-15`, is a parser gap).
- The app's "newest member" for Xbox falls back to the `containers.index` FILETIME
  (`savedir.py:85-89`, `msstore.py:404`). That time is bumped for both members at sync: over
  the 84 A/B pairs of the six `wgs`-type Xbox folders (live, 3 full, 2 snapshots) it picks a
  different newest member than the meta time in 37, including 5 of the 14 live slots with a
  meta time, while the meta-time order agrees with the play-time order in 77/77 pairs with
  different play times (including the `xgs` mirror and the live alias: 48/112 and 103/103).
  Example: live X3 shows A as newest (index times equal) but B is newer (meta 2025-09-18
  10:04:04 vs A 10:03:20 UTC; B has 44 s more play).
- Names: the 296-B metas of X4/X9/X14 hold "CrossSaveTest", "Exobiology1", "The
  Cartographers" at 0x14; the app shows them unnamed.
- Difficulty: raw u32 0x114 is Custom/Creative on X4, X6, X9, X14; the app shows "Normal".
- `set_ms_meta_timestamp` (`msstore.py:346`) is a no-op on 296/360-B metas, so an Xbox
  "promote" only moves the index time. Seen on X6B: data and meta hashes never changed; the
  index time went 1725120556 -> 1734074169 with the 2026-10-02 promote and is **still**
  1734074169 in live after the undo, although the restored pre-promote snapshot holds
  1725120556 (side issue: the Xbox undo did not restore that index time).

### 2.4 A/B pairs and what a "version" is (question b)

VERIFIED (dedup scope, 590 slot captures; 587 with both members, 3 with A only):

| Measure | Result |
|---|---|
| A and B same identity | 587 / 587 |
| A and B ever share a data hash | never (0 hashes seen as both A and B) |
| Newer member (meta time) | Steam A 386, B 111; Xbox A 37, B 47, tie 6 |
| Newer member has >= play time | 581 / 581 (6 ties excluded) |
| play-time gap A vs B | median 40 s, p90 338 s, max 3,501 s |
| timestamp gap A vs B | p25 59 s, median 21 min, p75 96 days, max 325 days |
| SaveSummary (location text) differs A vs B | 252 / 587 |
| distinct member files / pair snapshots / current members | 195 / 106 / 105 |

So the A/B pair is one playthrough at two points in time. The months-long timestamp gaps with
near-equal play time are "touches": the save was only loaded (the autosave A is rewritten, the
restore point B stays old) - e.g. 2026-02-22 16:00-16:08 UTC, eight saves were each loaded and
re-saved without play-time change (Polestar, Ex-Beachhead, CrossSaveTest, Leviathan,
Exobiology-385qs, Exobiology1, All Parts Building, PhoenixesToGive).

INFERRED recommendation: a *version* in storage is a member file keyed by data sha256 (195 here;
meta is an attribute, 1:1 with data in this vault - 195 meta plaintexts for 195 data files).
The *user-facing* version is the captured pair (106), labelled by its newer member (105
distinct), because the restorable unit is the pair: `repopulate_slot` copies both members
(SOURCED `operations.py:306-364`) and the game loads whichever member is newer.

### 2.5 Duplication and storage (question c)

VERIFIED (`os.walk` + sha256 over the vault folder; `<R7>` ad-hoc scripts):

| Category in `_SaveVault` | Bytes | Files | Distinct |
|---|---|---|---|
| `cache\*.DDS` thumbnails | 1,772 MB | 2,326 | 184 files = 139 MB |
| Steam save data `save*.hg` | 1,302 MB | 818 | see below |
| Xbox blobs (data + meta) | 326 MB | 310 | see below |
| accountdata | 2.7 MB | 54 | - |
| Steam metas | 0.4 MB | 818 | - |
| Xbox index / containers | 0.1 MB | 160 | - |
| catalog / oplog / id cache | 0.8 MB | 3 | - |
| **Total** | **3,405 MB** | **4,517** | |

- Save data inside the vault: 968 copies = 1,628 MB -> 154 distinct = 358 MB (78 % duplicate
  bytes). Including live and the in-place folders: 1,237 copies = 2,122 MB -> 195 = 468 MB.
- The cache comes from Steam full backups taken with `include_cache=True`, which copies the
  whole live folder (SOURCED `operations.py:170-171`); 14 entries carry 116-136 MB each.
- Copies per version (all 1,237 copies, mirrors included): 67 versions have a single copy;
  the most-copied has 33.
- No data hash appears on both platforms (cross-save is never byte-identical).
- A content-addressed store would hold the in-vault content in ~0.5 GB (358 MB data +
  139 MB cache + small files) instead of 3.4 GB (INFERRED from the above).
- **Unique-holder risk.** 73 versions are held by exactly one folder: 33 in a full backup,
  16 live only, 16 in an in-place folder outside the vault, 6 in snapshots
  (`snapshot-steam-20260625-154113`: 2 Main Save versions from June;
  `snapshot-steam-20260920-211359`: 4), 2 in extracts. Snapshot pruning keeps the newest 20 by
  age and deletes folders whole (SOURCED `operations.py:31`, `148-159`), so it can delete the
  only copy of a version.

### 2.6 Branching evidence (question d)

VERIFIED from the slot timeline (`<R7>\slot_timeline.txt`), per-identity version lists
(`<R7>\versions.py`) and the resolved oplog (`<R7>\oplog_edges.py`):

| # | Case | Evidence | Visible to |
|---|---|---|---|
| 1 | **Rollback fork, Main Save (S2), Steam S9, 2026-10-05** | 17:41 local full backup holds A=9e4b2f00 (pt 1,911,191). The game then saved A=b9c8b750 (14:48 UTC, +20 s play, a load/quit). 17:48:50 and 17:49:49 local the owner repopulated S9 from the 17:41 backup; the pre-op snapshot of the second repopulate still shows b9c8b750, so the first was reverted within a minute. Live A=54cfd111 (10-15) descends from 9e4b2f00; b9c8b750 is a dead-end sibling. | oplog only; no play-time or timestamp inversion |
| 2 | **Cross-platform fork, Creative Primary** | Xbox X8: B=13981763 (2026-02-14, pt 139,170), A=e70091c0 (2026-07-16, same pt = a touch). Steam continued from the Xbox state (S1 2026-03-04 pt 140,781; later S13 to pt 162,773). e70091c0 was saved after Steam versions with more play. Both branches are live now (S13 and X8). | meta inversion (the only one in the vault) + content |
| 3 | Cross-save direction | Steam descends from the Xbox copy for 0CC4C25E, DECA9E50, 2407C94B, 93E6EB04 (content-consistent; for 0CC4C25E the Xbox 02-14 and Steam 03-04 versions have identical discovery sets). The Xbox copies are stale ancestors, not forks - playing one would make a fork. | meta + content |
| 4 | Park and restore, no fork | The Cartographers left Steam S13 on 2026-06-25 (extract, then S13 repopulated with Creative Primary - inferred from the imported snapshot label "auto: pre-repopulate slot 13"), went back live in S3 on 08-22 from the in-place backup "b4 v6.4" slot 13. Same version (A=6217c242, B=34e78afc) both times. | oplog + hashes |
| 5 | Slot moves | 0CC4C25E S1 -> S13 (2026-06-25); DECA9E50 S13 -> S4 (08-22, undone) -> S3; 893FC5AF S15 -> cleared -> S1 (09-20) -> gone by 09-27 (permadeath save; deletion reached the cloud per R8). | oplog + timeline |
| 6 | **External edit sessions** | Same meta timestamp and play time, different bytes, file mtime = edit time (`<R7>\jdiff.py`): 2026-09-29 13:44:23-13:45:36 UTC S10A, S2A, S3A, S5A, S6A - only `*Inventory.Slots[].Amount` raised to 9999/200/40/10; 2026-09-23 14:31 S14A (Egg Gifting) - one inventory slot added; 2026-06-30 19:59 S7B (Artemis) - ships rewritten (class C -> S, inventory resized, new ship resource/seed). None is in the oplog. | hash + content diff only |
| 7 | Cloud interference | Clear S15 had to be done twice (09-17); clear S3 (09-19) did not stick (same bytes back in the 09-20 backup, still live); first 10-05 repopulate reverted; S1 vanished between 07:59:39 and 08:02:04 local on 09-20 (cause unrecorded) and was repopulated into an empty slot. | oplog + snapshots |
| 8 | Twins (same id in two slots of one capture) | 0 in all 52 folders | - |
| 9 | Same-platform play-time regression in capture order | 0 | - |

INFERRED: real forks so far came from (a) the owner restoring an older capture after a short
session and (b) cross-save leaving a stale copy that was later opened. Both abandoned branches
were "touches" (no play time). Once the organizer makes "use an older version" one click,
forks where *both* sides progress will become normal, and must be recorded, not guessed.

### 2.7 Lineage signals and their measured accuracy (question e)

**Ground truth** (VERIFIED, `<R7>\lineage_lib.py:consecutive_pairs`): for each platform and
slot, consecutive captures (full, snapshot, in-place ordered by newest save time, live last;
extracts skipped) holding the same identity with a changed version and **no vault op writing
that slot in between**: 64 "current version" steps + 115 member-level steps = 179 known
ancestor pairs (14 pairs straddling a vault write excluded). 18 of the 179 have no play-time
change: 5 are touches (later timestamp) and 13 have an identical meta timestamp - all 13 are the
external edits of 2.6 row 6.

**Context handling** (VERIFIED): a save has `BaseContext` and/or `ExpeditionContext` and an
`ActiveContext` (Main/Season); 10 true pairs switch it (Season -> Main 9, Main -> Season 1),
and Main Save (S2) had its expedition context *replaced* by a new expedition (expedition
`^TIME` 10,571 -> 3,462). Signals must compare like contexts and treat a replaced expedition
context as new. Layouts: 96 versions Base only, 81 Base+Expedition, 16 Expedition only,
2 pre-Waypoint (`PlayerStateData` at top level).

| Signal | Where | Behaviour on 179 known ancestor pairs | Verdict |
|---|---|---|---|
| Meta TotalPlayTime | meta | never decreased (18 equal) | necessary condition; free |
| Meta timestamp (Steam 0x164, Xbox 0x120) | meta | never decreased (13 equal) | necessary condition; free; editors keep it |
| Xbox `containers.index` time / file mtime | index / fs | index time picks the wrong newest member in 37/84 captured pairs (2.3); file mtime re-stamped by restores and editors | do not use for order (mtime does date an external edit) |
| Data `PlayerStateData.TimeStamp`, `CommonStateData.TotalPlayTime` | data | equal to meta (median -1 s; 193/195 play times) except a dormant base context | redundant with meta |
| Global stats (`^GLOBAL_STATS`, 470 ids) | data | 8 ids decrease (COMM_09_ODD_DAY, BIG_FALL, DEV_NOTES, EXP_STANDING, VISIT_EXT_BASE, COMM_CONTRIBUTE, MEDITATION, SPACE_TIME; SPEED_MINE too) | use 31 core counters (never down, up in >= 20 % of pairs: TIME, PLAY_SESSIONS, APP_SESSIONS, DIST_WALKED/FLY/JETPACK/WARP/PULSE, DISC_SYSTEMS/PLANETS, MONEY_EVER, NANITES_EVER, PARTS_PLACED, TIMES_IN_SPACE ...) |
| VisitedSystems (512-entry log, per context) | data | oldest dropped at the front, new appended; the **last entry is mutable** (current planet). `X[:-1]` shifted is a prefix of Y in 261/261 comparisons | strongest divergence signal |
| Discovery Store records (`RID` -> owner `TS`) | data | store capped at ~3,200-3,250 records; eviction is not by age; records saved minutes before a save can vanish: strict containment fails on 70/179, "newer unique records" test false-alarms on 13/179 | not a containment test; usable to bound fork time |
| Base objects (by base address + object timestamp) | data | both sides have objects the other lacks in 163/179 true pairs | not usable without more work |
| `^PLAY_SESSIONS` | data | rises in 163/179 pairs, including all 5 zero-play-time "touch" pairs with a later timestamp; unchanged in the 13 equal-timestamp edits | orients touches; editor edits stay unorientable |

**Classifier** "Y descends from X" = meta play time and timestamp not lower AND no core
counter lower (shared, non-replaced contexts) AND VisitedSystems of X (minus last) extends to
Y. Results (VERIFIED, `<R7>\lineage_tree.py`, `<R7>\lineage_eval.py`):

| Test | Result |
|---|---|
| Known ancestor pairs accepted | 179 / 179 |
| Same pairs reversed, wrongly accepted | 13 / 179 - exactly the external edits (equal timestamp and play time); nothing in meta or progress content orients them, only capture order or file mtime |
| Content alone orients a known pair (forward consistent, backward not) | 157 / 179 |
| All 1,340 intra-identity version pairs: incomparable (neither descends) | 6 - all are Creative Primary's Xbox touch e70091c0 vs early Steam versions = the real fork |
| Parent inference (parent = most-progressed earlier version that passes the test) vs the 64 current-version steps | 64 / 64 consistent; one fork node found (Xbox 13981763, 2026-02-14) |
| Meta-only inference (play time, timestamp) on this vault | identical parents for all 195 versions |
| Unrelated saves posing as relatives (10,263 cross-identity pairs that pass the meta test) | content rejects 95 % (visited log rejects 9,699, counters alone 5,862), 3 % not comparable, 2 % falsely accepted (tiny / new saves) |
| Rollback fork #1 (b9c8b750 vs live) | classified "descends" - wrong, and unavoidable: the abandoned side added nothing |

How often does a step leave a trace? Of the 64 current-version steps: a core counter rose in
88 %, new systems were visited in 66 %, new discovery records appeared in 69 %; 11 % (7
steps) left nothing (VERIFIED).

**Answer to "descends or diverged, and where":** Descent is reliable (High) from meta + core
counters + visited log. Divergence is provable only when the abandoned side did something:
saved later with less progress (meta inversion), or advanced a counter / visited a system the
other side lacks (content). A fork after a touch or an editor change cannot be seen in
content (11 % of steps). The fork point is the most-progressed captured version both sides
descend from (for Creative Primary: Xbox B 13981763, 2026-02-14 14:57 UTC). When the ancestor
was never captured, the newest discovery record the two share bounds the fork from below
(Creative Primary: 2026-02-14 13:06, consistent) and the visited-log alignment counts systems
visited on each side since.

**Recommended signal set** (INFERRED from the measurements above):

| Purpose | Signals | Confidence |
|---|---|---|
| Identity | `SaveUniversalId` from the meta (Steam 0x15C, Xbox 0x118), data id as a check | High |
| Version | sha256 of the data file | High |
| Order within one identity | meta play time, then meta timestamp (Xbox 0x120), then capture order | High |
| "Y descends from X" | order above + 31 core counters + VisitedSystems extension, per context, expedition replacement handled | High (179/179, no false forks) |
| Fork detection | play-time/timestamp inversion, or a counter / visited-log violation in both directions | Medium (blind to touch and edit forks) |
| Fork point | most-progressed captured version both sides descend from; else newest shared discovery record | Medium; Low-Medium for the time bound |
| External edit | same meta timestamp and play time, different hash; file mtime dates it | High |
| Exact lineage | recorded app operations + live journal (2.8) | High once built |

### 2.8 oplog.jsonl (question f)

SOURCED: one JSON line per op `{ts, op, entry_id, snapshot_id, detail, changed}`
(`operations.py:958-966`); `detail` for repopulate is `"<source folder NAME> slot S -> slot D"`
(`operations.py:362`, `:521`); `changed` lists file names only.

VERIFIED (42 records, 2026-06-30 .. 2026-10-05): full_backup 17, extract_slot 7,
repopulate_slot 7, restore_full 4, clear_slot 3, undo 2, promote_member 1, import_vault 1.
The import of an old vault (+10 entries on 2026-07-01) brought no oplog history, so ops before
2026-06-30 (e.g. the 06-25 repopulate of S13) survive only as snapshot labels.

Resolved slot edges (`<R7>\oplog_edges.py`):

| When (local) | Edge | Outcome |
|---|---|---|
| 08-22 15:19:31 | live S4 := extract-steam-20260625-154017 S13 (The Cartographers), evicting Exobiology-385qs (extracted 15:18:54) | undone: an extract restored as full at 15:19:52 wiped the other slots, a full restore at 15:20:10 recovered |
| 08-22 15:38:54 | live S4 := extract S4 (same bytes) | undone 15:39:17 |
| 08-22 16:49:50 | live S3 := in-place "b4 v6.4" S13 (The Cartographers), evicting CrossSaveTest-300qs (extracted 16:48:48) | effective, still live |
| 09-17 10:34, 17:12 | clear S15 (893FC5AF) | first did not stick |
| 09-19 15:02:48 | clear S3 (The Cartographers) | did not stick |
| 09-20 08:02:04 | live S1 := full-steam-20260920-075937 S1 (AF246F0C) into an empty slot | effective |
| 09-20 21:13:59 | live S1 := snapshot-steam-20260917-171206 S15 (893FC5AF), evicting AF246F0C (extracted 21:13:02) | effective; slot empty again by 09-27 |
| 10-02 12:35 / 13:00 | promote X6B, then undo | no net change |
| 10-05 17:48:50, 17:49:49 | live S9 := full-steam-20261005-174129 S9 (Main Save) | first reverted; second stuck -> fork #1 |

Two of these (08-22 16:49 and 09-20 21:13) are the owner's "move to live with eviction" done
by hand in three steps (backup, extract the evictee, repopulate).

Can the oplog seed lineage? Yes for these edges, after resolving the source folder name to an
entry and reading the hashes from that entry's folder. Gaps: the source is a folder *name*
(the in-place PC-B folder is named exactly like the live `st_<steamid64>` folder, so a
repopulate from it would be ambiguous); no hashes, ids or entry ids for source, destination or
evictee; `restore_full`/`undo` record files, not slots or versions; nothing from the game,
Steam Cloud, Xbox sync or editors; nothing confirms a write survived.

What a future record must hold for exact lineage (INFERRED from the gaps above):
1. op id, UTC time, app version; platform and a stable live-folder key (no account id needed
   in the UI).
2. Per written member: slot, A/B, data sha256 and meta sha256 as written, playthrough id,
   meta timestamp, play time.
3. Source as a version reference (entry id + slot + member hashes, or a content-store key),
   never a folder name.
4. Evicted members: hashes, id and where they were stored (entry id / version key).
5. Kind: restore-older, move, swap, clear, promote, undo-of `<op id>`; optional user note.
6. A later "confirmed" mark from a scan that re-read the slot, so cloud reverts show up.
7. A **live journal**: on every app start/scan, the hash of each live member; a changed hash
   becomes an observed edge (previous -> new) of kind "game", "edit" (same timestamp and play
   time, different bytes) or "deleted". Hashing all live members costs well under 0.1 s.

### 2.9 Costs (question g)

VERIFIED (`<R7>\costs.py` -> `costs.json`, project pure-Python decoder, warm OS cache;
`<R7>\features.log` for the lz4-C research pass):

| Operation | Measured |
|---|---|
| sha256 of all 1,237 data copies (2,122 MB, 1 MiB reads) | 1.24 s (first inventory pass incl. meta decrypt and double hashing: 7.7 s; R1 measured 2.65 s) |
| First-chunk decode, 195 versions, pure Python | 4.44 s total (~23 ms each) |
| Full decode, 195 versions (1,562 MB JSON), pure Python | 67.2 s total; median 0.27 s, max 0.81 s (18.8 MB) |
| UTF-8 decode + `json.loads`, 195 versions | 20.3 s total |
| De-obfuscation with R5's mapper (avoidable by reading obfuscated keys) | 68.8 s total |
| Feature extraction used here | 11.4 s total |
| Same decode with the lz4 C package (research only) | 1.1 s total |

A first full content index of this vault therefore costs about 1.5 min (decode + parse +
extract, obfuscated keys) to 2.6 min (with full de-obfuscation), once; each new version then
costs ~0.5-1 s. R5 covers per-file memory and the decoder speed-ups.

## 3. Implications for the design

**Identity (S1) - recommended, High confidence.** Key a line by `SaveUniversalId` read from
the meta (Steam 0x15C, Xbox 0x118 once the parser is fixed) with the data id as a check
(1,221/1,237 copies identified; all consistent). Fallback for pre-Waypoint saves:
`platform:slot:name` (1 identity here; Medium-low, cannot split two same-named runs). Names,
difficulty, mode and `UsedPlatforms` are attributes, never identity.

**Version (S1/S6) - High.** Storage version = member file keyed by data sha256 (meta kept as an
attribute; editors and promote can change meta or data alone). User-facing version = captured
pair, labelled by its newer member. Show "no-progress" steps (same play time) as such: 18 of
179 known steps were touches/edits, and showing them as full versions is noise.

**Lineage (S3) - three tiers.**
1. *Recorded* edges from app operations (2.8 list) - exact, High, once confirmed by a scan.
2. *Observed* edges from the live journal (consecutive hashes of one live slot) - High
   (misses a rollback that happens and is played over between two scans).
3. *Inferred* edges for legacy data - parent = most-progressed earlier version passing meta +
   core counters + visited log. High for linear history (64/64), Medium for detecting forks
   (catches inversions and both-progressed forks; blind to touch/edit forks), Low-Medium for
   placing an uncaptured fork in time (discovery-record bound). Keep inferred edges marked as
   inferred in the UI.

**Storage (S6).** Folder-per-backup is 6-7x larger than its distinct content; the thumbnail
cache alone is half the vault. Any redesign should (a) stop copying `cache` or store it
deduplicated, (b) index versions by hash so the catalog can answer "where else does this
version live", and (c) make pruning and deletion refuse to drop the last copy of a version.

**Live-slot ops (S5).** Writes can be undone by Steam Cloud (three cases); "move to live" must
verify after the next scan, not only after the write. The in-place entry that is the live
`wgs` folder must be recognised as live (resolve paths), or every Xbox save appears twice.

**UI (S7).** 30 lines, 26 live; 3 lines live on both platforms with different versions;
per-line version counts 1-20 (pair snapshots). Cross-platform lines need an
ahead/behind/diverged state, not just two badges (Creative Primary is diverged; The
Cartographers and Exobiology1 are "Xbox behind Steam").

**Notes and facts (S2/S4).** The owner encodes quicksilver in names and they go stale; an
indexed "quicksilver" fact plus notes per version would replace that habit. A version diff
("inventory amounts changed in 46 slots") explains editor sessions that nothing else can.

**Fix before building on it.** The Xbox meta parser (id, time, name, difficulty) and the Xbox
"newest member" order (2.3) feed identity, sorting and the live marker; R1 and R8 flag it too.

## 4. Open questions and candidate options

1. **One line per playthrough, or per playthrough and platform?**
   (a) One line, two live badges and an ahead/behind/diverged state - matches the owner's "all
   past versions of a save-file are 1 line"; needs the lineage tiers above. (b) One line per
   (id, platform) - simpler, no cross-platform reasoning; splits Creative Primary and three
   others into two lines each.
2. **What the expanded line lists.** (a) Pair snapshots (106; the restorable unit). (b) Current
   members only (105; simplest to read). (c) Member files (195; exact but doubles the list).
3. **No-progress versions (touches, editor edits).** (a) Show all. (b) Collapse into the
   previous version as "re-saved / edited, no play". (c) Hide unless they are the only copy of
   different bytes (editor edits are different bytes and may matter).
4. **History for the existing vault.** (a) Flat, time-ordered list until lineage is recorded
   (no wrong edges; no tree for old data). (b) Meta-only inferred tree (same result as content
   on this vault; instant). (c) Meta + content inferred tree (needs the content index; catches
   both-progressed forks later). All three must mark inferred edges.
5. **Where lineage is recorded from now on.** (a) Extend `oplog.jsonl` with the fields in 2.8.
   (b) A separate lineage/version store (e.g. per-version records keyed by hash with parent
   links) fed by ops and the live journal. (c) Both: oplog for undo, store for history.
6. **Storage.** (a) Keep folders, add a hash index (no migration; no space saved). (b)
   Content-addressed version store for new captures, old folders indexed in place (saves
   space going forward). (c) Migrate everything (~3.4 GB -> ~0.5 GB; one-time risk on the
   owner's real data). Separately: exclude, dedupe or keep the thumbnail `cache`.
7. **Retention.** (a) Keep today's keep-20 snapshot pruning. (b) Version-aware pruning that
   never deletes the last copy of a version. (c) Owner pins versions; only unpinned duplicates
   are pruned.
8. **Pre-Waypoint saves without id.** (a) `platform:slot:name` as today. (b) A content
   fingerprint (e.g. oldest discovery records / base addresses; the oldest discovery record
   is stable across versions for 25 of 30 identities here). (c) Let the owner merge or split
   lines by hand.
9. **Saves edited outside the game.** (a) Treat as normal versions. (b) Flag "edited outside
   the game" when meta time and play time are unchanged but bytes differ (cheap), with the
   content diff on demand.
10. **Names as notes.** (a) Leave names alone, add notes and a quicksilver column. (b) Also
    warn when a name's `-NNNqs` disagrees with the save's quicksilver.

## Appendix A - scripts and data (all under `<R7>`)

| File | Purpose |
|---|---|
| `inventory.py` -> `inventory.json`, `folders.json` | read-only scan of live + every catalog entry: meta fields, hashes, data id |
| `common.py` | loader; identity and best-timestamp rules |
| `identity_table.md`, `identities.txt` | per-identity tables |
| `slot_timeline.txt` | slot occupancy per capture, Steam and Xbox |
| `versions.py <id-prefix>` | version list of one identity |
| `oplog_edges.py` | oplog -> slot edges with hashes |
| `features.py` -> `features\<sha256>.json`, `features.log` | decode + de-obfuscate each distinct version once; lineage features |
| `jdiff.py <sha-prefix> <sha-prefix>` | structural diff of two versions |
| `lineage_lib.py`, `lineage_explore*.py`, `lineage_eval.py`, `lineage_tree.py`, `infer_tree.py` | ground truth, signals, classifier, tree inference |
| `costs.py` -> `costs.json` | pure-Python hash/decode timings |

Run with `PYTHONUTF8=1 uv run --with lz4 python <script>` from `C:\Devel\NMSVault` (the lz4
package is research-only; `costs.py` uses only the project's decoder). Scratchpad keys (parent
session): `findings.R7-dedupe`, `-xbox-meta`, `-storage`, `-oplog-branching`, `-content`,
`-lineage-signals`, `-names`, `-costs-tree`.
