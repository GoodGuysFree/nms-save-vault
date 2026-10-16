# D1 - Core data model of the save-file organizer

Design wave, core design D1 (subjects S1 identity and versions, S3 lineage, S4 notes; decisions
C1-C6, C15-C22, C27-C28). Branch `design/save-organizer`. Binding inputs:
`docs/organizer/01-consolidation.md` (C1-C32, section 6 seams) and research R1-R8. Nothing under
`src/`, `tests/` or `packaging/` was changed; the prototype in Appendix A ran on copies.

Evidence tags: **VERIFIED** = measured by D1 (script named in Appendix A); **SOURCED** = read in
code (`file:line`) or a research report; **INFERRED** = reasoning. Redaction: `st_<steamid64>`,
`<xbox-account>`, `%APPDATA%`, `%LOCALAPPDATA%`. Playthrough ids are shown as 8 hex digits,
version ids as 8 hex digits.

## 0. Owner summary

1. One list of every save file you have (30 today), each with its versions, where it is live (S9, X8), play time, last played and your note; built in about 3 s the first time, under 0.3 s after.
2. From the first release the app keeps a journal of what every live slot holds, so moves, game sessions and cloud reverts become history; your existing backups are turned into history once (tested on copies of your data: all 30 save files connected, both real branch points found).
3. Notes: one living note per save file with its edit history (each new version remembers the note as it was), plus a label, comment and lock per version, and a pin per save file.
4. Your choice (F2): how history looks - recommended: the version table with a narrow branch column and a "key versions / all" toggle.
5. Your choice: show guessed history for old backups right away, marked "guessed" (recommended), or only after content indexing.

## 1. Scope, non-goals, dependencies

**In scope.** Identity (`pid`), versions (`vid`), captures and pairs; the organizer store in
`<vault>/organizer/` (files, schemas, lock, schema versions, `vault_id`); the scanner and its
per-machine cache; the live journal (observation, edges, events, binding rule C16); lineage
assembly (tiers, owner corrections, Combine/Split); inference for history recorded before the
organizer existed; notes (C19-C22); the read model every UI renders (lines, versions, live
board, history graph with lanes); the abstract `VersionLocator`; the one-time bootstrap of an
existing vault; read-only CLI exposure; phase mapping.

**Non-goals.** Storage implementation, `preserve`, pruning and retention (D2/D3, F3, F9);
content extraction, facts and the index cache (D4); live writes, plans, undo and the verification
UI (D5); widgets and mockups (D6+). D1 never writes save data, live folders, entry folders,
`catalog.json` or `oplog.jsonl`.

**Dependencies.**

| Needs | From | Phase |
|---|---|---|
| **P1 fixed**: Xbox meta id u64 @0x118 and save time u32 @0x120 read when the meta is >= 296 B and u32 @0x124 is 2003 or 2004; names and difficulty on 296-B metas; Xbox "newest member" by meta time. Without it Xbox `pid` costs a data decode and the board shows the wrong loaded member in 5 of the 14 live Xbox slots that carry a meta time (R7 2.3). | Phase 0 | 0 |
| P2 / D2 rescue rule (no version lost to snapshot pruning) | D2 | 0-1 |
| `VersionLocator` read side (`locate`, `open`, `is_durable`) | D2 `FolderLocator` | 1 |
| `VersionLocator.preserve` | D2 (folders), D3 (store) | 2 |
| `record_op` calls; verification UI | D5 | 2 |
| `facts`, `view`, `request`, `changes`, `lineage_signals` | D4 | 3 |
| Read-model consumers | UI designs | 1+ |
| New primitives `platform.user_cache_dir(host)`, `safety.exclusive_lock(path)` | this design (2.3, 2.4) | 1 |

## 2. Design

### 2.1 Module layout

```
src/nms_save_vault/core/organizer/
  __init__.py   Organizer facade: open, refresh, model, notes, corrections, bootstrap
  model.py      identifiers and value types (pure; JSON codecs with unknown-key preservation)
  store.py      OrganizerStore: <vault>/organizer/ files, header, lock, compat, atomic IO
  cache.py      ScanCache: per-machine hash cache + entry capture cache
  scanner.py    scan(): captures + version registry from live sources and catalog entries
  journal.py    Journal: observe() appends records; replay() derives edges + events
  recorder.py   LineageRecorder: the D5-facing API (record_op, expected_pair, op_outcome)
  lineage.py    LineageGraph: tier precedence, corrections, Combine/Split, effective pid
  infer.py      classifiers (meta, content) + parent inference for roots         [phase 4]
  notes.py      save-file notes with history, version label/comment/lock, pins
  readmodel.py  Line, VersionRow, LiveBoard, HistoryGraph, sorting, filters, lanes
  locator.py    VersionLocator ABC, Location, OpenedVersion, LiveMember
  bootstrap.py  one-time pass over an existing vault + report
  folders.py    D2's FolderLocator (D2 design)
```

The package is platform-neutral (no OS branches). It uses `savedir`, `msstore`, `catalog`,
`safety`, `aliases`, `lz4_block` and `playtime._id_from_bytes`; core modules do not import it,
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
| no id anywhere, meta readable | `legacy:<platform>:<slot>:<name or <unnamed>>` | `legacy` |
| meta unreadable and no data id | `unknown:<vid[:16]>` | `unknown` |
| owner Split (2.7.4) | `split:<raw pid>:<n>` | `owner` |

- A vid's pid is fixed when the vid is first registered (its earliest observation). Later
  captures of the same bytes in another slot reuse it, so a legacy save keeps its line after a
  move. Two legacy pids are *compatible* (2.6.3) when platform and name match; a child adopts
  its parent's pid.
- Consistency: when D4 reports `pid_in_data` for a vid and it differs from a meta-derived pid,
  the registry uses the data id and the scan reports an `id-mismatch` problem (none exist in the
  owner's data: Steam 997/997, Xbox 224/224 equal; R7 2.2, D5 meta survey).
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
class StoreRef:         # a D3 version-store object
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
    @property
    def pair(self) -> "PairKey": ...
    @property
    def head(self) -> MemberObs | None: ...

PairKey = tuple[Vid | None, Vid | None]     # (A, B); string form "<A or ->/<B or ->"
```

**Head rule** (the member the game loads, the label of a pair): the present member with the
highest `(meta_ts, play_time)`; a full tie picks A. This matches `SlotView.newest`, which keeps
the first maximum (SOURCED `savedir.py:110-116`); it differs from today's Xbox order only
until P1 is fixed (index time vs meta time, R7 2.3).

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
class RawLocation:      # where the scanner saw a vid; D2/D3 classify durability (2.11)
    where: SourceRef
    slot: int
    member: Member
    entry_kind: Literal["live", "full", "snapshot", "extract", "imported", "inplace"]
    managed: bool       # catalog entry flag; False for live and in-place
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

EdgeTier = Literal["recorded", "observed", "inferred", "owner"]
EdgeKind = Literal["play", "touch", "edit", "cross-save", "unknown"]
EdgeMethod = Literal["op", "oplog", "journal", "capture-sequence",
                     "classifier-meta", "classifier-content", "owner"]

@dataclass(frozen=True)
class Edge:             # game-state descent: child's state was produced from parent's state
    parent: Vid
    child: Vid
    tier: EdgeTier
    kind: EdgeKind
    method: EdgeMethod
    confidence: Literal["high", "medium", "low"]
    op_id: str | None   # the operation that put `parent` into the slot (binding), if any
    created: int        # UTC unix of the evidence (observation or op time; inference time)
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
("edited outside the game", R7 2.6 row 6); `cross-save` when the classifier links versions of
different platforms; `unknown` otherwise (owner corrections only). Place, swap, restore-older and
undo are **operation kinds** reached through `op_id`; deletions and cloud reverts are **journal
events**, not edges (Deviations, item 1).

### 2.3 The organizer store `<vault>/organizer/`

| File | Owner | Holds | Write pattern | Size, owner's vault | Size at ~2,000 versions |
|---|---|---|---|---|---|
| `organizer.json` | D1 | header: schema, `vault_id`, created, app | atomic replace, once | < 1 KB | < 1 KB |
| `journal.jsonl` | D1 | live-slot observations and op bindings (2.6) | single fsynced append under the lock | 98 KiB after bootstrap (VERIFIED) | ~1.1 MiB, + ~0.4 MiB/year of play (INFERRED: ~600 changed slots/year x ~0.6 KB) |
| `notes.json` | D1 | save-file notes + history, version notes, locks, pins (2.9) | atomic replace | 0 | 100-300 KB |
| `lineage.json` | D1 | owner corrections, Combine aliases, Splits, bootstrap report (2.7) | atomic replace | ~3 KB | < 50 KB |
| `ops.jsonl` | D5 | operation intent/completion log | D5's design | - | - |
| `storage.json` | D2 | manifests, roles, retention | D2's design | - | - |
| `.lock` | D1 | OS lock target, never rewritten | - | 0 B | 0 B |

Other designs may add files; every file in this folder follows the conventions below and is
written only while holding the store lock.

**Conventions.**
- UTF-8, `json.dumps(..., ensure_ascii=True, separators=(",", ":"))` for JSONL and `indent=1`
  for JSON files; files are pure ASCII on disk, so no reader can mis-decode them (DESIGN.md
  encoding trap; notes stay Unicode through `\u` escapes, C22).
- Every JSON file starts with `{"schema": N, "kind": "nmsvault.organizer.<name>",
  "read_compat": R, "write_compat": W, ...}`. A reader built for schema S reads the file when
  `R <= S` and writes it only when `W <= S`; otherwise the organizer opens read-only with a
  plain message ("This vault's organizer data was written by a newer version"). Additive fields
  never bump `R` or `W`.
- Unknown keys survive a rewrite: every record type decodes known fields and keeps the rest in
  an `_extra` dict that `to_json()` merges back; unknown top-level keys and unknown JSONL record
  types (`"t"`) are preserved verbatim. This is the fix for the catalog v1 trap where an older
  app crashes on an unknown member key (SOURCED `catalog.py:133`, R1 2a A2).
- Atomic replace = `safety.atomic_write_bytes` (temp in the same folder, fsync, `os.replace`;
  SOURCED `safety.py:53-66`), after first copying the current file to `<name>.prev` (one
  generation, for manual recovery).
- A file that fails to parse is never overwritten: the store opens read-only and reports the
  file and the error.
- All times are UTC unix seconds (int). No absolute paths are stored anywhere (D11); entries are
  referenced by id, live folders by `LiveRef`.

**`organizer.json`**:

```json
{"schema": 1, "kind": "nmsvault.organizer", "read_compat": 1, "write_compat": 1,
 "vault_id": "9c1e0a52d3b84c0f9d6a1b2c3d4e5f60", "created": 1792137600,
 "app": "nms-save-vault 0.3.0"}
```

`vault_id` = `uuid.uuid4().hex`, written once when the store is created. It keys the
per-machine caches (2.4). A copied vault keeps the same id; the caches stay correct because their
entries are keyed by absolute path or by content hash.

**Lock.** `<vault>/organizer/.lock`, held only for a read-modify-write transaction (milliseconds),
never for the life of the app.

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
holder leaves a lock). A per-process `threading.RLock` makes nested transactions in one process
re-entrant.

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

**Read-only mode** (newer schema, unparseable file, read-only medium, `LockTimeout`): everything
is shown, nothing is recorded, and the UI shows one line saying why.

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
    entries: Mapping[str, "EntryScan"]           # per catalog entry: captures, kind, managed, created
    captures: tuple[Capture, ...]
    versions: Mapping[Vid, VersionInfo]          # registry (journal metas merged by the facade)
    pairs: Mapping[PairKey, PairInfo]
    problems: tuple[ScanProblem, ...]
    game_running: bool | None
    timings: Mapping[str, float]

def scan(vault: Vault, sources: Sequence[Source], cache: ScanCache, *,
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
2. **Entries** = `vault.entries`. Resolve the folder (D11: relative for managed entries once the
   catalog supports it, absolute for in-place). An entry whose resolved path equals a live
   source's path or its `xgs` twin is `entry-is-live` and skipped (P9: the owner's
   `inplace-20260624-182102` is the live wgs folder; counting it double-counts every Xbox save,
   R7 2.1). Whole-folder kinds (`full`, `snapshot`, `inplace`, `imported`) yield 15 captures;
   an `extract` yields its own slot only. Fingerprint first (2.4); rescan only on change.
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
   `datetime.fromisoformat(s).astimezone(timezone.utc)`; SOURCED `catalog.py:240`), in-place
   entries use the newest meta time inside, `time_quality="estimated"`.
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
        "bv": 4220, "fmt": 2004, "dsize": 18812345, "size": 8812345}},
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
 "preserved": {"f073ea37...": {"kind": "entry", "entry": "extract-steam-20261016-080054",
                               "slot": 9, "member": "B"}},
 "status": "done", "undo_of": null, "note": "", "app": "nms-save-vault 0.4.0"}
```

`src`: `app` (D5 or the phase-1 hook) or `oplog` (bootstrap, from a legacy `oplog.jsonl`
line; `op_id` = `legacy-<line index>`). `kind` for app ops is D5's (`place`,
`place_into_empty`, `restore_older`, `swap`, `evict`, `undo`, `promote`); legacy ops map
`repopulate_slot` -> `repopulate`, `clear_slot` -> `clear`, `restore_full`, `undo`
(`promote_member` changes only the meta, so it binds the same pair).

#### 2.6.2 Observing a scan

```python
@dataclass(frozen=True)
class ObserveResult:
    written: int                         # records appended
    events: tuple[JournalEvent, ...]     # derived for the new records (for UI notices / D5)
    skipped: tuple[tuple[LiveTarget, str], ...]   # (target, reason): unstable, game running, unreadable

class Journal:
    def __init__(self, store: OrganizerStore, registry: Mapping[Vid, VersionInfo]): ...
    def observe(self, scan: ScanResult, *, src: Literal["scan", "verify"] = "scan") -> ObserveResult:
        """Under the store lock: re-read the journal tail, and for every live target whose pair
        differs from its last record append an obs record. Targets of an unreadable folder, and
        targets with an unstable member, are skipped (never recorded as 'deleted')."""
    def replay(self) -> "JournalState":
        """Pure function of the records + registry: per-target state, edges, events, op outcomes."""
```

Who calls `observe`: the GUI on start and on every refresh; D5 before an operation (pre-flight:
the evicted state is journaled first) and after it (`src="verify"`); `nmsvault scan`; the
phase-1 hook (2.6.7). Read-only CLI listings do not (2.13, option O-D1-3).

#### 2.6.3 Replay algorithm (edges and events)

Replay runs per target over its records sorted by `(at, seq)`. `first_seen(v)` is the smallest
`ObsOrder` at which `v` appears anywhere: journal records, or entry captures at
`(created, -1)`. `compatible(p, n)` is `pid(p) == pid(n)` or both legacy with the same platform
and name.

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
    if state.src == "op" and set(cur) == set(state.evicted) and set(cur) != set(state.vids):
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
              and play_time(c) <= play_time(n) and meta_ts(c) <= meta_ts(n)]
        if ok:
            p = max(ok, key=lambda c: (play_time(c), meta_ts(c), cands.index(c)))
            if state.src == "op" and p in state.vids:
                method = "op" if state.op.src == "app" else "oplog"
                edge(p, n, tier="recorded", method=method, op_id=state.op.op_id)
                made_from_placed = True
            elif rec.src == "backfill":
                edge(p, n, tier="inferred", method="capture-sequence")
            else:
                edge(p, n, tier="observed", method="journal")
        elif any(not compatible(c, n) for c in cands):
            emit replaced(vids=[n])                # a different save entered the slot outside the app
        cands.append(n)                            # later new members may descend from n
    if state.src == "op":
        if set(cur) == set(state.vids) or made_from_placed: emit confirmed(op_id=state.op.op_id)
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

Confidence: `recorded` and `observed` edges are high. `capture-sequence` edges are high when
both observations have exact times, medium otherwise.

#### 2.6.4 Tiers, kinds and how each edge arises

| Tier | Method | Arises when | Example (owner's data) |
|---|---|---|---|
| recorded | `op` | the first new members seen in a slot after an app operation placed a pair there; parent = the placed member | (from phase 2 on) |
| recorded | `oplog` | same, the operation being a resolved legacy `oplog.jsonl` line (bootstrap) | Main Save 9e4b2f00 -> 54cfd111 after the 2026-10-05 17:49 repopulate |
| observed | `journal` | consecutive live observations of one slot by the new app | (from phase 1 on) |
| inferred | `capture-sequence` | consecutive app-made backups/snapshots of one live slot (bootstrap backfill) | 130 edges |
| inferred | `classifier-meta` / `classifier-content` | a version still without a parent after the above (2.8) | 30 edges, 4 cross-save |
| owner | `owner` | the owner's "set parent" | - |

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
    def observe(self, scan: ScanResult, *, src: Literal["scan", "verify"]) -> ObserveResult: ...
    def expected_pair(self, target: LiveTarget) -> PairKey | None:
        """The placed pair of the newest done op on `target` that no later op superseded."""
    def op_outcome(self, op_id: str, target: LiveTarget) -> Literal[
            "pending", "confirmed", "reverted", "superseded", "unexpected", "unknown"]:
        """From replay: pending = no observation since the op; confirmed = the next observation
        holds the placed pair or a recorded child of it; reverted = it holds the evicted pair;
        superseded = another op on the target came first; unexpected = anything else (deleted,
        replaced, another version); unknown = no such op record."""

@dataclass(frozen=True)
class OpLineage:
    op_id: str                 # "op-<UTC yyyymmddThhmmssZ>-<4 hex>" (section 6)
    kind: Literal["place", "place_into_empty", "restore_older", "swap", "evict", "undo", "promote"]
    at: int
    target: LiveTarget
    placed: PairKey            # (None, None) for evict
    evicted: PairKey | None
    source: "Location | None"  # where the placed bytes came from
    preserved: Mapping[Vid, "Location"]   # evicted vid -> durable location (D2/D3 preserve)
    status: Literal["done", "failed", "rolled_back"]
    undo_of: str | None = None
    note: str = ""
    app: str = ""
```

D5 owns the verification state and messages; D1 only answers from the records. The owner's
history shows exactly the three cloud cases R7 found as `reverted` events (2.12).

#### 2.6.7 Recording before D5 ships (C18)

Phase 1 adds one call after each successful live write of today's operations
(`repopulate_slot`, `restore_full`, `clear_slot`, `promote_member`, `undo_last`): a post-write
scan of the written folder, then `record_op` per written slot with `src="app"`, the legacy kind,
the written pair and, as `evicted`, the journal's previous state. A failure to record is logged
as a warning and never fails the operation. D5 replaces these calls in phase 2 (coordinate: the
hook touches `operations.py`, which D5 owns).

### 2.7 Lineage assembly

#### 2.7.1 Precedence

`LineageGraph.build(journal_state, lineage_state, registry, inferred)` gives each vid at most one
parent:

1. an owner `set_parent(child, parent | None)` (None = "this version has no known parent");
2. else the journal's edge of the highest tier: recorded > observed > inferred
   (capture-sequence); within one tier the later evidence wins; every conflict is listed in
   `LineageGraph.conflicts` (0 on the owner's data);
3. `not_related(a, b)` drops any edge between `a` and `b` from step 2 and from inference;
4. a vid still without a parent goes to inference (2.8, phase 4).

An edge that would close a cycle (only owner corrections can) is dropped and reported.

#### 2.7.2 `lineage.json`

```json
{"schema": 1, "kind": "nmsvault.organizer.lineage", "read_compat": 1, "write_compat": 1,
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
  (so the journal carries a split-off twin forward); else `canonical(raw_pid)`. Inferred edges do
  not carry a split. A line's history shows a link to the other line where an edge crosses
  (`CrossLink`). `unsplit(split_pid)` deletes the entry.

Inference runs on canonical raw pids (aliases applied, splits not), so Split never changes which
edges are guessed.

#### 2.7.5 Protection reasons (for D2/D3 pruning, C27)

```python
def protection(vid: Vid) -> frozenset[Literal["live", "locked", "noted", "tip", "fork", "placed", "evicted"]]
```

The partner of a protected head gets the same reasons. D2/D3 decide what each reason allows.

### 2.8 Inference for history before the organizer (phase 4)

**Meta classifier** `meta_descends(x, y)`: same canonical line, `x.play_time <= y.play_time`,
`x.meta_ts <= y.meta_ts`, not `not_related`.

**Content classifier** `content_descends(x, y)` (R7 2.7, 179/179 known ancestor pairs, no false
fork) with D4's `LineageSignals`; for each context present in both:
- the expedition context counts as *replaced* when its `season_number` differs, or when its
  `time` went down while the base context's `time` did not; replaced contexts are skipped;
- in every shared, non-replaced context none of the 31 core counters is lower in `y`
  (tolerance 1e-6; a missing stat is 0);
- with `X = visited_x[:-1]` (the last entry is the current, mutable system) and `n = len(X)`,
  some `d` in `[0, n)` has `X[d:] == visited_y[:n-d]` (true when `n <= 1`).
If no context is shared, content says nothing and the meta result stands.

**Signals D4 must extract per vid** (`index.lineage_signals(vid)`, aligned with D4's draft):
`pid_in_data` (`'0x%016X'` or None), `play_time_s`, `active_context` (`Main` / `Season` / None),
and per context `base` / `expedition` when present: `core` = exactly these 31 global-stat ids
(without `^`; VERIFIED list, `core_counters.json`): AMMO_FIRED, APP_SESSIONS, COMM_09_DAY,
DISC_PLANETS, DISC_RARE_SYS, DISC_SYSTEMS, DIST_FLY, DIST_JETPACK, DIST_PULSE, DIST_WALKED,
DIST_WARP, ESEEN_SYSTEMS, EXTREME_WALK, ITEMS_TELEPRT, LONGEST_LIFE, MONEY_EVER, MP_FULL_COUNT,
MP_FULL_TIME, MP_SESSIONS, NANITES_EVER, PARTS_PLACED, PLAY_SESSIONS, PORTAL_WARPS,
RES_EXTRACTED, SHIP_SUMMON, STORM_WALK, TIME, TIMES_IN_SPACE, TSEEN_SYSTEMS, VISIT_PLANETS,
WSEEN_SYSTEMS; `time` (the TIME stat); `visited` (VisitedSystems as u64 in file order, <= 512,
last entry included); `season_number`. Optional, display only: `newest_disc_ts` (newest
discovery record owned by the player), which bounds an uncaptured fork from below (R7 2.7,
Low-Medium).

**Parent choice** for a vid `y` with no parent: candidates are the versions `x` of the same
canonical line with `order(x) < order(y)`, `x` not a descendant of `y`, accepted by the content
classifier when both have signals, else by the meta classifier. Parent = the candidate with the
largest `(play_time, meta_ts, first_seen)` - "the most-progressed earlier version" (R7 2.7,
64/64 consistent). Kind `cross-save` when the platforms differ. Confidence: content -> high;
meta -> medium; meta with equal play time -> low (an edit cannot be oriented; R7: 13 of 179
reversed pairs accepted, exactly the editor sessions).

Inferred edges are derived on every refresh, never persisted: the inputs (registry, journal
metas, signals) are durable, the algorithm is deterministic, and a better classifier can then
improve old history. Cost: under 1 ms on the owner's data, < 20 ms at 2,000 versions (INFERRED
from the prototype).

### 2.9 Notes (C19-C22)

`notes.json`:

```json
{"schema": 1, "kind": "nmsvault.organizer.notes", "read_compat": 1, "write_compat": 1,
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
  `comment` (<= 4,000), `locked` (exempt from any pruning; both members of `pair` are protected).
- **Snapshot of the save-file note at capture time**: the journal's `note_revs` (2.6.1). A
  version's note snapshot = the revision recorded when the version was first journaled;
  "what changed in the note" = text diff between the snapshots of a version and its parent.
  Versions known only from bootstrap have none.
- Automatic description (C19, C14): when label and comment are empty the UI shows D4's
  `changes(child, parent)` for the lineage parent.
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
    def locked_vids(self) -> frozenset[Vid]      # heads and partners, for D2/D3
```

### 2.10 The read model

All read-model objects are frozen dataclasses with `to_dict()` (JSON-safe for a web UI: tuples
become lists, frozensets sorted lists). Labels the model builds are ASCII; names, summaries and
notes are user/game text. No raw account key, entry path or absolute path appears: live sources
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
    def timeline(self, pid: Pid) -> tuple["TimelineItem", ...]       # F2 option A
    def resolve(self, ref: str) -> Pid | Vid | LiveTarget | None     # CLI/UI references (2.13)
    def token(self, target: LiveTarget) -> str
    def target_of(self, token: str) -> LiveTarget | None

class FactsProvider(Protocol):          # implemented by D4's index (names follow D4's draft)
    def view(self, vid: Vid) -> object | None              # FactsView; None = not indexed yet
    def lineage_signals(self, vid: Vid) -> "LineageSignals | None"
    def request(self, vids: Sequence[Vid], priority: int) -> None
    def changes(self, child: Vid, parent: Vid) -> list["Change"]
    def is_milestone(self, child: Vid, parent: Vid) -> bool
    def sort_value(self, view: object, name: str) -> int | float | str | None   # for facts:<name>
```

`build` asks D4 to index every line's representative version first (`priority=0`), then the
other heads (`priority=1`); it never waits for the index.

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
    is_tip: bool            # the live head has no later version anywhere
    behind_by: int          # play seconds behind the line's most-progressed version (0 = newest)

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
    cross_platform: Literal["n/a", "same", "steam-ahead", "xbox-ahead", "diverged", "unknown"]
    play_time: int                      # max over versions
    last_played: int                    # max meta_ts
    version_count: int                  # distinct pairs (C6), versions with bytes
    gone_count: int                     # versions known only from the journal
    branch_count: int                   # tips
    note_preview: str                   # first non-empty line of the save-file note, <= 80 chars
    has_note: bool
    pinned: bool
    locked_count: int
    flags: frozenset[str]
    representative: Vid                 # the version whose facts stand for the line
    facts: object | None                # D4 FactsView of `representative` (phase 3), opaque to D1
```

- Representative = the most-progressed live head; else the newest tip by `(meta_ts, play_time)`.
- `cross_platform`: when both platforms have a live head, compare the most-progressed live head
  of each: Xbox head an ancestor of the Steam head -> `steam-ahead`; the reverse ->
  `xbox-ahead`; equal play time and time -> `same`; neither -> `diverged`; either head without a
  known parent chain -> `unknown`. With one platform live: `n/a`. Owner's data (VERIFIED): The
  Cartographers and Exobiology1-600qs `steam-ahead`, Creative Primary `diverged` (R7 2.6, same).
- Line flags: `live`, `cross-platform`, `diverged`, `twin` (same pid live in two slots of one
  platform), `at-risk` (a live pair without a durable vault copy, or a version whose only vault
  copies are prunable), `edited` (a version edited outside the game), `expedition`,
  `legacy-id`, `pinned`, `has-note`, `unreadable`.

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
    kind: Literal["live", "durable", "prunable", "external", "store"]
    label: str              # "Live: Steam slot 9", "Vault: backup 2026-09-17",
                            # "Vault: auto snapshot 2026-09-20 (may be pruned)", "Outside vault: <folder>"
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
    parent: Vid | None      # display parent (2.10.4)
    parent_tier: EdgeTier | None
    parent_kind: EdgeKind | None
    parent_confidence: str | None
    children: int
    label: str
    comment: str
    locked: bool
    note_rev: int | None    # save-file note revision when first journaled
    folded_into: Vid | None # set when this row is folded (default mode)
    facts: object | None
```

Version flags: `live` (head of a live pair), `live-partner`, `tip`, `fork`, `root`, `touch`
(parent edge kind touch), `edited` (either member's edge is `edit`: "edited outside the game"),
`only-live` (every copy is live), `only-snapshot` (vault copies exist but all prunable, not
live), `outside-only` (copies only in in-place folders, maybe live), `gone` (no bytes left),
`locked`, `noted`, `placed` / `evicted` (a slot-level operation placed or evicted it; 2.10.4),
`cross-save`,
`inferred` (parent edge inferred), `unreadable`.

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
    vault_copy: Literal["durable", "partial", "prunable-only", "outside-vault", "none"]
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

`vault_copy` is per pair: `durable` when every present member has a durable location (D2's
definition: managed full/extract/imported or store, verified); `partial` when only some do.
`suggest_rank` is the generic order (empty slots, then unpinned slots whose pair is durable,
least recently played first, then other unpinned, pinned never); D5's eviction picker adds the
"same save file first" rule for a given source. On the owner's data 8 of the 29 occupied live
slots hold a pair with no copy anywhere outside live (16 members; R7: 16 live-only versions).

#### 2.10.4 HistoryGraph (projection, folding, key versions)

Nodes and edges come from the member-level graph (2.7) of one effective line:

1. **Display nodes** = every pair head of the line, plus every member-level **fork point** that
   is not a head. The second set matters: Creative Primary's true fork is the Xbox restore point
   B `13981763`, which is never the loaded member of any capture (VERIFIED). Versions that are
   neither are shown inside their pair's row as the partner.
2. **Display parent** of a node = its nearest proper ancestor (member graph) that is a display
   node. Display edges carry the tier, kind and confidence of the member edge into the child
   and `cross_platform = parent.platform != child.platform`.
3. **Folding** (`mode="default"`, C5): a node whose display edge kind is `touch` or `edit`, with
   at most one child, that is not live, not a fork, not noted/locked and not placed/evicted, is
   folded into its parent: the parent gets `folded: {"touch": n, "edit": m}` and the child's
   edge gets `hidden += 1`. `mode="all"` folds nothing. Owner's data: 10 of 106 nodes fold
   (VERIFIED).
4. **Key versions** (`mode="key"`): a node is key if it is live (head of a live pair), a tip,
   a fork, a root, noted or locked, placed or evicted, the first node after a platform change,
   the first node with a new non-empty name, or D4's `is_milestone(child, parent)` says so
   (phase 3). Other nodes collapse; the edge between two key nodes carries `hidden` = the
   number of versions in between. Owner's data: 60 of 106 nodes are key; Main Save shows 4 of
   20, Creative Primary 4 of 12 (VERIFIED, Appendix A).

"Placed or evicted" (rules 3 and 4, and the `placed`/`evicted` flags) means the head of a pair
that a slot-level operation put into a slot or took out of it: D5's place, place into empty,
restore older, swap, evict and undo, and the legacy repopulate and clear. Whole-folder restores
(legacy `restore_full` and `undo`) bind slots in the journal but do not mark versions; on the
owner's data 7 heads are placed or evicted.

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
    confidence: str
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
    nodes: tuple[GraphNode, ...]          # in row order
    edges: tuple[GraphEdge, ...]
    lanes: int
    pass_through: tuple[tuple[int, ...], ...]   # per row: lanes with a line passing (no node)
    links: tuple[CrossLink, ...]
```

#### 2.10.5 Lane assignment (every UI draws the same tree)

Input: the display nodes `N` of the chosen mode (after folding or collapsing) and their display
parents.

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

Owner's data: 28 lines need one lane, 2 need two (VERIFIED). ASCII rendering as the CLI would
print it (`*` node, `|` lane passing, `/` connector into the parent's lane; times UTC):

```text
Main Save (S2), mode "all": 20 nodes, 2 lanes
*    2026-10-15 20:39  532.3h  S  f073ea37  live S9, tip
| *  2026-10-05 14:48  530.9h  S  b9c8b750  tip (dead end)
*/   2026-10-05 14:13  530.9h  S  9e4b2f00  fork, placed 2026-10-05
*    2026-09-26 20:48  530.3h  S  4528014e
...  15 more versions
*    2026-03-30 14:33  506.4h  S  89eb6181  root

Main Save (S2), mode "key": 4 of 20
*    2026-10-15 20:39  532.3h  S  f073ea37  live S9, tip
| *  2026-10-05 14:48  530.9h  S  b9c8b750  tip
*/   2026-10-05 14:13  530.9h  S  9e4b2f00  fork, placed
:    16 versions hidden
*    2026-03-30 14:33  506.4h  S  89eb6181  root

Creative Primary, mode "all": 12 nodes, 2 lanes
*    2026-10-08 13:23   45.2h  S  e414de0e  live S13, tip
...  6 more Steam versions
| *  2026-07-16 05:07   38.7h  X  e70091c0  live X8, tip (touch)
* |  2026-06-30 18:16   40.8h  S  d4e1fedc
* |  2026-06-27 20:36   39.5h  S  d642b721
* |  2026-03-04 16:27   39.1h  S  cb14a141  first Steam version (cross-save)
*/   2026-02-14 14:57   38.7h  X  13981763  fork, root (Xbox restore point)
```

#### 2.10.6 Timeline (F2 option A)

`timeline(pid)` = every pair of the line by `saved_at` descending, interleaved with the line's op
records ("Older version 2026-10-05 put live in Steam slot 9", "Moved to the vault from Steam
slot 3") and journal events (reverted, deleted, returned). Each item whose display parent is
not the next older item carries `continues_from: Vid` - a branch shown as text, without lanes.

### 2.11 The VersionLocator (C28)

```python
@dataclass(frozen=True)
class Location:
    vid: Vid
    kind: Literal["live", "entry", "store"]
    ref: SourceRef
    slot: int | None          # live / entry slot; None for a store object
    member: Member | None
    platform: Platform
    durable: bool             # counts as a kept vault copy (D2/D3 decide; live never durable)
    prunable: bool            # may be removed by retention (snapshots under keep-N)
    external: bool = False    # outside the vault (in-place folder): kept by the owner, not by us

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
        """Every known location, durable first, then prunable, external, live. In-memory."""
    @abstractmethod
    def open(self, vid: Vid, prefer: Location | None = None) -> OpenedVersion:
        """Read the bytes (prefer the given location, else the first durable one), verify
        sha256 == vid, decrypt the meta. Raises VersionUnavailable."""
    @abstractmethod
    def preserve(self, members: Sequence[LiveMember], *, op_id: str, reason: str) -> list[Location]:
        """Make each member durable in the vault (store if absent, then re-read and verify the
        hash); never a prunable snapshot. One returned location per member, same order.
        Raises PreserveFailed; D5 refuses the live write then (phase 2)."""
    @abstractmethod
    def is_durable(self, vid: Vid) -> bool:
        """True when at least one durable, verified vault copy exists."""
```

D2's `FolderLocator` (`core/organizer/folders.py`) implements it over D1's `ScanResult`
locations plus its manifests; D3's store locator adds store objects. Phase 1 needs only
`locate`, `open` and `is_durable`. A shared conformance test module (section 5, row 10) runs
against every implementation.

### 2.12 Bootstrap of an existing vault

Runs once, automatically, the first time the organizer opens a writable vault (or by
`nmsvault organizer bootstrap`), in a worker thread, with progress.

1. `scan(..., deep=True)` - hashes every copy.
2. Map app-made whole-folder entries (`full`, `snapshot`, managed) to live sources: the entry's
   `source` (live folder name) must equal a live source's `account_key` on the entry's platform
   (from the id tag `full-steam-` / `snapshot-xbox-`, else `savedir.platform_of`); with a single
   live source per platform a missing match maps to it with a note; otherwise the entry is
   `entry-unmapped`. In-place, imported and extract entries are not timeline evidence: they are
   owner copies of unknown time, possibly partial (the prototype showed they produce false
   "deleted" and "resurrected" events), and their versions get parents by inference.
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
6. Replay -> edges and events; phase 4 adds inference for roots.

Idempotent: a second run with `bootstrap.done_at` set only appends backfill for entries that are
not yet represented (a later `import_vault` of old backups), sorted into place by `(at, seq)`.
`--dry-run` computes everything in memory and writes nothing.

**Expected results on the owner's data** (VERIFIED by the prototype on copies, Appendix A):

| Measure | Result |
|---|---|
| Folders | 52 found; live wgs alias and `xgs` mirror skipped; 1,177 member copies |
| Versions / pairs / heads / lines | 195 / 106 / 105 / 30 (pid from meta 193 vids, legacy 2) |
| Journal records written | 196 (96 obs, 100 op placements from 16 oplog ops), 98 KiB, parse 0.7 ms |
| Oplog | 42 lines: 7 repopulate, 3 clear, 6 restore/undo resolved; 0 unresolved |
| Edges, phase 1 (no inference) | 135: 130 capture-sequence, 5 recorded via oplog; 60 roots |
| Edges, phase 4 (+ meta inference) | 165 = 195 - 30 roots, one tree per line; 30 inferred, 4 of them cross-save |
| Edge kinds | play 139, touch 15, edit 7, cross-save 4 |
| Content check | all 165 edges also pass R7's content classifier |
| Events | 3 reverted (clear S15 2026-09-17, clear S3 2026-09-19, first repopulate S9 2026-10-05: R7's three cloud cases), 2 deleted (S1 2026-09-20 unrecorded, S1 by 2026-09-27) |
| Forks | Main Save `9e4b2f00` -> live `54cfd111`/`f073ea37` (recorded) + dead end `b9c8b750` (phase 1); Creative Primary Xbox B `13981763` -> Steam branch (cross-save) + Xbox `e70091c0` (phase 4) |
| Cross-platform | 2 steam-ahead, 1 diverged |
| At risk | 8 live slots with no copy (16 members); 9 versions whose only vault copies are snapshots |

**Report** (`BootstrapReport`, shown once in the GUI and printed by the CLI):

```python
@dataclass(frozen=True)
class BootstrapReport:
    folders_scanned: int; entries_skipped: tuple[tuple[str, str], ...]   # (entry id, reason)
    copies_hashed: int; versions: int; pairs: int; lines: int; legacy_lines: int
    journal_records: int; ops_resolved: int; ops_unresolved: tuple[tuple[str, str], ...]
    edges_by_method: Mapping[str, int]; roots: int; forks: tuple[tuple[str, str], ...]  # (line name, date)
    events: Mapping[str, int]; at_risk_live: int; at_risk_snapshot_only: int
    problems: tuple[ScanProblem, ...]; seconds: float
```

GUI wording example (phase 1): "Built the history of your 30 save files from 33 backups: 135
links from your backups and the app's log, 1 branch point. 8 live saves have no copy in the
vault." Phase 4 adds: "30 more links guessed from save times and play time, 1 more branch point."

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

1. D1 never writes save data, live folders, entry folders, `catalog.json` or `oplog.jsonl`.
2. Showing things writes nothing in the vault: scan, read model and listing commands touch only
   the per-machine cache.
3. Every store write holds the lock and is an atomic replace or one fsynced append; a file that
   does not parse is never overwritten.
4. Unknown keys, files and record types are preserved; a newer schema is never written by an
   older reader (`write_compat`).
5. The journal records only validated live members and nothing while the game runs; an
   unreadable folder is never recorded as deleted.
6. Every string built from an account key, entry id or path goes through `aliases.redact`
   before display; the stores hold no absolute paths.
7. An organizer failure never blocks the classic app: the organizer view shows the error,
   backup/restore keep working.
8. Inferred edges are always marked as such (tier and confidence travel to every view).

### 2.15 Performance budget

| Step | Owner's vault (source) | ~2,000 versions (INFERRED) |
|---|---|---|
| Cold scan, `scan_any` over 52 folders | 0.98 s (R1 2h) | ~3 s |
| Hash every copy (first run on a machine) | 1.24-2.65 s for 1,177-1,237 copies (R7 2.9, R1 2h) | ~5-8 s |
| Warm refresh: live scans + changed live hashes + 49 entry fingerprints | < 0.2 s (live Steam 18-21 ms, wgs 28-43 ms, R1 2h; live hashing < 0.1 s, R7 2.8) | < 0.5 s |
| Journal parse + replay | 0.7 ms parse (VERIFIED) | ~10 ms + ~10 ms replay |
| Inference (meta / content) for roots | < 1 ms | < 20 ms |
| Read model + history + lanes, all lines | 0.8 ms graph + lanes (VERIFIED) | < 50 ms |
| Bootstrap | ~3-4 s, nearly all scan and hash | ~10 s |
| Data-id fallback (legacy / pre-P1 Xbox only) | ~23 ms per version, once (R7 1) | same |

Targets the implementation must meet: first organizer open <= 5 s on the owner's vault with
progress shown; warm refresh <= 0.3 s; nothing above 50 ms on the Tk thread.

## 3. Options

### F2 - History view style (the owner's choice)

All three are views over the same `HistoryGraph` / `timeline` data; the model ships all three,
the UI design picks which to draw.

| | A Linear timeline | B Lane graph | C Hybrid (recommended) |
|---|---|---|---|
| Data | `timeline(pid)`: versions by time + op/journal events; `continues_from` on branch starts | `history(pid, mode)`: nodes with `(row, lane)`, edges, `pass_through` | `versions(pid)` rows joined with `history(pid, mode)` lanes: each row gets `lane`, `lanes`, `pass_through[row]`; toggle `key` / `default` / `all` |
| Owner sees | a list like a game load menu; a branch is a line of text "continues from 2026-10-05" | a git-like tree, one lane per branch, newest on top | the version table he already wants, with a narrow lane column (1-2 lanes here) and the same columns as the line view |
| Strengths | simplest to draw (any list widget), reads like "what happened when" | branches are visible at a glance | one table for "versions" and "history"; scales to 37+ versions with the key toggle |
| Weaknesses | a tree is hard to follow when both branches progress (what the organizer will make common) | separate view; needs a canvas/SVG; labels compete with lines | lane column needs care at large zoom; still a canvas or per-row glyphs |
| Cost | S | M | M |

Recommendation **C** (parent's lean): the owner's data has 1-2 lanes per line, so a narrow
column carries the branches without a second view, and the key toggle (Main Save: 4 of 20)
answers "where did this branch come from" quickly.

### O-D1-1 - Guessed history for backups made before the organizer (owner)

- (a) **Show it now, marked "guessed"** (recommended): meta-only inference (2.8) as soon as
  phase 4 ships; it gave the same 30 parents as the content classifier on this vault, and the
  content classifier upgrades the confidence once D4 indexes.
- (b) Only after content indexing: no meta guesses; lines stay a flat list until D4's signals
  exist for both ends of a guess.
- (c) Never guess: history starts with the backfill and the journal; older versions are roots.

### O-D1-2 - What a pin is attached to (owner; default chosen)

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
  first organizer write (bootstrap). Without it the organizer still shows lines and the board
  from a scan, read-only.
- **Older app versions** (up to today's v0.2.8) never look inside `organizer/`: `looks_like_vault_dir`
  checks only `catalog.json`, `Vault.ensure` creates only `backups/`, `snapshots/`, `extracts/`,
  and pruning removes only snapshot folders (SOURCED `catalog.py:163-165, 248-252`,
  `operations.py:148-159`). Their live writes are not journaled; the new app sees the result at
  its next scan as `observed` changes, and the `returned`/`replaced` rules keep a restore done by
  the old app from producing a wrong edge (an older version re-appearing is `returned`, with no
  edge into it; the versions the game later writes in that slot descend from it through ordinary
  `observed` edges).
- **Old `import_vault`** copies entries but not `organizer/` (VERIFIED by D3's compat experiment
  X6); notes stay behind in the source vault. The new `import_vault` merges stores:
  `merge_store(dst, src)` - journal records not yet present (by `id`) appended and sorted into
  place by `(at, seq)`; save-file notes: the destination keeps its text, a different source text
  becomes a new revision "merged from <vault>"; version notes: destination wins, source fills
  missing, `locked` is OR-ed; corrections, aliases, splits: union, destination wins conflicts
  (reported); new entries trigger the idempotent backfill.
- **D11**: the store has no absolute paths; entries are referenced by id and resolved through
  the catalog API, live folders by `LiveRef` (folder name), which is the same on every machine
  for one account. A vault opened on another machine shows journal history for live folders
  that are not present there as history only.
- **Two machines, one account** (vault on a USB drive): both live folders map to the same
  `LiveRef` (Steam Cloud keeps them in sync); differences between machines appear as ordinary
  changes. Acceptable, and documented in the UI help.
- **Downgrade then upgrade**: the gap appears as one larger change per slot; nothing is lost.
- **`playthrough-ids.json`**: left alone (not read, not deleted); obsolete once P1 is fixed.
- **Catalog v2 (D11 relative paths, D2's work)**: the scanner resolves entry folders only
  through the catalog API, so it is unaffected.

## 5. Test plan

Risk-based. Real saves only read-only (skip when absent); every write on `tmp_path` copies or
synthetic fixtures; the organizer cache always under `tmp_path` in tests.

**Fixtures** (new or promoted to `tests/conftest.py`):
- `steam_pair(folder, slot, *, pid, play, ts, name, payload, member="both")` - promote
  `steam_save` from `test_playtime.py:43-63`; distinct payload bytes give distinct vids.
- `make_wgs_account` fixed to real 296/360-B metas with id @0x118, time @0x120, format @0x124
  (today 432 B with zeros; R1 G3) - lands with P1.
- `org_vault(tmp_path)` - a vault root, catalog with full/snapshot/extract/in-place entries built
  by the real operations on sandbox folders, plus an `oplog.jsonl`.
- `journal_records([...])` - builds obs/op records from a compact DSL
  (`obs(T9, A=v1, B=v2, at=...)`, `op(T9, placed=..., evicted=...)`).
- `fake_locator`, `fake_facts` (signals per vid), `frozen_clock`.
- `real_vault_dir` - the owner's vault, read-only, skip when absent; cache in `tmp_path`;
  before/after fingerprint of vault and live folders asserted equal.

| # | Area | Tests (each a meaningful scenario) |
|---|---|---|
| 1 | Journal replay | linear play; touch; edit; A and B both new in one observation (chain by time); op then child -> recorded; op then cloud revert -> `reverted`, no edge, `op_outcome` reverted; deletion; resurrection; an old version re-copied -> `returned`; different pid -> `replaced`; failed op binds nothing; capture and op in the same second; tie-break determinism; twins create a fork; legacy pid compatible across a slot move |
| 2 | Golden bootstrap | a synthetic vault reproducing R7's two forks (rollback via repopulate whose first attempt was reverted; cross-platform stale Xbox copy) -> exact edges, events, forks; dry run writes nothing |
| 3 | Store | atomic replace + `.prev`; unknown keys and record types survive a rewrite; `read_compat`/`write_compat` refusal; torn last journal line ignored and not merged; unparseable file -> read-only, untouched |
| 4 | Lock | two spawned processes doing 200 read-modify-write transactions each on `notes.json` and appends on the journal -> no lost update, `seq` unique and dense; Windows and POSIX code paths (the POSIX one in CI) |
| 5 | Scanner | alias entry (live path) skipped; whole-folder vs extract captures; cache hit/miss on size/mtime; fingerprint reuse; game running -> nothing recorded; unstable member (truncated copy) not journaled; legacy pid fixed at first registration; data-id fallback cached by vid |
| 6 | Notes | revisions and history; `base_rev` conflict; `note_revs` snapshot written with new versions; Unicode (Hebrew, CJK, emoji) round trip; ASCII-only UI test still passes |
| 7 | Lineage | precedence owner > recorded > observed > inferred; `not_related` blocks inference; cycle guard; combine/uncombine; split propagation through journal edges only |
| 8 | Inference | meta classifier; content classifier on synthetic signals (counter decrease, visited not extending, replaced expedition by season number and by time); parent = most progressed; cross-save kind; confidence levels |
| 9 | Read model | 15 + 15 board; cross-platform states; at-risk flags; folding; key filter; lane assignment golden ASCII for the two fork shapes and a 3-branch case; sort and filter |
| 10 | Locator conformance | abstract suite D2/D3 run: `open` verifies the hash, `locate` order, `preserve` returns verified durable non-prunable locations, `is_durable` |
| 11 | CLI | listing commands leave the vault byte-identical (fingerprint); `scan` writes only `journal.jsonl`; reference resolution and ambiguity errors; `--json` contains no account key |
| 12 | Real data (read-only) | scanner over the owner's vault: invariants (A/B same pid in every capture, every live pid has a line, no exceptions, vault unchanged); bootstrap `--dry-run` matches the 2.12 table within tolerance |
| 13 | Performance | synthetic 2,000-version vault: warm refresh < 0.5 s, read model < 50 ms, lanes < 10 ms per line |

## 6. Implementation plan

Sizes: S < 200 lines of code plus tests, M 200-500, L > 500. Each step is one commit with its
tests (`uv run pytest` green).

| # | Step | Size | Phase |
|---|---|---|---|
| 0 | Prerequisites owned elsewhere: P1 Xbox meta parser + fixture lengths; P9 alias handling in `discover`; P2 via D2's rescue rule | - | 0 |
| 1 | `platform.user_cache_dir` + `safety.exclusive_lock` (+ two-process lock test) | S | 1 |
| 2 | `organizer/model.py`: ids, value types, JSON codecs with `_extra` | M | 1 |
| 3 | `organizer/store.py`: header, `vault_id`, compat, atomic JSON, journal append/tail read, transactions, read-only mode | M | 1 |
| 4 | `organizer/cache.py` + `organizer/scanner.py` (registry, pairs, alias, validation, pid rules) | M | 1 |
| 5 | `organizer/journal.py`: `observe` + `replay` (edges, events, binding) | L | 1 |
| 6 | `organizer/recorder.py` + the phase-1 hook in today's operations (with D5) | M | 1 |
| 7 | `organizer/bootstrap.py` (backfill + oplog resolution + report, no inference) | M | 1 |
| 8 | `organizer/notes.py` + `lineage.json` skeleton (bootstrap block, pins) | M | 1 |
| 9 | `organizer/readmodel.py`: Line, VersionRow, LiveBoard, sort/filter (no HistoryGraph) | M | 1 |
| 10 | `Organizer` facade + CLI `saves`, `versions`, `board`, `note`, `pin`, `scan`, `organizer bootstrap` | M | 1 |
| 11 | Real-data read-only smoke test + performance test | S | 1 |
| 12 | Facts hook: `representative`, `facts`, `facts:<name>` sorting, key-version milestone hook | S | 3 |
| 13 | `organizer/infer.py`: meta classifier + roots inference | S | 4 |
| 14 | Content classifier on D4 `lineage_signals` | S | 4 |
| 15 | Corrections, Combine/Split, effective pid, `protection()` | M | 4 |
| 16 | HistoryGraph: projection, folding, key filter, lanes, `timeline` | M | 4 |
| 17 | CLI `history`, `lineage ...`; `merge_store` in `import_vault` | M | 4 |

Phase 1 ships the organizer view, the journal, recording (backfill + legacy-op hook) and notes;
the history view, inference and corrections ship in phase 4 (C18: recording must not wait for
the history view).

## 7. Risks and open questions

1. **Journal granularity.** Changes between two scans are invisible; a rollback that is played
   over before the next scan looks linear (R7 3). Mitigation: scan on start, refresh, before and
   after every operation. A background watcher is a possible later addition.
2. **Local-time conversion** of catalog `created` and oplog `ts` uses this machine's time-zone
   rules; a vault made in another zone shifts by the offset (ordering within one vault is
   unaffected; captures vs ops can swap only within the offset).
3. **Unorientable edits** (equal time and play time): ordered by observation; inference marks
   them low confidence.
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
   edit, cross-save, unknown. Place, swap, restore-older and undo are operation kinds linked by
   `op_id`; deleted, reverted, resurrected, returned, replaced, partial and the op outcomes
   confirmed / unexpected are journal events. Reason: a "cloud-revert edge" from the placed
   version to the old one would claim the old version descends from the new one (a false
   parent, and a cycle risk).
2. **Edge fields.** Added tier `owner`, a `method` field, and `target`.
3. **Capture.** `where` is a typed `SourceRef` (`LiveRef` / `EntryRef` / `StoreRef`); added
   `time_quality` and `src`. `LiveRef` has an `instance` disambiguator for two same-named live
   folders on one machine (two Proton prefixes, raised by D5).
4. **Notes key.** Version notes are keyed by the pair's head vid (105 heads vs 106 pairs here).
5. **History nodes.** Pair heads plus member-level fork points (a non-head member can be the
   real fork point, Creative Primary).
6. **pid.** Added `split:<raw pid>:<n>` (owner Split) and `unknown:<vid[:16]>`; the legacy
   form is fixed at a version's first registration.
7. **Locator.** `preserve` takes a batch of `LiveMember` (target, member, expected vid) so a
   storage can keep an evicted pair as one entry; `open(vid, prefer)` returns `OpenedVersion`
   with the location read. `Location` gained `external`.
8. **Store.** `journal.jsonl` carries op bindings written by `record_op` (idempotent on
   `(op_id, target)`); `ops.jsonl` is reserved for D5's operation log and `storage.json` for D2.
9. **Cache.** The per-machine root is the new `platform.user_cache_dir()`; D4's index and D5's
   open-operation markers share `<cache>/vaults/<vault_id>/`.

## Appendix A - prototype and evidence

All work was read-only on real data. Files in
`%LOCALAPPDATA%\Temp\claude\c--Devel-NMSVault\be828316-f5a0-4419-bd50-12964a376ba0\scratchpad\D1\`:

| File | Purpose |
|---|---|
| `r7copy\` | copies of R7's `inventory.json`, `folders.json`, `features\*.json`, `common.py`, `lineage_lib.py` |
| `vaultcopy\oplog.jsonl` | copy of the vault oplog; sha256 `29b3ece9...598aaa` = R1's recorded value (unchanged) |
| `bootstrap_proto.py` | the bootstrap of 2.12 over the copies: captures, registry, oplog resolution, timelines + replay (2.6.3), meta inference, R7 content check, projection, folding, key filter, lanes, journal size |
| `proto_run4.txt`, `proto_run5.txt`, `proto_run6.txt` | outputs quoted in 2.10 and 2.12 (`proto_run6.txt` section "DOC RULES" recounts folding, key versions and at-risk slots with the exact rules of 2.10.4) |
| `core_counters.json` | the 31 core counter ids (2.8) |
| `journal_preview.jsonl` | the 196 backfill records the bootstrap would write (account key replaced by a placeholder) |

Run: `PYTHONUTF8=1 uv run python <D1>\bootstrap_proto.py` from `C:\Devel\NMSVault` (no
third-party package needed; R7's features were already decoded).
