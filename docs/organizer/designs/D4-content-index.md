# D4 - Content index engine

Design wave, 2026-10-16; revised 2026-10-17 for the owner's decisions (`01-consolidation.md`
4a and the answers to `03-design-review.md`). Subject S2 (decisions C7-C14, the indexing half of
C11/C31) and defect P8. Bound by `../01-consolidation.md` sections 2 and 6; aligned with D1
(`D1-core-model.md`: `FactsProvider`, read model), D3 (bytes only through `VersionLocator.open()`)
and D5. Audience: implementing agents (completeness wins) and the parent.

Evidence tags: **VERIFIED** = measured or checked by D4 on copies; **SOURCED** = read in code
(`file:line`) or in a research report; **INFERRED** = reasoning. All D4 experiments ran on copies:
the R5 corpus (one copy of each of the vault's 195 distinct data files) and R6's decoded samples,
with throwaway scripts in `D4WD` =
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\<session>\scratchpad\D4` (Appendix A). Nothing was
written to the vault, the live folders, in-place backups, the safety backup or app state. No
production code, tests or binding docs were changed.

---

## 0. Owner summary

- Every save version gets a fact card: story, Atlas and expedition progress, glyphs, base parts
  and legacy parts, ships, companions, galaxies with bases, money - plus a short "what changed".
- "What changed" compares a version only with its recorded parent; a version without one shows
  "no recorded parent" and no list.
- Your 195 versions index in 12 s on this PC (about 0.5-1 min on an average PC), live saves
  first; a new version takes under 1 s; the cache is under 3 MB, per PC, always rebuildable.
- Read-only, runs outside the window (no freezes), reads and stores no account ids, never holds
  your notes.
- Your decisions: legacy parts = "placed" and "still buildable" badges; ships = "12 (3
  corvettes) +5 archived"; companions = custom name, else creature type; ship, galaxy and
  expedition names shipped as small tables; while the game runs, index live saves only, with one
  worker.

---

## 1. Scope, non-goals, dependencies

### 1.1 Scope

| Item | Decision / source | Section |
|---|---|---|
| Decoder fixes in core: slice-copy speed-up, size caps, bounds checks, explicit HGSAVEV2 "unsupported", lossless invalid UTF-8 | P8, R5 F3/F8 | 2.2 |
| HashName port; extractors address fields by readable name | C7, R5 F1 | 2.3 |
| Extractor framework: registry, contexts, graceful degradation, privacy filter, normalisation | C9, R6 2.0 | 2.4 |
| Fact schema v1 (owner's facts + top extras) and read-time derived views | C10, R6 sec 5 | 2.5 |
| The `FactsProvider` contract D1's read model consumes | D1 2.10, review V-14 | 2.6 |
| Lookup tables | C12, C13, R6 2.9 | 2.7 |
| Execution: spawn worker pool, priorities, cancellation, POSIX fallback, UI progress contract | C11, C31, R5 F5 | 2.8 |
| Per-machine sqlite cache | C8, section 6 | 2.9 |
| Scheduling: first index, live first, new versions, on-demand deep decode | C10, R5 Q7 | 2.10 |
| "What changed" against the recorded parent; nothing without one | C14, R5 F7, owner Q5 | 2.11 |

### 1.2 Non-goals

- Writing anything except the cache file; the index never opens a save, vault or live path for
  writing (invariant I1).
- A raw JSON viewer, a bundled mapping file or de-obfuscated copies (R5 Q1-B; later, if wanted).
- Account-level facts (titles, unlocked specials in `accountdata.hg`; R6 2.1): not per save; a
  later per-account card (option O10).
- Per-part or per-slot tables in the cache: deep detail is decoded on demand (C10).
- UI layout and widgets (D6); this design fixes only what a UI may call.
- HGSAVEV2 decoding (no real sample exists; explicit "unsupported" result).
- Lineage (D1) and live writes (D5). The index supplies no lineage evidence: parents come only
  from D1's recorded, observed and "seen in backups" links, never from content.
- Ownership: no platform account id is read, compared or stored (owner Q6).

### 1.3 Dependencies (who calls whom)

| Party | Gives D4 | Takes from D4 | Phase |
|---|---|---|---|
| D1 core model | `vid`s (scanner, journal), `vault_id`, `platform.user_cache_dir(host)` (D1 2.4), the vault-level `VersionLocator` (D1's composite: D3 store first, legacy folders until fold) with `open(vid) -> OpenedVersion(data, meta_plain, platform, meta_format, location)`, sha256 verified; `request(...)` calls after each refresh | the `FactsProvider` subset (2.6): `view(vid)` for `Line.facts` and `facts:<name>` sorting (`FactsView.numbers`); `changes(child, parent)` for automatic descriptions (C19); `is_milestone(child, parent)` for key versions | 3 |
| D3 version store | implements `open()` for store entries; D4 reads bytes only through `open()` | nothing | 3 |
| D5 live operations | `request(new vids, NEW)` after an operation | nothing; the index never blocks an operation | 3 |
| UI (D6) | user actions (open a version card, rebuild) | `view`, `card`, `changes`, `deep`, `progress`, `events_since` - all non-blocking | 3 |
| Front-ends (`gui.py`, `cli.py`) | the execution policy (OS-dependent, computed in `core/platform.py`) | `IndexService` lifecycle | 3 |

Phasing (01-consolidation section 5): decoder fixes are Phase 0 (P8); the index engine is
Phase 3; deep on-demand detail is Phase 3b.

---

## 2. Design

### 2.1 Module map

| Module | New / changed | Role | Imports | May branch on OS |
|---|---|---|---|---|
| `core/lz4_block.py` | changed | hardened slice-copy block decoder, caps, container sniffing | `formats` | no |
| `core/savejson.py` | new | data bytes -> parsed (obfuscated) JSON root; surrogateescape; size cap; escape repair | `lz4_block` | no |
| `core/hashname.py` | new | SpookyHash V2 short path + MBINCompiler `HashName`; cached `key()` | stdlib | no |
| `core/msstore.py` | doc only | docstring stops claiming HGSAVEV2 support; `HGSAVEV2_HEADER` re-exported from `lz4_block` | - | yes (existing) |
| `core/platform.py` | +1 function | `index_execution_policy(host, ...)` beside D1's `user_cache_dir` | - | yes (OS edge module) |
| `core/index/__init__.py` | new | public API re-exports | - | no |
| `core/index/schema.py` | new | TypedDicts, `SCHEMA_VERSION`, `EXTRACTOR_VERSION`, `DECODER_VERSION`, `validate()` | - | no |
| `core/index/nodes.py` | new | `get()` / `items()` by readable name, normalisers | `hashname`, `privacy` | no |
| `core/index/privacy.py` | new | deny list, the one privileged accessor (`owner_platform`) | `hashname` | no |
| `core/index/extract/` | new | `__init__` (registry, `run_extractors`), `save.py`, `progression.py`, `bases.py`, `fleet.py`, `companions.py`, `inventories.py`, `stats.py` | `nodes`, `lookups.parts` | no |
| `core/index/lookups/` | new | `galaxies`, `missions`, `ships`, `seasons`, `journey`, `parts`, `creatures`, `_overrides` | - | no |
| `core/index/derive.py` | new | read-time `FactsView` / `FactsCard` from raw facts + lookups | `lookups` | no |
| `core/index/changes.py` | new | fact diff, noise rules, templates, `is_milestone` | `derive` | no |
| `core/index/worker.py` | new | `run_job(job) -> IndexResult` (top-level, picklable, no tkinter) | `savejson`, `extract` | no |
| `core/index/pool.py` | new | `ExecutionPolicy`, dispatcher, process / thread executors, budget, watchdog | `multiprocessing` | no |
| `core/index/cache.py` | new | `IndexCache` (sqlite3, WAL, writer thread, read LRU, recovery) | `sqlite3` | no |
| `core/index/service.py` | new | `IndexService` facade (implements D1's `FactsProvider`) | all above | no |
| `cli.py` | +subcommand | `nmsvault index status / build / show / changes` | `core.index` | (front-end) |
| `tools/gen_lookups.py` | new, dev-only | regenerates `core/index/lookups/*.py` from an unpacked game dump | anything (DESIGN.md D6) | n/a |
| `tools/bench_index.py` | new, dev-only | reproduces the performance numbers in 2.14 | - | n/a |

The package path `nms_save_vault.core.index` matches D1's `nms_save_vault.core.organizer`. The
decoder pieces stay flat in `core/` because they are save-format code used outside the index
(`playtime`, `tools/nms_visit_history.py`). Only `core/platform.py` (already the per-OS module,
where D1 also adds `user_cache_dir`) decides OS-dependent execution; `core/index` receives the
decision as an argument (DESIGN.md D7: environment as arguments).

### 2.2 Decoder work in core (P8)

#### 2.2.1 Defects being fixed

| # | Defect | Evidence | Consequence |
|---|---|---|---|
| a | Per-byte match copy `lz4_block.py:111-114` | VERIFIED D4: 68.3 s for all 195 versions vs 20.5-22.0 s with slice copies (3.1-3.3x) | slow index, 1.1 s per big save |
| b | `bytearray(expected_size)` at `lz4_block.py:77` allocates whatever a chunk header claims (u32, up to 4 GiB) | R5 F8 VERIFIED (256 MB claim allocated) | memory exhaustion on a corrupt file |
| c | No check that a match offset stays inside the produced output: `mpos = d - offset` goes negative and `out[mpos]` reads from the zero-filled END of the buffer | D4 code reading (`lz4_block.py:108-114`), Python negative indexing; INFERRED consequence | a corrupt block can decode to wrong bytes silently |
| d | A literal run past `expected_size` grows the buffer (`out[d:d+lit] = ...` slice assignment extends a bytearray); a match past it raises `IndexError` | D4 code reading; R5 F8 (`IndexError` on a too-small bare block) | wrong length or a non-`ValueError` exception |
| e | A chunk that decodes to fewer bytes than its header says is not noticed | D4 code reading (`return bytes(out[:d])`) | silent truncation |
| f | `HGSAVEV2` blobs (Xbox, game 4.52-4.99) fall into the bare-block path; `msstore.py:59` defines the header but nothing uses it; `msstore.py:22-24` and `DESIGN.md:118` claim support | R5 F8 VERIFIED ("invalid LZ4 match offset 0"), SOURCED libNOM `PlatformMicrosoft_Read.cs` | misleading docs; undefined result |
| g | Invalid UTF-8 inside procedural ids (0-260 bytes per save) breaks a strict decode; `errors="replace"` is lossy | R5 F8, R6 2.0 VERIFIED | parse failure or lossy text |

#### 2.2.2 New core API

```python
# core/lz4_block.py
class LZ4FormatError(ValueError):
    """A block or container that cannot be LZ4-decoded: corrupt, truncated or inconsistent."""

class UnsupportedContainer(ValueError):
    """A recognised save container this version cannot decode (Xbox HGSAVEV2, game 4.52-4.99)."""

HGSAVEV2_HEADER = b"HGSAVEV2\x00"            # moved from msstore (msstore re-exports the name)
MAX_SAVE_DECOMPRESSED = 256 * 1024 * 1024     # 14x the largest real save (18.8 MB JSON, R7)
MAX_BARE_BLOCK = 64 * 1024 * 1024             # pre-4.52 Xbox saves are 6-7 MB (R6 2.0)

def decompress_block(src: bytes, expected_size: int, *, exact: bool = False) -> bytes: ...
def container_kind(data: bytes) -> Literal["chunked", "hgsavev2", "unframed"]: ...
def decompress(data: bytes, expected_size: int | None = None, *,
               max_total: int = MAX_SAVE_DECOMPRESSED) -> bytes: ...
# walk_chunks / stats / ContainerStats / is_chunked: unchanged (structural, never allocate).
```

Block decoder (the exact twin of the verified research code `D4WD\scripts\hardened_lz4.py`):

```python
def decompress_block(src, expected_size, *, exact=False):
    if not 0 <= expected_size <= MAX_SAVE_DECOMPRESSED:
        raise LZ4FormatError(...)
    out = bytearray(expected_size)
    s = d = 0
    n = len(src)
    while s < n:
        token = src[s]; s += 1
        lit = token >> 4
        if lit == 15:
            lit += read_length_extension()          # each read: if s >= n -> LZ4FormatError
        if lit:
            end = s + lit
            if end > n:              raise LZ4FormatError("literals run past the end of the block")
            if d + lit > expected_size: raise LZ4FormatError("output larger than declared")
            out[d:d + lit] = src[s:end]; s = end; d += lit
        if s >= n:
            break                                    # last sequence: literals only
        if s + 2 > n:                raise LZ4FormatError("truncated match offset")
        offset = src[s] | (src[s + 1] << 8); s += 2
        if offset == 0 or offset > d: raise LZ4FormatError("match offset outside the output")
        mlen = (token & 0x0F) + (read_length_extension() if token & 0x0F == 15 else 0) + 4
        if d + mlen > expected_size: raise LZ4FormatError("output larger than declared")
        mpos = d - offset
        if offset >= mlen:
            out[d:d + mlen] = out[mpos:mpos + mlen]         # one slice copy
        else:                                               # overlap: repeat the period
            pat = out[mpos:d]; q, r = divmod(mlen, offset)
            out[d:d + mlen] = pat * q + pat[:r]
        d += mlen
    if exact and d != expected_size:
        raise LZ4FormatError(f"block decoded to {d} bytes, header says {expected_size}")
    return bytes(out) if d == expected_size else bytes(out[:d])
```

`decompress()`:

1. `kind = container_kind(data)`; `hgsavev2` -> `UnsupportedContainer` (message names the game
   versions); nothing is allocated.
2. `chunked`: walk the chunks; every `decompressed_size <= 0x80000` (format maximum,
   `formats.SAVE_CHUNK_DECOMPRESSED_MAX`) and the sum `<= max_total`, else `LZ4FormatError`
   **before any allocation**; decode each chunk with `exact=True` (VERIFIED: 0 of the vault's
   chunks decode shorter than their header); join. `src` slices are `bytes` (a `memoryview`
   source measured ~20 % slower, VERIFIED).
3. `unframed` with `expected_size`: `expected_size <= MAX_BARE_BLOCK` else `LZ4FormatError`;
   `exact=False` (the meta size is the buffer size; libNOM does the same).
4. `unframed` without a size: unchanged `ValueError`.

Behaviour table:

| Input | Today | New |
|---|---|---|
| valid chunked / bare save | bytes | identical bytes (VERIFIED 195/195 vs today and vs C lz4) |
| chunk claims > 0x80000, or total > `max_total` | allocates the claim | `LZ4FormatError`, nothing allocated |
| offset 0 | `ValueError` | `LZ4FormatError` (a `ValueError`) |
| offset beyond produced output | silent wrong bytes | `LZ4FormatError` |
| output beyond declared size | literals: buffer grows; matches: `IndexError` | `LZ4FormatError` |
| truncated block | `IndexError` or short output | `LZ4FormatError` (chunks) / short output (bare, as today) |
| chunk shorter than its header | unnoticed | `LZ4FormatError` |
| HGSAVEV2 | misread as a bare block | `UnsupportedContainer` |

Caller compatibility (SOURCED): `playtime.py:109` catches `(ValueError, IndexError)` - still
fine; `operations.py:934` and `msstore.py:357` use `stats()` only (unchanged);
`tools/nms_visit_history.py:236` catches `Exception`. Both new exceptions subclass `ValueError`.

Measured (VERIFIED, `D4WD\scripts\bench_hardened.py`, `cmp_overhead.py`; CPython 3.12.10,
Core Ultra 9 275HX): all 195 versions, 1,549 MB of JSON: today 68.3 s, hardened 20.5-22.0 s,
C `lz4` package 1.5 s (reference only). The bounds checks cost ~15 % against R5's unchecked
variant (11.0 s vs 9.1-9.7 s on 790 MB). `json.loads` (~21 s for the vault) is now the floor.

#### 2.2.3 `core/savejson.py` (new)

```python
@dataclass(frozen=True)
class DecodedSave:
    root: dict[str, Any]                    # obfuscated keys, as parsed
    container: Literal["chunked", "unframed"]
    chunks: int
    decompressed_bytes: int
    decompressed_sha256: str                # detects re-encoded but identical content
    json_bytes: int                         # up to the first NUL
    repaired_escapes: int                   # libNOM escape repair applied (0 in this vault)

class SaveDecodeError(ValueError):
    kind: Literal["corrupt", "unsupported", "too-large"]

def decode_save(data: bytes, *, bare_size_hint: int | None = None,
                max_decompressed: int = lz4_block.MAX_SAVE_DECOMPRESSED) -> DecodedSave:
    """lz4_block.decompress -> split at the first NUL -> UTF-8 with errors='surrogateescape'
    (lossless) -> json.loads. On 'Invalid \\escape' retry once after libNOM's repair
    (backslash + raw TAB/LF/CR -> \\t/\\n/\\r; Extensions/Span.cs:104-105). A non-dict root is
    'corrupt'. UnsupportedContainer -> kind 'unsupported'; size caps -> 'too-large'."""
```

All 195 versions parse unmodified (R5 F8, D4 run: 0 errors). Surrogates never leave the
worker: every string that reaches a fact is normalised (2.4.6).

#### 2.2.4 How the safety-critical decoder is tested

`lz4_block` is on the project's risk list ("LZ4-block decode"); the app only reads with it, but
every repopulate preflight and the identity cache depend on it. Tests (section 5.1) combine:
a differential test against the **current decoder kept verbatim inside the test module as the
reference**; a stdlib-only test LZ4 encoder for round-trip properties (overlaps, long lengths,
64 KiB offsets); exhaustive truncation and seeded byte-flip mutation tests that accept only
"same output" or `LZ4FormatError`; explicit edge cases for every row of the behaviour table; an
optional differential test against the `lz4` package when it is installed (`importorskip`; dev
only, never a runtime dependency); and an opt-in read-only test that decodes every live save
with both decoders and compares sha256 of the output. Rule kept: `lz4_block` is tested
directly, never mocked.

### 2.3 HashName and readable names (C7)

#### 2.3.1 `core/hashname.py`

```python
SEED = 8268756125562466087                  # MBINCompiler SaveFileMapping/Program.cs:31-48
_ALPHABET_NOTE = "'0'+(h % 68), +6 above 'Z': 0-9 : ; < = > ? @ A-Z a-y"

def spooky_short(message: bytes, seed1: int, seed2: int) -> tuple[int, int]:
    """SpookyHash V2 short path (messages < 192 bytes). Public domain algorithm."""

def hash_name(name: str) -> str:
    """The 3-character obfuscated save key of a readable field name.
    Raises ValueError for names of 192+ UTF-8 bytes (longest real name: 60, VERIFIED)."""

@functools.lru_cache(maxsize=None)
def key(name: str) -> str: ...
```

R5's port (110 lines, stdlib `struct` only) reproduces 1,476/1,476 pairs of MBINCompiler's
mapping 7.7.0.1 and 47/47 libNOM legacy names (VERIFIED by R5). D4 re-checked every name the
fact schema uses: 0 missing from the mapping, 0 hash mismatches (VERIFIED). Cost 2.9 us per
first computation; ~200 distinct names per worker -> < 1 ms. Attribution goes into the README
credits with the implementation (algorithm constants from MBINCompiler, LGPL-3.0 project,
credited to AndASM; SpookyHash by Bob Jenkins, public domain).

#### 2.3.2 Addressing fields

```python
# core/index/nodes.py
def get(node: object, *path: str, default: object = None) -> object:
    """Walk dicts by readable names: get(psd, "UniverseAddress", "RealityIndex").
    Each name goes through hashname.key(); a name on the privacy deny list raises
    PrivacyError (2.4.5). Non-dict intermediate -> default (never an exception)."""

def items(node: object, *path: str) -> list:
    """get() that returns [] unless the value is a list."""
```

Hot loops (15,000 base objects in the main save) use module-level constants
(`_OBJECT_ID = key("ObjectID")`) to skip the call. No mapping file ships (C7).

#### 2.3.3 Guarding against typos and drift

A misspelt readable name hashes to a key that never occurs, so the fact is silently `None`. Two
tests close that hole:

1. **Pinned snapshot** `tests/data/hashname_pins.json`: `{"source": "MBINCompiler mapping.json
   7.7.0.1 (LGPL-3.0)", "pairs": [[name, key], ...]}` with every name the extractors use
   (~170), ~100 sampled others, the legacy names R5 found still in saves (`BuildingType` a0>,
   `ThirdPersonShip` cid, `ActiveSpaceBattleLevel` bQN) and the one known collision `NE3`.
   Test: `hash_name(name) == key` for all pairs.
2. **Name coverage**: an `ast` scan of `core/index/extract/*.py` and `privacy.py` collects every
   string literal passed to `get()`, `items()` or `key()`; each must appear in the pin file.
   Adding a fact means adding its names to the pins (copied from a current mapping release),
   which is exactly the review step a new field needs.

### 2.4 Extractor framework

#### 2.4.1 Layouts and contexts (C9)

| Layout | Detected by | Contexts | Save-level source |
|---|---|---|---|
| context, base only | `BaseContext.PlayerStateData` present, no `ExpeditionContext` | `base` | `CommonStateData` |
| context, base + expedition | both present | `base`, `expedition` | `CommonStateData` |
| context, expedition only | only `ExpeditionContext` | `expedition` | `CommonStateData` |
| pre-context (bv < ~4154) | top-level `PlayerStateData` | `base` (layout `pre-context`) | `PlayerStateData` itself (`SaveName`, `TotalPlayTime` live there; no `CommonStateData`) |

VERIFIED on the vault: 193 context saves + 2 pre-context; 179 base contexts and 97 expedition
contexts across 195 versions (matches R7: 96 base only, 81 both, 16 expedition only, 2
pre-Waypoint). `primary` = `base` when present, else `expedition` (C9).

#### 2.4.2 Registry

```python
# core/index/extract/__init__.py
@dataclass(frozen=True)
class SaveInput:
    root: dict                        # parsed root (obfuscated keys)
    common: dict | None               # CommonStateData, or None (pre-context)
    layout: Literal["context", "pre-context"]

@dataclass(frozen=True)
class CtxInput:
    name: Literal["base", "expedition"]
    psd: dict                         # PlayerStateData of the context
    container: dict | None            # BaseContext / ExpeditionContext (GameMode), None pre-context
    save: SaveInput

@dataclass(frozen=True)
class Extractor:
    name: str                         # "ships"
    scope: Literal["save", "context"]
    fields: tuple[str, ...]           # output keys it owns (schema validation, error blanking)
    fn: Callable[[SaveInput | CtxInput, dict], None]

def save_extractor(name: str, fields: tuple[str, ...]): ...      # decorator
def context_extractor(name: str, fields: tuple[str, ...]): ...   # decorator

def run_extractors(decoded: DecodedSave) -> "VersionFacts":
    """Runs every save extractor once and every context extractor per context. Each call is
    isolated: an exception blanks that extractor's fields, appends
    {"extractor": "<ctx>.<name>", "error": "<Type>: <message, max 200 chars>"} and sets status
    'partial'. Then validate() against schema v1 (undeclared keys dropped and reported)."""
```

One global `EXTRACTOR_VERSION` (int) is bumped by any change to extractor output; per-extractor
versions save nothing because the decode dominates (R5 F6; VERIFIED here: extraction is 1.1 s
of ~43 s CPU for the whole vault).

#### 2.4.3 Extractor list (v1)

| Extractor | Scope | Facts (2.5) | Notes |
|---|---|---|---|
| `save_basics` | save | `json_version`, `active_context`, `save_name`, `play_time_s`, `pid_in_data` | pre-context: from `PlayerStateData` |
| `season` | save | `season` | milestones with embedded targets (2.5.4) |
| `discoveries` | save | `discoveries` | store counts by type |
| `ctx_basics` | context | `game_mode`, `timestamp`, `galaxy`, `summary`, `units`, `nanites`, `quicksilver`, `glyphs`, `nexus`, `title`, `teleporters`, `known_tech` | |
| `difficulty` | context | `difficulty` | |
| `words` | context | `words` | race order 0 Gek, 1 Vy'keen, 2 Korvax, 4 Atlas, 8 Autophage |
| `products` | context | `known_products`, `legacy_known` | 89 legacy ids (extraction-time constant) |
| `missions` | context | `missions` | raw progress of curated ids (2.5.3) |
| `stats` | context | `stats` | ~56 curated `^GLOBAL_STATS` ids (2.5.3) |
| `bases` | context | `bases`, `exocraft_bays` | own base types only, identity, legacy placed, layout hash |
| `ships` | context | `ships`, `archived_ships` | |
| `companions` | context | `companions`, `eggs`, `pet_slots` | |
| `fleet` | context | `freighter`, `frigates`, `multitools` | |
| `settlements` | context | `settlements` | |
| `inventories` | context | `inventories` | digests only (2.5.5) |

#### 2.4.4 Graceful degradation

- One failing extractor never sinks a version: its fields are absent, the error is recorded,
  status `partial`; all other facts stand. A fault-injection test proves it (5.3).
- Missing optional subtrees are not errors: values become `None`, `[]` or `{}`.
- Type surprises (a string where an int is expected) go through normalisers that return `None`.
- Unknown enum strings are kept raw; unknown base types are counted in `bases.unknown_types`
  (never treated as own); unknown ship models fall back to the model stem at display time.
- Decoder failures produce a status without facts: `corrupt` (`LZ4FormatError`, bad JSON),
  `unsupported` (HGSAVEV2, too large), never an exception out of the worker.

#### 2.4.5 Privacy filter

Ownership blocks hold platform account ids and usernames: base `Owner{UID, LID, USN, PTK, TS}`,
base `LastEditedById` / `LastEditedByUsername` / `PlatformToken`, discovery `OWS`, pet
`SenderData`, `CommonStateData.UsedDiscoveryOwnersV2`, account `BlockList` (R6 2.0, R8; D4
VERIFIED the base keys). The index reads none of them, so none can be persisted (owner Q6: no
ownership ids are processed). Three layers:

1. **Deny list in the accessor.** `DENIED = {"Owner", "OWS", "SenderData",
   "UsedDiscoveryOwnersV2", "LastEditedById", "LastEditedByUsername", "PlatformToken", "UID",
   "LID", "USN", "BlockList", "RID"}`. `get()` raises `PrivacyError` when a path contains one of
   them. One privileged accessor in `privacy.py` reads inside them:
   `owner_platform(base) -> str | None` returns `Owner.PTK` only (`"ST"`, `"XB"`, ...; a
   platform code, not an id) for the bases' `ptk` fact.
2. **Schema whitelist.** Facts are built only from declared fields; `validate()` drops any
   undeclared key (so "dump a subtree" mistakes cannot leak).
3. **Nothing else crosses the boundary.** The index never receives the live folder name
   (`account_key`), paths or meta plaintext of other versions; errors are truncated messages
   without values or paths; `External*` bases contribute only a count.

Free text (save, base, ship and companion names) is the player's own text and is kept as typed.

#### 2.4.6 Normalisation (`core/index/nodes.py`)

| Helper | Rule | Why (evidence) |
|---|---|---|
| `u32(v)` | `v & 0xFFFFFFFF` for ints | `Units` is stored signed: -1 = 4,294,967,295 (R6 VERIFIED) |
| `u64(v)` | `"0x.."` string or int -> int, masked to 64 bits | addresses come as strings and ints in one list (R6: 250 / 148) |
| `hex64(v)` | `"0x%016X"` | 64-bit values stay exact in JSON (`--json` output) and in every reader of it |
| `galaxy_of(addr)` | `(u64(addr) >> 32) & 0xFF` | base `GalacticAddress` layout (R6 2.7, 357/357 VERIFIED) |
| `text(v, limit=128)` | strip `<...>` markup, surrogates -> U+FFFD, trim, truncate | `<PET1>The Hangar</>` (R6); sqlite TEXT must be valid UTF-8 |
| `gid(v)` | `text(v)` minus a leading `^` | game ids `^ACT1_STEP1`, `^T_WALLM_H`; ids are stored without `^` |
| `seed(v)` | `[bool, "0x.."]` or `"0x.."` -> `hex64` | ship / pet seeds |
| `number(v)` | `Value{IntValue | FloatValue}` -> int or float | `^GLOBAL_STATS` values |

Every string in the facts is valid Unicode and every game id is `^`-free; numbers are JSON
numbers except 64-bit values (hex strings).

### 2.5 Fact schema v1

`SCHEMA_VERSION = 1`. Facts are **raw**: ids, counts, indices, digests. Everything that needs a
game table (names, milestone completion, ship types, journey rank) is derived at **read time**
(2.5.6), so a lookup update never forces a re-index. The only game data used at extraction time
are stable id sets (89 legacy part ids, own base types), and changing those bumps
`EXTRACTOR_VERSION`. Read-time derivation from raw progress reproduces R6's computed milestone
values exactly (VERIFIED: Steam main 30/30, 11/11, 5/5, 4/4, 0/4, 3/3, 3/3, 5/5, 5/5, 10/10,
12/21; Xbox main pirates 0/3, base computer 10/21).

#### 2.5.1 Types

```python
class ExtractorError(TypedDict):
    extractor: str                       # "base.ships"
    error: str                           # "<Type>: <message>", <= 200 chars, no paths/values

class ContainerFacts(TypedDict):
    kind: Literal["chunked", "unframed"]
    chunks: int
    decompressed_bytes: int
    decompressed_sha256: str

class SeasonFacts(TypedDict):
    id: int | None                       # SeasonData.SeasonId (0 = no expedition)
    number: int | None                   # SeasonNumber (46 = Swarm)
    display: int | None                  # DisplayNumber (22)
    name_key: str                        # "UI_SEASON_22_NAME" (resolved at read time)
    milestones: list[list[float | bool]] # [[value, amount, optional], ...] aligned (2.5.4)
    final_reward: bool                   # SeasonState.HasCollectedFinalReward
    special_rewards: int                 # len(EarnedSeasonSpecialRewards)

class SaveFacts(TypedDict):
    layout: Literal["context", "pre-context"]
    json_version: int | None             # top-level Version (BaseVersion + 512 x mode)
    active_context: Literal["Main", "Season"] | None
    save_name: str                       # the real name (Xbox metas can be empty; R6 2.8 #1)
    play_time_s: int | None
    pid_in_data: str | None              # SaveUniversalId, '0x%016X' (D1 consistency check)
    season: SeasonFacts | None
    discoveries: dict                    # {"total": int, "by_type": {"Planet": int, ...}}

class OwnBase(TypedDict):
    id: str                              # stable identity (2.5.2)
    name: str
    type: str                            # HomePlanetBase | FreighterBase | PlayerShipBase | PlayerSpaceBase | PlayerSpaceStationBase
    galaxy: int | None
    address: str | None                  # '0x%016X'
    parts: int                           # len(Objects)
    legacy_structures: int               # objects in BASIC_LEGACY (69 ids)
    legacy_freighter: int                # objects in FREIGHT_LEGACY (20 ids)
    orig_version: int | None             # OriginalBaseVersion (3/5/6/7/8)
    layout: str                          # 16-hex digest of the ObjectID histogram (2.5.2)

class Ship(TypedDict):
    slot: int; name: str; model_path: str; cls: str | None; seed: str | None; primary: bool

class ContextFacts(TypedDict, total=False):
    game_mode: int | None                # 1 normal, 5 permadeath, 6 expedition (R6)
    timestamp: int | None                # PlayerStateData.TimeStamp = last played in this context
    difficulty: dict                     # {preset, easiest, hardest, death, crafting_free, locked}
    galaxy: int | None                   # current galaxy (RealityIndex)
    summary: str                         # SaveSummary of the context
    units: int | None                    # u32
    nanites: int | None
    quicksilver: int | None              # Specials
    glyphs: int | None                   # popcount(KnownPortalRunes & 0xFFFF), 0..16
    nexus: bool                          # HasAccessToNexus
    title: str                           # BannerTitleId (selected title)
    teleporters: int                     # len(TeleportEndpoints)
    known_tech: int
    known_products: int
    legacy_known: dict                   # {"structures": 0..69, "freighter": 0..20}
    words: dict                          # {"gek": n, "vykeen": n, "korvax": n, "atlas": n, "autophage": n}
    missions: dict                       # {"version": int, "current": str, "progress": {id: int}}
    stats: dict                          # {id: number}, ids without '^' (2.5.3)
    bases: dict                          # {"own": [OwnBase], "external_cached": int,
                                         #  "orphan_corvette_layouts": int,
                                         #  "unknown_types": {type: n}, "ptk": [str]}
    exocraft_bays: list[str]             # GARAGE_* / SUMMON_GARAGE ids placed
    ships: list[Ship]
    archived_ships: list[dict]           # [{"name", "ship_class", "inv_class"}]
    companions: list[dict]               # [{"slot", "name", "creature", "biome", "seed"}]
    eggs: int
    pet_slots: int                       # unlocked companion slots
    freighter: dict                      # {"has": bool, "name", "model", "cls"}
    frigates: int
    multitools: list[dict]               # [{"name", "model", "cls"}]
    settlements: dict                    # {"claimed": int, "names": [str]}
    inventories: dict                    # {container: {"slots", "capacity", "digest"}} (2.5.5)

class VersionFacts(TypedDict):
    schema: int                          # 1
    extractor: int                       # EXTRACTOR_VERSION that produced it
    status: Literal["ok", "partial", "corrupt", "unsupported", "error"]
    errors: list[ExtractorError]
    container: ContainerFacts | None
    save: SaveFacts | None
    contexts: dict[str, ContextFacts]    # keys "base" and/or "expedition"
    primary: Literal["base", "expedition"] | None
```

Facts are serialised as compact JSON (`ensure_ascii=False`, `sort_keys=True`). VERIFIED sizes on
the vault: median 8.4 KB, max 26 KB per version, 2.27 MB for all 195.

#### 2.5.2 Bases: ownership, identity, layout

- **Own bases** = types `HomePlanetBase`, `FreighterBase`, `PlayerShipBase`, `PlayerSpaceBase`,
  `PlayerSpaceStationBase` (R6 2.3). `External*` (other players' bases, up to 228 cached) are
  only counted; `UITempShipBase` (corvette editor scratch) is ignored; corvette layouts whose
  `UserData` slot does not hold a corvette in `ShipOwnership` are orphans (13 of 35 in R6) and
  only counted.
- **Identity** (needed by "what changed"; VERIFIED necessary on 64 real lineage steps): the
  freighter base and corvette layouts move with the vehicle, so their address changes every
  save. `id` = `"freighter"` for `FreighterBase`; `"corvette:<ship slot>"` for
  `PlayerShipBase`; otherwise `"<type[:12]>:" + sha256([address, Position])[:12]` (two bases on
  one planet share the planet's address, so the position is part of the identity).
- **Layout digest** = sha256 of the sorted `ObjectID` histogram. Object `Timestamp` changes on
  ~5,500 objects and `UserData` on 120-220 objects in every save of the main playthrough, and
  the base `Owner` block churns too (VERIFIED, `D4WD\scripts\base_churn.py`), so hashing whole
  objects reports every base as "changed".
- **Galaxies with bases** = distinct `galaxy` of own `HomePlanetBase` entries (R6 2.7; the
  option to include stations and outposts is O2).
- **Legacy parts** = per own base, counts of objects in the two legacy groups (C13), plus
  `legacy_known` from `KnownProducts` per context.

#### 2.5.3 Missions and stats captured raw

- `missions.progress` keeps `{id: Progress}` for every `MissionProgress` entry whose id starts
  with one of the chain prefixes `ACT1_STEP ACT2_STEP ACT3_STEP ATLAS SENTINELS_ ROBOMISS_
  WORLDSM PURPM PIRATES WATERSTORY BIO_SHIP OVERSEER BASE_UPGRADE EPILOGUE_ ART_ APOLLO_DEAD
  DID_ A3_CHOICE ENABLE_NEXUS SETTLEMENT_PRE` and whose progress is not -1. VERIFIED: median 11,
  max 104 ids per context (260 B / 1.9 KB). New steps of known chains (a future `ACT1_STEP14`)
  are captured without an extractor change; a new chain prefix bumps `EXTRACTOR_VERSION`.
- `missions.version` = `MissionVersion` (selects the final-stage values at read time),
  `missions.current` = `CurrentMissionID`.
- `stats` keeps these `^GLOBAL_STATS` ids (without `^`), ~1.2 KB per context (VERIFIED):
  - 31 counters that never decrease along a known parent step (R7 2.7; VERIFIED by D4 on 179
    steps): AMMO_FIRED APP_SESSIONS COMM_09_DAY DISC_PLANETS DISC_RARE_SYS DISC_SYSTEMS DIST_FLY
    DIST_JETPACK DIST_PULSE DIST_WALKED DIST_WARP ESEEN_SYSTEMS EXTREME_WALK ITEMS_TELEPRT
    LONGEST_LIFE MONEY_EVER MP_FULL_COUNT MP_FULL_TIME MP_SESSIONS NANITES_EVER PARTS_PLACED
    PLAY_SESSIONS PORTAL_WARPS RES_EXTRACTED SHIP_SUMMON STORM_WALK TIME TIMES_IN_SPACE
    TSEEN_SYSTEMS VISIT_PLANETS WSEEN_SYSTEMS;
  - `WORDS_LEARNT DISC_CREATURES DISC_FLORA DISC_MINERALS DEATHS SENTINEL_KILLS TRA_STANDING
    WAR_STANDING EXP_STANDING BUI_STANDING TGUILD_STAND WGUILD_STAND EGUILD_STAND PIRATE_STAND
    STATIONS_OWNED PETS_ADOPTED SHIPS_BOUGHT FRIGATES EXPEDITIONS MONEY JM JM_BANKED ATLAS_PATH
    ATLAS_LOOPS HOME_REALITY`.
  Stats that do decrease are never reported as changes (N15): BIG_FALL COMM_09_ODD_DAY
  COMM_CONTRIBUTE DEV_NOTES EXP_STANDING MEDITATION SPACE_TIME VISIT_EXT_BASE.

#### 2.5.4 Expedition milestones (rule replaced)

R6 counted a milestone as done when its value is >= 1 (INFERRED). The save embeds the targets:
`CommonStateData.SeasonData.Stages[].Milestones[].{Amount, IsOptional}` aligned one to one with
`SeasonState.MilestoneValues` (VERIFIED on the expedition-only Xbox save: 48 = 48 entries, pairs
such as (150, 150) done and (0, 150) not done). The extractor stores `[value, amount, optional]`
per milestone; done = `value >= amount`. When the stages are missing or misaligned it stores
`amount = 1` and reports `season: milestone targets missing` (old rule as fallback). On the same
save both rules give 9 of 48.

#### 2.5.5 Inventory digests

So that edits outside the game and item changes are visible in "what changed" without storing
inventories: per context `inventories[container] = {"slots": filled slots, "capacity": len(
ValidSlotIndices), "digest": sha256(sorted (Id, Amount, MaxAmount, X, Y))[:12]}` for containers
`exosuit` (general + tech + cargo), `freighter` (all three), `storage` (`Chest1..10`,
`ChestMagic`, `ChestMagic2`, cooking, rocket locker, fishing, food unit, corvette storage),
`vehicles` (all exocraft), `ship:<slot>` and `multitool:<index>`. VERIFIED: with digests the
seven editor sessions M0 described (e.g. "item amounts changed in 33 slots; 1 slot added") are
reported ("inventory changed: exosuit (+1 slot), freighter, ship") instead of "no progress";
cost included in the 1.1 s extraction total.

#### 2.5.6 Read-time views

`derive.py` turns raw facts plus the current lookup tables into two frozen, JSON-safe
dataclasses (`to_dict()`), computed on demand in < 1 ms and cached per `(vid, LOOKUP_VERSION)`.

`FactsView` - the flat row D1 puts on `Line.facts` and version rows (D1 2.10.1); every UI column
and `facts:<name>` sort key comes from here:

```python
@dataclass(frozen=True)
class FactsView:
    vid: str
    status: str                          # ok | partial | corrupt | unsupported | error
    stale: bool                          # produced by an older EXTRACTOR_VERSION (refresh queued)
    primary: Literal["base", "expedition"] | None
    active: Literal["base", "expedition"] | None      # which context the game loads
    save_name_in_data: str
    pid_in_data: str | None              # SaveUniversalId '0x%016X' (D1's id consistency check)
    # owner's facts (primary context)
    story: tuple[int, int] | None        # Artemis Path (done, 30)
    story_ending: str | None             # "new galaxy" | "stayed" | None
    atlas: tuple[int, int] | None        # (done, 11)
    chains: Mapping[str, tuple[int, int]]  # every chain (2.7)
    glyphs: int | None
    own_bases: int
    base_parts: int                      # all own bases, orphan corvette layouts excluded (O2)
    planet_bases: int
    planet_base_parts: int
    largest_base: tuple[str, int] | None
    galaxies_with_bases: tuple[tuple[str, int], ...]   # (name, planet bases), most first
    legacy_placed: int                   # objects in legacy groups across own bases
    legacy_placed_bases: int
    legacy_buildable: tuple[int, int]    # (structures of 69, freighter modules of 20)
    ships: int                           # fleet incl. corvettes
    corvettes: int
    archived_ships: int
    ship_names: tuple[str, ...]          # display names (custom, else type), primary first
    companions: int
    companion_names: tuple[str, ...]     # custom name, else friendly creature type (O4)
    eggs: int
    # extras
    expedition: Mapping[str, object] | None   # {"name", "number", "done", "total", "final_reward", "active"}
    current_galaxy: str | None
    journey_rank: str | None
    quicksilver: int | None
    nanites: int | None
    units: int | None
    freighter: str | None                # "HarmonicLepton (S)"
    frigates: int
    words: int
    deaths: int | None
    systems_discovered: int | None
    last_played: int | None              # primary context TimeStamp
    numbers: Mapping[str, float]         # every numeric above by name, for facts:<name> sorting
    chips: tuple[str, ...]               # ASCII milestone chips (O8): "Story 30/30", "Atlas 11/11",
                                         # "Exp 22 Swarm 15/23", "Glyphs 16", "Legacy parts"
    lookups: str                         # LOOKUP_VERSION used
```

`FactsCard` - the version card: per context, every own base (name, type label, galaxy name,
parts, legacy counts), every ship (display name, type, class, primary), archived ships,
companions with display names, the milestone checklist (per chain step: done / stage s of f /
not started; ending choices), expedition milestones (value / amount / optional), difficulty and
key settings, freighter and frigates, multitools, settlements, exocraft bays, words per race,
selected stats with labels, inventory fill per container, and the version's errors.

### 2.6 The `FactsProvider` contract for D1

D1 declares the protocol its read model consumes (D1 2.10); `IndexService` (2.12) implements it
with exactly these names and types, and the front end passes the service to `ReadModel.build` as
`facts`:

```python
class FactsProvider(Protocol):
    def view(self, vid: Vid) -> FactsView | None: ...
    def changes(self, child: Vid, parent: Vid, *,
                edge_kind: str | None = None) -> ChangeSet | None: ...
    def is_milestone(self, child: Vid, parent: Vid) -> bool | None: ...
    def request(self, vids: Iterable[Vid], priority: Priority) -> int: ...
```

| Call | Rule |
|---|---|
| `view` | cache read, < 1 ms, never queues work; `None` = not indexed yet or index off, and D1 shows meta-level columns |
| sorting by `facts:<name>` | D1 sorts by `view.numbers.get(name)`; versions without a view or without that number sort last. `numbers` holds every numeric `FactsView` field by its field name; pairs such as `story` and `atlas` by their done count |
| `changes` | only for a drawn edge (recorded, observed, "seen in backups" or owner); `edge_kind` is D1's kind (`play`, `touch`, `edit`, `unknown`); `None` = one of the two versions is not indexed yet (both are then queued at NEW). D1 never calls it for a version without a parent (2.11.1) |
| `is_milestone` | `True` when the change set has an item of weight >= 85; `None` while either version is not indexed |
| `request` | queues; returns how many vids were newly queued. D1's facade after each refresh: LIVE = live members, TIP = line representatives and branch tips, NEW = vids first seen since the last run, BACKFILL = every other version, once per session. INTERACTIVE comes only from an opened version card or compare view, never from a read-model build, so the reserved worker stays free for the owner |

`card`, `facts`, `deep`, `status` and the progress calls (2.12) are for the UI and the CLI, not
part of the protocol.

### 2.7 Lookup tables (C12)

#### 2.7.1 Layout

```
core/index/lookups/
  __init__.py     LOOKUP_VERSION = "b25729564.1"; galaxy_name(), expedition_name(),
                  ship_type(), ship_class_name(), journey_rank(), chain_status(),
                  creature_type(), base_type_label()  - all with raw-id fallbacks
  galaxies.py     GALAXY_NAMES: tuple[str, ...]           (256)
  missions.py     CHAINS: dict[str, tuple[str, ...]]      (11 chains), CHOICES,
                  FINAL_STAGES: dict[str, tuple[tuple[int, int], ...]]  (change points)
  ships.py        FOLDER_TYPES (9), SPECIAL_MODELS (9), SHIP_CLASS_NAMES (11)
  seasons.py      EXPEDITIONS: dict[int, tuple[int, str]] (SeasonNumber -> display, name),
                  SEASON_NAME_KEYS: dict[str, str]        (UI_SEASON_n_NAME -> English)
  journey.py      JOURNEY_RANKS (12), POINTS_PER_RANK = 5
  parts.py        LEGACY_STRUCTURES (69), LEGACY_FREIGHTER (20), OWN_BASE_TYPES (5)
  creatures.py    CREATURE_TYPES: dict[str, str]          (hand-made, ~30)
  _overrides.py   curated display names and chain membership (INFERRED items, reviewed by hand)
```

Each generated module starts with a header naming the game build it came from, the date, the
generator and "generated - edit `_overrides.py` instead". Total size ~15 KB.

#### 2.7.2 Tables, use and fallback

| Table | Used | Size | Source | Fallback | Stability |
|---|---|---|---|---|---|
| Legacy part ids | extraction | 69 + 20 | `basebuildingobjectstable` groups BASIC_LEGACY / FREIGHT_LEGACY (build 25729564; R6 VERIFIED) | none needed; a change bumps `EXTRACTOR_VERSION` | stable |
| Own base types | extraction | 5 | R6 2.3 VERIFIED | unknown types counted, never own | stable |
| Galaxy names | read | 256 | game data 1-10 + community list (R6 2.7) | `Galaxy N` (1-based) | stable |
| Mission chains + final stages | read | ~130 ids, change points only (~6 KB) | mission tables (R6 2.1) | unknown final stage -> "started" with raw stage | changes with updates |
| Ship model -> type, special models | read | 9 folders + 9 models | `rewardtable` + model folders (R6 2.5) | model stem, e.g. `BIGGS2` | grows per reward ship |
| Ship class enum names | read | 11 | archived ships (R6) | raw enum | stable |
| Expedition names | read | 23 numbers + name keys | `historicalseasondatatable` + English loc (R6) | the save's own `SeasonName` key, else `Expedition #N` | +1 per expedition |
| Journey ranks | read | 12 | `journeymilestonetable` (R6; `JM // 5` INFERRED) | `Rank N` | stable |
| Creature friendly types | read | ~30 | hand-made (no names in game data) | id title-cased (`Flyinglizard`) | grows slowly |

Final-stage change points: the stored form for `ACT1_STEP13` is `((1, 27), (10, 46), (11, 49),
(15, 50), (22, 54))`; the value for a save is the last point with `version <= MissionVersion`,
else the last point (tables end at 39 while current saves are 40, R6 VERIFIED). Completion:
`Progress >= final`, `Progress == 2147483647` counts as done (R6 INFERRED); choice missions
(`EPILOGUE_NEWGAL`, `ART_DEAD`, ...) have final 0, so "started" means "chosen".

Read-time application means only `parts.py` changes require a re-index; every other table
update takes effect on the next view.

#### 2.7.3 Generator and licensing

`tools/gen_lookups.py` (dev-only; may use anything per DESIGN.md D6) reads an unpacked game dump
through its metadata index (R6's `build_lookups.py` is the working prototype) and rewrites the
generated modules; `_overrides.py` holds the curated, reviewed parts (display names "Hauler /
Explorer / Exotic / Solar / Interceptor", `COCKPIT_A` as Exotic, chain membership). A test checks
the modules are internally consistent (every chain id has final stages, exactly 256 galaxy
names). `tests/test_ui_text.py` already forces every string literal under `src/` to ASCII; all
256 galaxy names and all 24 expedition names are ASCII (VERIFIED on R6's tables), and the
generator rejects a non-ASCII name instead of writing it.

Licensing: the names are Hello Games game content plus one community list. Shipping ~350 short
strings as data inside a GPL-3.0 app is common practice among community tools (libNOM / NomNom
ship far larger tables), but it is the owner's call (option O7). The code is ours; each table
names its source.

### 2.8 Execution (C11, C31)

#### 2.8.1 Policy

```python
@dataclass(frozen=True)
class ExecutionPolicy:
    mode: Literal["processes", "thread", "off"]
    workers: int                      # processes: 1-4; thread: 1; off: 0
    reserve_interactive: bool         # keep one process free for on-demand jobs (workers >= 3)
    memory_budget: int                # bytes of estimated worker heap in flight (default 1.2 GB)

# core/platform.py (the OS edge module)
def index_execution_policy(host: Host, *, portable_kit: bool, cpu_count: int | None,
                           setting: str | int = "auto") -> ExecutionPolicy:
    """auto: processes with min(4, max(2, cpu_count - 1)) workers (C11: 2-4; a 1-2 core PC
    gets 1) on Windows (kit or source) and on POSIX source checkouts; 'thread' in POSIX kits
    until the spawn check of the verification ladder (DESIGN.md T2/T3) passes (constant
    SPAWN_VERIFIED_POSIX_KIT flips then). reserve_interactive when workers >= 3.
    setting: 'auto' | 0 (off) | 1..8, from state.json key 'index_workers'."""
```

The front-end (`gui.py` / `cli.py`, allowed to know the environment) calls it and passes the
result into `IndexService` (DESIGN.md D7). `core/index` never inspects the OS.

#### 2.8.2 Process pool

- `multiprocessing.get_context("spawn").Pool(workers, maxtasksperchild=25)`, created lazily on
  the first job, closed after 60 s idle, `terminate()` on cancel or exit (18 ms in the kit, R5
  F5.2 VERIFIED). `spawn` everywhere: `fork` with Tk/X11 state is unsafe (R5 F5.4).
  `multiprocessing.Pool` rather than `ProcessPoolExecutor`: it can terminate running work, and
  `max_tasks_per_child` needs Python 3.11 while the project supports 3.10.
- Workers run `core.index.worker.run_job`, a top-level function in an importable module that
  imports no `tkinter` (the kit's `_runtime\app` is on the `._pth` path; R5 F5.2 VERIFIED spawn
  under the GUI bootstrap, which skips `--multiprocessing-fork` children at
  `packaging/sitecustomize.py:126`). Entry modules must be import-safe because spawn re-imports a
  main module that has a `__spec__` (`python -m nms_save_vault.gui`): a test enforces it.
- Workers are read-only by construction: bytes in, facts out. They open no files at all.

```python
@dataclass(frozen=True)
class IndexJob:                       # pickled to the worker
    vid: str
    kind: Literal["facts", "deep"]
    data: bytes                       # verbatim data-file bytes from VersionLocator.open()
    bare_size_hint: int | None        # Xbox meta size field (0x10) for pre-4.52 unframed blocks
    extractor_version: int

@dataclass(frozen=True)
class IndexResult:
    vid: str
    kind: str
    actual_sha256: str                # computed by the worker over job.data
    status: Literal["ok", "partial", "corrupt", "unsupported", "error", "mismatch"]
    facts: dict | None                # VersionFacts
    deep: dict | None
    timings_ms: dict[str, int]        # hash, decode, parse, extract
    error: str | None                 # sanitised

def run_job(job: IndexJob) -> IndexResult:
    """Never raises. sha256(job.data) != job.vid -> 'mismatch' (not cached). MemoryError ->
    'error' (retried alone once). Everything else maps to a status (2.4.4)."""
```

Bytes travel to the worker through the pool's pipe (largest real save 6.3 MB; INFERRED
~5-10 ms). D1's `Location` has no path and the storage layer requires reads through `open()`, so
the parent's dispatcher thread calls `open(vid)` (sha256 verified there; D3 measured 1.43 ms for
2.1 MB) and
the worker re-hashes (4 ms for 6 MB) so the cache key is always the hash of the bytes actually
decoded (invariant I2).

#### 2.8.3 Dispatcher (parent)

One daemon thread `index-dispatch`:

```text
loop:
    wait for (queue not empty and capacity) or stop/pause
    job = queue.pop_best()            # tier order (2.10); inside a tier: newest-first or largest-first
    est = estimated_heap(job)         # 9 x JSON bytes; JSON = size_hint(vid) or 4 x data size
    if est > budget / 2:              # oversized (> ~67 MB JSON): wait until nothing runs, run alone
        wait until in_flight == 0
    elif not job.interactive and reserve_interactive and in_flight == workers - 1:
        wait
    elif in_flight_heap + est > budget:
        wait
    opened = locator.open(job.vid)    # VersionUnavailable -> failure table, next job
    hint = ms_meta_size(opened.meta_plain) if opened.platform == "xbox" and not chunked(opened.data) else None
    pool.apply_async(run_job, (IndexJob(...),), callback=results.put, error_callback=on_crash)
watchdog (every 1 s): a job running longer than max(60 s, 10 s per 10 MB JSON) -> terminate the
    pool, recreate it, mark that job 'error: timeout' (retried once, alone), re-queue the others.
```

Budget: the owner's largest save needs ~170 MB of heap (R5: 151 MB for 18.2 MB JSON, process peak
~270 MB), so 4 concurrent fit in 1.2 GB with room; a low-memory PC can lower the setting.

#### 2.8.4 Writer (parent)

One daemon thread `index-write` owns the only sqlite write connection (the parent process is the
only cache writer, C11). For each result: one transaction (version row + context rows, or a
failure row), commit, update the progress snapshot, append an `IndexEvent`. A killed
worker or app loses only in-flight versions (R5 F5.3). The writer never touches UI objects.

#### 2.8.5 Thread mode (POSIX kits until verified) and "off"

Same dispatcher; `run_job` runs inline on the dispatcher thread, one job at a time; cancellation
is cooperative between jobs (an in-flight job finishes, <= 0.56 s here). `json.loads` holds the
GIL, so a Tk window hitches 0.2-0.4 s per big save (R5 F5.1 VERIFIED) - an accepted, documented
degradation. `off`: nothing is indexed; views return `None`; the UI says "content index off".

#### 2.8.6 How a UI consumes progress without blocking

```python
@dataclass(frozen=True)
class IndexProgress:
    state: Literal["idle", "running", "paused", "throttled", "off", "error"]
    queued: int
    running: int
    done: int
    failed: int
    done_bytes: int                   # estimated JSON bytes, for a progress bar
    total_bytes: int
    eta_s: float | None
    message: str                      # ASCII, e.g. "Indexing 12 of 195 versions"

@dataclass(frozen=True)
class IndexEvent:
    seq: int                          # monotonically increasing per service
    kind: Literal["indexed", "failed", "stale", "cleared", "deep-ready", "finished"]
    vid: str | None
    status: str | None
```

- `progress()` returns an immutable snapshot replaced under a lock: O(1), never waits on work.
- `events_since(seq, limit=500)` reads a ring of the last 2,000 events: one consumer model for
  the Tk poller and the CLI's progress lines.
- Tk (D6): `root.after(250, poll)`; `poll` reads `events_since(last_seq)`, refreshes only rows
  whose `vid` changed (`view(vid)` is a < 1 ms read), updates the status line from `progress()`,
  and reschedules. No Tk call ever happens off the Tk thread.
- `--json` output uses `FactsView.to_dict()` / `FactsCard.to_dict()` (64-bit values are strings).
- Nothing a UI calls decodes a save; decoding happens only in workers (C31).

### 2.9 Cache

#### 2.9.1 Location and keying

`<user_cache_dir>/vaults/<vault_id>/index-v1.sqlite` (+ `-wal`, `-shm`) - D1's shared
per-machine folder (D1 2.4): Windows `%LOCALAPPDATA%\NMSSaveVault\cache`, macOS
`~/Library/Caches/NMSSaveVault`, Linux `$XDG_CACHE_HOME/NMSSaveVault` (else `~/.cache/...`).
Keyed to the vault by `vault_id` (section 6); a vault without a store uses D1's
`nostore-<hash>` folder name. Rows are content-addressed by `vid`, so the file stays valid for
any vault holding the same bytes. `IndexCache.open()` refuses a path that resolves inside the
vault or a live save root (invariant I1). The cache never lives on a network share: it is always
under the local user cache directory (R5 F6, SOURCED from the SQLite docs: WAL does not work over
a network filesystem and remote use has led to corruption).

#### 2.9.2 Schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
-- schema=1, created=<unix>, app_version, vault_id, last_open=<unix>

CREATE TABLE version (
  vid                 TEXT PRIMARY KEY CHECK (length(vid) = 64),
  status              TEXT NOT NULL CHECK (status IN ('ok','partial','corrupt','unsupported','error')),
  schema              INTEGER NOT NULL,
  extractor           INTEGER NOT NULL,
  decoder             INTEGER NOT NULL,
  container           TEXT,
  layout              TEXT,
  data_bytes          INTEGER,
  json_bytes          INTEGER,
  decompressed_sha256 TEXT,
  play_time           INTEGER,
  save_name           TEXT,
  pid_in_data         TEXT,
  primary_ctx         TEXT,
  save_facts          TEXT,                 -- JSON SaveFacts (+ container facts)
  errors              TEXT NOT NULL DEFAULT '[]',
  hash_ms INTEGER, decode_ms INTEGER, parse_ms INTEGER, extract_ms INTEGER,
  indexed_at          INTEGER NOT NULL,
  last_seen           INTEGER NOT NULL      -- last request() that named this vid (GC)
);

CREATE TABLE context (
  vid   TEXT NOT NULL REFERENCES version(vid) ON DELETE CASCADE,
  ctx   TEXT NOT NULL CHECK (ctx IN ('base','expedition')),
  facts TEXT NOT NULL,                      -- JSON ContextFacts
  glyphs INTEGER, own_bases INTEGER, base_parts INTEGER, ships INTEGER,
  companions INTEGER, quicksilver INTEGER, galaxies_with_bases INTEGER,
  PRIMARY KEY (vid, ctx)
);

CREATE TABLE failure (                      -- transient problems, never facts
  vid TEXT PRIMARY KEY, attempts INTEGER NOT NULL, last_error TEXT NOT NULL,
  last_at INTEGER NOT NULL, kind TEXT NOT NULL   -- unavailable | mismatch | worker-died | timeout
);

CREATE INDEX version_extractor ON version (extractor);
CREATE INDEX version_status ON version (status);
```

The schema needs no JSON1: JSON is parsed in Python (the materialised scalar columns are for
cheap ad-hoc queries and the CLI). Python 3.10+ `sqlite3` is enough on every platform.

#### 2.9.3 Reads and writes

- Writer: one connection on the writer thread (`busy_timeout = 5000`), one transaction per
  result.
- Readers: thread-local connections (`check_same_thread` stays on) + an LRU of 512 parsed
  `VersionFacts`; WAL lets readers run during writes. `facts(vid)` < 1 ms; all 195 versions load
  and parse in ~9 ms (R5 F6 VERIFIED).
- Two app instances: both may index; rows are idempotent (same bytes + extractor -> same facts),
  so the cost is duplicate CPU, never corruption.

#### 2.9.4 Invalidation and rebuild

| Change | Effect |
|---|---|
| `EXTRACTOR_VERSION` bump | rows stay readable with `FactsView.stale = True`; re-queued at REFRESH |
| `DECODER_VERSION` bump (e.g. HGSAVEV2 support) | only rows with status `corrupt` / `unsupported` / `error` re-queued |
| cache layout change (schema major) | new file `index-v2.sqlite`, filled by a full rebuild; the old file is deleted (derived data, C8) |
| lookup tables change | nothing (read time) |
| owner "Rebuild content index" | rows deleted, everything queued (the ~12 s / ~1 min first-index cost) |
| vid unknown to every location for 180 days (`last_seen`) | row deleted by GC at startup |

#### 2.9.5 Corruption and failures

- Open: `PRAGMA quick_check` (milliseconds at this size). On `sqlite3.DatabaseError` or a failed
  check: close, rename to `index-v1.corrupt-<UTC>.sqlite` (keep only the newest one), create a
  fresh file, queue a full rebuild, show one line "Content index rebuilt (cache file was
  damaged)". Nothing is lost (C8: derived data only).
- Disk full or I/O error while writing: the writer stops indexing (state `error`), cached facts
  stay readable, the message names the cause; no retry loop.
- Read-only cache directory: the session uses an in-memory database with a warning.
- A newer schema in the file name never collides (file names carry the major version).

#### 2.9.6 Size

VERIFIED (`D4WD\db\facts-w4.sqlite`, all 195 versions): 3.24 MB including a lineage table of
0.49 MB that this design no longer has (facts 2.27 MB, rest overhead). Without it ~2.75 MB =
~14 KB per version; 1,000 versions ~14 MB; 10,000 ~140 MB (INFERRED, linear).

### 2.10 Scheduling

#### 2.10.1 Priority tiers

```python
class Priority(IntEnum):
    INTERACTIVE = 0   # a version card or compare view is open; deep decode
    LIVE = 1          # current live members (what the board shows)
    TIP = 2           # each line's representative and branch tips
    NEW = 3           # versions first seen since the last run, or created by an operation
    BACKFILL = 4      # every other known version
    REFRESH = 5       # stale rows (older extractor / decoder)
```

Inside a tier: INTERACTIVE first-in-first-out; LIVE, TIP, NEW newest `meta_ts` first (user
value); BACKFILL and REFRESH largest first (longest-processing-time order shortens the tail;
C11 "size-ordered"). A vid requested again at a better tier moves up; a vid already indexed at
the current extractor version is never queued again.

#### 2.10.2 Triggers

| When | Request |
|---|---|
| App start, after D1's first scan (~1 s) | LIVE: live member vids; TIP: line representatives and tips; NEW: vids first seen since the last run; BACKFILL: the rest; REFRESH: stale rows |
| After a D5 operation or a D1 capture | NEW: the new vids |
| D1's journal observes a live change (focus / refresh) | LIVE: the changed vids |
| A version card opens | INTERACTIVE: the vid if not indexed, plus `request_deep(vid)` |
| The game is running (`safety.is_game_running()` True or None) | 1 worker; BACKFILL and REFRESH paused (O11) |
| Index setting "off" | nothing |

The index never blocks or delays an operation (invariant I8).

#### 2.10.3 Expected times on the owner's vault

VERIFIED on copies (this PC, 24 cores; `D4WD\scripts\run_index.py`): 195 versions, 1,549 MB JSON.

| Run | Wall time | Notes |
|---|---|---|
| 4 spawn workers | 11.8-12.8 s | first result 0.7 s; 0 errors; 0 partial |
| 1 process | 45.8 s | sums: decode 21 s, `json.loads` 21 s, extraction 1.1 s |
| per version | median 169 ms, p90 513 ms, max 562 ms | largest (18.8 MB JSON): decode 292, parse 247, extract 11 ms |

INFERRED for the shipped policy and other PCs: 3 background workers + 1 reserved ~16 s here; a
typical 4-8 core PC with 1.5-3x slower cores (R5 F4) 25-60 s; the LIVE tier (<= 60 members)
finishes in the first ~5-10 s. A new version after an operation: one job, 0.17 s median here,
<= ~1.7 s on slower hardware.

#### 2.10.4 On-demand deep decode (version card)

The card opens immediately with cached facts. The deep section (every own base's part
histogram, every inventory's slots, loose placeables) is decoded by the reserved worker: decode +
parse <= 0.56 s here, plus ~10-20 ms dispatch and transfer. Latency budget: <= 1 s here, <= 2 s on
a typical PC; a spinner appears after 150 ms. Results stay in an in-memory LRU of 5 versions
(not persisted in v1; the deep data is up to ~100 KB per version and depends on optional lookup
tables).

```python
class DeepFacts(TypedDict):
    vid: str
    contexts: dict[str, dict]   # ctx -> {"bases": [{"id", "name", "type", "parts_by_id": {ObjectID: n}}],
                                #          "inventories": {container: [{"id", "amount", "max", "x", "y"}]},
                                #          "loose_objects": {ObjectID: n}}
```

Part and item names (2,124 part ids, 4,780 items; HG content) are an O7 decision; v1 shows ids.

### 2.11 What changed (C14)

#### 2.11.1 Contract

```python
@dataclass(frozen=True)
class Change:
    kind: str                     # "base.new", "milestone.complete", ... (table 2.11.4)
    ctx: Literal["base", "expedition", "save"]
    weight: int                   # ordering; >= 85 marks a milestone-class change
    text: str                     # ASCII template + game/user names (Unicode data, C22)
    data: Mapping[str, object]    # structured values for a richer UI

@dataclass(frozen=True)
class ChangeSet:
    child: str
    parent: str
    header: str                   # "+2.7 h played" | "no progress (re-saved)" | "edited outside the game" ...
    items: tuple[Change, ...]     # sorted: weight desc, then magnitude desc
    def top(self, n: int = 3) -> tuple[Change, ...]: ...
    def lines(self, n: int | None = None) -> list[str]: ...   # header + item texts (section 6's list[str])

def changes(child: VersionFacts, parent: VersionFacts, *,
            edge_kind: str | None = None) -> ChangeSet: ...
def is_milestone(cs: ChangeSet) -> bool: ...   # any item with weight >= 85
```

The parent is always D1's drawn parent: a recorded, observed, "seen in backups" or owner link
(C14, rule N1). The index never chooses one, and nothing replaces a missing one: a version with
no recorded parent gets no change set, and the UI shows "no recorded parent" (owner Q5; the text
comes from D1's read model). A comparison with the previous version by date is never made (N1).
Cost VERIFIED: 64 real lineage steps in 15 ms (0.24 ms each).

#### 2.11.2 Algorithm

1. Both versions must be indexed (else `None`, and both are requested at NEW).
2. Header from the play-time delta `dt` and D1's edge kind (`play`, `touch`, `edit`,
   `unknown`): `edit` -> "edited outside the game"; otherwise `dt >= 360 s` -> "+X.Y h played";
   else, when no item survives the rules, "no progress (re-saved)" (dt = 0) or "+N min played,
   nothing indexed changed".
3. Pair contexts like for like (base with base, expedition with expedition); apply N8/N9 first.
4. Emit items per fact family (2.11.4), apply the noise rules (2.11.3), sort, and keep all items
   (the UI shows `top(3)` in lists and everything on the card).

#### 2.11.3 Noise rules (each found or confirmed on real data)

| # | Rule | Evidence |
|---|---|---|
| N1 | Compare with the drawn parent only, never with the previous version by time; no parent, no diff | R5 F7: by-time order made 8 bases "gone" then "new" again |
| N2 | Own bases only; `External*` never enter facts; orphan corvette layouts and `UITempShipBase` excluded | R5 F7, R6 2.3 |
| N3 | Base identity by type (freighter singleton, corvette by ship slot, others by address + position), then a second pass re-matches unmatched new/gone bases by (type, name, galaxy) | VERIFIED: without it nearly every step showed the freighter and corvette layouts as "gone" + "new"; the second pass removed an "Iaoyun Prime" gone/new flip |
| N4 | Ignore object `Timestamp` / `UserData` churn: compare part counts and the ObjectID histogram | VERIFIED: ~5,500 timestamps change per save |
| N5 | Fold per-base deltas of 1-2 parts into one line "minor edits in N bases (+-k parts)" (weight 15); report the 3 largest other deltas, then "N more bases changed" | VERIFIED: freighter +-1 every few saves = a portable `U_PARAGON` placed and picked up |
| N6 | Never diff the discovery store (capped at ~3,200 records, evicts fresh records); use the monotone `DISC_*` stats | R5 F7, R7 2.7 |
| N7 | A/B members written seconds apart: one header line, no items | R5 F7; VERIFIED 16 of 64 steps |
| N8 | Expedition replaced (season number differs, or expedition `TIME` fell while base `TIME` did not): one line "new expedition: X (replaces Y)", no field diff of that context | R7 2.7; VERIFIED (Swarm replaced Remnant) |
| N9 | Expedition-only save that became a normal save: "expedition converted into a normal save"; suppress the context-switch line | VERIFIED (2 steps) |
| N10 | Editor edits (edge kind `edit`): header says so; inventory digests carry the story; the inventory line gets weight 75 when `dt < 360 s`, 8 otherwise (every play session changes inventories) | VERIFIED vs M0's edit list |
| N11 | Currencies in one line, quicksilver first; units in compact form (`+2.6M`) | readability |
| N12 | Difficulty preset and death consequences in one line, weight 30 - the owner really toggles them (Custom/None <-> Normal/ItemGrave across consecutive Main Save versions) | VERIFIED |
| N13 | Ships by (seed, model) identity; renames and class changes are separate low-weight lines; several ships collapse to "N ships" | VERIFIED (ShipsToGive churn) |
| N14 | Inventory changes grouped per container kind with the net slot change ("7 ships (-32 slots)") | VERIFIED: one line per ship was unreadable |
| N15 | Never report the 8 stats that can decrease (2.6) | R7 2.7 |
| N16 | Top-N: weight, then magnitude; default 3 in lists | owner reads short text |

#### 2.11.4 Item catalogue and templates

All template text is ASCII (the `tests/test_ui_text.py` rule covers `src/`); names inside quotes
are data and may be Unicode (C22).

| Kind | Weight | Template |
|---|---|---|
| `milestone.complete` | 100 | "Artemis Path completed" / "{chain} completed" |
| `expedition.start` | 100 | "expedition started: {name}" |
| `expedition.replaced` | 100 | "new expedition: {name} (replaces {old})" |
| `expedition.converted` | 100 | "expedition converted into a normal save" |
| `expedition.complete` | 100 | "expedition {name} completed" |
| `milestone.regress` | 95 | "{chain} {a} -> {b} of {n}" (only across forks or edits) |
| `milestone.progress` | 90 | "{chain} {a} -> {b} of {n}" |
| `base.new` | 90 | "new base '{name}' ({parts} parts, {galaxy})" |
| `base.gone` | 88 | "base '{name}' gone ({parts} parts)" |
| `galaxy.first_base` | 85 | "first base in {galaxy}" |
| `ship.new` | 80 | "new ship '{name}' ({type}, {class})"; several: "{n} new ships ({types})" |
| `freighter.new` | 80 | "freighter acquired '{name}' ({class})" |
| `ship.gone` | 78 | "ship '{name}' gone ({type})" |
| `inventory` (edit, dt < 360 s) | 75 | "inventory changed: exosuit (+1 slot), 2 ships, storage" |
| `companion.new` | 70 | "new companion '{name}'" / "new companion ({type})" |
| `settlement.claimed` | 70 | "settlement claimed '{name}'" |
| `companion.gone` | 68 | "companion '{name}' gone" |
| `context.switch` | 60 | "now loads into the expedition" / "now loads into the base game" |
| `glyphs` | 60 | "glyphs {a} -> {b} of 16" |
| `legacy.placed` | 60 | "legacy parts placed {a} -> {b}" |
| `ship.class` | 55 | "ship '{name}' class {a} -> {b}" |
| `base.parts` | 50 | "base '{name}' parts {+d}" (freighter base: "freighter"; corvette: "corvette layout {slot}") |
| `base.more` | 49 | "{n} more bases changed" |
| `expedition.progress` | 45 | "expedition {name}: milestones {a} -> {b} of {n}" |
| `journey` | 40 | "journey rank {a} -> {b}" |
| `currencies` | 33 | "quicksilver +1,500; nanites +3,818; units +2.6M" |
| `difficulty` | 30 | "difficulty {a} -> {b} (death: {c} -> {d})" / "death consequences {c} -> {d}" |
| `frigates` | 25 | "frigates {a} -> {b}" |
| `words` | 22 | "words learnt +{d}" |
| `discoveries` | 21 | "discovered +34 systems, +3 planets, +14 creatures" |
| `rename` | 20 | "base '{a}' renamed '{b}'"; "companion '{a}' name cleared" |
| `base.minor` | 15 | "minor edits in {n} bases ({+-k} parts)" |
| `save.rename` | 15 | "renamed '{a}' -> '{b}'" |
| `galaxy.current` | 12 | "galaxy {a} -> {b}" |
| `inventory` (play) | 8 | as above |

In the expedition context items carry the prefix "[expedition] " unless the expedition is the
primary context.

#### 2.11.5 Examples on real versions

Prototype output (`D4WD\scripts\proto_changes.py` over the 64 "current version" steps of R7's
ground truth, `D4WD\changes-T1.txt`; items per step median 3, max 15). The prototype predates
rules N5, N10 weights, N13 grouping and N14; lines those rules change are marked `*`. Lines the
final rules fold away anyway (per-base deltas of 1-2 parts under N5, play-session inventory
lines under N10) and some low-weight tail lines are left out for brevity; the full output is in
`changes-T1.txt`. Headers about editor sessions come from D1's edge kind, which the prototype
did not have. One base name chosen by the owner is replaced by `<base>`.

```text
Main Save (S2), Steam slot 9, 0d12c1bd -> b540615b
  +2.7 h played
  new expedition: Swarm (replaces Remnant)
  new ship 'The Wraith' (The Wraith, S)
* base 'Duenos 100K Act. Indium Mine' parts +1          -> "minor edits in 1 base (+1 part)" (N5)
* inventory changed: exosuit (-4 slots), freighter (-7 slots), ship (-16 slots), storage, vehicles  (weight 8, N10)

Main Save (S2), 6536c83c -> c1016dff
  +3.3 h played
  new base '<base>' (259 parts, Euclid)
  new base 'Thgater-Rorev Prime' (228 parts, Eissentam)
  new base 'Iaoyun Prime' (180 parts, Euclid)
  nanites +36,082
  difficulty Custom -> Normal (death: None -> ItemGrave)

SHIPSTOGIVE-1400QS, Steam slot 11, fe6f0d76 -> 2edf513f
  +0.6 h played
  new base 'Hilbert Portal' (72 parts, Hilbert Dimension)
  glyphs 2 -> 16 of 16
  difficulty Custom -> Normal (death: None -> ItemGrave)
  discovered +2 systems, +1 planets
  galaxy Odyalutai -> Hilbert Dimension

Our Journey Continues, Steam slot 15, 2aed1bc0 -> cd7782b2
  +1.5 h played
  [expedition] new base 'Yolsham Colony' (10 parts, Euclid)
  expedition Our Journey Continues: milestones 8 -> 14 of 48
  [expedition] journey rank Juno -> Rosetta
  [expedition] quicksilver +400; units +2,628,509        * units "+2.6M" (N11)

Exp23 Our Journey Continues, f023dd15 -> 3a8bf6b0
  +4.7 h played
  expedition converted into a normal save
* now loads into the base game                          -> suppressed (N9)
  renamed '' -> 'Exp23 Our Journey Continues'

PhoenixesToGive-3200qs, 0134b812 -> e815ed36   (same play time and save time: an editor session)
  edited outside the game                               (header from D1's edge kind)
  inventory changed: exosuit (+1 slot), freighter, ship

Artemis-936qs, 53749374 -> 6732a752   (editor session)
* new ship 'Starborn Phoenix' (Starborn Phoenix, S)    -> "3 new ships (Starborn Phoenix, Utopia Speeder, The Wraith)" (N13)
* new ship 'Zooming Xenon' (Utopia Speeder, S)
* new ship 'The Wraith' (The Wraith, S)

ShipsToGive-2400qs, 92080c74 -> e1fac19c
  +1.1 h played
  new ship 'Herald2Give' (Boundary Herald, S)
* new ship '(unnamed)' (Explorer, C) / new ship '(unnamed)' (Shuttle, B)
  ship 'Runner2Give' gone (Starborn Runner)
* inventory changed: exosuit (+23 slots), ship (-12 slots), ship (+23 slots), ...  -> "exosuit (+23 slots), 7 ships (-32 slots)" (N14)

Creative Primary, Steam slot 13, de233200 -> e414de0e
  +1.0 h played
  new base 'Tophes-Uga Junction' (193 parts, Euclid)
  nanites +5,042; units +18,478,919                      * units "+18.5M" (N11)
  discovered +1 systems, +31 creatures

Egg Gifting, Steam slot 14, 2019b7e3 -> c2096dde
  no progress (re-saved)

Main Save (S2), 2e228119 -> f1220e3d
  +4 min played, nothing indexed changed
```

### 2.12 Public API

```python
class IndexService:
    def __init__(self, *, cache_path: Path, vault_id: str, locator: VersionLocator,
                 policy: ExecutionPolicy,
                 size_hint: Callable[[str], int | None] | None = None,   # D1: meta SizeDecompressed
                 game_running: Callable[[], bool | None] = safety.is_game_running,
                 clock: Callable[[], float] = time.time) -> None: ...

    # D1 FactsProvider (2.6; all non-blocking: cache reads or queueing only)
    def view(self, vid: str) -> FactsView | None: ...
    def changes(self, child: str, parent: str, *,
                edge_kind: str | None = None) -> ChangeSet | None: ...
    def is_milestone(self, child: str, parent: str) -> bool | None: ...
    def request(self, vids: Iterable[str], priority: Priority) -> int: ...   # newly queued

    # facts and cards (UI, CLI)
    def views(self, vids: Iterable[str]) -> dict[str, FactsView]: ...
    def facts(self, vid: str) -> VersionFacts | None: ...
    def card(self, vid: str) -> FactsCard | None: ...
    def status(self, vid: str) -> Literal["not-indexed", "queued", "running", "ok", "partial",
                                          "corrupt", "unsupported", "unreadable", "stale"]: ...

    # scheduling
    def request_deep(self, vid: str) -> None: ...
    def deep(self, vid: str) -> DeepFacts | None: ...                        # None until ready
    def rebuild(self) -> None: ...

    # control and progress
    def progress(self) -> IndexProgress: ...
    def events_since(self, seq: int, limit: int = 500) -> list[IndexEvent]: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def cancel(self) -> None: ...      # terminate in-flight jobs now; the queue is kept
    def close(self) -> None: ...       # terminate the pool, drain the writer, close connections
```

CLI (`cli.py`, read-only towards the vault, D1 decision 7):

```text
nmsvault index status                       counts by status, stale rows, cache size, policy
nmsvault index build [--workers N] [--rebuild] [--live-only]   foreground, progress lines
nmsvault index show <ref> [--json] [--card]                    facts of a version (ref via D1 ReadModel.resolve)
nmsvault index changes <child-ref> [<parent-ref>]              parent defaults to D1's drawn parent;
                                                               none -> prints "no recorded parent"
```

An explicit `<parent-ref>` is a comparison the owner asked for (like D6's compare view), not a
parent; the output header names both versions.

### 2.13 Error handling and safety invariants

| Situation | Handling | Visible as |
|---|---|---|
| `open(vid)` fails (`VersionUnavailable`) | failure row, retried next session, 3 strikes | status `unreadable` |
| bytes do not hash to vid | `mismatch`, failure row, not cached | `unreadable` until the next scan |
| live file torn while the game writes | caught by `open()` verification or the worker hash | as above; LIVE re-requested after the next journal change |
| corrupt container / JSON | cached with status `corrupt` and the error | "facts unavailable: damaged data" |
| HGSAVEV2, too large | cached `unsupported` | "facts unavailable: format not supported" |
| extractor exception | `partial`, error recorded | facts shown, card lists the error |
| `MemoryError` in a worker | `error`, retried once alone | `error` after the retry |
| worker hang | watchdog terminates the pool, job retried once alone | `error: timeout` |
| worker crash | pool replaces it; the job is re-queued once | as above |
| cache damaged / disk full / read-only | 2.9.5 | one status line |

Invariants (each has a test, section 5):

- **I1 Read-only.** No index code opens a save, vault or live path for writing; workers open no
  files; the only files written are the cache database files under the per-machine cache dir,
  which must not resolve inside the vault or a save root.
- **I2 Content addressing.** Facts are stored under the sha256 of the exact bytes decoded,
  computed in the worker.
- **I3 Derived only (C8).** The cache holds no user-authored data; deleting it loses nothing.
- **I4 Privacy.** No platform id, username or ownership block is read or persisted; only the
  platform code `PTK` (2.4.5).
- **I5 Kill-safe.** Workers can be terminated at any moment; the parent commits one transaction
  per result.
- **I6 No decoding on a UI thread (C31).** Every UI-facing method reads the cache only.
- **I7 Bounded memory.** Size caps before allocation (2.2); the dispatcher's heap budget.
- **I8 Advisory.** Operations never wait for or depend on the index; missing facts degrade to
  meta-level display.

### 2.14 Performance budget

| Item | Budget | Measured / basis |
|---|---|---|
| Decode of all 195 versions, one core | <= 25 s | 20.5-22.0 s VERIFIED (today 68.3 s) |
| Full first index, 4 workers, this PC | <= 15 s | 11.8-12.8 s VERIFIED |
| Full first index, typical PC | <= 60 s | INFERRED 25-60 s |
| One new version | <= 0.6 s here, <= 2 s typical | max 0.56 s VERIFIED |
| Extraction per version (parsed tree) | <= 20 ms | max 14 ms VERIFIED |
| `view()` / `facts()` on a UI thread | <= 1 ms warm | sqlite + JSON parse of <= 26 KB (INFERRED from R5's 8.6 ms for all 195) |
| `changes()` | <= 1 ms | 0.24 ms VERIFIED |
| Deep decode for a card | <= 1 s here, <= 2 s typical | decode + parse 0.56 s max VERIFIED |
| Worker memory | <= 300 MB each | R5 VERIFIED 270 MB process peak for 18 MB JSON |
| Cache size | ~14 KB per version | 3.24 MB / 195 VERIFIED, minus the dropped lineage table (2.9.6) |
| UI event-loop lateness while indexing (process mode) | idle-level (p99 < 15 ms) | R5 F5.1 VERIFIED (process pool p99 11.2 ms) |
| Cancel | < 50 ms | `terminate()` 18 ms in the kit, R5 VERIFIED |

---

## 3. Options (owner-facing choices in D4's scope)

Decided: the owner accepted every default below (01-consolidation 4a, "Index display"). The
implementation builds only the decided option; the others stay listed for the record.

| # | Question | Options | Decision |
|---|---|---|---|
| O1 | "Legacy base parts" (R6 Q1) | (a) parts from LEGACY STRUCTURES / LEGACY FREIGHTER MODULES placed in own bases; (b) (a) plus "still buildable" from known products, as two badges; (c) old bases (`OriginalBaseVersion` < 8) | **(b)** - C13; same cost as (a); (c) is a different concept, available on the card as "base created in an older format" |
| O2 | Base-part scope (R6 Q2) | (a) planet bases only; (b) all own bases (planet, freighter, corvettes with a live ship, stations, outposts); (c) per-base table | **(b) for the column + (c) on the card**; the card also shows the planet-only total |
| O3 | Ships (R6 Q3) | (a) fleet incl. corvettes; (b) fleet + archive; (c) separate counts | **(c) in one cell**: "12 (3 corvettes) +5 archived" |
| O4 | Companion names (R6 Q4) | (a) custom names only; (b) custom name, else creature type; (c) (b) plus eggs | **(b)**, with "+8 eggs" on the card |
| O5 | Which context represents a save (R6 Q5) | (a) the context the game loads; (b) base, expedition as a badge with progress; (c) two rows | **(b)** - C9; expedition-only saves use the expedition |
| O6 | Galaxy names (R6 Q6) | (a) embed the 256-name list; (b) game-data names 1-10, "Galaxy N" beyond; (c) port the name generator | **(a)** - C12; (c) is much more code for no extra benefit here |
| O7 | Where game tables come from, and licensing (R6 Q7) | (a) small curated constants with English names, generated by a dev tool and reviewed; (b) ids only, raw fallbacks everywhere ("Galaxy 10", "Expedition #22", "BIGGS"); (c) read the installed game data at runtime (needs a pak/MBIN reader; not stdlib-friendly) | **(a)**; part and item names (deep view) stay ids |
| O8 | Milestone presentation (R6 Q8) | (a) chips (Story 30/30, Atlas 11/11, Sentinel, Autophage, Exp n/m); (b) full checklist per chain; (c) journey rank only | **(a) in lists + (b) on the card** |
| O9 | Index depth (R6 Q9, C10) | (a) curated catalog only; (b) + per-base part histograms and inventories in the cache; (c) de-obfuscated copy per version | **(a) + on-demand deep decode** (C10); (b) costs ~100 KB per version for little gain; (c) up to 40 MB each |
| O10 | Account-level facts (titles, unlocked specials) | (a) not in v1; (b) a per-account card from `accountdata.hg` | **(a)** now, (b) later if wanted |
| O11 | Indexing while the game runs | (a) full speed; (b) one worker, live and new versions only; (c) pause until the game exits | **(b)** - live facts stay fresh without taking cores from the game |
| O12 | CPU use / worker count | `auto` (2-4 workers: cores - 1, capped at 4; 1 on a 1-2 core PC), or a fixed 1-8, or off | **auto**, exposed as a setting |
| O13 | Raw JSON viewer (R5 Q1-B) | (a) none; (b) bundle MBINCompiler's mapping (LGPL-3.0, 61 KB) for a viewer later | **(a)** in this phase |

R5 section 4 questions and where they were settled: Q1 key names -> C7 (HashName, no mapping
file); Q2 decoder -> P8 slice copy (2.2); Q3 execution -> C11 spawn pool, thread mode only in
POSIX kits (2.8); Q4 location -> per-machine cache (section 6); Q5 format -> sqlite (transactions,
incremental writes, 3 MB; JSON sidecars were slower on first read under antivirus, R5 F6); Q6
depth -> C10 (O9); Q7 when -> 2.10; Q8 changelog -> C14 (2.11); Q9 HGSAVEV2 -> explicit
"unsupported" (2.2).

---

## 4. Migration and compatibility

### 4.1 First index of the owner's vault

No migration: the cache does not exist yet. On the first start of a Phase 3 build the service
creates `index-v1.sqlite`, then the D1 scan (~1 s) feeds requests (2.10.2). Expected: live slots
show facts within ~5-10 s, everything within ~15 s on this PC (~0.5-1 min typical). The window
stays responsive throughout (process mode). Meta-level columns work before any fact exists.

### 4.2 Re-indexing on extractor upgrades

An app update that changes extractor output bumps `EXTRACTOR_VERSION`. Old rows remain visible
(marked stale) and are refreshed at REFRESH priority, largest first; the owner sees a short
"refreshing content index" progress line. A decoder-only change (`DECODER_VERSION`) retries only
failed rows. Lookup-table updates need nothing.

### 4.3 Game updates

| What changes | Effect | Handling |
|---|---|---|
| New fields | ignored until an extractor reads them | add readable names to the pins (2.3.3), bump `EXTRACTOR_VERSION` |
| Renamed or removed fields | the fact becomes `None` (never an exception) | per-extractor "expected field missing" counter in `index status`; pins catch it at development time |
| New mission steps of known chains | captured by prefix; final stage unknown -> "started" | regenerate `missions.py` |
| New chains, expeditions, reward ships, creatures | raw ids shown ("Expedition #24", model stem) | regenerate lookups; no re-index |
| New own base type | counted in `unknown_types`, not own | extractor update |
| `MissionVersion` beyond the tables | last change point used (R6) | regenerate |
| New container format | `UnsupportedContainer` / `corrupt` status, never a crash | decoder update + `DECODER_VERSION` |
| Layout change (contexts renamed) | `partial` with "unknown layout" | extractor update |

### 4.4 Portability (D11) and machines

The cache is per machine by design. A vault opened from a USB drive or NAS on another computer
gets a fresh local cache there (one first index). Notes, labels and lineage are in the vault
store (D1), never in the cache, so nothing user-authored is lost or diverges.

### 4.5 App upgrades

The cache file name carries its layout's major version, so an upgrade never misreads an older
file (2.9.4). The decoder change is compatible with every existing caller (2.2.2).
`DESIGN.md:118` and `msstore.py:22-24` are corrected in the same commit as the HGSAVEV2 change.

### 4.6 POSIX

Format code is platform-neutral (DESIGN.md). Execution starts in thread mode in POSIX kits and
switches to processes once the ladder check (spawn under `packaging/build_posix_kit.py`'s
launcher, T2/T3) passes; source checkouts use processes from day one.

---

## 5. Test plan (risk-based)

Highest risk first. All fixtures are synthetic; no real save data is committed. Real saves are
touched only by opt-in, read-only tests.

### 5.1 Decoder (`tests/test_lz4_block.py`, extended; `tests/lz4_testcodec.py` new helper)

- `lz4_testcodec.py`: a stdlib-only greedy LZ4 block encoder (4-byte hash table, minimum match 4,
  last 5 bytes as literals, offsets up to 65,535) plus a raw sequence builder
  `seq(literals, offset, match_len)` for hand-made edge cases.
- Differential: the current per-byte decoder is copied verbatim into the test module as
  `_reference_block`; for generated blocks (random, repetitive, JSON-like, all-same-byte)
  `decompress_block(b, n) == _reference_block(b, n)`.
- Round trip: `decompress_block(encode(x), len(x), exact=True) == x` for seeded random inputs
  covering literal lengths 0, 14, 15, 270, 300, 70,000 and match lengths 4, 18, 19, 274, 500.
- Overlap: offset 1 (run-length), offset 2-4 with long matches, offset == match length.
- Errors (each a `LZ4FormatError`, never `IndexError`): offset 0; offset > produced bytes (new:
  today silently wrong); literal past the end; truncated length extensions; truncated offset;
  output overrun via literals and via matches; chunk shorter than its header (`exact`).
- Mutation: for a valid block, every truncation point and 200 seeded single-byte flips yield
  either the original bytes or `LZ4FormatError`; output length never exceeds the declared size.
- Container: chunk claiming 0x80001 and a total over `max_total` fail before allocation (assert
  via a tiny `max_total` and a patched-size header, no large allocation); HGSAVEV2 header ->
  `UnsupportedContainer` (fixture shaped like `R5WD\robust\hgsavev2_synth.bin`, rebuilt in the
  test); trailing stale bytes still ignored; existing tests unchanged.
- Optional: differential against the `lz4` package when installed (`pytest.importorskip`).
- Opt-in real data (`NMSVAULT_REAL_DATA=1`, skips otherwise): every present live member is
  decoded by the old and the new decoder; sha256 of the outputs must match; files opened `rb`.

### 5.2 HashName (`tests/test_hashname.py`, `tests/data/hashname_pins.json`)

Pinned pairs; alphabet and length property; `ValueError` at 192 bytes; the AST name-coverage
test (2.3.3); distinct keys for every name the extractors use.

### 5.3 Extractors (`tests/index/`)

- `savebuilder.py`: builds a save from a readable-name dict (keys hashed with `hash_name`),
  serialises it, appends a NUL and wraps it in a chunked container of literal-only LZ4 blocks
  (valid LZ4), or an unframed block with a size hint.
- Golden fixtures (expected `VersionFacts` JSON beside each): base only; base + expedition with
  a running season (milestone targets embedded); expedition only; pre-context layout; legacy
  parts placed and known; orphan corvette layout; `External*` and `UITempShipBase` bases; two
  bases on one planet; `Units = -1`; addresses as strings and ints; markup in names; an invalid
  UTF-8 byte inside a product id; settlement claimed; inventories.
- Degradation: fault injection (a registered extractor that raises) -> status `partial`, its
  fields absent, all others identical to the golden output.
- Normalisers: unit tests per helper.

### 5.4 Privacy (`tests/index/test_privacy.py`)

A fixture whose `Owner`, `OWS`, `SenderData`, `UsedDiscoveryOwnersV2`, `LastEditedBy*` and
`PlatformToken` carry sentinel values (`"SENTINEL-UID-000000001"`, `"SENTINEL_USERNAME"`,
`"0xDEAD..."`): the serialised facts, errors and cache file bytes contain none of them;
`get(..., "Owner")` and `get(..., "UsedDiscoveryOwnersV2")` raise `PrivacyError`; the bases'
`ptk` fact holds only the platform codes; an AST scan of `core/index/` finds no reader of a
denied name outside `privacy.owner_platform`.

### 5.5 Cache (`tests/index/test_cache.py`)

Create / reopen; one-transaction-per-result (a simulated crash between results leaves no partial
rows); stale extractor rows served with `stale` and re-queued; decoder bump re-queues only
failures; corrupt file (garbage bytes in a temp file) -> renamed and rebuilt; refusal to place the
cache inside a vault or save root; reads during writes (WAL, two threads); GC by `last_seen`.

### 5.6 Execution (`tests/index/test_dispatcher.py`, `test_pool.py`)

- Dispatcher logic with a fake executor and fake clock (pure): tier order, newest-first vs
  largest-first, oversized runs alone, reserved interactive slot, memory budget, watchdog
  timeout path, pause / resume / cancel, game-running throttling.
- Real spawn pool (2 workers, synthetic saves): results arrive, `terminate()` mid-run leaves no
  rows for in-flight jobs, a hash mismatch is not cached, thread mode gives identical facts.
- Import safety: importing `nms_save_vault.gui` and `nms_save_vault.cli` creates no Tk root and
  starts nothing (spawn re-import).

### 5.7 Derivation and changes (`tests/index/test_derive.py`, `test_changes.py`)

Lookup fallbacks (unknown galaxy, expedition, model, mission); final-stage change points;
read-time chains reproduce the R6 example values on a synthetic save built to match; one test
per noise rule N1-N16 with a minimal fact pair; header rules per edge kind (`play`, `touch`,
`edit`, `unknown`); `is_milestone`; ASCII-only templates (covered by `tests/test_ui_text.py`
for literals).

### 5.8 Service contract

- `IndexService` satisfies D1's `FactsProvider` protocol: a conformance test calls the four
  methods with D1's argument types and checks the return types of 2.6 (`changes` and
  `is_milestone` return `None` while a version is unindexed).
- `facts:<name>` sorting: every numeric `FactsView` field appears in `numbers` under its name.
- The API never decodes on the calling thread (a locator spy proves `open()` is called only from
  the dispatcher thread); `events_since` sequence semantics; `view()` on an unindexed vid
  returns `None` and queues nothing unless asked.
- A read-model build (D1's facade with a real `IndexService`) queues nothing at INTERACTIVE.

### 5.9 Not in CI

`tools/bench_index.py` reproduces 2.14 on a developer's copies; kit checks (Windows kit spawn
smoke = T1 in CI build; POSIX kit spawn = T2/T3) follow DESIGN.md's ladder.

---

## 6. Implementation plan

Sizes: S < 150 lines including tests, M 150-500, L > 500. Every step is one commit with its tests
(project rule: done means committed).

| # | Step | Size | Phase |
|---|---|---|---|
| 1 | `lz4_block` hardening: slice copies, bounds checks, `exact` chunks, caps, `LZ4FormatError`; `tests/lz4_testcodec.py`; decoder tests 5.1 | M | 0 |
| 2 | HGSAVEV2 sniffing -> `UnsupportedContainer`; move `HGSAVEV2_HEADER`; fix `msstore.py:22-24` and `DESIGN.md:118` | S | 0 |
| 3 | `core/savejson.py` (`decode_save`) + tests; `tools/nms_visit_history.py` switches to it (lossless text) | S | 0 |
| 4 | Opt-in read-only real-data decoder equivalence test | S | 0 |
| 5 | `core/hashname.py` + pins + AST name-coverage test | S | 3 |
| 6 | `core/index/nodes.py` + `privacy.py` (deny list, `owner_platform`) + tests | S | 3 |
| 7 | `schema.py` (TypedDicts, `validate`) + `tests/index/savebuilder.py` | M | 3 |
| 8 | Extractors: save basics, season, discoveries; context basics, difficulty, words, products, stats | M | 3 |
| 9 | Extractors: bases (identity, legacy, layout, ptk), ships, companions, fleet, settlements, exocraft | M | 3 |
| 10 | Extractors: missions, inventories; golden fixtures complete | M | 3 |
| 11 | Lookups package + `tools/gen_lookups.py` + `derive.py` (`FactsView`, `FactsCard`, chips, numbers) | M | 3 |
| 12 | `worker.py` (`run_job`) + `cache.py` (schema, recovery, reads, writer) | M | 3 |
| 13 | `pool.py` (policy, dispatcher, process and thread executors, budget, watchdog) + `platform.index_execution_policy` | L | 3 |
| 14 | `service.py` (`IndexService`, events, D1 `FactsProvider` conformance 2.6) | M | 3 |
| 15 | `changes.py` (rules N1-N16, templates, `is_milestone`) | M | 3 |
| 16 | CLI `nmsvault index ...` + `tools/bench_index.py`; README facts (test count, capability) | S | 3 |
| 17 | UI hook: the Tk poller (D6) consumes `progress` / `events_since` | S (D4 part) | 3 |
| 18 | Kit checks: Windows kit spawn smoke; POSIX kit spawn on the ladder, then flip `SPAWN_VERIFIED_POSIX_KIT` | S | 3 |
| 19 | Deep decode on demand (`request_deep` / `deep`) + card integration | M | 3b |

Steps 1-4 are independent of everything else and fix a live robustness risk (4 GiB allocation on
a corrupt file); they can ship in any Phase 0 release.

---

## 7. Risks and open questions

| # | Risk / question | Mitigation or owner of the answer |
|---|---|---|
| 1 | Chain membership (which mission ids form a storyline) is INFERRED from ids and table names; the Titan chain was never started in any sample | raw progress is stored, so membership can be corrected in `_overrides.py` without a re-index; verify once against the in-game log |
| 2 | Journey rank = `JM // 5` is INFERRED (`JM_BANKED` differs) | check once against the in-game screen; read-time, so a fix needs no re-index |
| 3 | Base identity is heuristic: a base moved or rebuilt reads as "gone" + "new" | second-pass re-match by (type, name, galaxy); acceptable residual |
| 4 | Game updates rename fields -> facts silently `None` | pins + coverage test at development time; per-extractor missing-field counters at run time |
| 5 | Spawn in POSIX kits unverified | thread mode there until the ladder check passes |
| 6 | Memory on low-RAM PCs (~270 MB per worker for the biggest save) | heap budget in the dispatcher; worker setting |
| 7 | CPU competition with the game | O11 default: one worker, live/new only, while the game runs |
| 8 | Licensing of game names | owner decided O7 (a); ids-only fallback exists |
| 9 | Task Manager shows extra NMSSaveVault.exe processes while indexing (spawned workers use the kit's renamed `pythonw.exe`) | cosmetic; idle pool closes after 60 s |
| 10 | Entry modules must stay import-safe for spawn | test 5.6 |
| 11 | Expedition milestones marked `IsOptional`: does the game's "n of m" count them? | open; the card shows both counts until verified in game |
| 12 | The facts schema will grow | `schema` + `EXTRACTOR_VERSION` in every row; tolerant readers; derive.py treats missing keys as `None` |
| 13 | Existing versions get a parent only from D1's oplog and "seen in backups" links; a version without one shows no "what changed" | by design (owner Q3, Q5); new versions get recorded or observed parents from phase 1 on |

---

## Deviations from 01-consolidation section 6

1. **API surface.** Section 6 lists `facts(vid)`, `request(vids, priority)` and
   `changes(child_vid, parent_vid) -> list[str]`. D4 keeps all three and adds read-time views
   (`view`, `views`, `card`), `is_milestone` (D1's key-version rule), `request_deep` / `deep`,
   and the progress contract (`progress`, `events_since`, `pause` / `resume` / `cancel` /
   `close`). `changes()` returns a `ChangeSet | None` whose `lines()` is the section's
   `list[str]`; it accepts D1's edge kind for the header. `view`, `changes`, `is_milestone` and
   `request` are D1's `FactsProvider` with D1's exact names and types (2.6).
2. **Raw facts, derived views.** `facts(vid)` returns raw ids and counts; names and milestone
   completion come from `view()` / `card()` at read time, so lookup updates never re-index.
3. **Cache location.** Keyed by `vault_id` as specified, inside D1's shared per-machine folder
   `<user_cache_dir>/vaults/<vault_id>/index-v1.sqlite`; rows are content-addressed by `vid`.
4. **How bytes reach workers.** D4 consumes `VersionLocator.open(vid)` (as section 6 says) on the
   parent's dispatcher thread and sends the bytes to workers, because D1's `Location` carries no
   path and the storage layer requires reads through `open()`; workers re-hash the bytes.
5. **C11 "POSIX kits fall back to one worker"** is implemented as one in-process thread, because
   a single spawn worker would still depend on the unverified spawn path; POSIX source checkouts
   use processes.

---

## Appendix A - evidence (throwaway, `D4WD`, never in the repo)

| File | What it shows |
|---|---|
| `scripts\hardened_lz4.py` | research twin of the proposed decoder |
| `scripts\bench_hardened.py` | byte identity vs today's decoder and C `lz4` on all 195 versions; timings |
| `scripts\cmp_overhead.py` | bounds-check overhead vs R5's unchecked variant |
| `scripts\proto_extract.py` | facts-v1 prototype: HashName access, registry, contexts, inventories |
| `scripts\run_index.py` | spawn pool (largest first), parent-only sqlite writer; wall times, sizes |
| `scripts\proto_changes.py`, `changes-T1.txt` | "what changed" on R7's 64 ground-truth steps |
| `scripts\base_churn.py` | which base / object fields change every save |
| `core_counters.py`, `r7copy\` | the 31 never-decreasing counters (2.5.3) recomputed from R7's data (patched copies of R7 scripts) |
| `db\facts-w4.sqlite`, `db\facts-w1.sqlite` | prototype caches (195 versions) |

Commands (from `C:\Devel\NMSVault`, `PYTHONUTF8=1`):
`uv run --with lz4 python -I -X utf8 D4WD\scripts\bench_hardened.py all 0` (the `lz4` package is
research-only); `uv run python -I -X utf8 D4WD\scripts\run_index.py 4 D4WD\db\facts-w4.sqlite`;
`uv run python -I -X utf8 D4WD\scripts\proto_changes.py D4WD\db\facts-w4.sqlite 8`.

Inputs reused: R5 corpus (`R5WD\corpus`, 195 sha256-verified copies), R5 `spooky.py` and
`bench.py` (copied to `D4WD\r5copy`), R6 lookups and decoded samples (`R6\lookups`,
`R6\saves\*\deob.json`), R7 features and inventory (read through patched copies).
