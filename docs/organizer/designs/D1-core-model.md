# D1 - Core data model of the save-file organizer

Design wave, core design D1 (subjects S1 identity and versions, S3 lineage, S4 notes; decisions
C1-C6, C15-C22, C27-C28). Branch `design/save-organizer`. Binding inputs:
`docs/organizer/01-consolidation.md` (C1-C32, section 4a owner decisions and review answers,
section 6 seams), `03-design-review.md` and research R1-R8. Nothing under `src/`, `tests/` or
`packaging/` was changed; the prototypes in Appendix A ran on copies.

Evidence tags: **VERIFIED** = measured by D1 (script named in Appendix A); **SOURCED** = read in
code (`file:line`) or a research report; **INFERRED** = reasoning. Redaction: `st_<steamid64>`,
`<xbox-account>`, `%APPDATA%`, `%LOCALAPPDATA%`. Playthrough ids are shown as 8 hex digits,
version ids as 8 hex digits.

## 0. Owner summary

1. One list of every save file you have (30 today), each with its versions, where it is live (S9, X8), play time, last played and your note; built in about 3 s the first time, under 0.3 s after.
2. From the first release the app keeps a journal of what every live slot holds, so moves, game sessions and cloud reverts become history. Your existing backups are linked once where two app-made backups of the same live slot follow each other ("seen in backups"; tested on copies of your data: 135 links, Main Save's real branch point found). Nothing is guessed: a version with no recorded link is listed by date.
3. Notes: one living note per save file with its edit history (each new version remembers the note as it was), plus a label, comment and lock per version, and a pin per save file.
4. A save live on Steam and Xbox shows each copy as its own tip: "Xbox 2h35 behind" when one lags, "two branches" when the journal saw both progress.
5. History looks as you chose (F2 = C): the version table with a narrow branch column and a "key versions / all" toggle.

## 1. Scope, non-goals, dependencies

**In scope.** Identity (`pid`), versions (`vid`), captures and pairs; the organizer store in
`<vault>/organizer/` (files, schemas, lock, schema versions, `vault_id`); the scanner and its
per-machine cache; the live journal (observation, edges, events, binding rule C16); lineage
assembly (tiers, owner corrections, Combine/Split); notes (C19-C22); the read model the Tk UI
(D6) and the CLI render (lines, versions, live board, history graph with lanes); the abstract
`VersionLocator` and the composite `VaultLocator`; the one-time bootstrap of an existing vault;
read-only CLI exposure; phase mapping.

**Non-goals.** Storage, `preserve`, the store listing and the vault copy-aside (D3); content
extraction, facts and the index cache (D4); live writes, plans, undo and the verification UI
(D5); widgets and mockups (D6). Guessed history: the owner decided that no link is ever inferred
(01 4a); there is no classifier. D1 never writes save data, live folders, entry folders, store
entries, `catalog.json` or `oplog.jsonl`.

**Dependencies.**

| Needs | From | Phase |
|---|---|---|
| **P1 fixed**: Xbox meta id u64 @0x118 and save time u32 @0x120 read when the meta is >= 296 B and u32 @0x124 is 2003 or 2004; names and difficulty on 296-B metas; Xbox "newest member" by meta time. Without it Xbox `pid` costs a data decode and the board shows the wrong loaded member in 5 of the 14 live Xbox slots that carry a meta time (R7 2.3). | Phase 0 | 0 |
| **P2 fixed**: snapshot pruning removed, so no catalog snapshot is ever deleted (keep everything, F9) | Phase 0 | 0 |
| `VersionLocator` read side over catalog folders (`locate`, `open`, `is_durable`) | D2's `FolderLocator` read side, kept as the legacy reader until fold | 1 |
| `VersionLocator.preserve` (`StoreLocator`), the entry listing `list_vault_entries` + store manifests, the vault copy-aside and fold | D3 (phase-2 upgrade) | 2 |
| `record_op` calls, `busy_targets`, the live-op lock, verification UI | D5 | 2 |
| `FactsProvider` = `view`, `changes`, `is_milestone`, `request` of D4's `IndexService` | D4 | 3 |
| Read-model consumers | D6, CLI | 1+ |
| New primitives `platform.user_cache_dir(host)`, `safety.exclusive_lock(path)` | this design (2.3, 2.4) | 1 |

## 2. Design

### 2.1 Module layout

```
src/nms_save_vault/core/organizer/
  __init__.py   Organizer facade: open, refresh, model, notes, corrections, bootstrap
  model.py      identifiers and value types (pure; JSON codecs)
  store.py      OrganizerStore: <vault>/organizer/ files, header, schema, lock, atomic IO
  cache.py      ScanCache: per-machine hash cache + entry capture cache
  scanner.py    scan(): captures + version registry from live sources, catalog and store entries
  journal.py    Journal: observe() appends records; replay() derives edges + events
  recorder.py   LineageRecorder: the D5-facing API (record_op, observe, expected_pair, op_outcome)
  lineage.py    LineageGraph: tier precedence, corrections, Combine/Split, effective pid
  notes.py      save-file notes with history, version label/comment/lock, pins
  readmodel.py  Line, VersionRow, LiveBoard, HistoryGraph, sorting, filters, lanes
  locator.py    VersionLocator ABC, VaultLocator, Location, OpenedVersion, LiveMember
  bootstrap.py  one-time pass over an existing vault + report
  folders.py    FolderLocator: read side over catalog folders (D2), used until fold
```

The package is platform-neutral (no OS branches). It uses `savedir`, `msstore`, `catalog`,
`store` (D3, read API only), `safety`, `aliases`, `lz4_block` and `playtime._id_from_bytes`;
`model.py` and `locator.py` import nothing from `store`, so D3 can import them. Core modules do
not import the package,
except the small phase-1 recording hook in `operations` (2.6.7). The two new OS-specific
primitives live in modules allowed to branch: `platform.user_cache_dir` and
`safety.exclusive_lock`.

### 2.2 Identifiers and value types

**pid** (playthrough id, one line per pid, C1):

| Case | `pid` | `pid_src` |
|---|---|---|
| Steam meta u64 @0x15C != 0 (`MetaInfo.slot_identifier`) | `f"{id:016X}"` | `meta` |
| Xbox meta (P1 fixed): length >= 296, u32 @0x124 in {2003, 2004}, u64 @0x118 != 0 | `f"{id:016X}"` | `meta` |
| meta id 0, first data chunk holds `WmU`/`ActiveSaveSlotId` (`playtime._id_from_bytes`, ~23 ms, cached per vid) | `f"{id:016X}"` | `data` |
| no id anywhere, meta readable | `legacy:<platform>:<slot>:<name>` (ends in `:` when the save has no name) | `legacy` |
| meta unreadable and no data id | `unknown:<vid[:16]>` | `unknown` |
| owner Split (2.7.4) | `split:<raw pid>:<n>` | `owner` |

- A vid's pid is fixed when the vid is first registered (its earliest observation). Later
  captures of the same bytes in another slot reuse it, so a legacy save keeps its line after a
  move. Two legacy pids are *compatible* (2.6.3) when platform and name match; a child adopts
  its parent's pid.
- Consistency (phase 3): when D4's `FactsView.pid_in_data` for a vid differs from a
  meta-derived pid, the registry uses the data id and the scan reports an `id-mismatch` problem
  (none exist in the owner's data: Steam 997/997, Xbox 224/224 equal; R7 2.2, D5 meta survey).
- Voyagers (pre-Worlds Xbox, u32 @0x124 = 0) has a non-zero u64 @0x118 that is not an id; the
  format gate above keeps it on the legacy key `legacy:xbox:13:Voyagers` (SOURCED R1 B6, R7 2.2).

**vid** = lower-case hex sha256 of one member's data file exactly as stored (Steam `saveN.hg`,
Xbox data blob). Verbatim copies keep it; no Steam file in the vault has trailing bytes, so the
full-file hash equals the valid-region hash (R7 2.1).

```python
Pid = str
Vid = str
Platform = Literal["steam", "xbox"]
Member = Literal["A", "B"]

@dataclass(frozen=True)
class LiveRef:          # a live account folder
    platform: Platform
    account_key: str    # the live folder's name: st_<steamid64> or <xbox-account>; never displayed raw
    instance: str = ""  # "" normally; sha1(resolved parent path)[:8] only when two live folders
                        # of one platform share a name on this machine (two Proton prefixes)

@dataclass(frozen=True)
class EntryRef:         # a catalog entry folder (any kind)
    entry_id: str

@dataclass(frozen=True)
class StoreRef:         # a D3 store entry; key = its entry id (a folded catalog entry keeps its id)
    key: str

SourceRef = LiveRef | EntryRef | StoreRef

@dataclass(frozen=True)
class LiveTarget:
    platform: Platform
    account_key: str
    slot: int           # 1..15
    instance: str = ""
    @property
    def ref(self) -> LiveRef: ...

@dataclass(frozen=True)
class MetaSnap:         # what the organizer keeps of a meta; enough for gone versions
    play_time: int      # seconds
    meta_ts: int        # UTC unix; Steam 0x164, Xbox 0x120 (P1); 0 when absent
    ts_src: Literal["meta", "index", "mtime"]   # fallback chain = MemberView.effective_timestamp
    name: str
    summary: str
    difficulty: int     # raw preset id (formats.DIFFICULTY_NAMES)
    game_mode: int      # 6 = Seasonal (expedition)
    season: int
    base_version: int
    meta_format: int
    size_decompressed: int
    data_size: int
    meta_sha: str       # sha256 hex of the meta plaintext; "" when unreadable. One vid can have
                        # several metas (a Place re-stamps the time, F5b; promote edits it)
    valid: bool         # the meta decoded with the right header (MemberView.valid)
    file_time: float    # data file mtime (Xbox: containers.index time) exactly as savedir reads
                        # it; the clock of the head rule when a member lacks a meta time

@dataclass(frozen=True)
class MemberObs:
    member: Member
    vid: Vid
    pid: Pid
    pid_src: str
    platform: Platform
    meta: MetaSnap
    xbox_sync: int | None = None    # containers.index sync state (Xbox live only)

@dataclass(frozen=True)
class Capture:          # one observation of one slot (section 6 "capture")
    where: SourceRef
    slot: int
    a: MemberObs | None
    b: MemberObs | None
    observed_at: int    # UTC unix
    time_quality: Literal["exact", "estimated"]
    src: Literal["scan", "entry", "op"]
    head_member: Member | None   # SlotView.newest when the slot was read, else head_of(a, b)
    @property
    def pair(self) -> "PairKey": ...
    @property
    def head(self) -> MemberObs | None: ...   # the member named by head_member

PairKey = tuple[Vid | None, Vid | None]     # (A, B); string form "<A or ->/<B or ->"
```

**Head rule** (the member the game loads, the label of a pair): one rule, `SlotView.newest`
(SOURCED `savedir.py`, phase 0). Candidates are the present members with a valid meta, else
every present member; one clock orders them all - the meta save time when every candidate has
one, else the file time (Xbox: the `containers.index` time); a tie picks A. Play time is not
used. The scanner records the result as `Capture.head_member`; `model.head_of(a, b)` applies
the same rule to data read back without its slot (store manifests, journal records).

**Order of versions** (used everywhere a "before/after" is needed):
`order(v) = (meta_ts, play_time, first_seen, vid)`, with `first_seen` an `ObsOrder`
`(at, seq)` (2.6.1). It is a strict total order.

```python
@dataclass(frozen=True)
class VersionInfo:      # registry entry, one per vid (derived; rebuilt every refresh)
    vid: Vid
    raw_pid: Pid
    pid_src: str
    platform: Platform
    meta: MetaSnap              # from the earliest observation
    meta_ts_last: int           # newest meta_ts seen (promote / re-stamp raise it)
    roles: frozenset[Member]    # seen as A, B or both (0 vids seen as both in the owner's data)
    first_seen: tuple[int, int] # ObsOrder
    locations: tuple["RawLocation", ...]   # where the bytes are now (scan); empty = gone

@dataclass(frozen=True)
class RawLocation:      # where the scanner saw a vid; durability per 2.11
    where: SourceRef
    slot: int
    member: Member
    entry_kind: Literal["live", "full", "snapshot", "extract", "imported", "inplace", "preserve"]
    managed: bool       # catalog entry flag; True for store entries; False for live and in-place
    seen_at: int        # capture time (UTC)

@dataclass(frozen=True)
class PairInfo:
    pair: PairKey
    head: Vid
    head_member: Member
    partner: Vid | None
    raw_pid: Pid
    first_seen: tuple[int, int]
    seen_in: tuple[tuple[SourceRef, int], ...]  # (where, slot) of every capture of this pair

EdgeTier = Literal["recorded", "observed", "owner"]
EdgeKind = Literal["play", "touch", "edit", "unknown"]
EdgeMethod = Literal["op", "oplog", "journal", "capture-sequence", "owner"]

@dataclass(frozen=True)
class Edge:             # game-state descent: child's state was produced from parent's state
    parent: Vid
    child: Vid
    tier: EdgeTier
    kind: EdgeKind
    method: EdgeMethod
    op_id: str | None   # the operation that put `parent` into the slot (binding), if any
    created: int        # UTC unix of the evidence (observation or op time; correction time)
    target: LiveTarget | None = None   # slot where the step was seen (journal / capture-sequence)

@dataclass(frozen=True)
class JournalEvent:
    kind: Literal["first-seen", "changed", "deleted", "partial", "reverted", "confirmed",
                  "unexpected", "resurrected", "returned", "replaced", "unstable"]
    target: LiveTarget
    at: int
    op_id: str | None
    before: PairKey | None
    after: PairKey | None
    vids: tuple[Vid, ...] = ()   # members the event is about
```

Edge **kind** is decided by the meta deltas from parent `p` to child `c`: `play` if
`c.play_time > p.play_time`; `touch` if equal play time and later `meta_ts` (the game re-saved
after a load, R7 2.4); `edit` if equal play time and equal `meta_ts` with different bytes
("edited outside the game", R7 2.6 row 6); `unknown` otherwise (owner corrections only), and
whenever `touch` / `edit` would compare times from different clocks (`ts_src`). Every
edge is evidence, never a guess: there is no inferred tier and no `confidence`. Place, swap,
restore-older and undo are **operation kinds** reached through `op_id`; deletions and cloud
reverts are **journal events**, not edges (Deviations, item 1). Hello Games cross-save copies a
save between platforms with different bytes; it produces no edge (2.10.1 compares the platforms'
live copies instead).

### 2.3 The organizer store `<vault>/organizer/`

| File | Owner | Holds | Write pattern | Size, owner's vault | Size at ~2,000 versions |
|---|---|---|---|---|---|
| `organizer.json` | D1 | header: schema, `vault_id`, created, app | atomic replace, once | < 1 KB | < 1 KB |
| `journal.jsonl` | D1 | live-slot observations and op bindings (2.6) | single fsynced append under the lock | 98 KiB after bootstrap (VERIFIED) | ~1.1 MiB, + ~0.4 MiB/year of play (INFERRED: ~600 changed slots/year x ~0.6 KB) |
| `notes.json` | D1 | save-file notes + history, version notes, locks, pins (2.9) | atomic replace | 0 | 100-300 KB |
| `lineage.json` | D1 | owner corrections, Combine aliases, Splits, bootstrap report (2.7) | atomic replace | ~3 KB | < 50 KB |
| `ops.jsonl` | D5 | operation intent/completion log | D5's design | - | - |
| `.lock` | D1 | store lock target, never rewritten | - | 0 B | 0 B |
| `.liveop.lock` | D5 | live-op lock target (D5 2.16); also taken by the phase-1 hook (2.6.7) | - | 0 B | 0 B |

D3's version store lives beside this folder in `<vault>/store/` and takes the store lock only to
commit an entry. Other designs may add files here; every file in this folder follows the
conventions below and is written only while holding the store lock.

**Conventions.**
- UTF-8, `json.dumps(..., ensure_ascii=True, separators=(",", ":"))` for JSONL and `indent=1`
  for JSON files; files are pure ASCII on disk, so no reader can mis-decode them (DESIGN.md
  encoding trap; notes stay Unicode through `\u` escapes, C22).
- Every JSON file starts with `{"schema": N, "kind": "nmsvault.organizer.<name>", ...}`. An
  older schema is migrated forward on the first write. A schema newer than the app opens the
  organizer read-only with a plain message ("This vault's organizer data was written by a newer
  version of the app"). Users never go back to older app versions (owner, 01 4a Q2), so there is
  no machinery for older readers.
- Atomic replace = `safety.atomic_write_bytes` (temp in the same folder, fsync, `os.replace`;
  SOURCED `safety.py:53-66`), after first copying the current file to `<name>.prev` (one
  generation, for manual recovery).
- A file that fails to parse is never overwritten: the store opens read-only and reports the
  file and the error.
- All times are UTC unix seconds (int). No absolute paths are stored anywhere (D11); entries are
  referenced by id, live folders by `LiveRef`.

**`organizer.json`**:

```json
{"schema": 1, "kind": "nmsvault.organizer",
 "vault_id": "9c1e0a52d3b84c0f9d6a1b2c3d4e5f60", "created": 1792137600,
 "app": "nms-save-vault 0.3.0"}
```

`vault_id` = `uuid.uuid4().hex`, written once when the store is created. It keys the
per-machine caches (2.4). A copied vault keeps the same id; the caches stay correct because their
entries are keyed by absolute path or by content hash.

**Store lock.** `<vault>/organizer/.lock`, held only for one read-modify-write transaction
(milliseconds), never for the life of the app. Holders: D1 transactions, D5's `ops.jsonl`
appends, D3's entry commit (manifest write and index update only, never the copying before it).

```python
# core/safety.py (new; OS branches allowed here)
class LockTimeout(OSError): ...

@contextmanager
def exclusive_lock(path: Path, *, timeout: float = 10.0, poll: float = 0.05) -> Iterator[None]:
    """Exclusive inter-process lock on `path` (created if absent, never deleted).
    Windows: msvcrt.locking(fd, LK_NBLCK, 1) on byte 0; POSIX: fcntl.flock(fd, LOCK_EX | LOCK_NB).
    Retries every `poll` s until `timeout`, then raises LockTimeout. Released by the OS when the
    process dies, so there are no stale locks. If the file system does not support locking
    (some NFS), yields with a warning recorded on the returned handle instead of failing."""
```

Rejected: an `O_EXCL` lock file with stale-lock stealing (two stealers can both win; a crashed
holder leaves a lock).

Rules for every holder:
- **Not re-entrant.** A process first takes a per-path `threading.Lock` (threads of one process
  queue there), then the OS lock. Acquiring the store lock on a thread that already holds it
  raises `RuntimeError("organizer store lock is not re-entrant")`, a programming error caught by
  the tests; callers never nest (D5 calls `preserve` and `record_op` outside its own appends).
- **Timeout per transaction.** `LockTimeout` (10 s) fails that one transaction with
  `StoreBusy(LockTimeout)`; the caller shows "Another NMS Save Vault window is saving - try
  again" and the store stays writable. It never switches the session to read-only.
- **Order.** The live-op lock is always taken before the store lock, never while holding it.

**Transactions.**

```python
class OrganizerStore:
    def __init__(self, vault_root: Path, *, create: bool, clock: Callable[[], float] = time.time): ...
    root: Path; vault_id: str | None; read_only: bool; problems: list[str]
    def transaction(self) -> ContextManager["StoreTx"]:
        """Acquire the lock, reload any file whose (st_mtime_ns, st_size) changed since it was
        last read, yield a mutable view; on normal exit write changed JSON files atomically and
        flush appended journal records (one write + flush + fsync per batch); release."""
    def journal_records(self) -> Sequence["JournalRecord"]: ...   # cached; incremental tail read
    def notes(self) -> "NotesState": ...
    def lineage(self) -> "LineageState": ...
```

Readers take no lock (atomic replace guarantees whole files). The journal reader ignores a final
line that has no newline or does not parse, and reports it; the next append first writes `"\n"`
when the file does not end with one, so a torn line never merges with a new record. The GUI and
CLI see each other's writes on their next refresh (file `(mtime_ns, size)` check).

**Read-only mode** (newer schema, unparseable file, read-only medium): everything is shown,
nothing is recorded, and the UI shows one line saying why.

### 2.4 Per-machine cache

```python
# core/platform.py (new; beside user_data_dir)
def user_cache_dir(host: Host | None = None) -> Path:
    """Windows %LOCALAPPDATA%\\NMSSaveVault\\cache; macOS ~/Library/Caches/NMSSaveVault;
    Linux $XDG_CACHE_HOME/NMSSaveVault when absolute, else ~/.cache/NMSSaveVault."""
```

`<user_cache_dir>/vaults/<vault_id>/` is shared: D1's `scan-cache.json`, D4's index database,
D5's `open-ops/` markers. A vault that has no store yet (never bootstrapped, or read-only) uses
`nostore-<sha256(resolved vault root)[:16]>` as the folder name.

`scan-cache.json` (derived; rebuildable; last writer wins; never in the vault):

```json
{"schema": 1, "vault_id": "...",
 "files":   {"<absolute data path>": [size, mtime_ns, "<vid>"]},
 "entries": {"<entry id>": {"fp": "<fingerprint>", "captures": [<Capture JSON>, ...]}},
 "ids":     {"<vid>": "<data pid hex or 0>"}}
```

- `files`: a data file whose `(st_size, st_mtime_ns)` still match is not re-hashed. Xbox blob
  names rotate on every write, so a changed Xbox save is a cache miss by construction.
- `entries`: entry fingerprint = sha256 of the sorted lines `relpath|size|mtime_ns` of the
  entry's save files (Steam `save*.hg`, `mf_save*.hg`; wgs `containers.index` and every file
  under the GUID folders). An unchanged fingerprint reuses the stored captures, skipping
  `scan_any` and hashing.
- `ids`: data-id lookups by vid (the id of given bytes never changes).
- Missing paths are dropped after a full scan. The CLI's `--no-cache` uses an in-memory cache.
- This replaces today's `<vault>/playthrough-ids.json`, which a read-like command writes into the
  vault and which is keyed by absolute path (SOURCED `playtime.py:122-163`, R1 C4); the
  organizer never reads or writes that file.

Size at the owner's vault: ~1,200 file entries + 49 entry capture sets, ~0.5 MB; at 2,000
versions ~2 MB, load ~30 ms (INFERRED from R5 F6 JSON timings).

### 2.5 Scanner

```python
@dataclass(frozen=True)
class ScanProblem:
    kind: Literal["folder-missing", "folder-unreadable", "member-unreadable", "member-unstable",
                  "entry-is-live", "entry-unmapped", "id-mismatch", "game-running", "game-unknown"]
    where: SourceRef | None
    slot: int | None
    detail: str           # ASCII; passed through aliases.redact before display

@dataclass(frozen=True)
class ScanResult:
    at: int
    live: Mapping[LiveRef, "LiveFolderScan"]     # per live source: 15 captures, cloud flags, writable
    entries: Mapping[str, "EntryScan"]           # per catalog or store entry: captures, kind, managed, created
    captures: tuple[Capture, ...]
    versions: Mapping[Vid, VersionInfo]          # registry (journal metas merged by the facade)
    pairs: Mapping[PairKey, PairInfo]
    problems: tuple[ScanProblem, ...]
    game_running: bool | None
    timings: Mapping[str, float]

def scan(vault: Vault, store: "VersionStore | None", sources: Sequence[Source], cache: ScanCache, *,
         game_running: bool | None, hash_live: bool = True, deep: bool = False,
         now: Callable[[], float] = time.time,
         progress: Callable[[int, int], None] | None = None,
         cancel: threading.Event | None = None) -> ScanResult: ...
```

Algorithm:

1. **Live sources** = `AppState.live_sources` (enabled, role live). For each: `savedir.scan_any`
   (Steam metas; wgs index + blobs; an `xgs` folder only when it has no `wgs` twin, which
   `msstore.find_microsoft_save_dirs` already guarantees). One `Capture` per slot 1-15, empty
   slots included (an empty slot is an observation).
2. **Entries** = phase 1: `vault.entries` (catalog); from phase 2: `list_vault_entries(vault,
   store)` (D3 `core/store/listing.py`). The same listing feeds the classic tab and the CLI,
   so every view sees the same backups; a store entry hides the catalog entry with the same id
   (fold and copy-in keep ids), so a folded backup is captured once. Whole-folder kinds (`full`,
   `snapshot`, `inplace`, `imported`) yield 15 captures; `extract` and `preserve` yield their
   own slots only.
   - **Catalog entry** (`ref` is an `EntryRef`): resolve the folder (D11: relative for managed
     entries once the catalog supports it, absolute for in-place). An entry whose resolved path
     equals a live source's path or its `xgs` twin is `entry-is-live` and skipped (P9: the
     owner's `inplace-20260624-182102` is the live wgs folder; counting it double-counts every
     Xbox save, R7 2.1). Fingerprint first (2.4); rescan only on change.
   - **Store entry** (`ref` is a `StoreRef`, phase 2 on): no file is read. `store.entry(id)`
     holds per member a `MemberRecord` with `vid`, `pid` and the meta fields, so each capture is
     built from the manifest (`where=StoreRef(id)`, `observed_at=created_utc`,
     `time_quality="exact"`, `src="entry"`; `MetaSnap` fields 1:1, `meta_sha` =
     `meta_plain_sha256`, `data_size` = the data file's `FileEntry.size`, `ts_src="meta"`; a
     `pid` of None goes through the fallback chain of 2.2, the only case that reads an object,
     cached by vid). A slot without `MemberRecord`s is an empty capture. A `MemberRecord`
     with `valid=False` is reported as `member-unreadable` and left out of the capture.
3. **Hashing**: `vid` via the cache, else `safety.sha256_file` (1 MiB reads). With `deep=True`
   everything is re-hashed (CLI `--rehash`).
4. **Member validation** (live members only): `lz4_block.stats(data)` chunk total must equal
   the meta's `SizeDecompressed` (Steam; the reliable tie, DESIGN.md) or the stream must walk
   cleanly (Xbox). A failing live member is `member-unstable`: it is shown, hashed, but never
   journaled (2.6). Game-running guard: if `game_running is True` every live member is treated
   as unstable for recording; `None` records with validation (DESIGN.md D5: unknown means warn,
   never "safe").
5. **pid** per 2.2; data-id fallback only for metas with id 0 (pre-P1 Xbox, pre-Waypoint),
   cached in `ids`.
6. **Registry**: one `VersionInfo` per vid (earliest observation wins the `MetaSnap`; later
   captures extend `locations`, `roles`, `meta_ts_last`), one `PairInfo` per distinct pair.
   Entry capture time = entry `created` (local naive ISO in catalog v1, converted with
   `datetime.fromisoformat(s).astimezone(timezone.utc)`; SOURCED `catalog.py:240`), store
   entries `created_utc`, in-place entries the newest meta time inside,
   `time_quality="estimated"`.
7. Problems never stop the scan (graceful degradation rule); each carries context.

Runs on a worker thread in the GUI (C31; `sha256` releases the GIL on large buffers); the CLI runs
it inline.

### 2.6 The live journal

#### 2.6.1 Records

`journal.jsonl` holds two record types; all share `t`, `id` (`<vault_id[:8]>-<seq>`), `seq`
(monotonic per file, assigned under the lock) and `at` (UTC; live records use
`max(now, last_at)` so the order never goes backwards when the clock does). Replay order is
`(at, seq)`; this is the `ObsOrder` used for `first_seen`.

**`obs`** - one live slot's content, written only when it differs from the last record for that
target (or on first sight):

```json
{"t": "obs", "id": "9c1e0a52-118", "seq": 118, "at": 1792137600,
 "src": "scan",
 "target": {"platform": "steam", "account": "st_<steamid64>", "instance": "", "slot": 9},
 "pair": ["54cfd111...", "f073ea37..."],
 "m": {"f073ea37...": {"member": "B", "pid": "DDDAD1BDD39F9C72", "pid_src": "meta",
        "plat": "steam", "pt": 1916188, "ts": 1791924000, "ts_src": "meta",
        "name": "Main Save (S2)", "summary": "...", "diff": 2, "mode": 1, "season": 0,
        "bv": 4220, "fmt": 2004, "dsize": 18812345, "size": 8812345,
        "msha": "6d1f03aa...", "valid": true, "ft": 1791924003.25}},
 "note_revs": {"DDDAD1BDD39F9C72": 3},
 "entry": null, "q": "exact"}
```

- `src`: `scan` (a live scan), `verify` (D5's post-write rescan), `backfill` (bootstrap, from an
  entry; `entry` names it).
- `m` describes every vid this journal has not described before, so the journal alone can draw
  history for versions whose bytes are gone.
- `note_revs`: for each pid of a newly described vid that has a save-file note, the note's
  revision at this moment - the "snapshot of the save-file note at capture time" (C19).

**`op`** - an operation's binding of one target slot (one record per target slot; a Swap writes
two with the same `op_id`; idempotent on `(op_id, target)`):

```json
{"t": "op", "id": "9c1e0a52-119", "seq": 119, "at": 1792137655,
 "src": "app", "op_id": "op-20261016T080055Z-3fa2", "kind": "restore_older",
 "target": {"platform": "steam", "account": "st_<steamid64>", "instance": "", "slot": 9},
 "pair": ["9e4b2f00...", "<B vid>"],
 "evicted": ["54cfd111...", "f073ea37..."],
 "source": {"kind": "entry", "entry": "full-steam-20261005-174129", "slot": 9},
 "preserved": {"f073ea37...": {"kind": "store", "entry": "preserve-steam-20261016-080054",
                               "slot": 9, "member": "B"}},
 "status": "done", "undo_of": null, "note": "", "app": "nms-save-vault 0.4.0"}
```

`src`: `app` (D5 or the phase-1 hook) or `oplog` (bootstrap, from a legacy `oplog.jsonl`
line; `op_id` = `legacy-<line index>`). `kind` for app ops is D5's (`place`,
`place_into_empty`, `restore_older`, `swap`, `undo`, `promote`, `panic_restore`; no app op
empties a slot, F8; D5's vault-only "keep a copy" writes no op record);
classic and legacy ops map `repopulate_slot` -> `repopulate`, `clear_slot` -> `clear`,
`restore_full`, `undo_last` -> `undo` (`promote_member` changes only the meta, so it binds the
same pair).

#### 2.6.2 Observing a scan

```python
@dataclass(frozen=True)
class ObserveResult:
    written: int                         # records appended
    events: tuple[JournalEvent, ...]     # derived for the new records (for UI notices / D5)
    skipped: tuple[tuple[LiveTarget, str], ...]   # (target, reason): unstable, game running,
                                                  # unreadable, operation in progress

class Journal:
    def __init__(self, store: OrganizerStore, registry: Mapping[Vid, VersionInfo]): ...
    def observe(self, scan: ScanResult, *, src: Literal["scan", "verify"],
                skip: frozenset[LiveTarget]) -> ObserveResult:
        """Under the store lock: re-read the journal tail, and for every live target in `scan`
        that is not in `skip` and whose pair differs from its last record (an obs or a done op)
        append an obs record. Targets in `skip` (reason "operation in progress"), targets of
        an unreadable folder and targets with an unstable member are skipped (never recorded
        as 'deleted')."""
    def replay(self) -> "JournalState":
        """Pure function of the records + registry: per-target state, edges, events, op outcomes."""
```

`skip` is required, so no caller can forget it. Callers and what they pass:

| Caller | `src` | `skip` |
|---|---|---|
| GUI start and refresh, `nmsvault scan` (through the `Organizer` facade) | `scan` | `busy_targets()`: the facade's hook, D5's `liveops.busy_targets(ctx)` from phase 2, empty before |
| D5 pre-flight (journals the state about to be evicted; the op is not open yet) | `scan` | `busy_targets(ctx)` |
| D5 post-write check, while its own op is still open (last event `validated`) | `verify` | `busy_targets(ctx)` minus the targets of its own op |
| Classic-operation hook (2.6.7) | `scan` | `busy_targets()` (empty in phase 1) |

Read-only CLI listings do not observe (2.13, O-D1-3).

#### 2.6.3 Replay algorithm (edges and events)

Replay runs per target over its records sorted by `(at, seq)`. `first_seen(v)` is the smallest
`ObsOrder` at which `v` appears anywhere: journal records, or entry captures at
`(created, -1)`. `compatible(p, n)` is `pid(p) == pid(n)` or both legacy with the same platform
and the same non-empty name (two unnamed saves match only as the same pid).

```text
state = None                     # what the slot holds now, per the records
last_full = None                 # the last non-empty pair seen or placed (for "resurrected")
for rec in records(target):
    if rec.t == "op":
        if rec.status != "done": continue          # failed / rolled back ops bind nothing
        state = State(vids=rec.pair, src="op", order=rec.order, op=rec,
                      evicted=state.vids if state else (None, None))
        if not empty(rec.pair): last_full = rec.pair
        continue
    cur = rec.pair
    if state is None:                              # first sight of this target
        emit first-seen; state = State(cur, "obs", rec.order)
        if not empty(cur): last_full = cur
        continue
    if state.src == "op" and cur == state.evicted and cur != state.vids:   # ordered pairs
        emit reverted(op_id=state.op.op_id)        # the cloud or another tool put the old save back
        state = State(cur, "obs", rec.order); last_full = cur or last_full
        continue                                   # no edges
    if empty(cur) and not empty(state.vids):
        emit deleted(op_id=None)                   # deleted outside the app
        if state.src == "op": emit unexpected(op_id=state.op.op_id)
        state = State(cur, "obs", rec.order); continue
    if empty(state.vids) and not empty(cur) and set(cur) == set(last_full or ()):
        emit resurrected                           # Steam Cloud brought deleted files back (R8)
        state = State(cur, "obs", rec.order); continue      # no edges
    if one_member(cur) and two_members(state.vids): emit partial
    new = [v for v in cur if v and v not in state.vids]
    back = [v for v in new if first_seen(v) < state.order]
    if back: emit returned(vids=back)              # an older version came back: no edge for it
    new = [v for v in new if v not in back]
    cands = [v for v in state.vids if v]           # ordered: A, B of the state
    made_from_placed = False
    for n in sorted(new, key=lambda v: (meta_ts(v), play_time(v), v)):
        ok = [c for c in cands if compatible(c, n)
              and play_time(c) <= play_time(n)
              and (ts_src(c) != ts_src(n) or meta_ts(c) <= meta_ts(n))]   # times on one clock
        if ok:
            p = max(ok, key=lambda c: (play_time(c), meta_ts(c), cands.index(c)))
            if state.src == "op" and p in state.vids:
                method = "op" if state.op.src == "app" else "oplog"
                edge(p, n, tier="recorded", method=method, op_id=state.op.op_id)
                made_from_placed = True
            elif rec.src == "backfill":
                edge(p, n, tier="observed", method="capture-sequence")   # "seen in backups"
            else:
                edge(p, n, tier="observed", method="journal")            # "seen in the slot"
        elif any(not compatible(c, n) for c in cands):
            emit replaced(vids=[n])                # a different save entered the slot outside the app
        cands.append(n)                            # later new members may descend from n
    if state.src == "op":
        if made_from_placed: emit confirmed(op_id=state.op.op_id)   # the game saved on top of it
        else: emit unexpected(op_id=state.op.op_id)   # neither placed, its child, nor the evicted pair
    if new: emit changed
    state = State(cur, "obs", rec.order)
    if not empty(cur): last_full = cur
```

Rules that came out of the prototype (VERIFIED, Appendix A): (1) at equal seconds a capture is
ordered before an op - a pre-op snapshot is taken before the write and the oplog line after it;
without this the 2026-10-05 rollback becomes a false chain; (2) on equal play time and time the
most recently added candidate wins, so an editor's change and the game's next save chain in one
deterministic way; (3) the `returned` rule stops a reverted or re-copied old version from being
drawn as a child of what was placed.

An observation that still holds the placed pair writes no obs record (its pair equals the op
record), so D5's own post-write check never confirms its op: an unchanged placed pair stays
`pending` until the game saves on top of it (2.6.6).

#### 2.6.4 Tiers, kinds and how each edge arises

These are the only edges; a version none of them reaches has no parent and is listed by date.

| Tier | Method | Arises when | UI wording | Example (owner's data) |
|---|---|---|---|---|
| recorded | `op` | the first new members seen in a slot after an app operation placed a pair there; parent = the placed member | "put live by the app" | (from phase 2 on) |
| recorded | `oplog` | same, the operation being a resolved legacy `oplog.jsonl` line (bootstrap) | "put live by the app" | Main Save 9e4b2f00 -> 54cfd111 after the 2026-10-05 17:49 repopulate |
| observed | `journal` | consecutive live observations of one slot by the new app | "seen in the slot" | (from phase 1 on) |
| observed | `capture-sequence` | consecutive app-made backups/snapshots of one live slot (bootstrap backfill, 2.12): the same kind of evidence as the journal, taken less often (owner, 01 4a Q3) | "seen in backups" | 130 edges |
| owner | `owner` | the owner's "set parent" (phase 4) | "set by you" | - |

#### 2.6.5 The binding rule (C16)

"After an operation places V into slot T, later live versions in T with the same id and play
time >= V's are children of V until the next operation on T" is the replay above: the op record
makes the placed pair the state; the next observation's new members get candidates from that
state, filtered by `compatible` (same id) and play time / time not lower; the next op replaces
the state. A different id ends the binding (`replaced`). Only the first new members after an op
get `recorded`; their descendants are `observed`.

#### 2.6.6 Verification against D5's expected pairs (C25)

```python
class LineageRecorder:
    def __init__(self, store: OrganizerStore, journal: Journal): ...
    def record_op(self, rec: "OpLineage") -> None:
        """Append one op record (idempotent on (op_id, target)); fsync; under the lock."""
    def observe(self, scan: ScanResult, *, src: Literal["scan", "verify"],
                skip: frozenset[LiveTarget]) -> ObserveResult: ...   # Journal.observe (2.6.2)
    def expected_pair(self, target: LiveTarget) -> PairKey | None:
        """The placed pair of the newest done op on `target` that no later op superseded."""
    def op_outcome(self, op_id: str, target: LiveTarget) -> Literal[
            "pending", "confirmed", "reverted", "superseded", "unexpected", "unknown"]:
        """From replay, at the first obs record on `target` after the op: none yet, or the slot
        still holds the placed pair -> pending; it holds a recorded child of the placed pair (the
        game saved on top of it) -> confirmed; it holds the evicted pair -> reverted; another op
        on the target came first -> superseded; anything else (deleted, replaced, another
        version) -> unexpected; no such op record -> unknown."""

@dataclass(frozen=True)
class OpLineage:
    op_id: str                 # "op-<UTC yyyymmddThhmmssZ>-<4 hex>" (section 6)
    kind: Literal["place", "place_into_empty", "restore_older", "swap", "undo", "promote",
                  "panic_restore",                                                          # D5
                  "repopulate", "clear", "restore_full"]                                    # classic hook
    at: int
    target: LiveTarget
    placed: PairKey            # (None, None) only for a classic clear
    evicted: PairKey | None
    source: "Location | None"  # where the placed bytes came from
    preserved: Mapping[Vid, "Location"]   # evicted vid -> durable store location (D3 preserve)
    status: Literal["done", "failed", "rolled_back"]
    undo_of: str | None = None
    note: str = ""
    app: str = ""
```

D5 owns the verification state and messages and adds its own evidence (a game session seen, an
upload) to turn `pending` into its "confirmed" (D5 2.12.1); D1 only answers from the records. The
owner's history shows exactly the three cloud cases R7 found as `reverted` events (2.12).

#### 2.6.7 Recording classic operations (C18)

From phase 1, every classic operation that writes a live folder (today `repopulate_slot`,
`restore_full`, `clear_slot`, `promote_member`, `undo_last`) runs inside the live-op lock
`<vault>/organizer/.liveop.lock` (`safety.exclusive_lock`, timeout 10 s -> the classic error
"Another NMS Save Vault window is changing live saves"), so GUI, CLI and a second app instance
never write one live folder at once. Inside the lock the hook observes the folder before the
write (journals the state about to be replaced); after a successful write it calls `record_op`
per written slot with the classic kind, the written pair and, as `evicted`, the journal's
previous state, then observes the folder again. Classic backups (`full_backup`,
`snapshot_live`) call `observe` on the folder they read, which narrows the journal's gaps (risk
1). A failure to record is logged as a warning and never fails the operation. D5 replaces the
live-write calls in phase 2 (the hook touches `operations.py`, which D5 owns; D5 takes the same
lock).

### 2.7 Lineage assembly

#### 2.7.1 Precedence

`LineageGraph.build(journal_state, lineage_state, registry)` gives each vid at most one parent:

1. an owner `set_parent(child, parent | None)` (None = "this version has no known parent");
2. else the journal's edge of the highest tier: recorded > observed (`journal` and
   `capture-sequence` alike); within one tier the later evidence wins; every conflict is listed
   in `LineageGraph.conflicts` (0 on the owner's data);
3. `not_related(a, b)` drops any edge between `a` and `b` from step 2;
4. a vid still without a parent is a root. Nothing is guessed for it.

An edge that would close a cycle (only owner corrections can) is dropped and reported.

#### 2.7.2 `lineage.json`

```json
{"schema": 1, "kind": "nmsvault.organizer.lineage",
 "corrections": [
   {"id": "cor-20261016T081500Z-a1b2", "kind": "set_parent", "child": "<vid>", "parent": "<vid or null>",
    "at": 1792138500, "note": ""},
   {"id": "cor-20261016T081700Z-c3d4", "kind": "not_related", "a": "<vid>", "b": "<vid>",
    "at": 1792138620, "note": ""}],
 "aliases": [{"pid": "4418B58A...", "into": "9E8385F0...", "at": 1792138700}],
 "splits":  [{"pid": "split:DDDAD1BDD39F9C72:1", "from": "DDDAD1BDD39F9C72",
              "vids": ["<vid>", "..."], "name": "Main Save - permadeath try", "at": 1792138800}],
 "bootstrap": {"done_at": 1792137600, "app": "nms-save-vault 0.3.0", "algo": 1, "report": {}}}
```

#### 2.7.3 Owner corrections

```python
def set_parent(child: Vid, parent: Vid | None, *, note: str = "") -> Correction
def mark_unrelated(a: Vid, b: Vid, *, note: str = "") -> Correction
def remove_correction(correction_id: str) -> None
```

Validation: `parent != child`; the parent is not a descendant of the child; both belong to the
same canonical line (else "Combine the two save files first"). A parent saved later than the
child is allowed (the owner may know better) and drawn upward (the lane spans of 2.10.5 work
for any row order).

#### 2.7.4 Combine and Split (C1 escape hatch)

- `combine(pids, into)`: alias entries `pid -> into` for the others; `canonical(pid)` follows
  aliases transitively, cycle-safe. Save-file notes: when several exist, the surviving note gets
  a new revision with the other texts appended under `--- merged from <name> ---`.
  `uncombine(pid)` deletes the alias (the merged note revision stays in history).
- `split(pid, vids, name)`: creates `split:<raw pid>:<n>` seeded with `vids` (the UI's "split
  this branch off" passes the branch, i.e. the node and its current descendants). Effective pid,
  in topological order: a seed takes the split pid; else a vid whose parent edge is recorded,
  observed or owner and whose parent's effective pid is a split of the same raw pid inherits it
  (so the journal carries a split-off twin forward); else `canonical(raw_pid)`. A line's history
  shows a link to the other line where an edge crosses (`CrossLink`). `unsplit(split_pid)`
  deletes the entry.

### 2.8 Steam and Xbox copies of one save (C2)

Hello Games cross-save copies a save between platforms with different bytes, so no edge ever
joins a Steam version to an Xbox version, and none is guessed. The owner's model (01 4a Q4): the
latest copy usually wins; a platform not opened for a long time lags and catches up when that
game is opened; if both vary, each platform effectively becomes its own branch. The model shows
each platform's live copy as its own tip and compares the two without ancestry.

For a line with live heads on both platforms, `S` and `X` = the most-progressed live head
(largest `(play_time, meta_ts)`) of each platform:

| State | Rule | UI wording |
|---|---|---|
| `same` | `S.play_time == X.play_time` | "Steam and Xbox in step" |
| `diverged` | the journal proves both progressed since they were last in step (below) | "two branches: Steam and Xbox both played since <sync date>" |
| `xbox-behind` / `steam-behind` | otherwise: the copy with less play time; its tip's `behind_by` = the difference | "Xbox copy 2h35 behind" |

"Both progressed" is computed in replay over the line's live targets of both platforms, merged
by `(at, seq)`:

```text
sync = None; progressed = set(); seen_pt = {"steam": set(), "xbox": set()}
for rec in obs and op records of this line's live targets, in (at, seq) order:
    P = rec.target.platform; Q = the other platform
    update the most-progressed live head of P from rec
    if both platforms have a head and their play times are equal:
        sync = rec.at; progressed = set()                  # last in step here
    elif sync is not None and rec added a `play` edge (recorded or observed) on P whose
            child's play time is not in seen_pt[Q]:        # a cross-save copy is not progress
        progressed.add(P)
    seen_pt[P] |= play times of the vids in rec.pair
diverged = sync is not None and progressed == {"steam", "xbox"}
```

Without a sync point (history from before recording) the state is never `diverged`. In
`diverged` both tips have `behind_by = 0`. Owner's
data (VERIFIED, `proto_drawn_only.py`): all three lines live on both platforms are
`xbox-behind` - The Cartographers 2h35, Exobiology1-600qs 0h24, Creative Primary 6h33. The
Xbox copy of Creative Primary is a re-save (no play) of a February restore point, so "behind" is
the owner's own reading: opening the Xbox game would bring the Steam state over.

```python
@dataclass(frozen=True)
class PlatformTip:
    platform: Platform
    target: LiveTarget          # the slot of the most-progressed live head on this platform
    head: Vid
    play_time: int
    saved_at: int               # head meta_ts
    behind_by: int              # play seconds behind the other platform's tip; 0 if not behind
```

`Line.platform_tips` holds one `PlatformTip` per platform with a live head (sorted Steam, Xbox);
`Line.cross_platform` holds the state (`n/a` with one platform live). The history view draws no
link between the two copies (2.10.4); the version card of a live head says "Xbox copy 2h35
behind the Steam copy" instead.

### 2.9 Notes (C19-C22)

`notes.json`:

```json
{"schema": 1, "kind": "nmsvault.organizer.notes",
 "save_files": {
   "DDDAD1BDD39F9C72": {
     "text": "Main run. Next: finish Atlas path; keep legacy wood parts.",
     "rev": 3, "at": 1792137600, "pinned": true,
     "history": [{"rev": 1, "at": 1791000000, "text": "Main run."},
                 {"rev": 2, "at": 1791500000, "text": "Main run. Next: Atlas path."}]}},
 "versions": {
   "9e4b2f00...": {"label": "before the 10-05 session", "comment": "", "locked": true,
                   "pair": ["9e4b2f00...", "<B vid>"], "pid": "DDDAD1BDD39F9C72",
                   "at": 1792137700}}}
```

- **Save-file note** per canonical pid: current `text` and `rev`; `history` keeps every earlier
  revision in full (small: ~30 lines x tens of revisions). `pinned` = never suggest evicting this
  save file (C20, C30; per save file so it follows the save across slots - option O-D1-2).
- **Version note** keyed by the **head vid** of the pair (105 heads for 106 pairs on the owner's
  data; the one shared head is two captures of the same loaded state): `label` (<= 80 chars),
  `comment` (<= 4,000), `locked` (the owner's "keep" mark, shown on the row and a key-version
  reason; with keep-everything, F9, nothing is pruned, so it guards nothing further).
- **Snapshot of the save-file note at capture time**: the journal's `note_revs` (2.6.1). A
  version's note snapshot = the revision recorded when the version was first journaled;
  "what changed in the note" = text diff between the snapshots of a version and its parent.
  Versions known only from bootstrap have none.
- Automatic description (C19, C14): when label and comment are empty the UI shows D4's
  `changes(child, parent)` for the version's drawn parent (`VersionRow.parent`). A version with
  no drawn parent shows nothing about changes, only "no recorded parent" (owner, 01 4a Q5); it
  is never compared with the previous version by date.
- Text rules: Unicode stored as is (C22); newlines normalised to `\n`; control characters
  other than `\n` and `\t` removed; limits checked at the API boundary. UI strings stay ASCII.
- Optimistic concurrency: `set_save_note(pid, text, base_rev)` raises `NoteConflict(current)`
  when the stored revision is not `base_rev` (GUI and CLI editing at once).

```python
class Notes:
    def save_note(self, pid: Pid) -> SaveNote | None
    def set_save_note(self, pid: Pid, text: str, *, base_rev: int | None) -> SaveNote
    def note_text_at(self, pid: Pid, rev: int) -> str | None
    def version_note(self, head: Vid) -> VersionNote | None
    def set_version_note(self, head: Vid, *, pair: PairKey, pid: Pid,
                         label: str | None = None, comment: str | None = None,
                         locked: bool | None = None) -> VersionNote
    def set_pinned(self, pid: Pid, pinned: bool) -> None
    def locked_vids(self) -> frozenset[Vid]      # heads and partners
```

### 2.10 The read model

All read-model objects are frozen dataclasses with `to_dict()` (JSON-safe for the CLI's
`--json`: tuples become lists, frozensets sorted lists). Labels the model builds are ASCII;
names, summaries and notes are user/game text. No raw account key, entry path or absolute path
appears: live sources
are shown through their state label passed through `aliases.redact`, and `to_dict()` writes a
`LiveTarget` as an opaque token `"<platform>:<live source index>:<slot>"` that a UI passes back
(to D5's entry points through `ReadModel.target_of`).

```python
class ReadModel:
    @classmethod
    def build(cls, scan: ScanResult, journal: JournalState, graph: LineageGraph,
              notes: NotesState, locator: VersionLocator, sources: Sequence[Source],
              facts: "FactsProvider | None", *, now: int) -> "ReadModel": ...
    def lines(self) -> tuple[Line, ...]
    def line(self, pid: Pid) -> Line | None
    def versions(self, pid: Pid) -> tuple[VersionRow, ...]          # newest first
    def live_board(self) -> LiveBoard
    def history(self, pid: Pid, mode: Literal["key", "default", "all"] = "default") -> HistoryGraph
    def events(self, pid: Pid) -> tuple["LineEvent", ...]            # 2.10.6
    def resolve(self, ref: str) -> Pid | Vid | LiveTarget | None     # CLI/UI references (2.13)
    def token(self, target: LiveTarget) -> str
    def target_of(self, token: str) -> LiveTarget | None

class FactsProvider(Protocol):   # the subset of D4's IndexService (D4 2.12) that D1 calls;
                                 # FactsView, ChangeSet, Priority are D4's types (TYPE_CHECKING import)
    def view(self, vid: Vid) -> "FactsView | None": ...            # None = not indexed yet
    def changes(self, child: Vid, parent: Vid, *,
                edge_kind: str | None = None) -> "ChangeSet | None": ...
    def is_milestone(self, child: Vid, parent: Vid) -> bool | None: ...   # None = not indexed
    def request(self, vids: Iterable[Vid], priority: "Priority") -> int: ...
```

`build` reads facts only (`view`, `is_milestone`; all non-blocking cache reads) and never waits
for the index. Requests come from the `Organizer` facade after each refresh, following D4's
triggers (D4 2.10.2): live member vids at `Priority.LIVE`, line representatives and branch tips
at `Priority.TIP`, vids first seen in this refresh at `Priority.NEW`, every other known vid at
`Priority.BACKFILL` once per session. `facts:<name>` sorting reads `view.numbers.get(name)`; a
missing view or name sorts last. `is_milestone` returning None counts as "not a milestone".
The id consistency check of 2.2 reads `view.pid_in_data` (requested from D4).

Building is a pure function (no I/O except `locator.locate`, which is in-memory): under 5 ms on
the owner's data, < 50 ms at 2,000 versions (INFERRED; prototype graph + lanes for all 30 lines
0.8 ms, VERIFIED).

#### 2.10.1 Line (one save file)

```python
@dataclass(frozen=True)
class LiveBadge:
    target: LiveTarget
    label: str              # "S9", "X8"; "S2.7" when a platform has several live sources
    head: Vid
    member_loaded: Member
    is_tip: bool            # the live head has no drawn child
    behind_by: int          # play seconds behind the line's most-progressed live head (0 = newest)

@dataclass(frozen=True)
class Line:
    pid: Pid                            # effective pid (aliases, splits applied)
    raw_pids: tuple[Pid, ...]
    identified: bool                    # False for legacy / unknown
    name: str                           # newest non-empty name of the line
    former_names: tuple[str, ...]       # most recent first
    difficulty: str                     # formats.difficulty_label of the representative version
    mode: str                           # "" or "Expedition 47" (game_mode 6 + season)
    platforms: tuple[Platform, ...]
    live: tuple[LiveBadge, ...]
    platform_tips: tuple[PlatformTip, ...]   # 2.8; one per platform with a live head
    cross_platform: Literal["n/a", "same", "steam-behind", "xbox-behind", "diverged"]   # 2.8
    play_time: int                      # max over versions
    last_played: int                    # max meta_ts
    version_count: int                  # distinct pairs (C6), versions with bytes
    gone_count: int                     # versions known only from the journal
    branch_count: int                   # tips of the drawn tree plus live heads outside it
    note_preview: str                   # first non-empty line of the save-file note, <= 80 chars
    has_note: bool
    pinned: bool
    locked_count: int
    flags: frozenset[str]
    representative: Vid                 # the version whose facts stand for the line
    facts: object | None                # D4 FactsView of `representative` (phase 3), opaque to D1
```

- Representative = the most-progressed live head; else the newest tip by `(meta_ts, play_time)`.
- `cross_platform` and `platform_tips`: 2.8.
- Line flags: `live`, `cross-platform`, `diverged`, `twin` (same pid live in two slots of one
  platform), `at-risk` (a live pair without a durable vault copy), `edited` (a version edited
  outside the game), `expedition`, `legacy-id`, `pinned`, `has-note`, `unreadable`.

Sort keys (`sort_lines(lines, key, descending=None)`): `name` (casefold), `play_time`,
`last_played` (default, descending), `versions`, `live` (live first, then badge order),
`platform`, `difficulty`, `risk`, `note`, and `facts:<name>` (any numeric fact, through D4's
view; missing sorts last). Filters (`LineFilter`): `live`, `platform`, `cross_platform`,
`has_note`, `pinned`, `at_risk`, `expedition`, `has_locked`, `text` (name, former names, note
text, version labels; case-insensitive).

#### 2.10.2 VersionRow (one pair)

```python
@dataclass(frozen=True)
class LocationSummary:
    kind: Literal["live", "vault", "external"]
    label: str              # "Live: Steam slot 9", "Vault: backup 2026-09-17",
                            # "Vault: safety snapshot 2026-09-20", "Vault: kept when replaced 2026-10-16",
                            # "Outside vault: <in-place entry label>" (until fold, 2.11)
    location: "Location"

@dataclass(frozen=True)
class VersionRow:
    pid: Pid
    pair: PairKey
    head: Vid
    head_member: Member
    partner: Vid | None
    name: str
    summary: str
    play_time: int
    saved_at: int           # head meta_ts
    difficulty: str
    mode: str
    platform: Platform
    locations: tuple[LocationSummary, ...]
    live_in: tuple[LiveTarget, ...]
    durable_copies: int
    flags: frozenset[str]
    parent: Vid | None      # drawn display parent (2.10.4); None = "no recorded parent"
    parent_tier: EdgeTier | None
    parent_kind: EdgeKind | None
    parent_method: EdgeMethod | None   # selects the wording of 2.6.4 ("seen in backups", ...)
    children: int           # drawn children
    in_tree: bool           # a node of the drawn tree (2.10.4 rule 3); False = listed by date
    key: bool               # key version (2.10.4 rule 5, for tree and dated rows)
    label: str
    comment: str
    locked: bool
    note_rev: int | None    # save-file note revision when first journaled
    folded_into: Vid | None # set when this row is folded (default mode)
    facts: "FactsView | None"
```

Version flags: `live` (head of a live pair), `live-partner`, `tip`, `fork`, `root`, `touch`
(parent edge kind touch), `edited` (either member's edge is `edit`: "edited outside the game"),
`only-live` (every copy is live), `outside-only` (copies outside live only in hand-made
in-place folders; until fold), `gone` (no bytes left), `locked`, `noted`, `placed` / `evicted`
(a slot-level operation placed or evicted it; 2.10.4), `unreadable`.

#### 2.10.3 LiveBoard (15 + 15 slots)

```python
@dataclass(frozen=True)
class BoardSlot:
    target: LiveTarget
    label: str                          # "S7"
    state: Literal["empty", "occupied", "partial", "unreadable"]
    pid: Pid | None
    name: str
    head: Vid | None
    partner: Vid | None
    member_loaded: Member | None
    play_time: int
    saved_at: int
    vault_copy: Literal["durable", "partial", "outside-vault", "none"]
    pinned: bool
    twin: bool                          # same pid in another slot of this platform
    other_platform: tuple[LiveTarget, ...]   # same pid live on the other platform
    cloud: str                          # Xbox worst-of member sync state; Steam "cloud on"/"cloud off"
    pending_op: str | None              # op_id with op_outcome "pending"
    last_event: JournalEvent | None     # newest event on this target in the last 30 days
    suggest_rank: int | None            # generic eviction order (C30)

@dataclass(frozen=True)
class LiveSource:
    ref: LiveRef
    label: str                          # state label, redacted
    platform: Platform
    writable: bool
    steam_cloud: bool | None
    slots: tuple[BoardSlot, ...]        # always 15

@dataclass(frozen=True)
class LiveBoard:
    sources: tuple[LiveSource, ...]     # state.json order
```

`vault_copy` is per pair: `durable` when every present member has a durable location (2.11: a
managed catalog entry of any kind, snapshots included once P2 is fixed, or a store entry);
`partial` when only some do; `outside-vault` when the only copies outside live are in hand-made
in-place folders (until fold); `none` otherwise.
`suggest_rank` is the generic order (empty slots, then unpinned slots whose pair is durable,
least recently played first, then other unpinned, pinned never); D5's eviction picker adds the
"same save file first" rule for a given source. On the owner's data 8 of the 29 occupied live
slots hold a pair with no copy anywhere outside live (16 members; R7: 16 live-only versions).

#### 2.10.4 HistoryGraph (projection, folding, key versions)

Nodes and edges come from the member-level graph (2.7) of one effective line, which holds only
the edges of 2.6.4. The history view draws that tree and lists everything else by date (owner
decision, 01 4a).

1. **Candidate nodes** = every pair head of the line, plus every member-level **fork point** that
   is not a head (a restore point the game never loaded can be the real fork point). Other
   versions are shown inside their pair's row as the partner.
2. **Display parent** of a candidate = its nearest proper ancestor (member graph) that is a
   candidate. Display edges carry the tier, kind and method of the member edge into the child
   and `cross_platform = parent.platform != child.platform` (only an F6 transfer, phase 5, or an
   owner correction can make it true).
3. **Tree or dated.** A candidate is a tree node when it has a display parent or a display
   child, or is the head of a placed or evicted pair. Every other head is a dated row
   (`HistoryGraph.dated`, newest first, `VersionRow.in_tree = False`): no lane, no link, no
   "what changed". Nothing joins the Steam and Xbox copies of a save (2.8). Owner's
   data (VERIFIED, `proto_drawn_only.py`): 80 of 105 heads are tree nodes, 25 dated; 21 of 30
   lines have a tree (20 need one lane, Main Save two), 9 are dated lists only.
4. **Folding** (`mode="default"`, C5): a tree node whose display edge kind is `touch` or `edit`,
   with at most one child, that is not live, not a fork, not noted/locked and not
   placed/evicted, is folded into its parent: the parent gets `folded: {"touch": n, "edit": m}`
   and the child's edge gets `hidden += 1`. A dated row is folded into the next older dated row
   of the same platform when both have the same play time and it is not live, noted or locked
   (a re-save without play; marker only, no link). `mode="all"` folds nothing. Owner's data: 4
   tree nodes fold (VERIFIED).
5. **Key versions** (`mode="key"`): a tree node is key if it is live (head of a live pair), a
   tip, a fork, a root, noted or locked, placed or evicted, the first node after a platform
   change, the first node with a new non-empty name, or D4's `is_milestone(child, parent)` says
   so (phase 3). Other tree nodes collapse; the edge between two key nodes carries `hidden` =
   the number of versions in between. A dated row is key if it is live, noted or locked, the
   newest or oldest dated row, or the first with a new non-empty name. `VersionRow.key` carries
   the result in every mode. Owner's data: 45 of 80 tree nodes are key; Main Save shows 4 of 15
   (VERIFIED).

"Placed or evicted" (rules 3, 4 and 5, and the `placed`/`evicted` flags) means the head of a pair
that a slot-level operation put into a slot or took out of it: D5's place, place into empty,
restore older, swap and undo, and the classic repopulate and clear. Whole-folder restores
(classic `restore_full` and `undo`, D5's `panic_restore`) bind slots in the journal but do not
mark versions; on the owner's data 7 heads are placed or evicted.

```python
@dataclass(frozen=True)
class GraphNode:
    vid: Vid
    row: int                 # 0 = newest
    lane: int
    version: VersionRow      # for a non-head fork point: the row of a pair it belongs to
    kind: Literal["version", "fork-member"]
    flags: frozenset[str]
    folded: Mapping[str, int]

@dataclass(frozen=True)
class GraphEdge:
    parent: Vid
    child: Vid
    tier: EdgeTier
    kind: EdgeKind
    method: EdgeMethod
    op_id: str | None
    cross_platform: bool
    hidden: int              # versions collapsed or folded on this path

@dataclass(frozen=True)
class CrossLink:             # edge to another line (Split / not combined)
    vid: Vid
    other_pid: Pid
    other_vid: Vid

@dataclass(frozen=True)
class HistoryGraph:
    pid: Pid
    mode: str
    nodes: tuple[GraphNode, ...]          # drawn tree, in row order
    edges: tuple[GraphEdge, ...]
    lanes: int
    pass_through: tuple[tuple[int, ...], ...]   # per row: lanes with a line passing (no node)
    links: tuple[CrossLink, ...]
    dated: tuple[VersionRow, ...]         # rule 3: every other version, newest first (all modes;
                                          # the UI applies `key` / `folded_into` per mode)
```

#### 2.10.5 Lane assignment (every UI draws the same tree)

Input: the tree nodes `N` of the chosen mode (after folding or collapsing) and their display
parents. Dated rows get no lane.

1. Rows: sort `N` by `order` descending; `row(n)` = index (newest at the top).
2. `latest(n)` = the largest `order` in the subtree of `n` (itself included).
3. Main child of `n` = the child with the largest `(latest(c), c is a live head, c.vid)`.
4. Branches: every node that is a root or not the main child of its parent starts a branch; a
   branch is the chain start -> main child -> main child ... .
5. Sort branches by `latest(start)` descending, then `start.vid`. The first is the trunk.
6. For each branch in that order: `span` = rows from the branch's newest node to its parent's
   row (or its own oldest row for a root), as a closed interval `[min, max]`; lane = the
   smallest `L >= 0` not used by an already placed branch whose span overlaps. Every node of
   the branch gets lane `L`.
7. `pass_through[r]` = lanes whose span contains row `r` and that have no node in row `r`.
   Each branch's first node connects to its parent's `(row, lane)`; a UI draws that connector
   as a diagonal or an elbow, its choice.

Owner's data: of the 21 lines with a tree, 20 need one lane and Main Save two (VERIFIED). ASCII
rendering as the CLI prints it (`*` node, `|` lane passing, `/` connector into the parent's lane,
`-` dated row; times UTC):

```text
Main Save (S2), mode "all": 15 linked versions, 2 lanes; 5 listed by date
*    2026-10-15 20:39  532.3h  S  f073ea37  live S9, tip
| *  2026-10-05 14:48  530.9h  S  b9c8b750  tip (dead end), seen in backups
*/   2026-10-05 14:13  530.9h  S  9e4b2f00  fork, placed 2026-10-05
*    2026-09-26 20:48  530.3h  S  4528014e  seen in backups
...  10 more linked versions
*    2026-06-23 18:50  520.7h  S  e276efcb  root
     no recorded link, by date:
-    2026-06-14 12:44  520.6h  S  b540615b
...  3 more
-    2026-03-30 14:33  506.4h  S  89eb6181

Main Save (S2), mode "key": 4 of 15 linked versions
*    2026-10-15 20:39  532.3h  S  f073ea37  live S9, tip
| *  2026-10-05 14:48  530.9h  S  b9c8b750  tip
*/   2026-10-05 14:13  530.9h  S  9e4b2f00  fork, placed
:    11 versions hidden
*    2026-06-23 18:50  520.7h  S  e276efcb  root

Creative Primary, mode "all": 9 linked versions, 1 lane; 2 listed by date
*    2026-10-08 13:23   45.2h  S  e414de0e  live S13, tip
...  7 more Steam versions, seen in backups
*    2026-06-27 20:36   39.5h  S  d642b721  root
     no recorded link, by date:
-    2026-07-16 05:07   38.7h  X  e70091c0  live X8, Xbox copy 6h33 behind
-    2026-03-04 16:27   39.1h  S  cb14a141
```

#### 2.10.6 Events of a line

```python
@dataclass(frozen=True)
class LineEvent:
    at: int
    kind: str                   # an op kind (2.6.6) or a JournalEvent kind
    target: LiveTarget
    op_id: str | None
    vids: tuple[Vid, ...]       # the versions it concerns (placed, evicted, or the event's vids)
    text: str                   # ASCII, redacted: "Older version put live in Steam slot 9",
                                # "Steam Cloud put the replaced save back in slot 9"
```

`ReadModel.events(pid)` = the line's op records and journal events (reverted, deleted, returned,
resurrected, replaced, unexpected), newest first. A version card shows the events whose `vids`
hold one of its members.

### 2.11 The VersionLocator (C28)

```python
@dataclass(frozen=True)
class Location:
    vid: Vid
    kind: Literal["live", "entry", "store"]
    ref: SourceRef            # LiveRef / EntryRef(entry id) / StoreRef(store entry id)
    slot: int                 # slot in the live folder or entry that holds the bytes
    member: Member            # the meta (and its Steam ordinal) belongs to that slot and member
    platform: Platform
    durable: bool             # a kept vault copy: any managed catalog entry (snapshots too,
                              # P2 fixed) or a store entry; never live or in-place
    external: bool = False    # in a hand-made in-place folder: kept by the owner, not by us

@dataclass(frozen=True)
class OpenedVersion:
    data: bytes               # verbatim; sha256 == vid (verified by open)
    meta_plain: bytes         # Steam: decrypted with the location's ordinal; Xbox: as stored
    platform: Platform
    meta_format: int
    location: Location        # where the bytes were read

@dataclass(frozen=True)
class LiveMember:
    target: LiveTarget
    member: Member
    expected_vid: Vid         # refuse if the live file no longer hashes to this

class VersionLocator(ABC):
    @abstractmethod
    def locate(self, vid: Vid) -> list[Location]:
        """Every known location: store, then durable catalog entries, external, live. In-memory."""
    @abstractmethod
    def open(self, vid: Vid, prefer: Location | None = None) -> OpenedVersion:
        """Read the bytes (prefer the given location, else the first in `locate` order), verify
        sha256 == vid, decrypt the meta. Raises VersionUnavailable."""
    @abstractmethod
    def preserve(self, members: Sequence[LiveMember], *, op_id: str, reason: str) -> list[Location]:
        """Make each member durable in the vault (store if absent, then re-read and verify the
        hash). One returned durable location per member, same order. Raises PreserveFailed;
        D5 refuses the live write then."""
    @abstractmethod
    def is_durable(self, vid: Vid) -> bool:
        """True when at least one durable, verified vault copy exists."""

class VaultLocator(VersionLocator):
    """The one locator handed to D4, D5 and the read model."""
    def __init__(self, readers: Sequence[VersionLocator],
                 preserver: VersionLocator | None) -> None: ...
    # locate: concatenation in reader order; open: `prefer`, then each reader in order;
    # is_durable: any reader; preserve: `preserver.preserve`, or
    # PreserveFailed("store-unavailable") when there is no writable store.
```

Composition by phase: phase 1 `VaultLocator([FolderLocator], None)`; from phase 2
`VaultLocator([StoreLocator, FolderLocator], StoreLocator)` - every preserve goes to the store
(the vault was copied aside first, D3); after fold and an owner-requested reclaim (phase 5),
`FolderLocator` still serves the live folders and any in-place entries. `FolderLocator`
(`core/organizer/folders.py`, D2's read side) answers from D1's `ScanResult` locations;
`StoreLocator` is D3's. A shared conformance test module (section 5, row 10) runs against both.

### 2.12 Bootstrap of an existing vault

Runs once, automatically, the first time the organizer opens a writable vault (or by
`nmsvault organizer bootstrap`), in a worker thread, with progress.

1. `scan(..., deep=True)` - hashes every copy.
2. Map app-made whole-folder entries (`full`, `snapshot`; managed catalog entries, and store
   entries of those kinds) to live sources: the entry's `VaultEntry.source` (live folder name)
   must equal a live source's `account_key` on the entry's platform
   (from the id tag `full-steam-` / `snapshot-xbox-`, else `savedir.platform_of`); with a single
   live source per platform a missing match maps to it with a note; otherwise the entry is
   `entry-unmapped`. These are the "seen in backups" evidence (owner, 01 4a Q3). In-place,
   imported, extract and preserve entries are not timeline evidence: they are owner copies of
   unknown time or partial (the prototype showed they produce false "deleted" and "resurrected"
   events), and their versions get no parent from them.
3. Resolve `oplog.jsonl` (read-only): `repopulate_slot` detail
   `(xbox )?<source folder name> slot S -> slot D` -> the unique entry whose folder name matches
   (managed: the id; in-place: the folder basename) -> its slot S pair is the placed pair;
   zero or several matches -> unresolved (reported). `clear_slot` -> empty pair. `restore_full`
   and `undo` -> the entry's / snapshot's 15 slot pairs. `promote_member` -> meta-only.
   `full_backup`, `extract_slot`, `import`, `import_vault` -> not live writes. Target folder:
   the unique live source of the op's platform (the oplog records no target; R1 2e E2),
   otherwise unresolved. Times: local naive -> UTC as in 2.5.
4. Order captures and ops on one axis per target (`(time, capture before op)`), assign `seq`,
   and append `obs` records (`src="backfill"`, only when the pair changed) and `op` records
   (`src="oplog"`); the final live state is a normal `obs` (`src="scan"`).
5. Write `lineage.json` `bootstrap` (time, app version, algorithm version, report counts).
6. Replay -> edges and events.

Idempotent: a second run with `bootstrap.done_at` set only appends backfill for full and
snapshot entries created before `done_at` that are not yet represented (a later import of old
backups), sorted into place by `(at, seq)`. Entries created after `done_at` are captures only
(versions and locations): the journal observed those live folders itself (2.6.7). A folded
entry keeps its id (D3), so backfill records stay valid after fold. `--dry-run` computes
everything in memory and writes nothing.

**Expected results on the owner's data** (VERIFIED on copies: D1's prototype, recounted
without inference by `proto_drawn_only.py`, Appendix A):

| Measure | Result |
|---|---|
| Folders | 52 found; live wgs alias and `xgs` mirror skipped; 1,177 member copies |
| Versions / pairs / heads / lines | 195 / 106 / 105 / 30 (pid from meta 193 vids, legacy 2) |
| Journal records written | 196 (96 obs, 100 op placements from 16 oplog ops), 98 KiB, parse 0.8 ms |
| Oplog | 42 lines: 7 repopulate, 3 clear, 6 restore/undo resolved; 0 unresolved |
| Edges | 135: 130 seen in backups, 5 recorded via oplog; 60 member roots; kinds play 113, touch 15, edit 7 |
| Drawn tree | 80 of 105 heads linked, 25 listed by date; 21 of 30 lines have a tree, 9 are dated lists |
| Content check | all 135 edges also pass R7's content classifier (a check of the evidence, not used by the app) |
| Events | 3 reverted (clear S15 2026-09-17, clear S3 2026-09-19, first repopulate S9 2026-10-05: R7's three cloud cases), 2 deleted (S1 2026-09-20 unrecorded, S1 by 2026-09-27) |
| Forks | 1: Main Save `9e4b2f00` -> live `54cfd111`/`f073ea37` (recorded) + dead end `b9c8b750` (seen in backups) |
| Cross-platform | 3 lines, all Xbox behind (2.8) |
| At risk | 8 live slots with no copy (16 members) |

**Report** (`BootstrapReport`, shown once in the GUI and printed by the CLI):

```python
@dataclass(frozen=True)
class BootstrapReport:
    folders_scanned: int; entries_skipped: tuple[tuple[str, str], ...]   # (entry id, reason)
    copies_hashed: int; versions: int; pairs: int; lines: int; legacy_lines: int
    journal_records: int; ops_resolved: int; ops_unresolved: tuple[tuple[str, str], ...]
    edges_by_method: Mapping[str, int]; roots: int; forks: tuple[tuple[str, str], ...]  # (line name, date)
    events: Mapping[str, int]; at_risk_live: int
    problems: tuple[ScanProblem, ...]; seconds: float
```

GUI wording (phase 1): "Built the history of your 30 save files from 33 backups: 135 links seen
in your backups and the app's log, 1 branch point. Versions with no recorded link are listed by
date. 8 live saves have no copy in the vault."

### 2.13 Read-only CLI

| Command | Writes | Output |
|---|---|---|
| `nmsvault saves [--sort KEY] [--live] [--platform steam\|xbox] [--find TEXT] [--json] [--no-cache]` | per-machine cache only | one row per line: name, live badges, versions, play time, last played, difficulty, note preview |
| `nmsvault versions <save> [--all] [--json]` | cache only | version rows with flags and where |
| `nmsvault board [--json]` | cache only | 15 + 15 slots |
| `nmsvault history <save> [--key \| --all] [--json]` (phase 4) | cache only | ASCII lanes as in 2.10.5 |
| `nmsvault note <save> [--set TEXT \| --append TEXT \| --history]` | `notes.json` | save-file note |
| `nmsvault note <version> [--label L] [--comment C] [--lock \| --unlock]` | `notes.json` | version note |
| `nmsvault pin <save> [--off]` | `notes.json` | |
| `nmsvault scan` | `journal.jsonl` | events since the last record |
| `nmsvault lineage set-parent <version> (<version> \| --root)`, `lineage unrelated <v> <v>`, `lineage combine <save> <save>`, `lineage split <version>` (phase 4) | `lineage.json` | |
| `nmsvault organizer bootstrap [--dry-run]` | journal, lineage (not with `--dry-run`) | the report |

References: `<save>` = pid prefix (>= 6 hex), a unique case-insensitive name, or a live badge
(`S9`, `X4`); `<version>` = vid prefix (>= 8 hex), a live badge (its head), or
`<save>@YYYY-MM-DD[THH:MM]` (the newest version saved at or before that time). Ambiguity is an
error listing the candidates. `--json` emits `to_dict()` output; account keys are never printed
(badges and redacted labels only). A read-only vault turns every writing command into an error
with the reason.

### 2.14 Error handling and safety invariants

1. D1 never writes save data, live folders, entry folders, store entries, `catalog.json` or
   `oplog.jsonl`.
2. Showing things writes nothing in the vault: scan, read model and listing commands touch only
   the per-machine cache.
3. Every store write holds the lock and is an atomic replace or one fsynced append; a file that
   does not parse is never overwritten.
4. A store file with a newer schema than the app is never written (read-only mode).
5. The journal records only validated live members and nothing while the game runs; an
   unreadable folder is never recorded as deleted.
6. Every string built from an account key, entry id or path goes through `aliases.redact`
   before display; the stores hold no absolute paths.
7. An organizer failure never blocks the classic app: the organizer view shows the error,
   backup/restore keep working.
8. Every drawn edge is evidence (recorded, observed or set by the owner) and carries its method
   to every view; nothing is inferred. A version without such an edge is listed by date and
   gets no "what changed".

### 2.15 Performance budget

| Step | Owner's vault (source) | ~2,000 versions (INFERRED) |
|---|---|---|
| Cold scan, `scan_any` over 52 folders | 0.98 s (R1 2h) | ~3 s |
| Hash every copy (first run on a machine) | 1.24-2.65 s for 1,177-1,237 copies (R7 2.9, R1 2h) | ~5-8 s |
| Warm refresh: live scans + changed live hashes + 49 entry fingerprints | < 0.2 s (live Steam 18-21 ms, wgs 28-43 ms, R1 2h; live hashing < 0.1 s, R7 2.8) | < 0.5 s |
| Journal parse + replay | 0.8 ms parse (VERIFIED) | ~10 ms + ~10 ms replay |
| Store entries (phase 2 on) | manifests only, no object reads | `list_vault_entries` cost per D3 |
| Read model + history + lanes, all lines | 0.8 ms graph + lanes (VERIFIED) | < 50 ms |
| Bootstrap | ~3-4 s, nearly all scan and hash | ~10 s |
| Data-id fallback (legacy / pre-P1 Xbox only) | ~23 ms per version, once (R7 1) | same |

Targets the implementation must meet: first organizer open <= 5 s on the owner's vault with
progress shown; warm refresh <= 0.3 s; nothing above 50 ms on the Tk thread.

## 3. Decisions

### F2 - History view style: C hybrid (owner)

The UI joins `versions(pid)` rows with `history(pid, mode)`: tree rows get `lane`, `lanes`,
`pass_through[row]`; dated rows follow under a separator; toggle `key` / `default` / `all`. A
linear timeline (A) and a separate lane graph (B) were considered; the owner chose C.

### Guessed history: none (owner)

History starts with the backfill ("seen in backups") and the journal; every other version is
listed by date (2.10.4). Showing inferred links (meta or content classifiers) was considered;
the owner rejected it.

### O-D1-2 - What a pin is attached to (default chosen)

- (a) **The save file** (recommended, implemented): the pin follows the save when it moves to
  another slot or platform; twins are both pinned.
- (b) The live slot: "slot 1 is my main slot" whatever is in it; needs `pinned_targets` in
  `notes.json` and a board-level toggle instead of a line-level one.

### O-D1-3 - Recording from read-only CLI commands (implementation choice, decided)

`nmsvault saves` and friends do not write the journal (decided: "showing never writes the
vault", the R1 C3 complaint). A CLI-only user records with `nmsvault scan`; every D5 operation
records anyway. Alternative rejected: record on every CLI listing (more history, surprising
writes, fails on read-only vaults).

## 4. Migration and compatibility

- **Existing vaults**: `catalog.json` v1 is not touched (C21). `organizer/` is created by the
  first organizer write (bootstrap, or the classic-operation hook). Without it the organizer
  still shows lines and the board from a scan, read-only.
- **Older app versions** are out of scope: users do not go back to them (owner, 01 4a Q2).
- **The store upgrade (phase 2)**: D3 copies the whole vault aside before the first store write,
  `organizer/` included, so a rollback restores the journal and notes as they were. Afterwards
  the scanner reads store entries (2.5) beside catalog entries; nothing in `organizer/` changes
  at fold (entry ids are kept).
- **`import_vault`** merges stores: `merge_store(dst, src)` - journal records not yet present (by
  `id`) appended and sorted into place by `(at, seq)`; save-file notes: the destination keeps its
  text, a different source text becomes a new revision "merged from <vault>"; version notes:
  destination wins, source fills missing, `locked` is OR-ed; corrections, aliases, splits:
  union, destination wins conflicts (reported); new entries trigger the idempotent backfill.
- **D11**: the store has no absolute paths; entries are referenced by id and resolved through
  the catalog API, live folders by `LiveRef` (folder name), which is the same on every machine
  for one account. A vault opened on another machine shows journal history for live folders
  that are not present there as history only.
- **Two machines, one account** (vault on a USB drive): both live folders map to the same
  `LiveRef` (Steam Cloud keeps them in sync); differences between machines appear as ordinary
  changes. Acceptable, and documented in the UI help.
- **`playthrough-ids.json`**: left alone (not read, not deleted); obsolete once P1 is fixed.
- **Catalog changes (D11 relative paths)**: the scanner resolves entry folders only through the
  catalog API, so it is unaffected.

## 5. Test plan

Risk-based. Real saves only read-only (skip when absent); every write on `tmp_path` copies or
synthetic fixtures; the organizer cache always under `tmp_path` in tests.

**Fixtures** (new or promoted to `tests/conftest.py`):
- `steam_pair(folder, slot, *, pid, play, ts, name, payload, member="both")` - promote
  `steam_save` from `test_playtime.py:43-63`; distinct payload bytes give distinct vids.
- `make_wgs_account` fixed to real 296/360-B metas with id @0x118, time @0x120, format @0x124
  (today 432 B with zeros; R1 G3) - lands with P1.
- `org_vault(tmp_path)` - a vault root, catalog with full/snapshot/extract/in-place entries built
  by the real operations on sandbox folders, plus an `oplog.jsonl`; `org_vault(store=True)` adds
  a D3 store with full, snapshot and preserve entries.
- `journal_records([...])` - builds obs/op records from a compact DSL
  (`obs(T9, A=v1, B=v2, at=...)`, `op(T9, placed=..., evicted=...)`).
- `fake_locator`, `fake_facts` (views per vid), `frozen_clock`.
- `real_vault_dir` - the owner's vault, read-only, skip when absent; cache in `tmp_path`;
  before/after fingerprint of vault and live folders asserted equal.

| # | Area | Tests (each a meaningful scenario) |
|---|---|---|
| 1 | Journal replay | linear play; touch; edit; A and B both new in one observation (chain by time); op then child -> recorded, `op_outcome` confirmed; op then no change -> `pending` (a verify observe writes nothing); op then cloud revert -> `reverted`, no edge, `op_outcome` reverted; deletion; resurrection; an old version re-copied -> `returned`; different pid -> `replaced`; failed op binds nothing; capture and op in the same second; tie-break determinism; twins create a fork; legacy pid compatible across a slot move; backfill steps -> observed `capture-sequence` edges |
| 2 | Observe `skip` | a busy target is skipped with reason "operation in progress" and journaled at the next refresh; D5's verify call (busy minus its own op) journals its own targets only |
| 3 | Golden bootstrap | a synthetic vault reproducing Main Save's rollback (repopulate whose first attempt was reverted) and a stale Xbox copy -> exact edges, events, the fork, `xbox-behind`, the dated rows; dry run writes nothing |
| 4 | Store | atomic replace + `.prev`; newer schema -> read-only, file untouched; older schema migrated on first write; torn last journal line ignored and not merged; unparseable file -> read-only, untouched |
| 5 | Lock | two spawned processes doing 200 read-modify-write transactions each on `notes.json` and appends on the journal -> no lost update, `seq` unique and dense; nested acquisition on one thread raises; a held lock -> `StoreBusy` for that transaction, the next one succeeds; Windows and POSIX code paths (the POSIX one in CI) |
| 6 | Scanner | alias entry (live path) skipped; whole-folder vs extract captures; store entries from manifests (no object read, `valid=False` member reported, empty slots of a full entry); cache hit/miss on size/mtime; fingerprint reuse; game running -> nothing recorded; unstable member (truncated copy) not journaled; legacy pid fixed at first registration; data-id fallback cached by vid |
| 7 | Notes | revisions and history; `base_rev` conflict; `note_revs` snapshot written with new versions; Unicode (Hebrew, CJK, emoji) round trip; ASCII-only UI test still passes |
| 8 | Lineage | precedence owner > recorded > observed; `not_related` drops an edge; cycle guard; combine/uncombine; split propagation through drawn edges |
| 9 | Steam and Xbox (2.8) | equal play time -> `same`; one side unchanged since the sync point -> `behind` with `behind_by`; both played since the sync point -> `diverged`; a cross-save copy arriving (play time the other side held) is not progress; no sync point -> never `diverged` |
| 10 | Read model | 15 + 15 board; at-risk flags; tree vs dated split; folding of tree nodes and of dated re-saves; key rule for both; lane assignment golden ASCII for the fork shape and a 3-branch case; a version without a drawn parent has `parent=None`; sort (incl. `facts:<name>` through `view.numbers`) and filter |
| 11 | Locator conformance | abstract suite run against `FolderLocator` and `StoreLocator`: `open` verifies the hash, `locate` order, `preserve` returns verified durable locations, `is_durable`; `VaultLocator` order and `preserve` without a store -> `PreserveFailed` |
| 12 | Classic hook | classic live writes from two processes serialise on `.liveop.lock`; the hook journals the state before and the op after; a recording failure never fails the operation |
| 13 | CLI | listing commands leave the vault byte-identical (fingerprint); `scan` writes only `journal.jsonl`; reference resolution and ambiguity errors; `--json` contains no account key |
| 14 | Real data (read-only) | scanner over the owner's vault: invariants (A/B same pid in every capture, every live pid has a line, no exceptions, vault unchanged); bootstrap `--dry-run` matches the 2.12 table within tolerance |
| 15 | Performance | synthetic 2,000-version vault: warm refresh < 0.5 s, read model < 50 ms, lanes < 10 ms per line |

D1 also runs inside the cross-design integration suite (01 section 5): place, swap, promote,
undo, crash points and a simulated cloud revert on synthetic live folders, asserting the
journal's edges, events and `op_outcome` together with D3's store entries and D5's logs.

## 6. Implementation plan

Sizes: S < 200 lines of code plus tests, M 200-500, L > 500. Each step is one commit with its
tests (`uv run pytest` green).

| # | Step | Size | Phase |
|---|---|---|---|
| 0 | Prerequisites owned elsewhere: P1 Xbox meta parser + fixture lengths; P9 alias handling in `discover`; P2 = pruning removed | - | 0 |
| 1 | `platform.user_cache_dir` + `safety.exclusive_lock` (+ two-process lock test) | S | 1 |
| 2 | `organizer/model.py`: ids, value types, JSON codecs | M | 1 |
| 3 | `organizer/store.py`: header, `vault_id`, schema check, atomic JSON, journal append/tail read, transactions, lock rules, read-only mode | M | 1 |
| 4 | `organizer/cache.py` + `organizer/scanner.py` (registry, pairs, alias, validation, pid rules) | M | 1 |
| 5 | `organizer/journal.py`: `observe` (with `skip`) + `replay` (edges, events, binding) | L | 1 |
| 6 | `organizer/recorder.py` + the classic-operation hook with the live-op lock (2.6.7) | M | 1 |
| 7 | `organizer/bootstrap.py` (backfill + oplog resolution + report) | M | 1 |
| 8 | `organizer/notes.py` + `lineage.json` skeleton (bootstrap block, pins) | M | 1 |
| 9 | `organizer/readmodel.py`: Line (incl. `platform_tips`, `cross_platform`), VersionRow, LiveBoard, `events`, sort/filter (no HistoryGraph) | M | 1 |
| 10 | `locator.py` (`VaultLocator`) + `folders.py` read side; `Organizer` facade + CLI `saves`, `versions`, `board`, `note`, `pin`, `scan`, `organizer bootstrap` | M | 1 |
| 11 | Real-data read-only smoke test + performance test | S | 1 |
| 12 | Scanner and bootstrap on `list_vault_entries` + store manifests; `VaultLocator` with `StoreLocator` as preserver; `busy_targets` hook from D5 | S | 2 |
| 13 | Facts hook: `representative`, `facts`, `facts:<name>` sorting, requests by D4 priority, key-version milestone hook | S | 3 |
| 14 | Corrections, Combine/Split, effective pid | M | 4 |
| 15 | HistoryGraph: projection, tree vs dated, folding, key rule, lanes | M | 4 |
| 16 | CLI `history`, `lineage ...`; `merge_store` in `import_vault` | M | 4 |

Phase 1 ships the organizer view, the journal, recording (backfill + classic-operation hook) and
notes; phase 2 adds the store as a capture source; the history view and corrections ship in
phase 4 (C18: recording must not wait for the history view).

## 7. Risks and open questions

1. **Journal granularity.** Changes between two scans are invisible; a rollback that is played
   over before the next scan looks linear (R7 3), and "seen in backups" links have the same limit
   at backup spacing. A cross-save copy that arrives while the other platform plays on, unseen,
   can make 2.8 report `diverged` for a lagging copy. Mitigation: scan on start, refresh, before
   and after every operation and backup. A background watcher is a possible later addition.
2. **Local-time conversion** of catalog `created` and oplog `ts` uses this machine's time-zone
   rules; a vault made in another zone shifts by the offset (ordering within one vault is
   unaffected; captures vs ops can swap only within the offset).
3. **Unorientable edits** (equal time and play time): ordered by observation order.
4. **An outside tool writing a brand-new same-id version with more play time** into a bound slot
   is taken as a child of the placed version (the `returned` rule catches only versions seen
   before). Accepted.
5. **P1 not fixed** -> Xbox identity by data decode and the wrong loaded member; the board must
   not ship before P1.
6. **Network or removable vaults**: locking on NFS may be unavailable (degraded, reported);
   appends on removable media rely on fsync.
7. **Store merge on vault import** can conflict (two notes for one save file); handled by
   revisions, reported.
8. **Legacy ids**: two different pre-Waypoint runs with the same name on one platform merge into
   one line; the owner splits them.
9. Open: should the journal also record the Xbox `containers.index` sync state per observation
   (to show "not uploaded yet")? Cheap to add later as an `obs` field; not needed for lineage.

## Deviations from 01-consolidation section 6

1. **Edge kinds.** Section 6 lists one kind set (play, restore-older, place, swap, edit, touch,
   cloud-revert, cross-save). Here an edge is only game-state descent: `kind` in play, touch,
   edit, unknown. Place, swap, restore-older and undo are operation kinds linked by
   `op_id`; deleted, reverted, resurrected, returned, replaced, partial and the op outcomes
   confirmed / unexpected are journal events. Reason: a "cloud-revert edge" from the placed
   version to the old one would claim the old version descends from the new one (a false
   parent, and a cycle risk).
2. **Edge fields.** Tiers are recorded / observed / owner (no inferred tier; "seen in backups"
   is observed with method `capture-sequence`); added `method` and `target`; no `confidence`.
3. **Capture.** `where` is a typed `SourceRef` (`LiveRef` / `EntryRef` / `StoreRef`); added
   `time_quality` and `src`. `LiveRef` has an `instance` disambiguator for two same-named live
   folders on one machine (two Proton prefixes, raised by D5).
4. **Notes key.** Version notes are keyed by the pair's head vid (105 heads vs 106 pairs here).
5. **History nodes.** Pair heads plus member-level fork points (a non-head member can be the
   real fork point); nodes without a drawn edge form the dated list.
6. **pid.** Added `split:<raw pid>:<n>` (owner Split) and `unknown:<vid[:16]>`; the legacy
   form is fixed at a version's first registration.
7. **Locator.** `preserve` takes a batch of `LiveMember` (target, member, expected vid) so a
   storage can keep an evicted pair as one entry; `open(vid, prefer)` returns `OpenedVersion`
   with the location read. `Location` always has `slot` and `member` (a store location names the
   entry slot holding the vid) and gained `external`; it has no `prunable` (nothing is pruned).
   The composite `VaultLocator` lives in D1.
8. **Store.** `journal.jsonl` carries op bindings written by `record_op` (idempotent on
   `(op_id, target)`); `ops.jsonl` and `.liveop.lock` are D5's.
9. **Cache.** The per-machine root is the new `platform.user_cache_dir()`; D4's index and D5's
   open-operation markers share `<cache>/vaults/<vault_id>/`.
10. **Facts.** D1 calls the `FactsProvider` subset of D4's `IndexService` (`view`, `changes ->
    ChangeSet | None`, `is_milestone -> bool | None`, `request(vids, Priority)`), not `facts(vid)`.

## Appendix A - prototype and evidence

All work was read-only on real data. Files in the session scratchpad
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\be828316-f5a0-4419-bd50-12964a376ba0\scratchpad\`:

| File | Purpose |
|---|---|
| `D1\r7copy\` | copies of R7's `inventory.json`, `folders.json`, `features\*.json`, `common.py`, `lineage_lib.py` |
| `D1\vaultcopy\oplog.jsonl` | copy of the vault oplog; sha256 `29b3ece9...598aaa` = R1's recorded value (unchanged) |
| `D1\bootstrap_proto.py` | the bootstrap of 2.12 over the copies: captures, registry, oplog resolution, timelines + replay (2.6.3), R7 content check, projection, folding, key filter, lanes, journal size (its meta-inference step predates the owner's decision) |
| `D1\journal_preview.jsonl` | the 196 backfill records the bootstrap would write (account key replaced by a placeholder) |
| `U1\proto_drawn_only.py`, `U1\run_drawn.txt` | the same prototype without inference, `capture-sequence` as observed, plus the tree/dated split, tree-only folding, key and lane counts and the Steam/Xbox play times quoted in 2.8, 2.10.4, 2.10.5 and 2.12 |

Run: `PYTHONUTF8=1 uv run python <scratchpad>\U1\proto_drawn_only.py` from `C:\Devel\NMSVault`
(no third-party package needed; R7's features were already decoded).
