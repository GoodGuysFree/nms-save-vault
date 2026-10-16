# R8 - Slot mechanics, move-to-live semantics, transfers, cloud sync

Status: research, wave 1. Author: R8 subagent, 2026-10-16. Feeds S5 (live-slot operations),
and through it S6 (storage), S7 (UI) and S8 (phasing).

Scope: (a) how the game treats slots; (b) account-level data; (c) what today's operations do,
plus draft semantics for the organizer's operations; (d) transfers between slots, accounts and
platforms; (e) cloud sync.

Method and safety:
- Sources: the code; libNOM.io source (master `652aabb`, 2025-10-28); the NomNom CHANGELOG
  (its public repo now holds only CHANGELOG and README, latest 7.00.2 of 2026-09-28); web
  sources (cited inline); and read-only probes of the live folders and the vault.
- The probes open files for reading only. No nmsvault command and no core write function was
  run, and nothing was written outside the R8 scratch folder. The vault's index files hash the
  same as at R1's baseline (catalog.json `a3a58014...a045b9`, oplog.jsonl `29b3ece9...598aaa`).
- Scripts and outputs are in `<scratchpad>\R8\scripts\*.py` and `<scratchpad>\R8\*.out.txt`,
  where `<scratchpad>` = `%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\<session>\scratchpad`.
  The Evidence index at the end lists them.
- Ids are classified, never printed. selfST = the owner's own steamid64, selfXB = the owner's
  own XUID. Playthroughs are named by the last 4 hex digits of their id (`..1C01`).
- R1 (`R1-core-audit.md`, sections 2d-2e) already inventories every operation with
  experiments E1-E11. This report cites R1 rather than repeating it, and adds the game-side,
  cloud and transfer facts.

Tags: VERIFIED = tested or read by me; SOURCED = cited; INFERRED = reasoning.

## 1. TL;DR

1. **The slot model holds.** There are 15 slots, each with A (auto-save) and B (restore
   point), since Frontiers 3.60 (2021; 5 slots before that). The member with the newer meta
   timestamp loads, and the other can be picked in game. The slot number does not bind a
   playthrough: the game kept writing saves after they were re-slotted.
2. **The vault's playthrough id is the game's own `CommonStateData.SaveUniversalId`**
   (obfuscated `<h0.WmU`), the cross-save id. That is the natural key for "one line". Forks
   share it.
3. **accountdata holds settings, seen/unlock lists and account-wide rewards.** It has no
   "last slot" pointer. The Steam and Xbox copies are byte-identical, because cross-save syncs
   them. Organizer moves never touch it. Today's full restore and undo silently roll it back.
4. **Xbox meta parsing is broken for every live Xbox save.** Timestamp (0x120) and
   SaveUniversalId (0x118) read as 0 in 30/30 metas; names are lost on all 8 metas of 296 B; the
   Xbox promote timestamp setter is a no-op on 30/30. This must be fixed before any Xbox
   organizer operation.
5. **Mixed slots (A and B from different playthroughs) are a real hazard.** Today's
   repopulate creates them (R1 E1). Every organizer operation must write both members.
6. **Steam Cloud keeps overwrites and undoes deletions.** Non-game overwrites stand in Steam's
   manifest (`remotecache.vdf`) with matching SHA-1 and were not reverted. Deletions
   made while the game is closed come back (slot 15 was cleared twice in one day). Design every
   operation as overwrites. "Free a slot" and "undo a fill into an empty slot" cannot stick
   under Cloud without the manual procedure.
7. **Xbox sync is per save.** A write marks the record Modified or Created in
   `containers.index`. The platform uploads 10-30 s after the game exits and shows a conflict
   dialog if the cloud copy changed too. A Deleted record propagates. The quota is 256 MB per
   title (67.7 MB used).
8. **The game re-owns a save itself.** `CommonStateData.UsedDiscoveryOwnersV2` lists every
   platform identity that has played the save. On first play under a new identity, the game
   re-owned bases and discoveries: `..1C01` changed 21/21 bases and 782/782 discoveries from
   Steam to Xbox with zero extra play time.
9. **libNOM.io/NomNom transfers are not verbatim.** They re-serialise the JSON and rewrite the
   PLATFORM token and the UID/LID/USN/PTK owners of discoveries, bases, settlements and ByteBeat
   songs. Their Copy deletes the destination member when the source member is missing, so they
   never create mixed slots.
10. **Steam<->Xbox (PC) can stay within the verbatim rule for Worlds-era saves.** The data
    stream is identical, the PLATFORM token is "Win|Final" on both, the meta converts field by
    field (XXTEA 432 B <-> plaintext 360/296 B), and the game migrates ownership. Old Xbox
    formats cannot be moved verbatim.
11. **Cross-account copies on one platform are unguarded today.** A full restore can also
    write the wrong `steam_autocloud.vdf` or Xbox `containers.index`. The account must become a
    first-class dimension.
12. **Proposed semantics (section 4).** `Place(V, S)` = preserve S durably, snapshot, write
    both members (V newest by every signal), validate, then write the journal and the lineage
    binding. Undo is slot-scoped and non-destructive.

## 2. Findings

### 2a. How the game treats slots

**A1. 15 slots x 2 members; history.** SOURCED + VERIFIED

- Patch 1.38 (October 2017) introduced 5 slots, each with an automatic and a manual sub-save
  ([dualshockers](https://www.dualshockers.com/no-mans-sky-patch-overhauls-games-save-system/),
  [pcgamesn](https://pcgamesn.com/no-mans-sky/no-mans-sky-atlas-rises-patch-138-save-system)).
- Frontiers 3.60 (September 2021): "The number of possible save slots has been increased from
  5 to 15, and the save select screen now supports scrolling"
  ([official notes](https://www.nomanssky.com/frontiers-update/)).
  - The same update introduced save streaming (the `0xFEEDA1E5` chunk format).
  - It also auto-backed-up the old saves to `strbackup`.
- No later change was found.
  - libNOM.io still uses `MAX_SAVE_SLOTS = 15` "in the latest game version"
    (`libNOM.io/Global/Constants.cs:38`).
  - The vault does the same (`formats.py:115`).
  - PlayStation without streaming has 5 (`PlatformPlaystation/PlatformPlaystation.cs:14`).
- Live today: Steam slots 2-15 are occupied and slot 1 is empty. Xbox has 15/15 occupied
  (`steam_meta_probe.py`, `xbox_meta_probe.py`).
- A second, empty `st_<steamid64>` folder exists on this PC (no files).

**A2. Which save loads.** SOURCED + INFERRED

- By default, the newer of the two members loads.
  - libNOM.io takes a Worlds-era save's time from the meta timestamp
    (`Platform/Platform_Initialize.cs:288-297`), and DESIGN.md records the same rule.
  - Players report that the newest loads and that both saves can be picked from the in-game
    menu ([thread 1](https://steamcommunity.com/app/275850/discussions/0/1639794651176038853),
    [thread 2](https://steamcommunity.com/app/275850/discussions/2/601902145259737793)).
- accountdata has no "continue this slot" pointer (B1), so "Continue" is not something the
  vault needs to set.
- Which slot the save screen highlights first was not determined (that needs the game
  running). INFERRED to be irrelevant to correctness.
- Roles are positional (`formats.py:119-126`): the auto-save always writes A (`f = 2k-1`), and
  restore points write B (`f = 2k`).

**A3. The slot number does not bind a playthrough.** VERIFIED (`reslot_evidence.py`)

- 'Creative Primary' (`..1C01`) has copies in slot 1 dated 2026-03-04, then 56 game-written
  copies in slot 13 from 2026-06-27 to 2026-10-08.
- `..AF84` was re-slotted 15 -> 1 by the vault's repopulate on 2026-09-20 (oplog). The game
  wrote a new slot-1 version the same day.
- Only two things are slot-bound, and both are handled today:
  - the Steam meta's XXTEA key: ordinal = file number + 1 (`meta.py:28-32`);
  - the Xbox container identifier `Slot<N>Auto` / `Slot<N>Manual`.
- Steam's `cache/*.DDS` files are location thumbnails named
  `<address>.<mode>.<place>.<steamid64>.DDS` (181 files). They are keyed by location and
  account, not by slot, so slot moves need not touch them.

**A4. A and B from different playthroughs ("mixed slot").** VERIFIED (code, R1 E1) + INFERRED
(game effect)

- **How it arises.** `repopulate_slot` skips a member the source lacks and leaves the
  destination's member in place (`operations.py:328-334` Steam, `500-504` Xbox).
  - R1 E1 reproduced it.
  - On Xbox the leftover member was newer, so it is the one the game would load.
- **The game made a related state itself.** Xbox slot 4 holds `..5151` with:
  - B (2025-01-10 09:06) owned by the Xbox identity;
  - A (09:17) owned by the Steam identity (`xplat_history2.py`).
  - INFERRED: a cross-save download replaced one member only. It is the same playthrough
    there, so it is harmless.
- **Game-side effect of a true mixed slot.** INFERRED from the positional roles:
  - the slot shows its newer member, and the other is still offered in game;
  - loading the other one continues the other playthrough, and its next auto-save overwrites
    A;
  - the first playthrough's latest auto-save is then lost unless it is in the vault.
- **libNOM.io avoids mixed slots.** `Copy` deletes the destination member when the source
  member does not exist (`Platform/Platform_FileOperation.cs:18-21`). NomNom 3.1.0.16 says
  "Copy/Move/Swap is still triggered on slots but the actual action is decided for each save".
- **Live today:** no mixed slot exists on either platform (A/B ids agree in both probes).

**A5. Xbox "newest" and identity are misread today.** VERIFIED (`xbox_meta_probe.py`,
`promote_noop_check.py`, `xbox_names.py`; R7 found the same)

- **What the metas carry.** The live wgs has 22 metas of 360 B (Worlds II, format 0x7D4) and 8
  of 296 B (Worlds I length).
  - Every one has a u32 timestamp at 0x120 and a u64 SaveUniversalId at 0x118.
  - The SaveUniversalId matches the data's id in 28/28 decodable saves.
- **How the parser misreads them.** `msstore.parse_ms_meta` gates those fields on the Steam
  lengths 384/432 (`msstore.py:329-338`):
  - timestamp and id read as 0 for 30/30;
  - the name is empty for 8/8 metas of 296 B, although the raw names ('The Cartographers',
    'CrossSaveTest', 'Exobiology1') are present at 0x14.
- **Xbox promote does not change the meta.**
  - `set_ms_meta_timestamp` requires `len >= 384` (`msstore.py:346`), so it is a no-op on
    30/30.
  - Xbox promote (`operations.py:454-459`) therefore moves only the `containers.index`
    FILETIME.
  - The owner's only Xbox promote (slot 6B, 2026-10-02 12:35) was undone 25 minutes later. That
    is consistent with it not taking effect, but not proven.
- **libNOM.io reference.**
  - Microsoft meta lengths are 24 / 280 / 296 / 360 B
    (`PlatformMicrosoft/PlatformMicrosoft.cs:22-25`).
  - The layout is in `PlatformMicrosoft_Initialize.cs:278-311`.
  - libNOM.io writes the timestamp for every platform (`Platform/Platform_Write.cs:232-246`).
- **The `containers.index` FILETIME is the local write or sync time, not the save time.**
  - It is later than the meta timestamp by 9-480 s normally, and by 30 h (Slot4), 103 days
    (Slot6Manual) or 484 days (Slot13) in places.
  - Slots 8, 10 and 15 share one FILETIME (2026-07-16 05:08:06), which is a batch download.
- **Consequence:** the app's Xbox "newest" falls back to that FILETIME
  (`savedir.py:85-89`). R7 measured 48 of 112 A/B pairs where it disagrees with the meta
  timestamp.

### 2b. Account-level data

**B1. What accountdata holds.** VERIFIED (`accountdata_probe.py`, libMBIN 7.07 mapping via
R5's mapper, 0 unknown keys)

- **File formats:**
  - Steam `accountdata.hg` is plain JSON (105,904 B, not chunked);
  - its meta `mf_accountdata.hg` is 432 B, keyed with ordinal 1;
  - the Xbox `AccountData` blob is a single LZ4 block (36,069 B) of the same JSON.
- **Content:** `Version` plus `UserSettingsData` (110 keys):
  - settings: controls, audio, language, gyro, VR, UI colour, custom bindings;
  - BlockList;
  - Seen lists: Substances 108, Technologies 315, Products 4123, WikiTopics 58;
  - unlocks: UnlockedWikiTopics 58, UnlockedTitles 286, UnlockedSpecials 764,
    **UnlockedSeasonRewards 289**, UnlockedTwitchRewards 235, UnlockedPlatformRewards 0;
  - cross-play and cross-save flags, BaseSharingMode, LastSeenCommunityMission.
- It has no last-played slot, no save list and no per-save state.
- The owner's flags now: CrossSaves = False, CrossSavesAutoUploads = True, CrossPlatform = True
  (`acct_flags.py`).

**B2. Steam and Xbox account data are byte-identical.** VERIFIED (`acct_compare.py`)

- Both are sha256 `07fa659a...`, 105,904 B decompressed.
- Cross-save carries account data between platforms.

**B3. What is account-wide by design.** SOURCED

- Non-consumable Quicksilver purchases have been account-wide since Frontiers: they are "no
  longer save slot specific" ([notes](https://www.nomanssky.com/frontiers-update/)).
- Expedition phase rewards can be claimed on any save from the Quicksilver vendor once earned
  ([community](https://steamcommunity.com/app/275850/discussions/0/6257258483698562423);
  `UnlockedSeasonRewards` above).
- Per save, by contrast (R5 structure dump):
  - `CommonStateData`: SaveName, SaveUniversalId, UsedPlatforms, UsedDiscoveryOwnersV2, season
    data, EarnedSeasonSpecialRewards;
  - the `BaseContext` / `ExpeditionContext` player state;
  - `DiscoveryManagerData`.

**B4. Moving saves in and out of live loses nothing account-level, but full restore and undo
roll it back.** VERIFIED (code)

- Repopulate, extract, clear and promote touch only slot files (`operations.py:280-400`,
  `701-760`).
- `restore_full`, and therefore `undo_last`, copy every `*.hg` including accountdata
  (`operations.py:70-72`, `258-264`). On Xbox the whole tree is mirrored, including the
  AccountData container (`537-554`).
- So a full restore or an undo rolls unlocks and settings back to the snapshot's.
- INFERRED: under Steam Cloud and cross-save, that rollback is then uploaded.

**B5. Expedition saves.** SOURCED (community) + VERIFIED (data)

- Xbox slot 12 is an in-progress expedition (GameMode 6, Season 47, ActiveContext "Season").
  Steam slot 15 'Exp23 Our Journey Continues' is a converted one (GameMode 1).
- An expedition save converts to a normal save when every milestone is done or time runs out
  ([thread](https://steamcommunity.com/app/275850/discussions/0/6257258483698562423)).
- So placing an older version of an in-progress expedition after its season has ended yields a
  converted save. Rewards already claimed stay claimed, because they are account-level. Warn;
  do not block.

### 2c. What the app does today (Steam vs Xbox)

R1 section 2d (D1-D7) has the full inventory, and R1 E1, E2, E3, E6 and E11 cover mixed slots,
single-level undo, the unchecked undo target, evict = extract + repopulate, and the
failed-write undo gap. The table below gives only the slot, meta, time and cloud mechanics this
topic needs. Line numbers are `operations.py` unless marked.

| Op | Steam: files and meta | Steam: mtime | Xbox: index, blobs, meta | Xbox: times | Snapshot / oplog |
|---|---|---|---|---|---|
| `repopulate_slot` | copies each present source member's data verbatim; meta re-keyed if the ordinal differs (337-340); `_validate_pair` before writing | Cloud on: write time; off: the source data's mtime (350-357) | `write_save` per present member: new GUID blobs, `container.N+1`, index rewritten last, record Modified (Created if new), superseded files deleted, xgs mirrored (`msstore.py:630-678`); meta verbatim | FILETIME = source `effective_timestamp`, today the source's FILETIME (A5), or now if 0 (507-509); blob mtimes the same (`msstore.py:610-612`) | snapshot of the whole live folder first; oplog after success |
| `extract_slot` | copies both members (if both files exist) to `extracts/<id>` | `copy2` keeps mtimes | copies the GUID dirs verbatim and builds a minimal `containers.index` with only those records (466-489) | - | no snapshot; oplog |
| `promote_member` | meta ts = max(A, B) + 1, re-encrypted with the same ordinal | meta and data mtime = the new ts **always, including under Cloud** (395-397) | `write_save` of the same bytes with the meta ts bumped, a no-op on real metas (A5) | FILETIME = new ts | snapshot; oplog |
| `clear_slot` | unlinks both members (719-727) | - | `delete_save` x2: record kept and flagged Deleted, blob dir and xgs dir removed (`msstore.py:681-714`) | FILETIME = now | snapshot; oplog |
| `restore_full` | overwrites all `*.hg`, **including accountdata**, plus the vdf; mirror deletes `*.hg` absent from the backup (258-270) | Cloud on: write time; off: the backup's mtime | reconciles the whole tree, including `containers.index`, from the backup; `mark_modified`; `mirror_xgs` (525-560) | as copied | snapshot; oplog |
| `undo_last` | `restore_full(last snapshot, mirror)` into whatever folder is passed | as restore | as restore | as restore | snapshot; oplog |

Gaps that matter for the organizer:

- **G1 One-member source -> mixed slot** (A4).
- **G2 Evicted content survives only in a prunable snapshot** (`SNAPSHOT_RETENTION = 20`,
  31, 148-159) unless it is separately extracted.
- **G3 Undo is whole-folder, single-level, and does not check its target.**
  - It reverts every slot and the accountdata.
  - Under Steam Cloud, the files it deletes come back (2e).
- **G4 The oplog has free-text detail**, with no target folder and no hashes (958-966).
- **G5 Steam promote stamps an old mtime under Cloud** (395-397). By the project's own
  reasoning (commit `42f0d2d`), Steam may then revert it.
- **G6 Xbox repopulate stamps the source's FILETIME** (an old time) as the new record's time
  (507-509). The Steam lesson (stamp the write time) is not applied on Xbox. The effect on Xbox
  sync is unknown.
- **G7 There is no account guard.** `_guard_same_platform` compares only the platform (97-100).
  - A Steam full restore copies the backup's `steam_autocloud.vdf`, which holds a Steam
    accountid (262-264; format VERIFIED).
  - An Xbox full restore writes the backup's `containers.index`, with its process id and account
    id, into the target account (537-554).
- **G8 There is no write-ahead record.** A crash between member writes leaves a half-written
  slot whose only trace is the snapshot (R1 E11 shows the related undo gap).

### 2d. Transfers

**D1. libNOM.io file operations (what NomNom does).** SOURCED (libNOM.io `652aabb`)

- **Copy works per save (container), not per slot.**
  - If the source save does not exist, Copy deletes the destination save.
  - Otherwise it loads the JSON into the destination and copies the "important properties" and
    the container extra (sizes, play time, name, summary, difficulty, last write time).
  - It then calls `Write(Destination, Source.LastWriteTime)`
    (`Platform/Platform_FileOperation.cs:15-43`).
  - `Write` re-serialises and recompresses the JSON (`Platform/Platform_Write.cs`
    PrepareData/CreateData/CompressData, about lines 56-120). It is **not verbatim**.
- **Move and Swap.** `Move` = Copy, then Delete the source (89-93). `Swap` deep-copies the
  destination and overwrites both ways, or does a Move if one side is empty (103-148).
- **Times.** With the default `SetLastWriteTime = true` (`Settings/PlatformSettings.cs:32`),
  the destination's meta timestamp and file times are set to the **source's** last write time
  (`Platform_Write.cs:29-46`). Copies keep their original age, the opposite of this project's
  Steam Cloud rule.
- **Microsoft writer.** It writes new GUID blob names and moves Synced -> Modified (Created
  stays Created). The record FILETIME is the save time, the global index time is the write
  time, and `containers.index` is rewritten (`PlatformMicrosoft/PlatformMicrosoft_Write.cs:11-54`,
  `160-193`, `209-271`).
- **Microsoft delete.** It removes the blob dir, flags the record Deleted and rewrites the index
  (`PlatformMicrosoft_FileOperation.cs:11-39`). The vault's `msstore` writer follows the same
  scheme (`msstore.py:559-714`).
- **README notes.**
  - Microsoft Store: "Reloading of modified saves while the game is running does not work".
  - Xbox consoles go through the Microsoft Store cloud sync, which is "triggered short after you
    close the game" (`README.md:69`, `77`).
  - Restart needed to apply: Steam AccountOnly, Microsoft Always (`PlatformSteam_Property.cs:34`,
    `PlatformMicrosoft_Property.cs:26`).

**D2. libNOM.io Transfer (to another platform or account).** SOURCED

- **Collecting the source.** `GetSourceTransferData` collects the source identity (UID, LID,
  USN, PTK). It also lists the source user's HomePlanetBase and FreighterBase bases, each with a
  "transfer?" decision (`Platform_FileOperation_Transfer.cs:98-127`).
- **Refusal.** Transfer refuses unless both identities are complete (144-145).
- **Per save:** copy the JSON, create a fresh platform extra (the meta is rebuilt for the
  destination), run `TransferOwnership`, and write (147-176).
- **`TransferOwnership` (188-208) rewrites:**
  - the root `PLATFORM` (`8>q`) to the destination architecture;
  - discovery records owned by the source UID;
  - the selected bases' `Owner`;
  - ByteBeat songs authored by the source UID;
  - settlements owned by the source UID.

  Each owner gets UID = the destination UID, plus LID, USN and PTK when they are non-empty
  (228-237).
- **JSON paths** (`Global/Constants.cs`); contexts are `vLc` (BaseContext) and `2YS`
  (ExpeditionContext):

  | Item | Path | Line |
  |---|---|---|
  | PLATFORM | `8>q` | 83 |
  | Discovery owners | `fDu.ETO.OsQ.?fB..[?(@.K7E == uid)]` | 89 |
  | Bases | `{ctx}.6f=.F?0[*]` | 124 |
  | Settlements | `{ctx}.6f=.GQA..[?(@.K7E == uid)]` | 125 |
  | ByteBeat songs | `<h0.8iI.ON4[?(@.m7b == uid)]` | 88 |
  | Owner keys | `K7E`=UID, `f5Q`=LID, `V?:`=USN, `D6b`=PTK | 157-160 |

- **Steam identity.** UID = LID = the steamid64 from the `st_` folder name. USN comes from the
  save or the Steam Web API. PTK is "ST" (`PlatformSteam_UserIdentification.cs:18-33`,
  `PlatformSteam_Property.cs:59-64`).
- **Microsoft identity.** UID = the decimal value of the wgs folder's 16-hex prefix
  (`PlatformMicrosoft_Initialize.cs:39-40`). PTK is "XB" and the architecture is "XB1|Final"
  (`PlatformMicrosoft_Property.cs:44-49`).
- NomNom has offered "transfer to another platform ... or to a different account within the
  same platform" since 3.5.0.17 (2021-08-25, NomNom CHANGELOG). Its source is no longer public.

**D3. The community procedure (before cross-save).** SOURCED

- A 2022 Game Pass -> Steam guide
  ([gamepretty](https://gamepretty.com/no-mans-sky-how-to-transfer-xbox-game-pass-saves-to-steam/)):
  1. Export the Xbox JSON and create a blank Steam save.
  2. Replace every UID and USN with the Steam values.
  3. Replace the Xbox LID "00000000000000000001" with the Steam UID.
  4. Replace "XB" with "ST".
- A 2021 thread says that without this, bases and discoveries "would still be owned by the
  gamepass account according to HG server". It also reports that pets and a creative save did
  not transfer ([thread](https://steamcommunity.com/app/275850/discussions/0/3117025249763825238)).
- Hello Games does not document what breaks without the rewrite. Community and editor
  consensus: base edit rights, settlement ownership and discovery ownership.

**D4. The game's own cross-save.** SOURCED

- Patch 5.25 (November 2024) added Cross-Save. Players link their platform accounts at
  cloud.nomanssky.com, then "upload and download saves from any connected platform, via the
  in-game save screen" ([notes](https://www.nomanssky.com/2024/11/cross-save-ps5-pro-normandy/)).
  libNOM.io marks it as version 525, TheCursedWithCrossSave (`Enums/GameVersionEnum.cs:67`).
- Rules ([cross-save page](https://cloud.nomanssky.com/cross-save)):
  - at most 5 saves are held in cross-platform storage;
  - the most recent save auto-uploads;
  - downloads go through the in-game Cross-Save Manager;
  - "If the game detects a mismatch between the size and timestamp of a saved file", the
    player chooses which to keep.

**D5. What the game does to ownership when a save changes identity.** VERIFIED
(`ownership_probe.py`, `pair_compare.py`, `xplat_history2.py`, `xplat_history3.py`; real data,
read-only)

- **The vault's playthrough id is `CommonStateData.SaveUniversalId`** (`<h0.WmU`, mapping
  v7.07). It holds the same value as the Steam meta at 0x15C and the Xbox meta at 0x118.
- **The save keeps an identity list.** `CommonStateData.UsedDiscoveryOwnersV2` (`<h0.F=J`) is a
  list of `{LID f5Q, UID K7E, USN V?:, PTK D6b, TS 3I1}`, one per platform identity that has
  played the save, in first-use order.
- `UsedPlatforms` (`xgu`) is `["Win"]` on every copy examined (all copies of the 4
  cross-platform playthroughs, on both platforms).
- **`..1C01` 'Creative Primary': a full re-own with no play time.**
  - Xbox 8B (2026-02-14, play time 139,170 s) lists `[selfST]`. Its 21 own bases and 782 own
    discoveries are selfST: a cross-save delivery not yet played.
  - Xbox 8A (2026-07-16, **same play time 139,170 s**) lists `[selfST, selfXB]`.
  - All 21 bases, at the same locations, and all 782 discoveries are now XB/selfXB; the empty
    PTKs were filled with "XB".
- **`..D19E` 'The Cartographers'.** The Xbox copies list `[selfXB]` and own as XB. The later
  Steam copies list `[selfXB, selfST]` and own as ST (2 bases, 26 discoveries).
- **`..A74F`: migration is not always complete.**
  - The Xbox copies list `[selfXB, selfST]` but hold 2 ST and 2 XB own bases, and ST
    discoveries.
  - The rule is not fully known. One hypothesis: it runs only when a new identity is appended.
- **PLATFORM token.** `8>q` is "Win|Final" on the newest member of all 14 occupied live Steam
  slots and of all 14 decodable live Xbox slots.
  - The PC Game Pass build writes the same token as Steam, so libNOM.io's "XB1|Final" is stale
    for PC.
  - Saves written on an Xbox console were not checked.
- **Conclusion:** identity fix-up is a built-in game step, tied to the identity list in the
  save. A verbatim copy that lands under another identity behaves like a cross-save download:
  it shows the old owner until first played, and then the game re-owns it.
- **UNTESTED:**
  1. a copy that did not come through Hello Games' servers;
  2. a save whose previous identities belong to a different person.

**D6. Data format compatibility.** VERIFIED + SOURCED

- **Same stream:** Steam since Frontiers 3.60 and Xbox since Worlds 5.00 use the same
  `0xFEEDA1E5` chunk stream (DESIGN.md; `PlatformMicrosoft_Write.cs:68-70`). All 14 decodable
  live Xbox saves decoded with the Steam chunk reader.
- **Older formats differ:**
  - Xbox before 4.52: a single LZ4 block;
  - Xbox 4.52 to 5.00: "HGSAVEV2" chunks (`PlatformMicrosoft_Read.cs:30-60`);
  - Steam before Frontiers: uncompressed JSON (`Platform/Platform_Read.cs:220-223`).
- Live Xbox slot 13 ('Voyagers') is not a stream. R7 counts 16 such Xbox copies.
- **Consequence:** only stream-format saves can move verbatim. Older ones need decompression
  and recompression, which breaks the project rule, or one load-and-save in the current game.

**D7. Meta conversion Steam <-> Xbox.** VERIFIED (`meta_layout_compare.py` on real metas) +
SOURCED (libNOM.io layouts)

| Field | Steam (decrypted, 432 B) | Xbox (plaintext, 360 B) |
|---|---|---|
| header, format | 0x00 `0xEEEEEEBE`, 0x04 format | - (format at 0x124) |
| hash area | 0x08-0x37, zero since format 2 (`PlatformSteam_Write.cs:39-42`; zero on the real meta) | - |
| SizeDecompressed | 0x38 | 0x10 (holds the compressed size between 4.52 and 5.50) |
| SizeDisk | 0x3C | - |
| BaseVersion, GameMode, Season, TotalPlayTime | 0x44, 0x48, 0x4A, 0x4C | 0x00, 0x04, 0x06, 0x08 |
| SaveName, SaveSummary (128 B each) | 0x58, 0xD8 | 0x14, 0x94 |
| Difficulty (u32; libNOM reads 1 byte) | 0x158 | 0x114 |
| SaveUniversalId (u64) | 0x15C | 0x118 |
| Timestamp (u32) | 0x164 | 0x120 |
| Meta format | 0x168 | 0x124 |
| Difficulty tag (64 B) | 0x16C | 0x128 |
| Total length | 0x1B0 (with 4 empty bytes at the end) | 0x168 |

- Worlds I lengths are Steam 384 B and Xbox 296 B.
- **Steam -> Xbox:** decrypt with the source ordinal, map the fields, and write plaintext.
- **Xbox -> Steam:** map the fields, set SizeDisk = data length and SizeDecompressed = chunk sum,
  then encrypt with the destination ordinal.
- Only the meta is transformed: the same class of change as today's re-key.
- Side note: Xbox slot 12's difficulty reads 258 (0x102) as a u32, but libNOM.io uses only the
  low byte (2 = Normal). The vault's u32 read gives no label for it (minor, display only).

**D8. Xbox platform constraints.** SOURCED
([MS devdocs, XGameSaveFiles](https://devdocs.xbox.com/build/core-features/common/game-save/xgamesavefiles))

- The PC path is `...\SystemAppData\xgs\<HexXuid>_<SCID>\`, which is the xgs mirror DESIGN.md
  describes.
- Uploads happen "around 10-30 seconds after the title ends". A conflict brings up a dialog.
- "Only one device at a time can access the user's data for a specific title".
- Quota: 256 MB per user per title; 64 MB per file.
- Measured live usage: wgs 67.7 MB (94 files), xgs 67.6 MB (62 files), about 3.8x headroom.

### 2e. Cloud sync

**E1. Steam Cloud.** SOURCED + VERIFIED

- **Sync timing.** Auto-Cloud syncs "when the application launches and exits". Files "created,
  modified, deleted" are replicated after the game exits
  ([Steamworks](https://partner.steamgames.com/doc/features/cloud)).
- **The per-file manifest:** `<Steam>\userdata\<accountid32>\275850\remotecache.vdf`
  (read-only probe; `remotecache_probe.py`).
  - ChangeNumber 2137, 60 entries.
  - Each entry has: root (4 = WinAppDataRoaming), size, localtime, time, remotetime, sha
    (SHA-1), syncstate 1, persiststate 0, platformstosync2 -1.
  - All 58 save-folder files match disk: same SHA-1, and localtime == mtime.
  - `steam_autocloud.vdf` itself is not listed. It holds only `accountid` (format VERIFIED).
  - Slot 1's files are absent. INFERRED: a deletion done in-session after the 2026-09-20
    repopulate.
- **Overwrites made outside the game stand.**
  - Slots 3A, 5A, 6A and 10A have mtimes of 2026-09-29 13:44-13:45, but their meta timestamps
    are months older, so the game did not write them; that writer is not in the vault's oplog.
  - They sit in the manifest with matching SHA-1 and localtime: accepted as the synced state,
    not reverted.
  - Whether Steam uploaded them from here or downloaded the same bytes is INFERRED (upload is
    likelier).
- **Deletions made while the game is closed come back at the next launch.**
  - The oplog has `clear_slot 15` twice on 2026-09-17 (10:34 and 17:12), and commit `42f0d2d`
    records the cause.
  - Community reports agree, and add that deletions made while the game is running do
    propagate ([thread](https://steamcommunity.com/discussions/forum/1/3183488224877619922)).
- **Conflict detection.** At launch Steam compares local files with its manifest and the cloud;
  community reports say by time and size
  ([thread](https://steamcommunity.com/discussions/forum/1/4511002848512134204)).
  - The project's rule "stamp the write time under Cloud" (`operations.py:253-257`, `348-350`)
    follows from this.
  - Its other half, "an older mtime gets reverted", is reasoning, not observation: commit
    `42f0d2d` says "could undo a restore".

**E2. Xbox / Game Pass sync.** SOURCED + VERIFIED

- Each record in `containers.index` has a sync state: 1 Synced, 2 Modified, 3 Deleted,
  5 Created (`formats.py:140-147`, libNOM enums). All 31 live records are Synced (index state
  3).
- A Deleted record is the cloud's instruction to delete. The vault keeps the record
  (`msstore.py:681-714`), as libNOM.io does.
- The platform uploads after the game exits and raises a conflict dialog if both sides changed
  (D8).
- INFERRED: if the owner also plays on an Xbox console or another PC, a vault write followed by
  a session there produces a conflict choice at the next PC launch.

**E3. Hello Games cross-save.** SOURCED

- It is separate from Steam and Xbox cloud.
- When enabled, it auto-uploads the most recent save, and it raises a conflict on a size or
  timestamp mismatch (D4).
- The owner has CrossSaves off now (B1).
- INFERRED: if it were on, placing an older version of an uploaded playthrough would raise a
  cross-save conflict at the next launch.

**E4. What the clear plan already says.** SOURCED (`operations.py:618-634`)

- Under Steam Cloud a deleted slot comes back. The procedure:
  1. Turn Cloud off for No Man's Sky.
  2. Clear the slot.
  3. Launch and quit the game once.
  4. Turn Cloud back on, and keep the local copy if asked.
- Xbox needs no warning: the Deleted record propagates.

## 3. Implications for the design

Constraints:

- **C1 Write only while NMS.exe is closed.** Both editions use NMS.exe (`safety.py:16`).
  "Unknown" counts as warn (DESIGN D5). Re-check immediately before the first live write.
- **C2 Data stays verbatim; only metas are transformed.** That covers re-key, timestamps and
  Steam<->Xbox conversion. Older Xbox formats are out of scope for transfer.
- **C3 No deletions in automated flows on Steam with Cloud on.**
  - Model every operation as overwrites.
  - Operations that need a deletion must show the procedure (E4) or refuse: freeing a slot,
    undoing a fill into an empty slot, and a mirroring full restore.
- **C4 Preserve before replacing.** Before any live write, the content being replaced must be
  in the vault durably, verified by hash. A prunable snapshot does not count.
- **C5 Write both members of the target slot.** V must come out newest by every signal the
  game or platform might use: meta timestamp, Steam file mtime order and Xbox FILETIME order.
- **C6 Fix the Xbox meta parsing and setting first** (A5). "Which member is current", identity
  and names on Xbox all depend on it.
- **C7 Account is part of a live target's identity** (platform + account folder).
  - Placement checks same or other account.
  - Never copy `steam_autocloud.vdf` or `containers.index` across accounts.
- **C8 Organizer operations never touch accountdata.**

Risks:

- **R1 Steam Cloud conflict from another device** (Steam Deck, another PC).
  - If the user picks "cloud", the placement silently reverts: the evicted save is safe in the
    vault, but the placed one leaves live.
  - Mitigation: after launch, rescan and compare live against the last operation's expected
    hashes.
- **R2 Xbox cloud conflict** from a console or another PC: same as R1.
- **R3 Twins.** Two live slots holding forks with one SaveUniversalId, which the organizer
  makes easy.
  - Cross-save and server-side behaviour for twins is unknown (INFERRED risk).
  - When two branches are live, SaveUniversalId alone cannot be "one line": lineage must
    separate them.
- **R4 Older versions of expedition saves past their season convert** (B5).
- **R5 Full restore and undo roll back account unlocks** (B4).
- **R6 Promote is unreliable on both platforms.** Under Steam Cloud it stamps an old mtime
  (G5); on Xbox it is a no-op (A5).
- **R7 Disk use.** A whole-folder snapshot is about 54 MB (Steam `*.hg`) or 68 MB (Xbox); 16
  snapshots take 756 MB now.

Opportunities:

- **O1** SaveUniversalId is Hello Games' own cross-platform save id: a stable key for "one
  line".
- **O2** UsedDiscoveryOwnersV2 says which identities and platforms a playthrough has been
  played under.
  - That gives badges ("played on Steam and Xbox").
  - It also gives a cheap "foreign save" check before a cross-account placement.
- **O3** `remotecache.vdf`, read only, shows Steam Cloud state per file.
  - It can drive "pending upload" warnings, and "will come back" warnings for files the
    manifest lists but disk lacks.
  - Finding it needs Steam-install discovery, and the path contains the accountid.
- **O4** The `containers.index` sync states give "pending upload" per Xbox save.
- **O5** Steam<->Xbox placement can stay inside the verbatim rule (D5-D7). Cross-platform is a
  meta-conversion problem plus an owner validation run, not a JSON-rewrite problem.

## 4. Semantics spec draft (organizer operations)

### 4.0 Definitions

- **Version V:** one save file, i.e. the data bytes (sha256 = version id) + meta plaintext +
  format/platform. It carries SaveUniversalId, play time, meta timestamp and name. R7 found
  data hash <-> meta plaintext to be 1:1 in the owner's vault (195/195).
- **Live target T** = (platform, account folder, slot S), with members S.A (auto, `f = 2S-1`)
  and S.B (restore point, `f = 2S`).
- **Partner of V:** the other member captured beside V in the same source slot, if any.
- **Cloud mode of T:** Steam Cloud on or off (`steam_autocloud.vdf` present); Xbox is always
  synced.
- **Placement:** writing a pair (P_A, P_B) into S.A and S.B.
- **Preserve(x):** make sure version x is stored durably in the vault (store it if absent, then
  verify by re-reading its hash). Never a prunable snapshot.
- **Journal:** a write-ahead intent record before the first live write, and a completion
  record after validation.

### 4.1 Place(V, T): move version V into live slot S, evicting S's current save to the vault

This covers "move a vault version to live", "use an older version of a live save" and "use a
backup of a non-live entry".

**Pre-flight** (read-only; no write if any check fails):

1. NMS.exe is not running. True -> refuse; None -> warn and require confirmation.
2. Resolve V:
   - it is readable;
   - its meta validates (Steam: decrypt with its ordinal; header correct);
   - SizeDecompressed == chunk sum;
   - its data is stream format (required if the move is cross-platform).
3. Classify the transfer: same platform + same account, same platform + other account, or
   cross-platform (matrix, section 5). Apply the gate chosen there.
4. Read S's current members: version id, SaveUniversalId, play time, meta timestamp and name
   for each. Look up whether the vault already holds each one (by hash).
5. Choose (P_A, P_B) by the member policy (fork Q1). Default proposal:
   - P_A = V;
   - P_B = V's partner if it exists and has the same SaveUniversalId; otherwise V again.
6. Compute the metas:
   - Steam: re-key P_A for ordinal 2S and P_B for ordinal 2S+1 (file number + 1);
   - cross-platform: convert (D7);
   - timestamps: keep V's original meta timestamp (fork Q2), and make P_B's meta timestamp
     lower than P_A's. If P_B is V's partner, edit its timestamp only when it was newer than V;
     if P_B is a second copy of V, set it to V's timestamp minus 1. Only metas change; data
     stays verbatim.
7. Compute the on-disk times:
   - Steam with Cloud on: mtime = now, with A's >= B's;
   - Steam with Cloud off: keep the source mtimes, but keep the same order;
   - Xbox: record FILETIME = now (A >= B), meta timestamp as in step 6.
8. Show the warnings in the plan:
   - play-time delta when V is an older version than what S holds ("S has 3h12m more than V;
     S's current version will be kept in the vault");
   - a different playthrough is being evicted;
   - twins: V's SaveUniversalId is already live in another slot;
   - the expedition season is over;
   - cross-account or other-person ownership;
   - Hello Games cross-save is on;
   - Xbox quota;
   - Steam Cloud other-device risk.

**Execute:**

9. Preserve each current member of S. Skip those already in the vault, but record that they
   were.
10. Write the journal intent: operation id, T, V, (P_A, P_B), evicted version ids, and the
    snapshot id to come.
11. Snapshot the live folder for crash recovery (whole-folder until a slot-scoped snapshot
    exists).
12. Write S.B first, then S.A. A crash between them leaves one new and one old member; the
    journal intent shows what is left to do, and the old pair is preserved (step 9) and in the
    snapshot (step 11).
    - Steam: per member, write data then meta, each atomically.
    - Xbox: `write_save` per identifier (new GUIDs, index rewritten last, Modified or Created,
      xgs mirrored).
13. Validate by rescanning S:
    - both members decode;
    - each data sha256 is the planned one;
    - SaveUniversalId == V's;
    - the newest member by meta timestamp is P_A = V;
    - Steam metas validate with their ordinals;
    - Xbox records exist and are not Deleted, and xgs equals wgs.
14. On a validation failure: put S back from the snapshot with a slot-scoped rewrite of the two
    members (not a whole-folder mirror), mark the journal entry failed, and surface the error.
15. Write the journal completion and the lineage:
    - the binding "T holds V since this operation";
    - for each evicted member, its version id and where it is in the vault;
    - the user's note, if they added one.

**Undo** (slot-scoped, non-destructive):

- Undo = Place(the previous members of S, T), using the preserved versions.
- First, Preserve S's current content if it differs from what the operation wrote (the user
  played since). That content becomes a new child version of V.
- It consists only of overwrites, so it sticks under Cloud.

### 4.2 PlaceIntoEmpty(V, T)

- The same as 4.1, with nothing to evict.
- Its undo is Evict(T) (4.4), so under Steam Cloud with Cloud on the undo needs the
  procedure.

### 4.3 Swap(S1, S2): two live slots of one live folder

- One journal entry and one snapshot. Preserve both slots; usually they are already identical
  to the last scan.
- Place(content of S2, S1) and Place(content of S1, S2) from the bytes held in memory. Each
  member keeps its own position; metas are re-keyed.
- Only overwrites are needed when both slots are full. If one is empty, the swap is a move, and
  the vacated slot needs a deletion (the Steam Cloud caveat applies).
- A cross-platform "swap" is two transfers; gate both.

### 4.4 Evict(T): free a slot

1. Preserve both members.
2. Write the journal intent and take the snapshot.
3. Delete:
   - Steam: delete both members. With Cloud on, refuse or run the guided procedure (fork Q6).
   - Xbox: `delete_save` x2 (records flagged Deleted).
4. Validate that the slot is empty.

Undo = PlaceIntoEmpty(the evicted members, T).

### 4.5 Older version of a live save

- This is Place(V, T), where T is the slot currently holding V's SaveUniversalId.
- The plan shows the play-time delta.
- Lineage: the next versions in T descend from V (a new branch). The evicted newer version
  stays as the tip of the other branch.

### 4.6 What each operation must record (lineage and recovery)

- The operation itself: id, time, app version, type.
- The target: platform, account folder and slot.
- For each written member:
  - version id and SaveUniversalId;
  - origin (vault entry + slot + member, or live slot);
  - meta timestamp written, file mtime or FILETIME written, ordinal used.
- For each evicted member:
  - version id, SaveUniversalId, play time, meta timestamp;
  - where it is preserved (version store key);
  - whether it was already in the vault.
- The snapshot id, the cloud mode, the warnings shown with the user's confirmation, and the
  user's note.
- **Binding rule for later scans:**
  - A later live version in T with the same SaveUniversalId and play time >= V's is a child of
    V, until the next operation on T.
  - A different SaveUniversalId ends the binding (a new game or another tool wrote the slot).

### 4.7 Failure modes

| Failure | When | Effect | Recovery |
|---|---|---|---|
| Game starts mid-operation | between pre-flight and write | it may read a half-written slot | re-check NMS.exe right before the first write; abort |
| Crash or power loss between member writes | step 12 | the slot holds the new B and the old A | intent without completion -> on the next start, offer to roll forward (redo the writes) or back (Place the previous members) |
| A member's data written, meta not | step 12 | SizeDecompressed mismatch in that member | the same journal recovery |
| Validation failure | step 13 | wrong bytes or ordinal | automatic slot-scoped rollback |
| Vault drive missing or full | step 9 | nothing preserved | refuse before any live write |
| Steam Cloud conflict at next launch | after the operation | the user may pick "cloud", and the placement reverts | a post-launch rescan detects the mismatch with the binding; offer to re-place |
| Xbox sync conflict | after the operation | same | same |
| Steam Cloud re-downloads deleted files | after Evict, or undo of a fill | the slot reappears | the procedure; detect it via `remotecache.vdf` |

## 5. Transfer feasibility and risk matrix

**Same platform, same account.**

| Aspect | Detail |
|---|---|
| Meta | Steam: re-key (`meta.re_key`). Xbox: verbatim, written as a new or updated index record. |
| Data | verbatim |
| Ownership | unchanged; the save already has this identity |
| Account data | untouched |
| Cloud | Steam: overwrites upload, deletions need the procedure. Xbox: Modified -> upload. |
| Today | supported (`repopulate_slot`), with gaps G1-G8 |
| Verdict | **GO** |
| Main risks | mixed slots, the Xbox meta bugs, deletions under Cloud |

**Steam account A -> Steam account B.**

| Aspect | Detail |
|---|---|
| Meta | re-key only: the meta is not account-bound, its key depends only on the ordinal (`meta.py:28-32`) |
| Data | verbatim |
| Ownership | the save lists A's identity. INFERRED: for the same person, the game appends B and re-owns on first play (D5); unknown for another person. |
| Account data | B's own, untouched |
| Cloud | B's Steam Cloud |
| Today | allowed and unguarded; a full restore also copies A's `steam_autocloud.vdf` |
| Verdict | feasible for the owner's own accounts after a validation run. Another person's save needs a libNOM.io-style rewrite, which is outside the verbatim rule. |
| Main risks | the wrong vdf on a full restore; Hello Games' server still lists A as the owner of uploaded bases until the game re-owns them |

**Steam -> Xbox (PC Game Pass).**

| Aspect | Detail |
|---|---|
| Meta | convert 432/384 B -> 360/296 B plaintext (D7) |
| Data | verbatim: stream format on both sides |
| Ownership | the game re-owns on first Xbox play (VERIFIED for cross-save deliveries); PLATFORM is "Win\|Final" on both |
| Account data | Xbox's own (identical when cross-save is on) |
| Cloud | Modified -> upload 10-30 s after exit; a console conflict is possible; quota 256 MB |
| Today | gated (`FeatureNotYetAvailableError`) |
| Verdict | feasible and verbatim; needs the owner validation run (T1) |
| Main risks | the non-cross-save path is untested; console cloud conflicts |

**Xbox -> Steam.**

| Aspect | Detail |
|---|---|
| Meta | convert 360/296 B -> 432/384 B; set SizeDisk and SizeDecompressed; encrypt with the destination ordinal |
| Data | verbatim only for stream format (Worlds 5.00 and later). Refuse older formats, or ask the owner to load and save once on Xbox. |
| Ownership | the game re-owns on first Steam play (VERIFIED, `..D19E`) |
| Account data | Steam's own |
| Cloud | Steam Cloud: new and overwritten files upload |
| Today | gated |
| Verdict | feasible for stream format |
| Main risks | as Steam -> Xbox, plus the format check |

**Xbox account X -> Xbox account Y.**

| Aspect | Detail |
|---|---|
| Meta | verbatim; written into Y's index (never copy X's index) |
| Data | verbatim |
| Ownership | as Steam A -> B |
| Account data | Y's own |
| Cloud | Y's cloud |
| Today | allowed; a full restore writes X's `containers.index` into Y (G7) |
| Verdict | as Steam A -> B |
| Main risks | as Steam A -> B |

**The official alternative is Hello Games cross-save.**
- The game does the meta, data, ownership and account-data work itself, through Hello Games'
  server.
- The owner can always use it once CrossSaves is on.
- Limits: 5 cloud saves, and manual in-game steps.

## 6. Open questions and candidate options

The owner is away; these are recorded as forks, not asked.

- **Owner's choice:** Q1, Q2, Q4, Q5, Q6.
- **Design agents' choice:** Q3, Q7, Q8, Q9, each with a recommendation below.

**Q1 Member policy for Place(V, S)** - owner

- (a) V + its partner when the partner has the same SaveUniversalId, otherwise V twice.
  Faithful and Cloud-safe; needs timestamp ordering, which may mean editing the partner's meta
  timestamp.
- (b) V twice, always. The simplest and Cloud-safe; the live slot loses the partner restore
  point, which stays in the vault.
- (c) V + delete the other member (libNOM.io style). Clean, but deletions do not stick under
  Steam Cloud, so the mixed slot returns after launch. Not recommended.

**Q2 Timestamp policy** - owner

- (a) Keep V's original meta timestamp, and set the on-disk times to now under Cloud. The
  in-game date stays honest and shows which version is live; the vault keeps the history.
- (b) Re-stamp the meta timestamp to now. The slot reads as just played; the original in-game
  date is lost.

**Q3 Undo model** - design agents

- (a) Slot-scoped and non-destructive (4.1). **Recommended.**
- (b) Keep today's whole-folder, single-level undo.
- (c) Slot-scoped undo for organizer operations, plus whole-folder undo only as a "panic
  restore" that asks whether to keep the current account data.

**Q4 Cross-platform (Steam <-> Xbox)** - owner

- (a) Verbatim data + meta conversion, relying on the game's identity migration. Ship after the
  owner validation runs T1 and T2.
- (b) libNOM.io-style ownership rewrite. It re-serialises the JSON, so it breaks the verbatim
  rule. It needs a JSON writer and upkeep for every game version: libNOM.io marks the code
  "EXTERNAL RELEASE: Update if new objects with an ownership have been added".
- (c) Never; point the user to Hello Games cross-save (5-save limit, in game).

**Q5 Cross-account and other people's saves** - owner

- (a) The same person only, detected through UsedDiscoveryOwnersV2 (identities the vault
  already knows), with a warning.
- (b) Allow anything, with a "bases may not be editable" warning.
- (c) Block.

**Q6 Steam Cloud and deletions** (Evict, undo of a fill, full restore with mirror) - owner

- (a) Refuse when Cloud is on, and show the procedure.
- (b) Do the local delete, show the procedure, and detect the resurrection via
  `remotecache.vdf`.
- (c) Avoid needing deletions at all: the organizer exposes only Place (replace) and never
  "empty a slot". The owner's flow always picks a live slot to evict while filling it.

**Q7 Where evicted versions live (S6)** - design agents

- A version store, a per-operation extract entry, or a distinct "parked" kind (R1 Q).
- Whichever is chosen must be exempt from snapshot pruning.

**Q8 The Xbox FILETIME on write** - design agents

- Now (the Steam rule's analogue) or the source save time (libNOM.io).
- The effect on Xbox sync is unknown. Recommended: now, with the meta timestamp setting the
  order.

**Q9 Show Steam Cloud state from `remotecache.vdf`** - design agents

- Read-only. It needs Steam-install discovery under D6/D7, and the path contains the accountid.

### Owner validation protocol

Only a live run can settle these. Each uses a throwaway save, with a verified full backup taken
first, and the app never runs them automatically.

- **T1 Steam -> Xbox.**
  1. With a dev build that lifts the gate, place a throwaway Steam save into an empty Xbox slot.
  2. Start NMS (Game Pass) and load it.
  3. Check: does it load; after one save, are the own bases editable and is
     UsedDiscoveryOwnersV2 extended; does the Xbox app sync without a conflict?
- **T2 Xbox -> Steam:** the mirror of T1.
- **T3 Steam Cloud:**
  - (a) An overwrite with mtime = now survives a launch (expected).
  - (b) An overwrite with the backup's old mtime: does Steam revert it? This settles the
    untested half of the project's rule.
  - (c) A deletion while closed comes back (already known).
- **T4 Mixed slot in game:** what the save screen shows, and which member the auto-save
  overwrites.
- **T5 Xbox promote:** with the meta timestamp fixed vs FILETIME-only, which member loads?
- **T6 Steam:** when the meta timestamp and the file mtime disagree, which member loads?

### Phasing hint (input to S8)

1. **Phase 1:** same platform + same account Place, Swap and Evict, honouring C3-C8, plus the
   Xbox meta fix.
2. **Phase 2:** cross-platform, via verbatim data + meta conversion, after T1 and T2.
3. **Later or never:** cross-account placement of another person's save.

## Evidence index

All scripts are read-only; files under `<scratchpad>\R8\`.

| Script | Output | Shows |
|---|---|---|
| `scripts\xbox_meta_probe.py` | console | Xbox meta lengths, timestamp at 0x120, id at 0x118, FILETIME deltas (A5) |
| `scripts\promote_noop_check.py`, `scripts\xbox_names.py` | console | parser and setter gaps on all 30 metas; empty names on the 296-B metas (A5) |
| `scripts\steam_meta_probe.py` | console | Steam meta timestamp vs mtime, A/B identity agreement, Cloud flag (A1, A4, E1) |
| `scripts\remotecache_probe.py` | console | Steam manifest vs disk (E1) |
| `scripts\oplog_probe.py` | console | live-writing operations in the vault oplog (E1, A5) |
| `scripts\ownership_probe.py` | `ownership_probe.out.txt` | PLATFORM token and owner classes for every live save (D5) |
| `scripts\pair_compare.py` | `pair_compare.out.txt` | the same bases and discoveries across Steam/Xbox copies (D5) |
| `scripts\xplat_history.py`, `xplat_history2.py`, `xplat_history3.py` | `xplat_history*.out.txt` | per-copy ownership timeline, UsedPlatforms, UsedDiscoveryOwnersV2 (D5, A4) |
| `scripts\accountdata_probe.py`, `acct_compare.py`, `acct_flags.py` | `accountdata_probe.out.txt` | accountdata structure, Steam == Xbox bytes, cross-save flags (B1, B2) |
| `scripts\meta_layout_compare.py` | console | Steam vs Xbox meta field mapping (D7) |
| `scripts\reslot_evidence.py` | console | playthroughs that changed slot and kept being played (A3) |

The libNOM.io clone is in `<scratchpad>\R8\ext\libNOM.io` (master `652aabb`); NomNom is in
`<scratchpad>\R8\ext\NomNom`. Run the scripts from the repo root with
`PYTHONUTF8=1 uv run --with lz4 python <script>`.
