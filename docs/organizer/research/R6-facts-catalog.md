# R6 - Save facts catalog

Research only. Every claim is tagged **VERIFIED** (checked in a copy of a real save or in the
unpacked game data), **SOURCED** (cited), or **INFERRED** (reasoning, not proven). Paths use
readable key names (from R5's mapping); the obfuscated paths are in Appendix A.

Evidence base: 38 save copies + the Steam `accountdata.hg`, copied out of the live folders and
the vault into the session scratchpad and decoded there (nothing was written anywhere else).
22 Steam (16 live, 6 from vault backups) and 16 Xbox (all live), 0.2 h to 532 h of play, game
BaseVersion 4146 to 4228, modes Normal / Custom / Creative / Survival / Permadeath / expedition.
Game data: unpacked build 25729564 at `F:\NMS-Unpack` (metadata index + item catalogue).

## 1. TL;DR

- Every fact the owner named is extractable with simple logic from the decoded save JSON; all
  were run over the 38 copies (zero unknown keys after de-obfuscation with R5's mapping).
- Cost: any data-file fact needs the whole save decoded - stdlib LZ4 0.07-0.80 s + `json.loads`
  0.02-0.23 s per save; computing ~60 facts from the parsed tree then takes 0-4 ms. Meta facts
  (name, summary, play time, difficulty, season, version, timestamp) stay free.
- A save can hold two independent game states, `BaseContext` and `ExpeditionContext`
  (`ActiveContext` = which one loads). 13/38 have an expedition context, 3 are expedition-only,
  1 old save (Echoes 4.40) has the pre-context layout. Every fact is per context.
- Glyphs: `PlayerStateData.KnownPortalRunes` is a 16-bit mask; count set bits (mains 16/16).
- Base parts: `PersistentPlayerBases` mixes the player's bases with up to 228 cached bases of
  other players (`External*` types) - filter by `BaseType`. Steam main: 57 own bases, 15,016
  parts; Xbox main: 38 bases, 7,156 parts.
- "Legacy base parts" = the game's build-menu groups LEGACY STRUCTURES (69 concrete/metal/wood
  `C_`/`M_`/`W_` parts) and LEGACY FREIGHTER MODULES (20 pre-Endurance freighter rooms). Detect
  placed (base objects) and still-buildable (`KnownProducts`). Placed in 5 saves; all 69 known
  only in 2 Steam saves; freighter modules known in 6 of 15 Xbox saves.
- Galaxies with bases: base `GalacticAddress` carries the galaxy in bits 32-39 (357/357 named
  bases match their teleporter's `RealityIndex`). Game data names only galaxies 1-10; a
  256-name community list is needed. Xbox main has bases in 11 galaxies.
- Ships: `ShipOwnership[12]`, empty slot = blank model path; type from the model folder plus 9
  fixed reward models; class from inventory. Corvettes are ship entries too; their layouts are
  `PlayerShipBase` records, 13 of 35 of which are orphans. 18 archived-ship slots exist.
- Companions: `Pets[18|30]`, empty = `CreatureID "^"`. Most have no custom name (Steam main: 4
  of 21) and the procedural species name is not stored - show the creature type instead.
- Milestones: `MissionProgress` + per-mission final-stage values from game data give exact
  completion for Artemis (30 steps + ending choice), Atlas (11), Sentinel, Autophage, purple
  systems, pirates, water, living ship, Overseer. Global `Stats` give journey rank, words,
  discoveries, standings. Titles are account-level (`accountdata`), not per save.
- Traps: `Units` stored signed (-1 = 4,294,967,295), addresses mixed hex-string/int in one
  list, invalid UTF-8 inside item ids, markup in names (`<PET1>..</>`), Xbox meta names empty
  while the JSON `SaveName` is set, the A/B members of one slot can differ (galaxy, bases).
- Lookup tables to ship are small (galaxy names, 89 legacy ids, ~20 ship models, ~130 mission
  ids with versioned thresholds, 23 expedition names) but go stale with each game update; the
  design must fall back to raw ids.

## 2. Findings

### 2.0 Method and layout facts every extractor needs

- **Decode.** Copies decoded with `lz4_block` (pure Python) and, for speed in research, the C
  `lz4` package; de-obfuscated with R5's `Mapper` (maps: MBINCompiler 7.07.0-pre1 mapping +
  libNOM.map embedded mapping + legacy). 0 unknown keys in all 39 files. **VERIFIED**
- **Invalid UTF-8.** Decompressed JSON contains raw non-UTF-8 bytes inside procedural product
  ids (e.g. `"^\x80\x80\u000F...#00000"`): 0-260 such bytes per save. Decode with
  `errors="surrogateescape"` (and write with the same) or the parse fails. **VERIFIED**
- **Account data is plain JSON.** Steam `accountdata.hg` starts with `{` (no `0xFEEDA1E5`
  chunks), 105,904 bytes; de-obfuscates with the account half of the mapping. **VERIFIED**
- **Old Xbox data blob.** The Echoes-era Xbox save (slot 13, BaseVersion 4146) is a single bare
  LZ4 block; its decompressed size is the Xbox meta's size field (6,655,832). **VERIFIED**
- **Top-level layouts** (38 saves). **VERIFIED**

  | Layout | Count | Top keys | Where `PlayerStateData` is |
  |---|---|---|---|
  | Context, base only | 24 | Version, Platform, ActiveContext, CommonStateData, BaseContext, DiscoveryManagerData | `BaseContext.PlayerStateData` |
  | Context, base + expedition | 10 | + ExpeditionContext | both contexts |
  | Context, expedition only | 3 | no BaseContext | `ExpeditionContext.PlayerStateData` |
  | Legacy (pre-context) | 1 | Version, Platform, PlayerStateData, SpawnStateData, GameKnowledgeData, DiscoveryManagerData | top level |

  - `ActiveContext` is `"Main"` (33) or `"Season"` (4); absent in the legacy layout.
  - Context layout moved `SaveName`, `TotalPlayTime`, `SeasonData`, `SeasonState` from
    `PlayerStateData` to `CommonStateData`. The legacy save still has them in
    `PlayerStateData`. **VERIFIED**
  - libNOM.io maps BaseVersion 4146 to Echoes 4.40 and 4147+ to Omega 4.50; our first
    context-layout save is 4154, so the split arrived at Omega or later. **SOURCED** (libNOM
    `Meta/GameVersion.cs`) + **INFERRED** (exact version of the split).
- **Context game mode.** `BaseContext.GameMode` / `ExpeditionContext.GameMode` are ints: 1 in
  every base context, 5 in the permadeath save, 6 in every expedition context (libNOM
  PresetGameMode numbering). JSON `Version` = meta BaseVersion + 512 x m, with m = 1 for every
  save here including the expedition-only ones (4740 = 4228+512) and m = 5 for the permadeath
  save (6783 = 4223+2560). **VERIFIED** (pattern) / **SOURCED** (enum: libNOM).
- **Per-context save time.** `PlayerStateData.TimeStamp` is the in-game save time of that
  context: base context equals the meta/container time within 0-151 s on the 7 saves checked;
  the expedition context of the Steam main was last played 2026-09-10 while the base context
  was saved 2026-10-15. Absent in the legacy layout. **VERIFIED**
- **Signed integers.** `Units` is stored as a signed 32-bit int: Steam main has `-1`
  (= 4,294,967,295 units). Mask with `& 0xFFFFFFFF`. **VERIFIED**
- **Mixed number encodings.** 64-bit address fields come as `"0x..."` strings or as JSON ints
  in the same list (base `GalacticAddress`: 250 strings, 148 ints among the 398 bases
  cross-checked in 2.7). **VERIFIED**
- **Markup in names.** Player-entered names can carry game markup: freighter name
  `<PET1>The Hangar</>`, summary `In the <PET1>Tikyars</> system`. Strip `<...>` for display.
  **VERIFIED**
- **Privacy.** Ownership blocks hold platform ids and usernames: base `Owner{LID,UID,USN,PTK}`,
  discovery `OWS`, pet `SenderData`, `CommonStateData.UsedDiscoveryOwnersV2`, account
  `BlockList`. An index must never store them (hash or drop). **VERIFIED**

### 2.1 Progression milestones

**Where.** `PlayerStateData.MissionProgress[]` = `{Mission "^ACT1_STEP1", Progress int, Seed,
Data, Stat, Participants}` (476-1,002 entries); `PlayerStateData.MissionVersion` (26-40);
`PlayerStateData.CurrentMissionID`; global stats in `PlayerStateData.Stats[GroupId
"^GLOBAL_STATS"].Stats[{Id, Value{IntValue|FloatValue}}]` (470 ids in the Steam main).
**VERIFIED**

**Completion logic.** `Progress == -1` = never started; `>= 0` = stage reached. A mission is
complete when `Progress >=` its final-stage value from game data
`metadata/simulation/missions/tables/*.mbin`, field `Missions.FinalStageVersions[{Version,
Progress}]` - take the entry with the largest `Version <= MissionVersion` (tables go to 39,
current saves are 40, so use the last entry). Final values change across versions
(ACT1_STEP13: 27 -> 46 -> 49 -> 50 -> 54), which is why the version matters. In both main
saves every completed story step equals its final value exactly (ACT1_STEP13 54, ACT2_STEP13
69, ACT3_STEP4 29, ATLAS11 36). `Progress` 2147483647 also appears (45 entries in the Steam
main). **VERIFIED** (1,836 missions extracted, 1,790 with final stages).

**Derivable milestones** (chain = mission ids; examples Steam main / Xbox main / others):

| Milestone | Mission ids (table) | Steam main (532 h) | Xbox main (296 h) | Others |
|---|---|---|---|---|
| Artemis Path (main story) | ACT1_STEP1-13, ACT2_STEP1-13, ACT3_STEP1-4 (coremission) | 30/30 | 30/30 | Steam s10 0/30 (current ACT1_STEP1), Xbox Survival s5 30/30 |
| Story ending choice | EPILOGUE_NEWGAL vs EPILOGUE_STAY; DID_RESET / DID_NOT_RESET | new galaxy | stayed | Survival s5: new galaxy |
| Artemis choice | ART_DEAD vs ART_SIM (+ART_SIM_TRUTH / ART_SIM_LIE), APOLLO_DEAD | Artemis dead | simulation, lie | |
| Atlas Path | ATLAS1-11; ATLAS_LOOP_STAR / _DENY (atlaspath) | 11/11, star seed | 11/11, star seed | Polestar s8, All Parts s12 11/11 |
| Space Anomaly / Nexus | ENABLE_NEXUS, NEXUS1..15; `HasAccessToNexus` | yes | yes | expedition contexts: often no |
| Sentinel line | SENTINELS_1-5 (sentinelsettlement) | 5/5 | 5/5 | Survival s5 5/5 |
| Autophage line | ROBOMISS_0,1,2,4 (+_1A, _3_NADA, _3_BUI) (npcbuilders) | 4/4 | 4/4 | |
| Purple systems (Worlds II) | PURPM1-3 (npcbuilders) | 3/3 | 3/3 | |
| Titan worlds (Worlds I) | WORLDSM0, WORLDSM1, WORLDSMA, WORLDSMB (npcbuilders) | 0/4 | 0/4 | none started anywhere |
| Pirates (Outlaws) | PIRATES1-3, PIRATE_MYSTERY1-4 (pirate) | 3/3 | 0/3 | Steam Polestar s8 3/3 |
| Water story | WATERSTORY1-5 (water) | 5/5 | 5/5 | |
| Living ship (Starbirth) | BIO_SHIP1-5 (spacepoi) | 5/5 | 5/5 | |
| Overseer | OVERSEER1-10 (mission) | 10/10 | 10/10 | |
| Base computer archive | BASE_UPGRADE1-21 (basecomputer) | 12/21 | 10/21 | |
| Settlement | SETTLEMENT_PRE, SETTLE_* + `SettlementHistory` | 3 claimed | 2 claimed | |

All rows **VERIFIED** as computed values; the chain *membership* (which ids make a storyline)
is **INFERRED** from ids and table names. Stats and missions can disagree: `^ATLAS_PATH` is 10
in both mains and 11 in "Artemis-936qs", but 0 in "Polestar" and "All Parts Building" whose
ATLAS1-11 are all complete (probably edited or expedition-seeded saves - **INFERRED**);
`^ATLAS_LOOPS` (Atlas resets) is 0 / 3 / 8 in Steam main / Xbox main / Artemis-936qs.
`Progress` 2147483647 is treated as complete (**INFERRED**). **VERIFIED** (values)

**Journey milestones / rank.** Game data `metadata/gamestate/stats/journeymilestonetable.mbin`
defines 12 ranks JOURNEY00-11 = Apollo, Pathfinder, Juno, Rosetta, Cassini, Kepler, Dawn,
Curiosity, Hubble, Viking, Magellan, Galileo with `PointsToUnlock` 0,5,...,55 **VERIFIED**. The
save stat `^JM` looks like those points (Steam main 87, Xbox main 86, fresh saves 0-5) - rank =
`JOURNEY[min(JM // 5, 11)]` is **INFERRED** (not compared with the in-game screen; a second
stat `^JM_BANKED` differs: 85 / 97). Medal
categories come from `metadata/reality/journey.mbin` (Journey, Race x3, Guild x6...) with
thresholds in `metadata/gamestate/stats/leveledstatstable.mbin` (survival variant exists).
**VERIFIED** (tables exist; per-medal level computation not built).

**Useful global stats** (Steam main values): `^WORDS_LEARNT` 1450, `^TWORDS/WWORDS/EWORDS/
BWORDS_LEARNT` 373/374/408/181, `^DISC_SYSTEMS` 1221, `^DISC_PLANETS` 378, `^DISC_CREATURES`
1576, `^DEATHS` 2, `^PORTAL_WARPS` 207, `^DIST_WARP` 1293, `^SENTINEL_KILLS` 4496,
`^TRA/WAR/EXP/BUI_STANDING` 311/341/320/49, `^PIRATE_STAND` 13, `^STATIONS_OWNED` 5,
`^PETS_ADOPTED` 18, `^SHIPS_BOUGHT` 70, `^PARTS_PLACED` 50890 (lifetime, not current),
`^ATLAS_PATH` 10, `^HOME_REALITY` 9, `^JM` 87. **VERIFIED**

**Titles.** Account-wide, not per save: `accountdata.hg` `UserSettingsData.UnlockedTitles`
(286 on the Steam account), also `UnlockedSpecials` 764, `UnlockedSeasonRewards` 289,
`UnlockedTwitchRewards` 235, `Seen*` lists. Per save there is only the selected title
`PlayerStateData.BannerTitleId` (`^T_WAKE_TITAN`, `^T_EXPD8`, `^T_DEFAULT`). Title rules
(stat, rank, mission, recipe) are in `metadata/gamestate/playerdata/playertitledata.mbin` (346
titles). **VERIFIED**

**Expeditions (seasons).** `CommonStateData.SeasonData` embeds the whole expedition definition
(`SeasonId`, `SeasonNumber`, `SeasonName` loc key, `DisplayNumber`, `Stages`, rewards...);
`CommonStateData.SeasonState{MilestoneValues[], RewardCollected, HasCollectedFinalReward}`;
`CommonStateData.EarnedSeasonSpecialRewards[]` (12 in each main); `PlayerStateData.
RedeemedSeasonRewards[]` (232 Steam main); `PlayerStateData.StartingSeasonNumber`. Name =
localise `SeasonName` (`UI_SEASON_21_NAME` = Remnant). `SeasonId 0 / SeasonNumber 1` means no
expedition (default "The Pioneers" record). Examples: Steam main has an expedition context in
progress - SeasonNumber 46 "Swarm" (Expedition 22), 15 of 23 milestone values >= 1, final reward
not collected; Xbox slot 12 is expedition-only, ActiveContext Season, "Our Journey Continues"
(Expedition 23) 9/48. **VERIFIED** (fields, names); "milestone done = value >= 1" is
**INFERRED** (some values are counts, e.g. 16.0).
SeasonNumber -> name/number table: `metadata/reality/tables/historicalseasondatatable.mbin`
(23 rows: 1 The Pioneers, 2 Beachhead, 3 The Cartographers, 4 Emergence, 9 Exobiology, 10 The
Blighted, 11 Leviathan, 12 Polestar, 17 Utopia, 18 Singularity, 19 Voyagers, 24 Omega, 26
Adrift, 27 Liquidators, 28 Aquarius, 29 The Cursed, 36 Titan, 37 Relics, 38 Corvette, 39
Breach, 45 Remnant, 46 Swarm, 47 Our Journey Continues). **VERIFIED**

**Cost:** full parse; lookup = mission ids + final stages (version-dependent).

### 2.2 Number of known glyphs

- Path: `PlayerStateData.KnownPortalRunes` (int bitmask, 16 glyphs). Count = popcount of the low
  16 bits. **VERIFIED**
- Examples: Steam main 65535 (16), Xbox main 16, Xbox "Egg Gifting Xbox" 32767 (15), Steam
  "Exobiology-385qs" 3 (2), Xbox slot 4 15 (4), fresh saves 0. **VERIFIED**
- Pitfalls: expedition contexts carry their own mask (0 in most); 8 base contexts show 0,
  including both "Creative Primary" saves - in Creative or freshly started saves 0 says little
  about progress. **VERIFIED** (values) / **INFERRED** (interpretation).
- Cost: full parse, no lookup.

### 2.3 Number of used base parts

- Path: `PlayerStateData.PersistentPlayerBases[]`: `{BaseVersion, OriginalBaseVersion,
  GalacticAddress, Position, Objects[{Timestamp, ObjectID "^T_WALLM_H", UserData, Position,
  Up, At}], Name, BaseType.PersistentBaseTypes, Owner, LastUpdateTimestamp, ...}`. Parts =
  `len(Objects)`. Loose placeables outside bases (beacons, save points, geobays) live in
  `PlayerStateData.BaseBuildingObjects[]`. **VERIFIED**
- Base types seen and whether they are the player's:

  | BaseType | Meaning | Own? |
  |---|---|---|
  | HomePlanetBase | planetary base | yes |
  | FreighterBase | freighter interior | yes |
  | PlayerShipBase | corvette layout (`UserData` = ship slot) | yes |
  | PlayerSpaceBase | space outpost (beacon/teleporter) | yes |
  | PlayerSpaceStationBase | player-owned space station (~120 `STACONNECT` each) | yes |
  | UITempShipBase | corvette-editor scratch layout (`UserData` 4444444444) | no |
  | ExternalPlanetBase / ExternalSpaceStationBase / ExternalSpaceBase | other players' bases cached in the save (up to 228) | no |

  Ownership by type is **VERIFIED** (names, contents); a cached `External*` base can be the
  player's own (1 case: Steam "Egg Gifting"), so "own = Home/Freighter/PlayerShip/PlayerSpace/
  PlayerSpaceStation" is a safe default. **VERIFIED**
- Examples (own bases only): Steam main 57 bases, 15,016 parts (planet 13,354, freighter 382,
  stations 1,123, corvettes 117, space 40), largest "Rerias Outpost" 2,921; Xbox main 38 bases,
  7,156 parts, largest "Nani Mela Farm Outpost" 1,948; "All Parts Building" 23 bases / 4,302;
  Xbox Survival slot 5 10 / 1,874; fresh saves 0. **VERIFIED**
- Pitfalls: (1) external bases dwarf own bases (Steam s02: 101 external with 10,395 parts vs
  5 own with 232) - never sum without filtering; (2) 13 of 35 `PlayerShipBase` records are
  orphans whose `UserData` points to a slot that no longer holds a corvette, so corvette parts
  should count only when `ShipOwnership[UserData]` is a corvette; (3) `Objects` does not
  distinguish complexity: only 2 of 15,016 parts in the main have
  `DoesNotCountTowardsComplexity`; the per-base limit itself lives in the globals file (not in
  the metadata index - not verified); the account setting `BaseComplexityLimitsEnabled` is in
  accountdata; (4) `^PARTS_PLACED` (50,890) is lifetime placements, not current parts;
  (5) `OriginalBaseVersion` 3/5/6/7/8 records the base-format generation a base was created
  with (BaseVersion is 8 everywhere). **VERIFIED**
- Cost: full parse; part names need `basebuildingobjectstable.mbin` + item catalogue (2,124
  ids, 13 unnamed) only if parts are listed by name.

### 2.4 Existence of legacy base parts

The term has one dominant meaning in the game and three secondary ones.

- **(A) Parts in the build menu's legacy groups - primary.** Game data
  `metadata/reality/tables/basebuildingobjectstable.mbin` groups
  `BASIC_LEGACY` = `UI_BUILD_GRID_BASIC_LEGACY` "LEGACY STRUCTURES" (69 parts: 23 each of
  `C_*` concrete, `M_*` metal, `W_*` wood: walls, floors, roofs, ramps, arches, doors, glass
  panels, triangles) and `FREIGHT_LEGACY` = "LEGACY FREIGHTER MODULES" (20 parts:
  `CUBEROOM_SPACE`, `CUBEROOMB/C_SPACE`, `CORRIDOR*_SPACE`, `CORSTAIRS_SPACE`,
  `S_CONTAINER0-9`, `GARAGE_FREIGHT`, `NPCFRIGTERM`). All 89 are still `ShowInBuildMenu=true`.
  **VERIFIED**. `legacyitemtable.mbin` maps each old structure to its modern twin and teaches
  the new recipe without removing the old one: `C_* -> S_*` (stone), `W_* -> T_*` (timber),
  `M_* -> F_*` (alloy), `RemoveOldRecipe=false, AddNewRecipe=true`. **VERIFIED**. Community:
  the "Legacy Structures" tab is "the very last tab" and exists only in saves created "before
  the change"; new saves cannot buy the recipes (Steam threads, Mar/Apr 2022). **SOURCED**
  - Detect *placed*: any own-base object whose id (without `^`) is in the 89-id set.
  - Detect *buildable*: `PlayerStateData.KnownProducts` contains the ids.
  - Results: placed in 5 saves - "All Parts Building" 490 legacy structures in 4 bases + 7
    legacy freighter modules; Xbox slots 3/5/6/7 have 43/25/42/14 legacy freighter modules in
    their freighters. Known: all 69 structures + 20 modules only in Steam "Polestar" and "All
    Parts Building" (both look save-edited - **INFERRED**); legacy freighter modules (19-20 of
    20) known in 6 of 15 Xbox saves including the Xbox main (20/20, none placed). Neither main
    has legacy structures. **VERIFIED**
- **(B) Legacy freighter interior** - the pre-Endurance freighter rooms, i.e. the
  `FREIGHT_LEGACY` subset of (A). Worth a separate flag: it marks an old freighter layout.
  **VERIFIED** (data) / **INFERRED** (Endurance as the cut-over, from the module names).
- **(C) Old bases** - `PersistentPlayerBases[].OriginalBaseVersion < BaseVersion` (values 3, 5,
  6, 7 vs 8). Says a base was created under an older base format, not that it has old parts.
  **VERIFIED** (values) / **INFERRED** (meaning).
- **(D) The old base-building table** - `legacybasebuildingtable.mbin` (170 ids such as `WALL`,
  `MAINROOM`, `CORRIDOR`, `BIOROOM`); 167 of them are still current ids, so a part listed there
  is not "legacy" in any player-visible sense. Not a useful signal. **VERIFIED**
- No save contains an object id missing from the current table (0 unknown ids across every
  base, own and cached, in the 38 saves), so "removed parts" do not occur in these samples.
  **VERIFIED**
- Cost: full parse; lookup = 89 ids (+ names if shown).

### 2.5 Number and names / types of ships

- Path: `PlayerStateData.ShipOwnership[12]`, each `{Name, Resource{Filename, Seed[bool,
  "0x.."], ...}, Inventory{Class.InventoryClass, ...}, Inventory_TechOnly, Inventory_Cargo,
  InventoryLayout, Location, ...}`; `PlayerStateData.PrimaryShip` = index of the current ship.
  Empty slot: `Resource.Filename == ""` and `Seed == [false,"0x0"]` (343 empty slots seen).
  **VERIFIED**
- Type: from the model path. Procedural models: `DROPSHIPS/DROPSHIP_PROC` Hauler,
  `FIGHTERS/FIGHTER_PROC` Fighter, `SHUTTLE/SHUTTLE_PROC` Shuttle, `SCIENTIFIC/SCIENTIFIC_PROC`
  Explorer, `S-CLASS/S-CLASS_PROC` Exotic, `SAILSHIP/SAILSHIP_PROC` Solar,
  `S-CLASS/BIOPARTS/BIOSHIP_PROC` Living ship, `SENTINELSHIP/SENTINELSHIP_PROC` Interceptor,
  `BIGGS/BIGGS` Corvette. Fixed reward models (from `rewardtable.mbin`
  `GcRewardSpecificShip`): FIGHTERCLASSICGOLD Golden Vector, FIGHTERSPECIALSWITCH Horizon
  Vector NX, WRACER Starborn Runner, WRACERSE Starborn Phoenix, VRSPEEDER Utopia Speeder,
  SPOOKSHIP Boundary Herald, BIOFIGHTER The Wraith, RASAMAMAGOLD Golden Rasamama S36,
  VINTAGEINTERCEPTOR Vintage Interceptor; `S-CLASS/ROYALPARTS/COCKPIT/COCKPIT_A` is
  unexplained (treated as Exotic). The game's own class enum (`ShipClass`: Fighter, Dropship,
  Scientific, Shuttle, Royal, Alien, Sail, Robot, Corvette, Freighter, SwarmDrone) is stored
  only on archived ships; folder rule + special-model table matched it for all 43 archived
  entries (the folder alone would misclass SPOOKSHIP and WRACER*, which sit in `FIGHTERS/`).
  **VERIFIED** (folder rule, enum, rewards) / **INFERRED** (display names Hauler / Explorer /
  Exotic / Solar / Interceptor for the enum values; COCKPIT_A).
- Class: `Inventory.Class.InventoryClass` (C/B/A/S). **VERIFIED**
- Archived ships: `PlayerStateData.ArchivedShipOwnership[18]` `{Ownership (full ship),
  ArchivedName, ArchivedClass.ShipClass, ArchivedInventoryClass}`; used = Ownership model path
  non-empty. Steam main 5, Xbox main 3, Steam s02 18/18. **VERIFIED**
- Examples: Steam main 12/12 - "Naturally Golden Tensor" (Golden Vector model, S), 3 unnamed
  corvettes (C/B/S), "Starborn Phoenix", "Dimensional Fissure" (Exotic S), "Toroidal Vortex",
  "Solar Dart" (Solar S), "Glass Arachnid" (Interceptor S, primary), "The Wraith", "Military
  Might" (Fighter S), "Iron Vulture" (Hauler S). Xbox main 11 incl. corvette "Nissh's Prize
  OF6", living ships "Hokulele" and "The Wraith". Fresh saves 1. **VERIFIED**
- Pitfalls: corvettes have `Name ""` unless the player names them (unnamed is not empty);
  corvette count must come from `ShipOwnership`, not from `PlayerShipBase` records (orphans);
  expedition contexts have their own fleet (1-3 ships); ship names may contain markup.
- Cost: full parse; lookup = folder map + 9 special models (embedded constants).

### 2.6 Names of companions

- Path: `PlayerStateData.Pets[]` (18 slots in saves up to BaseVersion 4207, 30 from 4213),
  `{CustomName, CreatureID "^FLYINGLIZARD", CreatureSeed, SpeciesSeed, GenusSeed, Biome.Biome,
  CreatureType.CreatureType, Descriptors[], BirthTime, Trust, Traits[], UA, SenderData,
  PetBattler*...}`. Empty slot: `CreatureID == "^"` (also BirthTime 0). Unlocked slots:
  count of true in `PlayerStateData.UnlockedPetSlots[]` (2-30). Eggs: `PlayerStateData.Eggs[]`
  same shape. **VERIFIED**
- Name: `CustomName` when set. Unnamed companions show a procedurally generated species name
  in game; the pet entry does not store it, so display falls back to the creature id
  (`FLYINGLIZARD`, `TRICERATOPS`, `QUAD_PET`...). Discovery records carry a name only when the
  player renamed the discovery (`Record[].DM.CN`: 72 in the Steam main, 8 of them animals);
  linking a pet to its species record was not attempted.
  `metadata/simulation/ecosystem/creaturedatatable.mbin` has ids but no English names.
  **VERIFIED** (pet entry, DM.CN, table) / **INFERRED** (in-game naming).
- Examples: Steam main 21 companions, 4 named ("Wanpito.", "Ettoz-Ettava.", "bluewing
  prototypebd1", "Lani-Nui"), 30 slots, 8 eggs; Xbox main 9, none named; Steam "Egg Gifting"
  13 (named "Abyssal Sandworm", "Black Dragon", "Griffin (Blue)"...). Ids across the samples:
  FLYINGLIZARD 56, FLYINGSNAKE 19, TRICERATOPS 12, ROBOTANTELOPE 10... **VERIFIED**
- Pitfalls: `SenderData` (gifted eggs) holds the sender's platform ids/username - never index;
  companions do not exist in expedition contexts of our samples (0).
- Cost: full parse; optional hand-made creature-id -> friendly-type table.

### 2.7 Galaxy names where there are bases

- Path: `PersistentPlayerBases[].GalacticAddress` (own bases, `HomePlanetBase` for
  planet bases). Layout of this 56-bit universe address: VoxelX bits 0-11 (signed), VoxelZ
  12-23 (signed), VoxelY 24-31 (signed), **RealityIndex (galaxy, 0-255) 32-39**,
  SolarSystemIndex 40-51, PlanetIndex 52-55; i.e. hex digits `P SSS RR YY ZZZ XXX`. Verified
  against every same-named teleporter (`TeleportEndpoints[].UniverseAddress{RealityIndex,
  GalacticAddress{VoxelX..PlanetIndex}}`): 357/357 named bases match (the 41 misses are
  external bases all named "..."). **VERIFIED**. Note: `VisitedSystems` uses a different
  packing (tools/nms_visit_history.py: X 0-11, Y 12-19, Z 20-31, S 32-43, P 44-47, no galaxy).
- Names: game data only has galaxies 1-10 (`UI_PLAYER_TITLE_REALITY1..10` "%NAME% of
  Euclid".."Eissentam"; `MAIN/SECOND/THIRD/FOURTH/FIFTH_GALAXY`). The other 246 names are not in
  any table; a 256-name community list exists (destructoid; the fandom wiki page could not be
  fetched) and agrees with the game for 0-9 and with in-save base names at index 89 ("Paholiang (G90)
  Vista", "Paholiang Patio" -> 90 Paholiang) and 255 ("Odya Floating Islands", "Killair
  Odyalutai Helotto" -> 256 Odyalutai). **VERIFIED** (data) + **SOURCED** (list).
- Examples: Steam main planet bases: Euclid 20, Eissentam 17, Odyalutai 8, Isdoraijung 1; Xbox
  main: Euclid 14, Odyalutai 7, Eissentam 4, Hilbert Dimension 2, Hesperius Dimension 2,
  Ickjamatew 2, Calypso, Hyades, Budullangr, Kikolgallr, Eltiensleen (11 galaxies); Xbox slot 6
  Paholiang; Steam "Leviathan" Euclid 2 + Isdoraijung 2. **VERIFIED**
- Pitfalls: string-or-int encoding; external bases must be excluded; corvette/space/station
  bases also carry an address (galaxy of where they were last parked) - decide whether they
  count. Current galaxy is `PlayerStateData.UniverseAddress.RealityIndex` (Steam main 9
  Eissentam; Xbox main A member 3 Hesperius, B member 255 Odyalutai - members differ).
- Cost: full parse; lookup = 256 names (constant).

### 2.8 Additional facts, ranked by value for choosing which save to put live

Rank reflects "helps the owner recognise a save and decide" (INFERRED judgement); all paths and
examples **VERIFIED** unless marked.

| # | Fact | Path / logic | Example (Steam main unless noted) |
|---|---|---|---|
| 1 | Save name (better source on Xbox) | `CommonStateData.SaveName` (legacy: `PlayerStateData.SaveName`) | Xbox slots 4/9/14: meta name empty, JSON "CrossSaveTest" / "Exobiology1" / "The Cartographers" |
| 2 | Expedition in progress + which | `ExpeditionContext` present; `SeasonData.SeasonName/DisplayNumber`, milestones | Swarm, Expedition 22, 15/23 |
| 3 | Live context | `ActiveContext` Main/Season | 4 of 38 load into the expedition |
| 4 | Difficulty preset + history | `PlayerStateData.DifficultyState.{Preset,EasiestUsedPreset,HardestUsedPreset}.DifficultyPresetType` | Normal/Normal/Normal; s04: Custom, easiest Creative |
| 5 | Custom settings that matter | `DifficultyState.Settings.{DeathConsequences, CraftingIsFree, CurrencyCost, SettingsLocked, ...}` (30 settings) | ItemGrave; Survival s5 DestroyItems |
| 6 | Story position | `CurrentMissionID` + chains in 2.1 | EPILOGUE_NEWGAL |
| 7 | Current galaxy / place | `UniverseAddress.RealityIndex` + `SaveSummary` | Eissentam, "On Planet (Oscar Major)" |
| 8 | Units / nanites / quicksilver | `Units` (u32 mask), `Nanites`, `Specials` | 4,294,967,295 / 194,122 / 22,344 |
| 9 | Freighter | `CurrentFreighter.Filename` non-empty; `PlayerFreighterName`; `FreighterInventory.Class` | "HarmonicLepton", capital, S |
| 10 | Frigates | `len(FleetFrigates)` | 29 (Xbox Survival 30) |
| 11 | Multi-tools | `Multitools[]` with model path; `Name`; `Store.Class` | 5 incl. "Atlas Sceptre" (staff) |
| 12 | Corvettes | ships with `BIGGS` model | 3 |
| 13 | Settlements claimed | `SettlementHistory[PlayerClaimedTime>0]`, name via `SettlementStatesV2[SeedValue].Name` | 3: Mobaya Settlement, Lost Onjosefi, ... |
| 14 | Player space stations / outposts | `PlayerSpaceStationBase` / `PlayerSpaceBase` count | 5 / 2 |
| 15 | Words known per race | `KnownWordGroups[{Group, Races[9]}]`, race index 0 Gek, 1 Vy'keen, 2 Korvax, 4 Atlas, 8 Autophage | 373/374/408/114/181 (= stats) |
| 16 | Journey rank | stat `^JM` (INFERRED mapping) | Galileo (87) |
| 17 | Discoveries | `DiscoveryManagerData.DiscoveryData-v1.Store.Record[].DD.DT` counts, or `^DISC_*` | 3,244 records |
| 18 | Standings | `^TRA/WAR/EXP/BUI_STANDING`, guilds `^TGUILD/WGUILD/EGUILD_STAND`, `^PIRATE_STAND` | 311/341/320/49 |
| 19 | Deaths | `^DEATHS` | 2 |
| 20 | Last played per context | `PlayerStateData.TimeStamp` | expedition 2026-09-10 |
| 21 | Game version saved with | meta BaseVersion -> libNOM table (ends at Voyagers 6.00 = 4189; vault goes to 4228) | 4228 = "Voyagers or later" |
| 22 | Platforms that touched the save | base `Owner.PTK` set (ST, XB, NL...; never the ids) | Xbox slot 9: ST + XB bases (so it came over from Steam - INFERRED) |
| 23 | Exocraft | geobays in `BaseBuildingObjects` (`GARAGE_S/M/L/SUB/MECH`, `SUMMON_GARAGE`); `VehicleOwnership[7]` fixed slots | Xbox main: M, SUB, summoner (INFERRED) |
| 24 | Teleport endpoints / systems visited | `len(TeleportEndpoints)`; `VisitedSystems` ring (512) | 215; 512 (full) |
| 25 | Selected title | `BannerTitleId` | T_WAKE_TITAN |
| 26 | Expedition rewards earned on this save | `CommonStateData.EarnedSeasonSpecialRewards[]` | 12 |
| 27 | Living-ship / special ships owned | from ship types | The Wraith |

Not useful as-is: `CommonStateData.UsedPlatforms` is `["Win"]` for both Steam and Xbox PC saves
(`[]` on fresh saves) - it cannot tell Steam from Xbox. `CommonStateData.SaveUniversalId`
(a hex u64 string) is a per-save id worth handing to R1/R7 (identity), not a display fact.
`HomeRealityIteration` tracks `^HOME_REALITY` (9 Steam main, 8 Xbox main) and looks like the
number of centre jumps; meaning **INFERRED**.

### 2.9 Name-resolution tables

| Table | Needed for | Size | Source | Stability |
|---|---|---|---|---|
| Galaxy names | galaxies with bases, current galaxy | 256 | community list (10 in game data) | stable |
| Legacy part ids | legacy flag | 89 ids (+ modern twin) | basebuildingobjectstable / legacyitemtable | stable unless HG retires more |
| Ship model -> type, special names | ships | 9 folders + 9 models | rewardtable + model paths | grows with each expedition reward ship |
| Mission chains + final stages | milestones | ~130 ids x versions | missions tables | changes with every update (versioned) |
| Expedition number -> name | expeditions | 23 rows | historicalseasondatatable + loc | +1 per expedition; the save also embeds its own SeasonData |
| Journey rank names | journey | 12 | journeymilestonetable + loc | stable |
| Base part names | listing parts by name (optional) | 2,124 | basebuildingobjectstable + catalogue | grows every update |
| Creature ids -> friendly type | companions (optional) | ~30 used | none in game data (hand-made) | grows slowly |
| Item names | inventories (future) | 4,780 | item catalogue | grows every update |

All extracted for this research into the scratchpad (`lookups\*.json`, Appendix B).

### 2.10 Cost (measured on this machine)

| Step | Small save (2.4 MB JSON) | Main save (18.2 MB JSON) |
|---|---|---|
| stdlib LZ4 (`core/lz4_block`) | 0.12 s | 0.80 s |
| `json.loads` | 0.05 s | 0.23 s |
| all facts in this report from the parsed tree | 1 ms | 3-4 ms |
| de-obfuscating the whole tree (research only) | - | not needed: facts read ~150 known obfuscated keys |

The data file must be decoded fully for any fact here; a partial parse would only save the
`json.loads` share. Meta-only facts are already in `catalog.json`. **VERIFIED**

## 3. Implications for the design

- **Index per data blob, per context.** Facts belong to one member file and one context; the
  same slot's A/B members already differ (Xbox main: galaxy 3 vs 255; 1 vs 14 cached external
  bases). Key the index by content hash (S1/S2), store `{base: {...}, expedition: {...}}`.
- **Index once, reuse forever.** ~0.1-1 s per unique blob; 1,207 member copies in the vault
  collapse to far fewer unique hashes (R7). A background indexer with a persistent cache is
  enough; no need for partial parsing.
- **Embed a small key map, not the full mapping.** The catalog reads ~150 obfuscated keys
  (Appendix A), like `tools/nms_visit_history.py` does; a full mapping file is only needed for
  a deep "everything" index (R5's call).
- **Filter, normalise, sanitise at extraction.** Own vs external bases; u32 masking; hex/int
  addresses; surrogateescape; markup stripping; never persist `Owner`/`OWS`/`SenderData`/
  usernames. Write the index without any platform id.
- **Facts must degrade.** Unknown mission ids, ship models, part ids and expedition numbers will
  appear after every game update. Show raw ids ("Expedition #24", "BIGGS2") rather than
  failing; version the lookup tables with the game build they came from.
- **Diff-friendly facts power "what changed" notes (S4).** Counts plus sets (ship names, base
  names with part counts, companion names, chain progress, galaxies) diff cleanly between two
  versions of one save; currencies and stats diff as numbers.
- **Account-level facts are not save facts.** Titles, unlocked specials/rewards live in
  `accountdata.hg` (one per Steam folder / Xbox account); show them per account, not per save.
- **Expedition-only and legacy saves need their own code paths** (no BaseContext; top-level
  PlayerStateData with SaveName/TotalPlayTime inside).
- **Opportunity:** the save embeds data that answers questions the meta cannot: real save name
  on Xbox, last-played time per context, whether an expedition is running, which platforms
  built its bases.
- **Risk:** game data names are Hello Games content; shipping extracted tables in a GPL app is a
  licensing question for the owner (option list below).

## 4. Open questions and candidate options

1. **What does "legacy base parts" mean to the owner?**
   - (a) Parts from the LEGACY STRUCTURES / LEGACY FREIGHTER MODULES groups placed in own bases
     (count + which bases). Matches the game's own word; cheap.
   - (b) (a) plus "can still build them" (`KnownProducts`): flags old saves that hold a
     capability new saves cannot get.
   - (c) Old bases (`OriginalBaseVersion` < 8). Different concept; noisy.
   - Trade-off: (a)+(b) as two separate badges is the most informative and costs nothing extra.
2. **"Used base parts" scope.** (a) planet bases only; (b) all own bases incl. freighter,
   corvettes, stations, outposts; (c) per-base table with the largest base highlighted. (b)
   total + (c) on expand gives both; corvette orphans must be excluded either way.
3. **Ships: count corvettes and archived ships?** (a) 12-slot fleet incl. corvettes;
   (b) fleet + archive; (c) separate columns (ships / corvettes / archived).
4. **Companion names.** (a) custom names only; (b) custom name else creature type id;
   (c) also eggs.
5. **Which context represents a save with an expedition in progress?** (a) the active one;
   (b) base always, expedition as a badge with its progress; (c) two rows.
6. **Galaxy names source.** (a) embed the 256-name community list; (b) only game-data names
   1-10 and "Galaxy N" beyond; (c) reimplement the game's name generator (a third-party port
   exists for region/system/planet names, codeberg pithos/system-name-calculator-ts; galaxy
   coverage unknown) - more code, no table, and it would also name systems and planets.
7. **Where lookup tables come from.** (a) small hand-curated constants in the app (ids only
   where possible, English names for galaxies/ships/expeditions); (b) generated tables shipped
   per game build (needs an update process; HG-content licensing question); (c) read the
   user's installed game data at runtime (needs a pak + MBIN reader: not stdlib-friendly,
   heavy). (a) is cheapest and degrades to raw ids.
8. **Milestone presentation.** (a) a few chips (Story done / ending, Atlas n/11, Sentinel,
   Autophage, Expedition n/m); (b) full checklist per chain; (c) journey rank only.
9. **Index depth.** (a) the catalog in this report only (KBs per blob); (b) catalog + per-base
   part histograms + inventories for future features; (c) keep a de-obfuscated copy per unique
   blob (up to 40 MB each pretty-printed) for ad-hoc queries. The raw blob stays in the vault
   anyway, so (a)/(b) with re-indexing on demand avoids storing (c).

Smaller points for the parent: the journey-rank mapping (`^JM` / 5) and the "season milestone
done = value >= 1" rule should be checked against the in-game screens once; the Worlds Part I
("Titan") chain ids were never started in any sample, so their completion values are untested.

## 5. Master table

Example columns use: SM = Steam main "Main Save (S2)" 532 h; XM = Xbox main "Main Save XBox
(X4)" 296 h; early = a 0.2-2 h save. PSD = `PlayerStateData` of the context.

| Fact | Path (readable) | Logic | Example | Lookup | Cost | Confidence |
|---|---|---|---|---|---|---|
| Glyphs known | PSD.KnownPortalRunes | popcount(& 0xFFFF) | SM 16, XM 16, Xbox s3 15, early 0 | - | full parse | VERIFIED |
| Story (Artemis) progress | PSD.MissionProgress[Mission ACT*_STEP*].Progress | done if >= final stage for MissionVersion | SM 30/30, XM 30/30, Steam s10 0/30 | missions final stages | full parse | VERIFIED |
| Story ending / choices | MissionProgress EPILOGUE_NEWGAL/STAY, ART_DEAD/ART_SIM | started or done = chosen | SM new galaxy + Artemis dead; XM stayed + simulation | ids | full parse | VERIFIED |
| Atlas Path | MissionProgress ATLAS1-11, ATLAS_LOOP_STAR/DENY; stat ^ATLAS_PATH | count done | SM 11/11, star seed | missions | full parse | VERIFIED |
| Sentinel / Autophage / purple / pirates / water / living ship / Overseer | MissionProgress chains (2.1) | count done | SM 5/5, 4/4, 3/3, 3/3, 5/5, 5/5, 10/10; XM pirates 0/3 | missions | full parse | VERIFIED values, INFERRED chain membership |
| Current mission | PSD.CurrentMissionID | raw id | SM EPILOGUE_NEWGAL | optional names | full parse | VERIFIED |
| Journey rank | GLOBAL_STATS ^JM | rank = JM // 5 capped at 11 | SM Galileo (87), early Apollo (0) | 12 names | full parse | INFERRED mapping |
| Expedition | CommonStateData.SeasonData / SeasonState; ExpeditionContext present | SeasonId 0 = none; name from SeasonName loc key | SM Swarm (Exp. 22) 15/23; Xbox s12 OJC (Exp. 23) 9/48 | 23 names | full parse | VERIFIED (done-rule INFERRED) |
| Titles unlocked | accountdata UserSettingsData.UnlockedTitles | count (account-level) | Steam account 286 | 346 rules (optional) | account file parse | VERIFIED |
| Base parts used | PSD.PersistentPlayerBases[own types].Objects | sum len; own = Home/Freighter/PlayerShip/PlayerSpace/PlayerSpaceStation; drop orphan corvette layouts | SM 57 bases / 15,016; XM 38 / 7,156; early 0 | - | full parse | VERIFIED |
| Largest base | same | max len(Objects), with Name | SM Rerias Outpost 2,921 | - | full parse | VERIFIED |
| Legacy parts placed | own bases' Objects[].ObjectID in 89-id set | count by group, bases affected | All Parts Building 490 + 7 in 4 bases; Xbox s3 43 freighter | 89 ids | full parse | VERIFIED |
| Legacy parts buildable | PSD.KnownProducts | count of the 89 ids | Polestar / All Parts 69+20; XM 0+20 | 89 ids | full parse | VERIFIED |
| Old bases | PersistentPlayerBases[].OriginalBaseVersion | < 8 | values 3/5/6/7 seen | - | full parse | VERIFIED values, INFERRED meaning |
| Ships (count, names, types, class) | PSD.ShipOwnership[].{Name, Resource.Filename, Inventory.Class.InventoryClass} | non-empty Filename; type from folder/special model | SM 12/12 (3 corvettes); XM 11; early 1 | folder map + 9 specials | full parse | VERIFIED (display names INFERRED) |
| Primary ship | PSD.PrimaryShip | index into ShipOwnership | SM 8 = Glass Arachnid | - | full parse | VERIFIED |
| Archived ships | PSD.ArchivedShipOwnership[].{ArchivedName, ArchivedClass.ShipClass} | non-empty Ownership model | SM 5, XM 3, Steam s2 18 | - | full parse | VERIFIED |
| Companions | PSD.Pets[].{CustomName, CreatureID, Biome} | CreatureID != "^"; name = CustomName else id | SM 21 (4 named); XM 9 (0 named) | optional id->type | full parse | VERIFIED |
| Eggs / pet slots | PSD.Eggs[], PSD.UnlockedPetSlots[] | non-empty / count true | SM 8 eggs, 30 slots | - | full parse | VERIFIED |
| Galaxies with bases | own HomePlanetBase GalacticAddress | (n >> 32) & 0xFF -> name | SM Euclid 20, Eissentam 17, Odyalutai 8, Isdoraijung 1; XM 11 galaxies | 256 names | full parse | VERIFIED (+SOURCED names) |
| Current galaxy | PSD.UniverseAddress.RealityIndex | index -> name | SM Eissentam; XM Hesperius (A) / Odyalutai (B) | 256 names | full parse | VERIFIED |
| Words known | PSD.KnownWordGroups[].Races[i] | count per race | SM 1450 (Gek 373, Vy'keen 374, Korvax 408, Atlas 114, Autophage 181) | race order | full parse | VERIFIED |
| Difficulty | PSD.DifficultyState.Preset / Easiest / Hardest / Settings | raw enums | SM Normal; s04 Custom (easiest Creative) | - | full parse (preset also in meta) | VERIFIED |
| Currencies | PSD.Units, Nanites, Specials | Units & 0xFFFFFFFF | SM 4,294,967,295 / 194,122 / 22,344 | - | full parse | VERIFIED |
| Freighter + frigates | PSD.CurrentFreighter.Filename, PlayerFreighterName, FreighterInventory.Class, FleetFrigates | has = Filename non-empty | SM "HarmonicLepton" S, 29 frigates | - | full parse | VERIFIED |
| Multi-tools | PSD.Multitools[].{Name, Resource.Filename, Store.Class} | non-empty Filename | SM 5 incl. Atlas Sceptre | model -> kind | full parse | VERIFIED |
| Settlements | PSD.SettlementHistory + SettlementStatesV2 | PlayerClaimedTime > 0; name by SeedValue | SM 3 | - | full parse | VERIFIED |
| Stations / space outposts | own PlayerSpaceStationBase / PlayerSpaceBase | count | SM 5 / 2 | - | full parse | VERIFIED |
| Discoveries | DiscoveryManagerData...Store.Record[].DD.DT or ^DISC_* | count | SM 3,244 records, 1,221 systems | - | full parse | VERIFIED |
| Standings, deaths | GLOBAL_STATS ^*_STANDING, ^DEATHS | raw | SM 311/341/320/49; 2 deaths | - | full parse | VERIFIED |
| Real save name | CommonStateData.SaveName (legacy: PSD.SaveName) | prefer over empty meta name | Xbox s4 "CrossSaveTest" | - | full parse | VERIFIED |
| Active context | ActiveContext | Main / Season | 4 of 38 Season | - | full parse | VERIFIED |
| Last played per context | PSD.TimeStamp | unix time | SM expedition 2026-09-10 | - | full parse | VERIFIED |
| Game version | meta BaseVersion | libNOM thresholds | 4228 = Voyagers or later | libNOM table | meta only (free) | SOURCED |
| Platforms that built bases | own bases Owner.PTK | distinct set, never ids | Xbox s9 {ST, XB} | - | full parse | VERIFIED |
| Exocraft | BaseBuildingObjects GARAGE_* / SUMMON_GARAGE | ids present | XM M, SUB, summoner | - | full parse | INFERRED |
| Name, summary, play time, mode, season, timestamp | meta (already catalogued) | - | - | - | free | VERIFIED (DESIGN.md) |

## Appendix A - obfuscated key paths (verified on the Steam main)

Base context shown (`vLc` = BaseContext, `6f=` = PlayerStateData); the expedition context is
`2YS.6f=...`, the legacy layout is top-level `6f=...`.

| Readable | Obfuscated |
|---|---|
| Version / Platform / ActiveContext | `F2P` / `8>q` / `XTp` |
| CommonStateData.{SaveName, TotalPlayTime, SaveUniversalId, UsedPlatforms} | `<h0.{Pk4, Lg8, WmU, xgu}` |
| CommonStateData.SeasonData.{SeasonNumber, SeasonName, DisplayNumber} | `<h0.Rol.{>dq, pU@, oew}` |
| CommonStateData.SeasonState.{MilestoneValues, HasCollectedFinalReward} | `<h0.qYy.{psf, erT}` |
| CommonStateData.EarnedSeasonSpecialRewards | `<h0.CJJ` |
| BaseContext / .GameMode / .PlayerStateData / ExpeditionContext | `vLc` / `vLc.idA` / `vLc.6f=` / `2YS` |
| DiscoveryManagerData.DiscoveryData-v1.Store.Record[].DD.DT | `fDu.ETO.OsQ.?fB[].8P3.<Dn` |
| ExpeditionContext.{GameMode, PlayerStateData, SpawnStateData} | `2YS.{idA, 6f=, rnc}` |
| PSD.SaveSummary / TimeStamp / StartingSeasonNumber | `n:R` / `b@r` / `6UA` |
| PSD.UniverseAddress.{RealityIndex, GalacticAddress} | `yhJ.{Iis, oZw}` |
| PSD.DifficultyState.{Preset.DifficultyPresetType, EasiestUsedPreset, HardestUsedPreset, Settings.DeathConsequences} | `LyC.{brJ.7ND, qAf, 4I:, :fe.n7p}` |
| PSD.Units / Nanites / Specials / KnownPortalRunes | `wGS` / `7QL` / `kN;` / `vrS` |
| PSD.KnownWordGroups[].{Group, Races} / KnownProducts | `MF2[].{MYl, D;o}` / `eZ<` |
| PSD.MissionProgress[].{Mission, Progress} / MissionVersion / CurrentMissionID | `dwb[].{p0c, tW6}` / `yq:` / `;R7` |
| PSD.Stats[].{GroupId, Stats[].{Id, Value.{IntValue, FloatValue}}} | `gUR[].{:rc, gUR[].{b2n, >MX.{>vs, eoL}}}` |
| PSD.BannerTitleId / HasAccessToNexus / HomeRealityIteration | `aNF` / `Ue9` / `QQp` |
| PSD.PersistentPlayerBases[].{BaseType.PersistentBaseTypes, GalacticAddress, Name, Objects[].ObjectID, BaseVersion, OriginalBaseVersion, UserData, Owner} | `F?0[].{peI.DPp, oZw, NKm, @ZJ[].r<7, h4X, BpT, CVX, 3?K}` |
| PSD.BaseBuildingObjects[].ObjectID | `c5s[].r<7` |
| PSD.ShipOwnership[].{Name, Resource.Filename, Resource.Seed, Inventory.Class.InventoryClass} / PrimaryShip | `@Cs[].{NKm, NTx.93M, NTx.@EL, ;l5.B@N.1o6}` / `aBE` |
| PSD.ArchivedShipOwnership[].{ArchivedName, ArchivedClass.ShipClass} | `g33[].{O=l, @P8.eEU}` |
| PSD.Pets[].{CustomName, CreatureID, Biome, BirthTime} / Eggs / UnlockedPetSlots | `Mcl[].{fH8, XID, 8jm, XwC}` / `uKZ` / `9Lg` |
| PSD.CurrentFreighter.Filename / PlayerFreighterName / FreighterInventory / FleetFrigates | `bIR.93M` / `vxi` / `8ZP` / `;Du` |
| PSD.Multitools[].{Name, Resource, Store} / VehicleOwnership | `SuJ[].{NKm, NTx, OsQ}` / `P;m` |
| PSD.SettlementHistory[].{PlayerClaimedTime, SeedValue} / SettlementStatesV2[].{Name, SeedValue} | `Hdt[].{<:S, qK9}` / `GQA[].{NKm, qK9}` |
| PSD.TeleportEndpoints / VisitedSystems / RedeemedSeasonRewards | `nlG` / `nwS` / `FnB` |

The same obfuscated key can mean different things in different places only for `NE3` (R5);
none of the keys above are affected. `Name` is `NKm` everywhere it appears here.

## Appendix B - research artifacts (throwaway, session scratchpad, not in the repo)

`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\be828316-f5a0-4419-bd50-12964a376ba0\scratchpad\R6\`

- `saves\<tag>\{data.bin, meta.bin, obf.json, deob.json}` - the 39 copies; `saves\manifest.json`.
- `lookups\` - `baseparts.json` (2,124 ids: name, groups, legacy group, menu visibility,
  complexity flag), `items.json` (4,780), `missions.json` (1,836 missions with final stages
  per version), `galaxies.json` (256), `seasons.json`, `obf_paths.json`.
- `facts\<tag>.json` - every fact in this report computed per save and context;
  `extract_facts.out` is the one-page summary.
- Scripts: `copy_samples.py` (read-only copy), `decode_all.py`, `deob_all.py` (R5 Mapper),
  `build_lookups.py` (reads `F:\NMS-Unpack` metadata index read-only), `scan_bases.py`,
  `extract_facts.py`, `obf_paths.py`.

Sources: libNOM.io `Meta/GameVersion.cs` and `Enums/GameVersionEnum.cs`
(https://github.com/zencq/libNOM.io); galaxy list
https://www.destructoid.com/how-many-galaxies-are-in-no-mans-sky/ ; legacy structures
community threads https://steamcommunity.com/app/275850/discussions/0/3183488724676494174 and
https://steamcommunity.com/app/275850/discussions/0/3279192886588693449 ; game data build
25729564 (`basebuildingobjectstable`, `legacyitemtable`, `legacybasebuildingtable`, mission
tables, `journeymilestonetable`, `journey`, `leveledstatstable`, `playertitledata`,
`rewardtable`, `historicalseasondatatable`, `creaturedatatable`, English localisation).
