# R5 - Full-save decoding, key mapping, performance, index storage

Research wave 1, 2026-10-16. Feeds subject S2 (content index), and parts of S3/S4 (history, changelog).

Everything here was measured on **copies** in the R5 work dir, written below as `R5WD` =
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\be828316-f5a0-4419-bd50-12964a376ba0\scratchpad\R5`.
No vault, live, safety-backup or app-state file was written. Scripts, raw outputs and decoded samples
stay in `R5WD` (never in the repo).

Machine: Intel Core Ultra 9 275HX (24 cores), 64 GB RAM, Windows 11, CPython 3.12.10 (the version the
kit ships). Typical user hardware will be slower (see F4).

Tags: **VERIFIED** = tested/measured here; **SOURCED** = cited; **INFERRED** = reasoning.

---

## 1. TL;DR

1. Obfuscated keys are a 3-character hash of the readable field name (MBINCompiler `HashName`:
   SpookyHash128, fixed seed). A 110-line pure-stdlib port reproduces **1476/1476** keys of the latest
   mapping, so extractors can be written with readable names and need **no mapping file**. VERIFIED.
2. Mapping files exist if a full de-obfuscation is wanted: MBINCompiler `mapping.json` (LGPL-3.0, 61 KB,
   a release asset: 163 releases carry it since 2021-09) and libNOM.map (GPL-3.0; adds 47 legacy keys,
   save/account split, one collision rule). Both are licence-compatible with this GPL-3 app. SOURCED.
3. The latest mapping alone leaves 3 keys unknown in 71 of the vault's 195 distinct versions (fields
   removed from libMBIN but still in saves); latest + libNOM's embedded + legacy leaves 0. VERIFIED.
4. A full content index of the **whole real vault** (195 distinct versions, 1.56 GB of JSON) takes
   **93.5 s** with today's pure-Python decoder in one process, **42 s** with a slice-copy fix, **11-24 s**
   with 4 worker processes; slowest single save 1.1 s; 0 errors. VERIFIED.
5. 74% of that time is decompression. Replacing the per-byte match copy in `core/lz4_block.py:111-114`
   with slice copies makes it 3.2-4.9x faster, byte-identical on all 195 versions. json.loads (~75-90
   MB/s) is then the floor. VERIFIED.
6. json.loads holds the GIL: a background **thread** freezes Tk 0.2-0.4 s per large save; a **spawn
   process pool** keeps Tk at idle latency. multiprocessing works inside (a copy of) the shipped kit,
   launched GUI-style through sitecustomize; pool.terminate() cancels in 18 ms. VERIFIED.
7. Memory: Python heap ~8x the JSON size (151 MB for the 18 MB main save); ~200 MB per worker. The
   decompressed size is known from chunk headers before decoding, so work can be scheduled by size.
   VERIFIED.
8. A partial (first-chunk, 22 ms) decode gives only "header" facts: name, play time, playthrough id,
   current galaxy, difficulty, companions, known tech/words. Bases (median 40% into the stream), ships,
   glyphs, frigates (~75%) and discoveries (92%) need a full decode. VERIFIED on all 195 versions.
9. Storage: sqlite3 3.49.1 with JSON1 + FTS5 ships in the kit; the derived index is tiny (4.3 MB sqlite
   or 1.5 MB JSON for 195 versions) and can be rebuilt from the save bytes keyed by sha256 + extractor
   version. Notes are user data and must not live only in that cache. VERIFIED.
10. Fact-level "what changed" diffs are free (37 diffs in 2.2 ms) but only meaningful against the true
    lineage parent and on ownership-filtered facts; raw tree diffs are noise (194k changed leaves).
    VERIFIED.
11. Robustness gaps before an indexer feeds every file to the core decoder: Xbox `HGSAVEV2` blobs are not
    decodable (constant defined, never used), no cap on claimed chunk size (u32 -> up to 4 GB
    allocation), invalid UTF-8 in procedural ids needs `surrogateescape`. VERIFIED.
12. Side issue: `packaging/sitecustomize.py:125-126` `-c`/`-m` bypass never fires, so `nmsvault-cli.exe -c`
    runs the CLI parser; the multiprocessing bypass does work. VERIFIED.

---

## 2. Findings

### F1. Where readable key names come from (topic a)

**F1.1 The key is a hash of the name.** SOURCED + VERIFIED.
- MBINCompiler `SaveFileMapping/Program.cs:31-48` (`HashName`, credited to AndASM):
  `SpookyHash128(utf8(name), seed1 = seed2 = 8268756125562466087)`, then three characters from
  `UHash1`: `h % 68`, `(h >> 21) % 68`, `(h >> 42) % 68`, each `+ '0'`, `+6` if above `'Z'` (alphabet
  `0-9 : ; < = > ? @ A-Z a-y`).
  https://github.com/monkeyman192/MBINCompiler/blob/development/SaveFileMapping/Program.cs
- Port: SpookyHash V2 short path + HashName in pure Python, `R5WD\scripts\spooky.py` (~110 lines,
  stdlib `struct` only). Reproduces **1476/1476** pairs of mapping.json 7.7.0.1 and **47/47** of
  libNOM.map's legacy file. 2.9 us per name. VERIFIED.
- SpookyHash is public domain ("SpookyHash is a public domain noncryptographic hash function",
  https://burtleburtle.net/bob/hash/spooky.html). SOURCED.
- Consequences: a key never changes meaning while a field keeps its name; new fields add keys; removed
  fields leave orphan keys in older saves; ambiguity only through hash collisions (one known, `NE3`).
  Examples: `KnownPortalRunes`=`vrS`, `PersistentPlayerBases`=`F?0`, `ShipOwnership`=`@Cs`, `Pets`=`Mcl`,
  `UniverseAddress`=`yhJ`, `RealityIndex`=`Iis`.

**F1.2 MBINCompiler mapping.json.** SOURCED + VERIFIED.
- Built by `Program.cs` from libMBIN's reflected field names of `GcPlayerStateData`,
  `GcPlayerCommonStateData`, `GcGalaxyWaypoint`, ~40 hand-added container names (`Version`, `Platform`,
  `ActiveContext`, `BaseContext`, `ExpeditionContext`, `PlayerStateData`, `DiscoveryManagerData`,
  `DiscoveryData-v1`, `Store`, `Record`, `DD`, `DT`, `UA`, ...), then an account half starting at
  `UserSettingsData` (`Program.cs:144`).
- Format: `{"libMBIN_version": "7.7.0.1", "Mapping": [{"Key": "<obf>", "Value": "<name>"}, ...]}`.
- Published as an MBINCompiler release asset: 163 releases carry `mapping.json` since 2021-09
  (14/35/19/31/37/27 per year 2021-2026); latest v7.07.0-pre1, 2026-10-08
  (`gh api repos/monkeyman192/MBINCompiler/releases`). Licence **LGPL-3.0** (`LICENSE.md`; GitHub's API
  shows NOASSERTION only because it does not classify that file).
- Churn (save half; `R5WD\scripts\mapdiff.py` over downloaded releases):

| Release | Date | Pairs (save + account) | Save-half keys added / removed vs previous row |
|---|---|---|---|
| v3.75.0 | 2021-12 | 761 (691 + 70) | - |
| v4.4.0 (Waypoint era) | 2022-10 | 983 | +212 / -3 |
| v4.60.0 | 2024-03 | 1166 | +132 / -2 |
| v5.02.0 (Worlds I) | 2024-07 | 1186 | +14 / 0 |
| v5.54.0 | 2025-02 | 1241 | +55 / -4 |
| v6.17.0 | 2025-11 | 1345 | +88 / -1 |
| v6.44.0 | 2026-06 | 1424 | +80 / -1 |
| v7.07.0 | 2026-10 | 1476 (1302 + 174) | +40 / 0 |

  Size 29.8 KB (2021) -> 60.8 KB (2026). No key changed meaning except the collision `NE3`
  (`ShipShield`, and since v5.54 also `AllowedToBePurpleWithoutAccess`).

**F1.3 libNOM.map** (https://github.com/zencq/libNOM.map, GPL-3.0 per the GitHub API). SOURCED.
- Embeds `mapping_mbincompiler.json` (now libMBIN 6.11.0.1, 1345 pairs), `mapping_legacy.json` (47
  names no longer in libMBIN, e.g. `ThirdPersonShip`, `BuildingType`) and `mapping_savewizard.json` (one
  rename for SaveWizard-edited saves, de-obfuscation only).
- Rules: each list is split at the `UserSettingsData` entry into save vs account maps (`Mapping.cs:110`);
  merge order embedded -> legacy -> wizard -> downloaded-if-newer (`Mapping.cs:89-98`); first match wins
  (`Mapping_Deobfuscation.cs:45`), so the embedded file beats a download for shared keys; `NE3` is
  resolved by path (`SolarSystemAttributes`, `Mapping.cs:29`).
- Update path: optional download of the latest release asset through the GitHub API (Octokit,
  unauthenticated, checks the 60/hour rate limit first; `Services/GithubService.cs:45-52`), stored on
  disk, used from the next start.

**F1.4 Coverage on the real vault.** VERIFIED (`R5WD\scripts\unknown_keys.py`, all 195 distinct
versions, 1281 distinct keys in use):

| Mapping set | Save-half keys | Unknown keys in the vault | Versions affected |
|---|---|---|---|
| latest 7.7 only | 1301 | 3: `a0>` BuildingType, `bQN` ActiveSpaceBattleLevel (dropped in 6.44), `cid` ThirdPersonShip | 71 / 195 |
| 7.7 + libNOM embedded 6.11 + legacy | 1349 | 0 | 0 |
| 2024-07 (v5.02) only | 1044 | 259 | 194 |
| 2021-12 (v3.75) only | 691 | 612 | 195 |

A stale bundled mapping hurts only a raw "show me the JSON" viewer; extractors that look up the names
they know are unaffected by fields they do not know.

**F1.5 The project today.**
- `tools/nms_visit_history.py:39-60` hard-codes 12 obfuscated keys and a 10-entry local map (readable
  names in comments); decodes with `errors="replace"` (`:171`, lossy, fine for a report).
- `core/playtime.py:32` hunts `"WmU"` in raw bytes. In libMBIN 7.7 `WmU` is
  `CommonStateData.SaveUniversalId` (the regex/docstring call it ActiveSaveSlotId - naming only). It sits at
  ~66 KB in all 193 modern versions (F2.3).
- Costs if a mapping file were bundled: 61 KB, `json.loads` 0.4 ms. VERIFIED.

**F1.6 Samples for R6/R7.** VERIFIED. `R5WD\decoded\save17.deob.json` ('Main Save (S2)', Steam slot 9,
532 h, 18.2 MB JSON, 40 MB pretty-printed, 0 unknown keys), `save10.deob.json` ('Ex-Beachhead-160qs'),
raw obfuscated text beside them (`*.obf.json`); helper `R5WD\scripts\nmsmap.py` (libNOM.map semantics in
~60 lines); pipeline `R5WD\scripts\deob.py`. Also recorded in scratchpad key `findings.R5-mapping`.

### F2. Structure of a save and what a partial decode gives (topic b)

**F2.1 Top level of a modern save.** VERIFIED on 'Main Save (S2)' (18,183,733 bytes of JSON;
`R5WD\scripts\structure.py`; full tree to depth 3 with offsets in `R5WD\decoded\save17.structure.txt`):

| Path (obfuscated) | Byte offset | Size | Share | Content |
|---|---|---|---|---|
| `Version` (`F2P`), `Platform` (`8>q`), `ActiveContext` (`XTp`) | 1-30 | tiny | | 4740, "Win\|Final", "Main" |
| `CommonStateData` (`<h0`) | 43 | 66 KB | 0.4% | SaveName, TotalPlayTime, ByteBeatLibrary, SeasonData (61 KB), SaveUniversalId at 66,014 |
| `BaseContext.PlayerStateData` | 66,144 | 8.60 MB | 47.3% | the normal game; 278 keys |
| `ExpeditionContext.PlayerStateData` | 8,671,428 | 8.63 MB | 47.5% | a second full player state (R6: present only if an expedition was entered) |
| `DiscoveryManagerData.DiscoveryData-v1.Store` | 17,302,080 | 0.88 MB | 4.8% | always last |

Largest parts of `BaseContext.PlayerStateData`: PersistentPlayerBases 3.78 MB (61 entries: 57 own + 4
`ExternalPlanetBase` = other players' bases), MissionProgress 1.05 MB (1002), Stats 0.99 MB (1190),
TerrainEditData 0.84 MB, StoredInteractions 0.60 MB, MaintenanceInteractions 0.25 MB, KnownWordGroups
120 KB (1450), ShipOwnership 98 KB (12), TeleportEndpoints 63 KB (215), Pets 28 KB (30).

**F2.2 Layout and container variants.**
- Base version < 4147 (before Omega 4.50): no contexts, `PlayerStateData` (`6f=`) at the top level
  (vault: Xbox 'Voyagers', bv 4146 = Echoes 4.40). libNOM.io keeps two JSONPaths per fact for this
  (Vanilla vs Omega structure, each obfuscated or plain; `Global/Json.cs` `GetPaths`). SOURCED + VERIFIED
  (R6 found the same top-level keys).
- Base version -> game version: libNOM.io `Meta/GameVersion.cs:46-79` (4140 Waypoint 4.00, 4146 Echoes
  4.40, 4147 Omega 4.50, 4149 Omega 4.52 "MicrosoftV2", 4153 Worlds I 5.00, 4189 Voyagers 6.00). The
  vault spans bv 4146-4228 (R6/R7). SOURCED.
- Xbox data blobs (libNOM.io `PlatformMicrosoft/PlatformMicrosoft_Read.cs:32-55`): one bare LZ4 block
  before Omega 4.52 and for AccountData; `HGSAVEV2` chunked format between 4.52 and Worlds I 5.00; the
  Steam `0xFEEDA1E5` stream since 5.00. SOURCED. The vault has 16 bare-block copies and no HGSAVEV2 (R7).
- Steam `accountdata.hg` is plain JSON (starts `{"F2P":4098,...`), not LZ4. VERIFIED on the copy.

**F2.3 Which facts sit in chunk 0.** VERIFIED (`R5WD\scripts\first_chunk.py`, first occurrence of each
key in all 195 versions; chunk 0 = first 0x80000 decompressed bytes; `R5WD\first-chunk-results.txt`):

| Fact (key) | Versions with it in chunk 0 | Median position | Max offset seen |
|---|---|---|---|
| SaveName, DifficultyState, UniverseAddress (current galaxy) | 195 / 195 | <1% | 94 KB |
| ActiveContext | 193 (2 old saves lack it) | 0% | 30 B |
| TotalPlayTime | 193 | 0% | 556 KB |
| SaveUniversalId (playthrough id) | 193 (2 old saves lack it) | 0.7% | 93 KB |
| Pets (companions) | 195 | 1.5% | 189 KB |
| KnownTech, KnownProducts, KnownWordGroups | 195 | 2-3% | 281 KB |
| Units, Nanites, Specials (quicksilver), Stats | 2 | 12% | 1.4 MB |
| PersistentPlayerBases | 0 | 40% | 4.3 MB |
| TeleportEndpoints, VehicleOwnership, ShipOwnership, KnownPortalRunes, FleetFrigates | 0 | 74-77% | 10.8 MB |
| DiscoveryManagerData | 0 | 92% | 18.0 MB |

Decoding chunk 0 alone costs ~22 ms in pure Python for every sample (F3). Most of the owner's named
facts (glyphs, bases and parts, ships, galaxies with bases) need at least 75% of the stream, so in
practice a full decode.

### F3. Cost per save (topic c)

VERIFIED (`R5WD\scripts\bench.py`, one process per sample, `R5WD\bench-results.jsonl`). "Slice-copy" is
a research variant of `decompress_block` (same algorithm, matches copied with one slice, overlapping
matches by repeating the pattern; ~15 lines differ). Heap peak = tracemalloc over decompress + decode +
json.loads. Process peak = peak working set incl. a second, fully de-obfuscated tree.

| Sample (copy) | Disk MB | JSON MB | Chunks | Pure-Py today s | Slice-copy s | lz4 C s | json.loads s | Full de-obf s | Heap peak MB | Process peak MB |
|---|---|---|---|---|---|---|---|---|---|---|
| Steam save10 'Ex-Beachhead-160qs' | 0.50 | 2.41 | 5 | 0.103 | 0.021 | 0.0015 | 0.025 | 0.053 | 26 | 75 |
| Steam save3 'PhoenixesToGive' | 1.84 | 6.18 | 12 | 0.258 | 0.067 | 0.0039 | 0.071 | 0.141 | 66 | 129 |
| Steam save5 'The Cartographers' | 3.32 | 14.50 | 28 | 0.706 | 0.202 | 0.0096 | 0.169 | 0.471 | 132 | 249 |
| Steam save17 'Main Save (S2)' | 6.34 | 18.18 | 35 | 0.767 | 0.233 | 0.0121 | 0.200 | 0.489 | 151 | 270 |
| Xbox 10B 'Light No Sky season 2' | 0.58 | 2.92 | 6 | 0.128 | 0.027 | 0.0018 | 0.030 | 0.065 | 30 | 81 |
| Xbox 9A (bv 4167) | 0.65 | 2.72 | 6 | 0.117 | 0.025 | 0.0017 | 0.027 | 0.056 | 28 | 81 |
| Xbox 1A 'ShipsToGive' | 2.42 | 8.30 | 16 | 0.371 | 0.091 | 0.0055 | 0.095 | 0.192 | 93 | 173 |
| Xbox 6B (bv 4154) | 2.54 | 7.13 | 14 | 0.308 | 0.095 | 0.0050 | 0.079 | 0.138 | 70 | 134 |
| Xbox 2B 'Main Save XBox (X4)' | 3.36 | 11.17 | 22 | 0.486 | 0.122 | 0.0075 | 0.129 | 0.248 | 121 | 212 |
| Xbox 8A 'Creative Primary' | 4.00 | 11.80 | 23 | 0.509 | 0.149 | 0.0079 | 0.130 | 0.298 | 123 | 212 |
| Xbox 13A 'Voyagers' (bv 4146, bare block) | 2.47 | 6.66 | 0 | 0.287 | 0.091 | 0.0037 | 0.078 | 0.101 | 66 | 126 |

- All three decoders give byte-identical output on these 11 samples, and the slice-copy decoder is
  byte-identical to the C reference on **all 195** vault versions (17.8 s total). VERIFIED.
- Today's decoder: ~24 MB JSON/s; slice-copy ~75 MB/s; json.loads 75-90 MB/s; UTF-8 decode 3-29 ms.
- The bottleneck is the per-byte match loop `core/lz4_block.py:111-114`
  (`for _ in range(mlen): out[d] = out[mpos]`).
- Rebuilding a de-obfuscated copy of the whole tree costs more than json.loads (0.49 s vs 0.20 s on the
  big save) and doubles memory: look facts up by obfuscated key instead.

### F4. Indexing every distinct version in the real vault (topic c)

VERIFIED (`R5WD\scripts\index_sim.py`, `R5WD\sim-results.jsonl`). Input: one copy of each of the 195
distinct data versions from R7's inventory (`findings.R7-dedupe`: Steam 151 + Xbox 44; 468 MB compressed,
1,562 MB JSON; copies sha256-verified into `R5WD\corpus`). Per version: read, decode, json.loads, extract
a realistic fact subset by computed keys (name, play time, playthrough id, galaxy, glyph count, units /
nanites / quicksilver, bases with part counts, ships, companions, discovery counts by type, missions,
stats, teleporters), one sqlite commit.

| Decoder | Processes | Wall s | Sum decode / json.loads / extract s | Slowest version s |
|---|---|---|---|---|
| pure-Python today | 1 | **93.5** | 68.8 / 21.1 / 0.3 | 1.14 |
| pure-Python slice-copy | 1 | **42.1** | 18.0 / 20.5 / 0.3 | 0.51 |
| lz4 C (reference only) | 1 | 26.3 | 1.1 / 20.4 / 0.3 | 0.26 |
| pure-Python today | 4 | 24.0 | 68.5 / 20.5 / 0.2 | 1.08 |
| pure-Python today | 8 | 12.9 | 70.8 / 21.5 / 0.3 | 1.17 |
| pure-Python slice-copy | 4 | **11.3** | 18.4 / 21.0 / 0.2 | 0.52 |

- 0 errors in every run; facts identical across all six runs (195/195). VERIFIED.
- Extraction by key lookup is negligible; json.loads is a ~21 s floor per core.
- INFERRED: 1.5-3x longer on typical user hardware; after the first index, each new version costs
  0.1-1.1 s.
- Before decoding, the indexer needs each file's sha256: 1.6 GB/s warm (all 2.1 GB of copies ~1.3 s CPU,
  plus cold-disk reading) and a header walk (`lz4_block.stats`, 176 ms for 195 files incl. reading) gives
  every version's decompressed size. R1 measured `scan_any` over all 52 folders at ~1 s. VERIFIED.
- The catalog stores no per-file hash (only `core/safety.py:41` and `core/operations.py:187` hash), so
  the indexer must hash, or cache sha256 by (path, size, mtime) the way `playtime._IdCache` caches ids.

### F5. Background indexing in the shipped runtime (topic d)

**F5.1 GIL vs Tk.** VERIFIED (`R5WD\scripts\tk_gil.py`, `R5WD\tk-gil-results.jsonl`): withdrawn Tk root,
`after(10)` ticks, lateness recorded; workload = the 18 MB save decoded + parsed + walked, 3 times.

| Scenario | Ticks | Median late | p99 | Max | Ticks late >100 ms |
|---|---|---|---|---|---|
| idle (3 s) | 181 in 3.0 s | 6.7 ms | 12.0 ms | 14.1 ms | 0 |
| thread: decode + json.loads + walk | 86 in 3.3 s | 23 ms | 223 ms | 223 ms | 3 |
| thread: json.loads only | 4 in 0.67 s | 109 ms | 405 ms | 405 ms | 2 |
| process pool (2, spawn) | 137 in 2.3 s | 6.7 ms | 11.2 ms | 11.5 ms | 0 |

json.loads holds the GIL for the whole parse (one freeze per large save); the pure-Python decoder
yields at the switch interval but halves the event-loop rate. Today the GUI runs core work on daemon
threads behind a modal busy dialog with an `update()` + `sleep(0.02)` loop (`gui.py:1355-1377`); that suits
modal operations, not a long non-modal indexer.

**F5.2 multiprocessing in the kit.** VERIFIED on a copy of `dist\NMSSaveVault` (v0.2.8; python.org
3.12.10 embeddable + tcltk, `packaging/build_portable.ps1:24,78-79`).
- I added `R5probe.exe` (a copy of the signed `pythonw.exe`, like `NMSSaveVault.exe`) and an
  `ENTRY_POINTS` entry in the copy's sitecustomize, so the probe ran inside `import site` exactly as the
  GUI does (`R5WD\scripts\r5probe.py`, report `R5WD\kitprobe-gui.json`).
- spawn `Pool(4)`: created in 0.046 s, first tasks done in 0.112 s; children were `R5probe.exe` processes
  (windowless); sitecustomize saw `sys.argv == ['-c', '--multiprocessing-fork']` in 6/6 child launches and
  did not start the app (`packaging/sitecustomize.py:126`); workers imported
  `nms_save_vault.core.lz4_block` from `_runtime\app` and decoded the 18 MB save in 1.07 s.
- `pool.terminate()` mid-work returned in 0.018 s.
- Kit DLLs include `_multiprocessing.pyd`, `_overlapped.pyd`, `_sqlite3.pyd` + `sqlite3.dll`
  (`build_portable.ps1:119`; R3 verified the imports).
- Worker functions must live in an importable module (`_runtime\app` is on the `._pth` path); the GUI's
  `__main__` has no file, so spawn has nothing to re-import. INFERRED from `multiprocessing.spawn`,
  consistent with the probe.

**F5.3 Cancellation and resume.** VERIFIED (terminate timing) / INFERRED (design).
- Process workers can be killed at once (18 ms); this is safe when workers are read-only (path in, facts
  out) and the parent is the only index writer. Thread workers can only be cancelled cooperatively
  between versions; an in-flight json.loads (up to 0.4 s here) cannot be interrupted.
- Resume is free when the unit of work is one distinct data blob (sha256) and each result is committed
  on arrival: a kill or crash loses only the in-flight versions (the sim committed per version).

**F5.4 POSIX.** INFERRED. Python 3.12 on Linux defaults to `fork`; forking a process with Tk/X11 state is
unsafe, so use `multiprocessing.get_context("spawn")` on every platform. The POSIX kits start through a
different launcher (`packaging/build_posix_kit.py`), so spawn there needs its own check (DESIGN.md ladder
T2/T3).

### F6. Index storage (topic e)

- **sqlite3 in the kit**: 3.49.1, `json_extract` works, FTS5 available, `sqlite3.threadsafety == 3`.
  VERIFIED by the kit probe (R3 also lists RTREE and MATH).
- **Sizes** (VERIFIED, sim DB): facts per version median 7.4 KB, max 29 KB of JSON; 195 versions = 1.5 MB
  of facts; sqlite with `version` + `base` (10,104 rows) + `ship` (1,403 rows) tables = 4.3 MB.
- **Read/write cost** (VERIFIED, 195 versions): sqlite load + parse all facts 8.6 ms; one `index.json`
  (1.51 MB) write 11 ms / read 14 ms; 195 per-version sidecars written atomically (`os.replace`) 110 ms,
  read 18 ms warm but 892 ms on the first read after writing (INFERRED: on-access AV scan of new files).
- **SQLite on network shares** (SOURCED): "WAL does not work over a network filesystem"
  (https://www.sqlite.org/wal.html); remote use "often leads to various kinds of trouble and grief ...
  This has led to database corruption" (https://www.sqlite.org/useovernet.html). DESIGN.md D11 wants a
  vault that opens from a USB drive or NAS.
- **Dedup potential for a deeper index** (VERIFIED, 38 versions of 'Main Save (S2)', both contexts): base
  entries 6,350 -> 2,345 distinct by content (174 MB of distinct base JSON); ship entries 912 -> 73;
  companion entries 2,232 -> 39. Per-version per-part base tables would be large; ships and companions
  dedupe almost completely.

Schema sketch (INFERRED; sqlite shown, the same shape works as JSON):

```sql
-- Derived cache: every row can be rebuilt from save bytes.
CREATE TABLE meta(k TEXT PRIMARY KEY, v);           -- schema_version, extractor_version
CREATE TABLE blob(                                   -- one row per distinct data file
  sha256 TEXT PRIMARY KEY,                           -- of the data file (Steam saveN.hg / Xbox data blob)
  size INTEGER, decompressed INTEGER,
  container TEXT,                                    -- chunked | bare | hgsavev2
  base_version INTEGER, layout TEXT,                 -- vanilla | omega
  uid TEXT,                                          -- CommonStateData.SaveUniversalId, NULL pre-Omega
  status TEXT, error TEXT,                           -- ok | partial | corrupt | unsupported
  extractor INTEGER, indexed_at INTEGER);
CREATE TABLE fact(sha256 TEXT, name TEXT, value, PRIMARY KEY(sha256, name));   -- scalar facts
CREATE TABLE base(sha256 TEXT, idx INTEGER, name TEXT, galaxy INTEGER, address TEXT, type TEXT,
                  owned INTEGER, parts INTEGER, legacy_parts INTEGER, content_hash TEXT,
                  PRIMARY KEY(sha256, idx));
CREATE TABLE ship(sha256 TEXT, idx INTEGER, name TEXT, model TEXT, content_hash TEXT, PRIMARY KEY(sha256, idx));
CREATE TABLE companion(sha256 TEXT, idx INTEGER, name TEXT, creature_id TEXT, content_hash TEXT,
                       PRIMARY KEY(sha256, idx));
-- Where each blob currently exists; rebuilt by every scan, so it may hold machine paths.
CREATE TABLE location(sha256 TEXT, source TEXT, folder TEXT, slot INTEGER, member TEXT,
                      platform TEXT, meta_ts INTEGER, is_live INTEGER);
```

Re-index rule: rows whose `extractor` is older than the code's are queued again; a full re-run is the F4
cost (11-94 s), so a single global extractor version is enough (per-fact-group versions save nothing,
because the decode dominates). User-authored data (notes, labels, manual lineage fixes) is a separate,
durable store, never in this cache.

### F7. Automatic "what changed" (topic f)

VERIFIED (`R5WD\scripts\diff_sim.py` over the sim DB; 38 versions of 'Main Save (S2)' ordered by play
time; `R5WD\diff-results.txt`):
- 37 fact-level changelogs in **2.2 ms** total. Real output lines: "+4.9 h played; nanites +3818;
  quicksilver -1500; missions tracked +17; new base 'SKYNET INDEPENDENT SENTINEL COMBAT UNIT' (378
  parts)"; "+0.4 h played; galaxy 0 -> 9; nanites +1196; new base 'Nani Mela Farm Outpost' (1947 parts)";
  "words known 1448 -> 1450"; "base 'Ladyaks Op4219 GGF Station Beta' parts -1".
- Noise seen:
  1. Play-time order is not lineage: v4->v5 shows 8 bases "gone", v5->v6 the same bases "new" again
     (versions from different branches interleave).
  2. Foreign bases (`ExternalPlanetBase`, 4 of 61 in save17) come and go.
  3. The discovery store shrinks as well as grows ("Planet -34, SolarSystem -9").
  4. A/B members written seconds apart give empty diffs ("+0.0 h").
- Full-tree structural diff of the pair with the most fact changes: load both 0.85 s (slice-copy),
  diff 0.25 s, ~240 MB heap; **193,764** changed leaves, 3,263 keys added, 3,095 removed - mostly
  TerrainEditData.Edits (118k across both contexts), discovery records (33k), base Objects (28k), Stats
  (13k). A raw diff is not a readable changelog.

### F8. Robustness (topic g)

VERIFIED on corrupted copies (`R5WD\scripts\robust.py`, today's decoder; `R5WD\robust-results.txt`) unless
tagged otherwise.

| Case | What happens today | Needed handling |
|---|---|---|
| Truncated file (cut inside chunk 6 of 12) | `walk_chunks` keeps the 6 whole chunks; decode returns a 3.1 MB prefix; json.loads fails | status "partial"; chunk-0 header facts still recoverable by regex |
| Zero-filled or empty file | `ValueError: no valid save chunks found ...` | status "corrupt" |
| Bit flips in a chunk payload | LZ4 does not notice (no checksum); json.loads fails | status "corrupt" (sha256 pins the bad copy) |
| Chunk header claiming 256 MB | `bytearray(256 MB)` allocated (`lz4_block.py:77`), then decodes fine; a u32 allows ~4 GB | cap each chunk at 0x80000 (format max) and a bare block at a sane limit before allocating |
| Bare block, too-small expected size | `IndexError` | catch `(ValueError, IndexError)` as playtime.py does |
| Xbox `HGSAVEV2` blob (4.52 to 5.00; libNOM `PlatformMicrosoft_Read.cs:32-55`) | `ValueError: invalid LZ4 match offset 0`: unsupported. `msstore.py:59` defines the header, nothing uses it, while `msstore.py:22-24` and `DESIGN.md:118` say it is handled | ~15 lines: 9-byte header, then per chunk `[u32 decompressed][u32 compressed]` + block (<= 0x100000). Synthetic fixture: `R5WD\robust\hgsavev2_synth.bin` |
| Backslash followed by a raw TAB/LF/CR in a string (libNOM `Extensions/Span.cs:104-105`, "Escaping gone wrong by HG") | json.loads fails, `strict=False` too (invalid escape) | libNOM's fix: replace raw 0x09/0x0A/0x0D with `t`/`n`/`r` before parsing. 0 cases in this vault (all 195 versions parse unmodified) |
| Invalid UTF-8 inside strings (procedural ids like `^\x80\x88.?+D#40141`; 0-260 bytes per save) | strict decode raises | decode with `errors="surrogateescape"` (lossless). libNOM decodes lossily and escapes known ids (`BINARY_MAPPING`) |
| Duplicate keys / raw control characters | 0 / 0 in the 18 MB save | none |
| Old Xbox bare block (bv < 4149; 16 copies in the vault, R7) | decodes with the meta's size | keep; without a meta size it cannot be decoded (libNOM falls back to a 255x buffer) |
| Pre-Omega layout (bv < 4147) | parses; facts under top-level PlayerStateData | extractors need both layouts |
| Huge saves | linear: heap ~8x JSON size; vault max 18.8 MB JSON (R7) | schedule by the size from chunk headers; run the largest alone |
| Live file read while the game writes | not tested (game not run) | INFERRED: torn read -> parse failure -> retry; prefer indexing live saves when NMS is closed |

### F9. Side findings (outside scope)

- `packaging/sitecustomize.py:125-126`: the `-c`/`-m` bypass never fires. At that point `sys.argv` is
  `['-c']` or `['-m', '--help']` (the flag is `argv[0]`; logged in the kit copy, `R5WD\kit-argv.log`), so
  `nmsvault-cli.exe -c "..."` runs the CLI parser on the code string (observed: `invalid choice`, exit 2, no
  side effects; `state.json` mtime unchanged). The `--multiprocessing-fork` check works. Fix: test
  `sys.argv[:1]`, or use `sys.orig_argv`.
- `msstore.py:22-24` and `DESIGN.md:118` say HGSAVEV2 is handled; it is not (F8).
- `core/playtime.py:32` calls the id key ActiveSaveSlotId; libMBIN 7.7 calls `WmU` SaveUniversalId.

---

## 3. Implications for the design

### Constraints
1. **Stdlib-only holds.** Decoding, parsing and extraction need nothing outside the stdlib. The C `lz4`
   package would save ~40% of single-core time at most (json.loads is the floor) and would break the
   design guarantee.
2. **The content index is derived data**: a pure function of (data-file bytes, extractor version). Key it
   by sha256 of the data file. Paths belong only in a separate, rebuildable location table.
3. **User-authored data is not derived.** Notes, labels and manual lineage fixes must live with the vault
   (D11 portability), be written atomically, and never exist only in a cache that "rebuild index" may
   delete. Keying version notes by blob sha256 makes "the note travels with every copy" automatic.
4. **Parsing big saves stays off the Tk thread.** For a smooth UI it should also stay off any thread in
   the GUI process: use spawn worker processes, with the parent as the only index writer.
5. **Workers are read-only by construction** (path in, facts out), which keeps `terminate()` safe and
   leaves the safety model unchanged.
6. **Memory**: ~200 MB per worker for the biggest current save; cap workers at about min(4, cores - 1)
   and run oversized saves alone.

### Risks
- HGSAVEV2 and the uncapped allocation (F8) need small core-decoder fixes before an indexer feeds it
  every file in the vault.
- SQLite in a vault on a NAS (F6).
- Spawn in the POSIX kits is unverified (F5.4).
- Reading live files while the game writes (F8).
- Extractor drift across game versions: two layouts (pre/post Omega), fields that appear and disappear
  (`ActiveSpaceBattleLevel` left libMBIN in 6.44 but is still in saves).
- Lists with mixed ownership (foreign bases) inflate counts and changelogs unless filtered (R6 owns the
  rules).

### Opportunities
- With the slice-copy decoder, a one-process full index takes ~42 s here and ~11 s with 4 workers.
  Re-indexing everything after an extractor change is cheap, so the first index can stay small and grow
  with features. There is no need to "deep index everything" now.
- A first-chunk header pass (22 ms per save) can fill name, play time, id, galaxy, difficulty and
  companions at once, before the full index finishes (playtime.py already reads ids this way).
- One pass per blob can produce everything S1 and S3 need as well: sha256, chunk stats, playthrough id,
  header facts and full facts.
- Hashing sub-trees (per base, ship, companion) gives cheap "changed?" flags and compact storage for
  later deep views (ships 8%, companions 2% distinct across 38 versions).
- On-demand deep views: decoding one version on request costs 0.2-1.1 s, so rich detail (inventories,
  every base part) can be computed when the user opens a version instead of being indexed up front.

---

## 4. Open questions and candidate options

**Q1. How do extractors get key names?**

| Option | For | Against |
|---|---|---|
| A. Readable names in code; keys computed at import by a ported `HashName` | no data file, nothing to update, any game version works for the names the code knows, testable against a pinned mapping, public-domain hash | ~110 lines of hash code; no full de-obfuscation for a raw viewer |
| B. Bundle mapping.json + libNOM legacy keys as package data, refreshed each app release | full de-obfuscation (raw JSON viewer/export); same data as community tools | 61 KB data file plus LGPL/GPL notices; stale between releases (only matters to a raw viewer) |
| C. B plus an opt-in download of the newest mapping.json from MBINCompiler releases | always current | a second network endpoint beside `updates.py`; rate limits; downloaded data to validate |
| D. Hard-coded obfuscated literals (today's `tools/nms_visit_history.py`) | zero machinery | unreadable code; every new fact needs a manual lookup |

A and B combine well: A for extraction, B only if a raw viewer is wanted.

**Q2. Decoder speed.**

| Option | For | Against |
|---|---|---|
| A. Keep today's decoder | no change | ~94 s single-core full index here; 1.1 s per big save |
| B. Slice-copy rewrite of `decompress_block` | 3.2-4.9x faster; byte-identical on all 195 versions; stdlib | touches a safety-critical core module (needs tests; the app only reads with it) |
| C. Use the `lz4` C package when installed | ~60x faster decoding | breaks "stdlib only"; only json.loads remains, so ~40% total gain |

**Q3. Background execution model.**

| Option | For | Against |
|---|---|---|
| A. One daemon thread (today's pattern) | simplest; matches existing code | ~94 s (42 s with B above); UI hitches of 0.2-0.4 s per big save; no hard cancel |
| B. spawn process pool (2-4 workers) | smooth UI; 11-24 s full index; 18 ms cancel; verified in the Windows kit | more moving parts; POSIX kits unverified; ~200 MB RAM per worker |
| C. Hybrid: thread for the first-chunk header pass, processes for full facts | instant header columns, smooth UI | two code paths |

**Q4. Where does the index live?**

| Option | For | Against |
|---|---|---|
| A. Vault root (`index.sqlite` or `index.json`) | travels with the vault (D11); one index per vault | SQLite on a NAS risks corruption; the vault gets a frequently rewritten derived file |
| B. Per-machine cache (e.g. `%LOCALAPPDATA%\NMSSaveVault\index-<vault-id>.sqlite`) | safe and fast; no NAS risk | rebuilt on each machine (~0.5-1.5 min here); must never hold notes |
| C. Both: small JSON facts in the vault, local sqlite rebuilt from it | portable and safe | two representations to keep in step |

**Q5. Index format.** sqlite (transactions, incremental updates, ad-hoc queries; 4.3 MB) vs one JSON file
(1.5 MB, 11 ms write / 14 ms read, whole-file rewrite) vs per-version sidecars (merge-friendly, atomic per
file, slow first read under AV). At 200-2000 versions all are fast; the choice is about NAS safety,
atomicity and debuggability.

**Q6. Index depth.** Curated facts only (R6's list), deep tables (every base part, inventories,
discoveries) with sub-tree dedupe, or curated facts plus an on-demand deep decode when a version is
opened.

**Q7. When to index.** On startup in the background (live first, then newest vault versions); after each
backup/snapshot (new blobs only); on demand when a row is opened; live saves again after the game exits.

**Q8. What the changelog compares.** Fact diff against the lineage parent (needs S3) vs against the
previous version by time (wrong across branches, F7) vs user notes only. Noise policy needed for foreign
bases, discovery-store churn and near-identical A/B pairs.

**Q9. Rare formats.** Implement HGSAVEV2 now (small; no real sample in the vault, so only a synthetic
fixture) vs show "format not supported" in the organizer.

---

## Appendix - reproduce

All commands from `C:\Devel\NMSVault`; scripts and data in `R5WD` (`-X utf8` because `-I` ignores
`PYTHONUTF8`; `--with lz4` only for the C reference runs):

| What | Command |
|---|---|
| De-obfuscate a copy | `uv run --with lz4 python -I R5WD\scripts\deob.py R5WD\decoded MAPPING... -- SAVECOPY...` |
| Key tree with offsets | `uv run python -I R5WD\scripts\structure.py R5WD\decoded\save17.obf.json 3 MAPPING...` |
| Hash check | `.venv\Scripts\python.exe -I R5WD\scripts\spooky.py R5WD\dl-mbin\mapping-v7.07.0-pre1.json` |
| Mapping churn | `.venv\Scripts\python.exe -I R5WD\scripts\mapdiff.py <mapping files oldest..newest>` |
| Per-save bench | `R5WD\scripts\run_bench.sh` -> `R5WD\bench-results.jsonl` |
| Whole-vault sim | `R5WD\scripts\run_sim.sh` -> `R5WD\sim-results.jsonl`, DBs in `R5WD\db` |
| Chunk-0 facts | `uv run --with lz4 python -I -X utf8 R5WD\scripts\first_chunk.py` |
| Unknown keys | `uv run --with lz4 python -I -X utf8 R5WD\scripts\unknown_keys.py` |
| Tk latency | `uv run python -I R5WD\scripts\tk_gil.py R5WD\samples\steam\save17.hg 3` |
| Robustness | `.venv\Scripts\python.exe -I R5WD\scripts\robust.py` |
| Changelog | `.venv\Scripts\python.exe -I -X utf8 R5WD\scripts\diff_sim.py R5WD\db\sim-current-1.sqlite DDDAD1BD full` |
| Kit probe | in the kit copy: `R5probe.exe OUT.json SAVECOPY...` (see F5.2) |

Downloaded sources (untrusted data, own folders): `R5WD\dl-mbin` (mapping.json releases, LICENSE.md,
Program.cs), `R5WD\dl-libnom` (libNOM.map resources and C#), `R5WD\dl-libnomio` (libNOM.io C#).
