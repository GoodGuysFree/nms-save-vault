# R9 - What Hello Games cross-save changes in a save's data

Status: research. Author: R9 subagent, 2026-10-18. Feeds D5 3.5 (F6 Steam <-> Xbox transfer) and
the owner runs T1/T2.

Question: when cross-save delivers a save from Steam to Xbox (or back), what changes in the save
data? Can F6 copy the data verbatim and convert only the meta, or must it rewrite the JSON?

Tags: VERIFIED = measured on the owner's real saves (read-only); INFERRED = reasoning.

## 1. Answer

1. **Cross-save delivery does not rewrite ownership.** VERIFIED. Both delivered-but-unplayed
   Xbox copies in the data (`..1C01` 8B, `..5151` 4A) still carry the source platform's
   identity on every own base and every own discovery.
2. **The game rewrites ownership itself**, at the first save under the other platform's
   identity, in both directions. VERIFIED. It touches three path families (section 5). The
   change is systematic (identity A -> identity B) but has exceptions the game chooses.
3. **Nothing else is platform-specific.** VERIFIED. The key schema, root `Platform`
   ("Win|Final"), `UsedPlatforms` (["Win"]), JSON text form and container layout are the same on
   both platforms. All other differences are play, timestamps the game rebases at every save,
   and game-version upgrades.
4. **Meta and account data.** VERIFIED. The metas differ only in the known field map (0 non-zero
   bytes outside it). `accountdata` is identical on both platforms, holds no ids, and is not
   involved.
5. **A verbatim data copy plus the meta conversion reproduces what cross-save delivers** for the
   owner's own accounts. No JSON rewrite is needed. Still UNTESTED: whether the game re-owns a
   copy that never passed Hello Games' servers (T1/T2 decide).

## 2. Method

- Sources (opened for reading only): live Steam and Xbox folders, the classic vault (49 catalog
  entries) and `D:\VaultCopy` (catalog plus 48 store manifests). That is 3,476 member records,
  30 playthroughs, and 4 playthroughs on both platforms (the same `SaveUniversalId`), with 38
  distinct data versions between them (8 Xbox, 30 Steam).
- Decoding: `core.savejson.decode_save`. Keys were named by running `core.hashname.hash_name`
  over the MBINCompiler and libNOM name lists; 0 keys were left unnamed.
- Diffs:
  1. structural, index-aligned;
  2. record-aligned for owner-bearing lists: bases by address and position, discovery records
     by `DD`, settlements by `UniqueId`;
  3. key-path schema;
  4. JSON text and container facts.
- Controls: Steam -> Steam pairs of the same playthrough show what play alone changes.
- Values are reduced to kinds (own steamid64, own XUID, other id, platform token, unix time and
  so on). No ids, names or coordinates appear here or in the outputs.

## 3. Pairs used

| Pair | From -> to | Extra play | Isolates |
|---|---|---|---|
| C (best) | `..5151` Xbox 4B (native) -> Xbox 4A (saved on Steam, delivered back, unplayed) | 59 s on Steam | X -> S re-own plus delivery, same game version |
| A | `..1C01` Xbox 8B (delivered from Steam, unplayed) -> Xbox 8A (first save on Xbox) | 0 s; game update between | S -> X re-own |
| B | `..1C01` Xbox 8B -> the nearest Steam version | 1,611 s on Steam | the delivered copy vs its source line |
| D | `..D19E` Xbox -> Steam | 5,686 s | X -> S |
| E | `..A74F` Xbox -> Steam | 1,413 s | X -> S, mixed-owner case |
| Controls | 4 Steam -> Steam pairs (`..1C01`, `..D19E`) | 1,293-4,755 s | play alone |

0 decompressed-JSON hashes are shared across platforms. No Steam version exists at either
delivered copy's play time (139,170 s and 37,866 s): the sources were never captured.

## 4. Differing paths, pair C (58,255 leaves in 42 normalised paths)

| Category | Paths (names only) | Leaves | Platform-systematic? |
|---|---|---|---|
| (a) play | `TerrainEditData.{Edits[].Position, Edits[].Data, BufferAges, BufferSizes}`; `PersistentPlayerBases[].Objects[].UserData`; `SpawnStateData.*`, `TeleportEndpoints[].*`, `Nexus*`, `Stats`, `MissionProgress`, `WonderPlanetRecords`, `SavedInteractionDialogTable`, `StoredInteractions` | 51,947 | no; the same paths change in the Steam -> Steam controls |
| (b) ownership | `PersistentPlayerBases[].Owner.{UID, LID, PTK}`; `DiscoveryManagerData.DiscoveryData-v1.Store.Record[].{OWS.UID, OWS.PTK, PTK}` | 42 (3 bases, 11 records) | yes: the game's re-own (section 5) |
| (c) platform tokens | none outside the owner `PTK`s; root `Platform` and `CommonStateData.UsedPlatforms` unchanged | 0 | no |
| (d) timestamps | `PersistentPlayerBases[].Objects[].Timestamp` (each moved by exactly the gap between the two saves), `TradingSupplyData[].Timestamp`, `PlayerStateData.TimeStamp`, `FreighterLastSpawnTime`, `LastCheckedForStatResetsTime`, base `LastUpdateTimestamp`, `CommonStateData.TotalPlayTime`, `TimeAlive` | 6,266 | no; a Steam -> Steam control moves 5,135 object timestamps by its 31.2 h gap |
| (e) other | none. Both versions have the same 2,335 key paths. `CommonStateData` differs only in `TotalPlayTime`, and the root is identical. | 0 | - |

The other pairs add only (a), (d) and game-version upgrades:

- **Pair A (4716 -> 4733).** The upgrade adds 124 key paths (for example `Pets[].PetBattler*`,
  `CommunityTeamData`, `Swarm*`) and drops `ActiveSpaceBattleLevel`; Steam shows the same
  upgrade. `FL.C` is dropped on 98 records, and 186 cached names of other players' bases are
  cleared once.
- **Pair B.** The schema is identical (4,762 paths each), and no owner `UID`, `LID` or `PTK`
  differs. 776 of 782 own discoveries are identical.
  - `Owner.USN` flips between empty and the name on 20 own bases. It does the same between
    Steam versions (17 in a control).
  - Xbox 8B caches 178 more of other players' bases. That cache also swings between 0.96 and
    4.1 MB across consecutive Steam versions.
- **Text and container, all 38 versions.** 1 byte after the NUL, 524,288-byte chunks and 0
  trailing bytes, on both platforms.

## 5. The ownership rewrite (made by the game, not by cross-save)

| Field | Steam form | Xbox form | Rewritten at re-own |
|---|---|---|---|
| own base `Owner.UID` | steamid64 | XUID | yes |
| own base `Owner.LID` | steamid64 | `"00000000000000000001"` | yes |
| own base `Owner.PTK` | `ST` | `XB` | yes |
| own base `Owner.USN` | name | name | not observable: the owner uses one name on both platforms |
| own discovery `OWS.UID` | steamid64 | XUID | yes |
| own discovery `OWS.PTK` | `ST` or `""` | `XB` or `""` | yes, set to the target |
| own discovery `OWS.LID` | as discovered | as discovered | no: keeps the discovering platform's LID |
| own discovery `Record.PTK` (record level) | - | - | set to the target; later saves clear it (INFERRED: an upload marker) |
| `CommonStateData.UsedDiscoveryOwnersV2` | - | - | appends `{UID, PTK, TS}` (LID and USN empty) when the identity is new; unchanged when it is already listed (pair C) |
| own `SettlementStatesV2[].Owner` | - | - | no: `..1C01`'s own settlement stays Steam-owned on Xbox |
| other players' owners (bases, settlements, discoveries, `SyncBuffersData` `OwnerOnlineId`/`OwnerPlatformId`) | - | - | no |

- **Scope.** Only the owner's own records (`UID` = the save's previous platform identity) are
  rewritten. Other players' records never change.
- **Size.** Pair A: 28 bases x 3 fields + 782 records x 3 fields = 2,430 leaves, plus one list
  entry, in an 11.8 MB JSON. Pair C: 42 leaves.
- **When it happens.**
  - Delivered, unplayed copies keep the source identity: 8B has 28/28 bases and 782/782
    records on Steam; 4A is on Steam after the Steam save.
  - The rewrite appears at the first save on the target (8A, same play time).
  - It runs even when the identity is already listed (pair C), so R8's "only when a new
    identity is appended" is wrong.
- **Not always complete.** `..A74F`'s Xbox copies keep 2 of 4 own bases (the freighter and one
  base) on the Steam owner.

## 6. Meta and account data

- **Meta.** 10 metas from the pairs were checked: 0 non-zero bytes lie outside the D5 3.5.2
  field map, and the size field equals the decompressed size in all of them.
  - 8B (delivered) -> 8A (first Xbox save): of the copied fields only BaseVersion differs;
    time and size change as on any save.
  - UID is equal in every pair. Name, summary and difficulty differ only where the player
    changed them (renames, location, difficulty preset). In pair C, the strings up to the NUL
    are equal.
- **accountdata.** The live Steam `accountdata.hg` and Xbox `AccountData` decode to identical
  JSON (105,903 B). It holds no own or foreign ids and no platform tokens. Hello Games syncs it
  on its own, and a slot transfer neither needs it nor touches it.

## 7. Conclusion for F6

- **Verbatim copy: yes, for the owner's own Steam <-> Xbox.** The data plus the D5 3.5.2 meta
  conversion gives the state that cross-save itself delivers: data with the source identity,
  which the game re-owns at the first save on the target.
- **A deterministic rewrite of the three path families is possible but not wanted.**
  - It would only pre-empt the game.
  - It needs re-serialising and recompressing, which breaks "never recompress".
  - It would also have to copy the game's exceptions (discovery `OWS.LID`, settlements,
    incomplete cases).
  - D5's rejection of option (b) stands.
- **Still UNTESTED; T1/T2 decide.**
  1. Does the game re-own a copy that never passed Hello Games' servers? The file carries no
     cross-save marker (pair C differs only by play, time and owner), so nothing in the data
     tells the game where the copy came from (INFERRED).
  2. A save whose other identity belongs to a different person.
- **Corrections to earlier reports.**
  - R8 D5 "Xbox LID = XUID": the Xbox base LID is `"00000000000000000001"`.
  - R8 D5 "only when a new identity is appended": falsified by pair C.
  - D5 3.5 "the game re-writes a save when cross-save delivers it": unproven. The source
    versions were never captured, so 0 identical pairs is expected.

## Evidence index

Scripts and outputs are in `<scratchpad>\xsave\`, where `<scratchpad>` =
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\<session>\scratchpad`. All are read-only and
print kinds and counts only.

| Script | Output | Shows |
|---|---|---|
| `xs_enum.py` | `xs_enum.out.txt`, `versions.json` | version inventory, cross-platform playthroughs, 0 shared JSON hashes |
| `xs_census.py` | `xs_census.out.txt`, `census.json` | owner-field kinds per version |
| `xs_owned.py` | `o_8B_8A.txt`, `o_8B_S1A.txt`, `o_S1A_S13A.txt`, `o_D19E.txt`, `o_5151_4B4A.txt`, `o_5151_4A3B.txt`, `o_A74F.txt` | record-aligned owner diffs (sections 4-5) |
| `xs_pair.py` | `p_5151_4B4A.txt`, `p_8B_8A.txt`, `p_8B_S1A.txt` | all differing paths and kinds (section 4) |
| `xs_schema.py` | `xs_schema.out.txt`, `xs_pathsets.out.txt` | key-path sets, text and container facts |
| `xs_tsdelta.py` | console | object-timestamp rebase equals the save gap |
| `xs_sizes.py` | `xs_sizes.out.txt` | section sizes (cached foreign bases) |
| `xs_meta.py` | `xs_meta.out.txt` | meta field equality, stray bytes |
| `xs_acct.py` | `xs_acct.out.txt` | accountdata Steam == Xbox, no ids |
