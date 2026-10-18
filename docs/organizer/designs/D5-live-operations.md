# D5 - Live-slot operations: move to live, swap, undo, recovery, verification

Status: design, wave 1 (2026-10-16), revised 2026-10-17 for the owner's decisions
(`01-consolidation.md` 4a and the answers to `03-design-review.md`). Subject S5 (C23-C26,
C29-C30; forks F4-F8, all decided). Binding inputs: `01-consolidation.md` sections 2, 4a, 5 and
6; R8 section 4 (the draft spec this document turns into a design); D1 (model, journal, locks),
D3 (version store: preserve, snapshots, listing). Audience: the implementing agents
(completeness wins) and the parent. No production code was written; every experiment ran on
synthetic sandboxes or as a read-only probe (Evidence index at the end).

Tags: VERIFIED = run or read by D5; SOURCED = cited from code or research; INFERRED = reasoning.

## 0. Owner summary

1. "Move to live" puts any saved version into the live slot you pick; what was there is copied to the vault and checked first.
2. Both halves of a slot (auto-save, restore point) are replaced together; the version you picked is the one the game loads.
3. Each move is logged before it starts; after a crash the app finishes or reverses it. Undo restores only that slot and keeps what you played since.
4. After your next game launch the app checks that Steam Cloud or Xbox did not bring the old save back.
5. As you decided: the restore point comes along; the in-game date shows "now" (the real date stays in the vault); another account's save gets one warning; the app only replaces, never empties a slot - it shows you the steps instead.
6. Steam <-> Xbox moves come last, after you test one each way with a throwaway save.

## Contents

1. Scope, non-goals, dependencies
2. Design: model and API (2.1-2.3), flows (2.4), slot writing (2.5-2.6), the operation journal
   (2.7), execute (2.8), validation and rollback (2.9), recovery (2.10), undo (2.11), post-launch
   verification (2.12), oplog v2 and lineage (2.13), errors (2.14), safety invariants (2.15),
   concurrency and performance (2.16)
3. Owner decisions: F4 member policy, F5 timestamps, F7 other accounts, F8 replace only,
   F6 Steam <-> Xbox transfer
4. Migration of today's operations
5. Test plan
6. Implementation plan
7. Risks and open questions
8. Deviations from 01-consolidation section 6
9. Evidence index

## 1. Scope, non-goals, dependencies

### 1.1 Scope

The core operation layer that changes live save slots, and only that layer:

- **Place(V, T)** - put a vault version V (a captured slot pair, chosen member loads) into live
  target T, evicting what T holds into the vault. Covers "move to live", "use a backup of a
  live save", "use a backup of a non-live save", and **restore-older** (V's line is the one in T).
- **PlaceIntoEmpty(V, T)** - the same with nothing to evict.
- **Swap(T1, T2)** - exchange two occupied live slots of one platform.
- **Promote(T, member)** - make the other member of a slot load; re-routed through the same
  machinery (fixes Xbox promote being a no-op, R8 A5).
- **Undo(op)** - slot-scoped and non-destructive (C24); multi-level per target.
- **Keep a copy(T)** - copy a live slot into the vault without any live write (D6's "Keep a copy
  in the vault now").
- **Empty-slot guidance** - the texts for emptying a slot by hand and for the refill risk of an
  empty slot. The app itself never empties a slot (F8).
- The **operation journal** (write-ahead intent, completion, verification state) and **recovery**
  of an interrupted operation on the next start.
- **Post-launch verification** (C25): Steam Cloud / Xbox reverts.
- **Oplog record v2** and the call into D1's lineage recorder.
- The **eviction-picker data** the UI renders (C30) and the explicit-target rule (C29).
- The **live-op lock** that serialises every live write, classic ones included (2.16).
- What happens to today's operations and their GUI/CLI entry points (section 4).
- Phase-5 **Steam <-> Xbox transfer** (F6): meta conversion spec, gates, owner runs T1/T2.

### 1.2 Non-goals

- UI layout and visuals (D6). D5 defines entry points, plan objects, confirmation codes and
  message texts; the UI decides how they look.
- Version identity, lineage edges, live-journal observation, notes and pins (D1).
- Where preserved bytes live and the vault upgrade (D3's version store).
- Content facts (D4). D5's operations need none; phase 2b reads one accountdata flag with D4's
  decoder (`decode_save`).
- Deleting live save files: no D5 path empties a slot or deletes a save file, except a rollback
  removing a file the same op created (F8, I9).
- Any check of who owns a save: no ownership id is read (F7b, owner Q6).
- Cross-account transfer with an ownership rewrite (libNOM.io-style JSON rewrite): it breaks the
  verbatim rule (R8 Q4b). Not designed.
- Pre-Frontiers / pre-Worlds formats as transfer sources (refused, 3.5).
- Writing while NMS.exe runs: organizer operations have **no override** (2.15 I1).
- Older app versions on an upgraded vault: not supported (owner Q2); D5 writes no compatibility
  records for them.

### 1.3 Dependencies

| Dependency | What D5 needs | Status / owner | Blocks |
|---|---|---|---|
| **P1** Xbox meta parse + timestamp setter | `parse_ms_meta` reads id 0x118, time 0x120, format 0x124, names for 280/296/360-B metas; `set_ms_meta_timestamp` works on 296/360 B; fixtures with real lengths | Phase 0 (spec in 4.4) | every Xbox operation (member order, identity, validation) |
| **P-X1** containers.index FILETIME drift (new, VERIFIED 9.3) | record-bytes-level index editing so untouched records stay byte-identical | Phase 0/2 (2.6) | exact Xbox rollback; avoids perturbing other saves' sync records |
| P3 GUI promote target | fixed by C29 (target named by the row) | Phase 0 | wrong-folder writes until the UI moves to D5 entry points |
| P4 Undo target | whole-folder undo becomes "panic restore" with a target check (4.3) | Phase 0 | old-style undo after organizer ops |
| P5 Clear-slot "in the vault" by slot number | replaced by vid-based durability (`is_durable`) | Phase 0 interim, Phase 2 final | eviction safety display |
| P6 One-member source into an occupied slot | Place always writes both members (I5) | Phase 0 interim refusal, Phase 2 final | mixed slots |
| P7 vdf / containers.index across accounts, accountdata rollback | panic restore never copies them; organizer ops never touch them (I7) | Phase 0 | cross-account corruption |
| P2 age-based snapshot pruning | pruning removed (01 phase 0); every snapshot is kept | Phase 0 | - |
| D1 ids, `LiveTarget`, `PairKey`, read model | live board (vids, MetaSnap, pins, line names, `Line.cross_platform`) | D1 | planner |
| D1 `LineageRecorder.record_op`, `expected_pair`, `op_outcome`, `observe(scan, *, src, skip)` events | lineage binding (C16), verification events | D1 | 2.12, 2.13 |
| D1 organizer store + lock (`safety.exclusive_lock`) | `ops.jsonl` (reserved for D5 by D1), one writer at a time | D1 | journal |
| D3 version store in store mode (`core.store.vault_mode(vault_root) == "store"`), reached after D3's copy-aside and upgrade | the only preserve provider and snapshot target from phase 2 on | D3, phase 2 | every organizer live op (`VAULT_UPGRADE_NEEDED` until then) |
| D1/D3 `VersionLocator` (D1's composite `VaultLocator`: `StoreLocator` first, legacy folders until fold) | `open(vid, prefer)`, `preserve(LiveMember...)` (= `StoreLocator.preserve`), `is_durable(vid)`, `locate(vid)` | D1, D3 | preserve-before-replace |
| D3 snapshots and listing | `VersionStore.add_from_live(kind="snapshot")` for the pre-op snapshot, `read_file(entry_id, path)` for rollback bytes, `list_vault_entries` + `open_entry_folder` for the panic restore, `add_from_live(kind="extract")` for Keep a copy | D3 | rule "auto-snapshot first"; exact rollback |
| UI (D6) | consumes 2.2 objects; must block app exit during `execute`; shows recovery and verification items | D6 | - |

## 2. Design

### 2.1 Terms (in addition to 01-consolidation section 6)

| Term | Meaning |
|---|---|
| position | `A` (auto-save, file `2k-1`, Xbox `Slot<k>Auto`) or `B` (restore point, file `2k`, Xbox `Slot<k>Manual`) of a live slot. Same as section 6 "member". |
| source pair | the version the user picked: D1 `PairKey (vid_a, vid_b)` plus `load` (the position that must load; default the newer member by `(meta_ts, play_time)`) plus `prefer` (the `Location` it was seen at, so the meta comes "as captured") |
| V | the member of the source pair that must load |
| partner | the other member of the source pair, if present |
| L / O | the position V is written to (L, "loads") and the other position (O) |
| pre-image | the exact live state of a target slot right before the first write: per position the data sha256, the on-disk meta sha256, size, mtime; on Xbox also the raw `containers.index` header bytes and the raw records of the two identifiers |
| post-image | the planned state after the write, same fields |
| op | one user-level operation with one `op_id` (section 6 format); a Place with its eviction is one op; a Swap is one op over two targets |
| operation journal | `<vault>/organizer/ops.jsonl`, the append-only event log of ops (2.7). Not D1's live journal (`journal.jsonl`), which records observations. |
| cloud mode | `steam-cloud` (target folder holds `steam_autocloud.vdf`), `steam-local` (no vdf), `xbox` (always synced) |
| durable | `VersionLocator.is_durable(vid)`: an exact, hash-verified copy exists in the vault. Every store entry counts, snapshots included (nothing is pruned); so do managed legacy folders until they are folded (D3). Hand-made in-place folders outside the vault do not |
| capture account | the live account a source pair was captured from: the live target it was seen in, or the `source` of the vault entry holding it (D3 `VaultEntry.source`); unknown for entries without one (imports of loose folders) |

### 2.2 Public API

Package `nms_save_vault.core.liveops` (platform-neutral; the only platform branching is the
Steam/Xbox writer split, which already lives behind `savedir`/`msstore`). Front ends call only
these functions; no UI reads save files or chooses a write target itself (section 6).

```python
# nms_save_vault/core/liveops/__init__.py  (re-exports)
def eviction_picker(ctx: OpContext, source: PairSource,
                    account: LiveAccount | None = None) -> EvictionPicker: ...
def plan_place(ctx: OpContext, source: PairSource, target: LiveTarget) -> PlacePlan: ...
def plan_swap(ctx: OpContext, first: LiveTarget, second: LiveTarget) -> PlacePlan: ...
def plan_promote(ctx: OpContext, target: LiveTarget, position: Position) -> PlacePlan: ...
def plan_undo(ctx: OpContext, op_id: str) -> PlacePlan: ...
def plan_panic_restore(ctx: OpContext, account: LiveAccount, snapshot_id: str | None = None, *,
                       restore_accountdata: bool = False) -> PlacePlan: ...      # 4.3
def plan_keep(ctx: OpContext, target: LiveTarget) -> PlacePlan: ...           # 2.4.1, vault only
def empty_slot_guide(ctx: OpContext, target: LiveTarget) -> EmptySlotGuide: ...  # 2.4.2
def execute(ctx: OpContext, plan: PlacePlan, confirmed: frozenset[str],
            progress: Callable[[Progress], None] | None = None, *,
            note: str = "") -> OpOutcome: ...
def pending_recovery(ctx: OpContext) -> list[RecoveryItem]: ...
def resolve_recovery(ctx: OpContext, op_id: str,
                     choice: Literal["forward", "back", "leave"]) -> OpOutcome: ...
def update_verification(ctx: OpContext, events: Sequence[JournalEvent]) -> list[VerifyItem]: ...
def verification_items(ctx: OpContext) -> list[VerifyItem]: ...
def op_history(ctx: OpContext, target: LiveTarget | None = None,
               limit: int = 50) -> list[OpSummary]: ...
def resolve_target(ctx: OpContext, target: LiveTarget) -> ResolvedTarget: ...
def busy_targets(ctx: OpContext) -> frozenset[LiveTarget]: ...          # 2.16, for D1 observe
def liveop_lock(ctx: OpContext, timeout: float = 10.0) -> ContextManager[None]: ...  # 2.16
```

All `plan_*`, `eviction_picker` and `empty_slot_guide` calls are **read-only** (no file is
created, not even the journal). `execute` re-checks everything that can change between plan and
execute (2.8 step 1). `note` is the owner's optional "Note for this move"; it goes into the
`preparing` event, the oplog line and D1's `OpLineage.note`. Every returned object is a frozen
dataclass with `to_dict()` (JSON-safe, for the CLI's `--json`). Every text field is ASCII and
already passed through `aliases.redact`; no text contains a path or an account id.

#### Context and policies

```python
@dataclass
class OpContext:
    vault: Vault                         # catalog.Vault (oplog, legacy entries until fold)
    versions: VersionStore | None        # D3; None while core.store.vault_mode() != "store"
    store: OrganizerStore                # D1: paths, vault_id, lock()
    locator: VersionLocator              # D1's composite VaultLocator (D3 StoreLocator first)
    lineage: LineageRecorder             # D1: record_op, observe, expected_pair, op_outcome
    readmodel: ReadModel                 # D1: live_board(), lines (names, cross_platform), pins
    sources: Sequence[state.Source]      # configured live sources (state.json)
    policies: Policies
    host: platform.Host                  # DESIGN.md D7: env and home arrive as arguments
    app_version: str
    machine_id: str                      # random per machine, user_data_dir(host)/machine-id
    game_probe: Callable[[], bool | None] = safety.is_game_running
    clock: Callable[[], float] = time.time
    faults: FaultInjector | None = None  # tests only (5.3); None in production

@dataclass(frozen=True)
class Policies:
    cross_platform: Literal["off", "validated"] = "off"                        # F6 (3.5)
```

The owner's decisions F4a, F5b, F7b and F8 are fixed behaviour (section 3), not switches; only
the decided option is implemented. `cross_platform` stays a constant because F6 opens per
direction after the owner's runs (3.5); it is not a user setting.

#### Inputs

```python
Platform = Literal["steam", "xbox"]
Position = Literal["A", "B"]

@dataclass(frozen=True)
class LiveAccount:
    platform: Platform
    account_key: str                     # live folder name (section 6)

# LiveTarget is D1's organizer.ids.LiveTarget(platform, account_key, slot, instance="");
# UIs may hold its opaque `token` ("<platform>:<live source index>:<slot>") and pass it back.

@dataclass(frozen=True)
class PairSource:
    vid_a: str | None
    vid_b: str | None
    load: Position | None = None         # None = newer member by (meta_ts, play_time)
    prefer: Location | None = None       # D1 Location where the pair was captured
    folder_slot: tuple[Path, int] | None = None   # classic adapter only (4.1): a folder that
                                                  # the locator does not know; bytes read directly

@dataclass(frozen=True)
class ResolvedTarget:                    # internal; never shown
    target: LiveTarget
    source_id: str                       # state.json Source.id
    folder: Path
    cloud_mode: Literal["steam-cloud", "steam-local", "xbox"]
    xgs_twin: Path | None
    label: str                           # "Steam slot 7" / "Xbox slot 4" (+ account label if >1)
```

#### The eviction picker (what the UI's "Move to live..." dialog renders, C30)

```python
@dataclass(frozen=True)
class Warning:
    code: str                            # stable code, table 2.3.3
    severity: Literal["info", "warn", "confirm", "block"]
    text: str                            # one ASCII sentence, redacted
    data: Mapping[str, object]           # numbers for formatting: seconds, slot, bytes

@dataclass(frozen=True)
class MemberFacts:
    vid: str | None
    pid: str | None
    name: str
    play_time: int                       # seconds, meta TotalPlayTime
    saved_at: int                        # unix, meta timestamp (Steam 0x164, Xbox 0x120)
    game_mode: int
    difficulty: int
    season: int
    size: int                            # data bytes
    durable: bool                        # locator.is_durable(vid)
    sync_state: str                      # Xbox 'Synced'|'Modified'|'Created'|'Deleted'; Steam ''

@dataclass(frozen=True)
class SlotCandidate:
    target: LiveTarget
    label: str                           # "Steam slot 7"
    state: Literal["empty", "occupied", "mixed", "partial", "unreadable"]
    a: MemberFacts | None
    b: MemberFacts | None
    newer: Position | None               # the member the game loads now
    pid: str | None                      # occupant (newer member)
    line_name: str                       # D1 read-model name of pid (fallback: meta name)
    play_time: int                       # newer member
    last_played: int                     # newer member's meta timestamp
    durable: bool                        # every present member is durable
    vault_copy: Literal["durable", "partial", "outside-vault", "none"]
                                         # = D1 BoardSlot.vault_copy (finer than `durable`, for the UI)
    pinned: bool                         # occupant's pid is pinned (D1 decision 9)
    same_line: bool                      # occupant pid == source pid
    suggested: bool
    suggest_reason: Literal["same_line", "empty", "in_vault_least_recent",
                            "least_recent", ""]
    kind: Literal["place_into_empty", "place", "restore_older", "noop"]
    outcome: str                         # button text, e.g. "Swap into Steam slot 7"
    warnings: tuple[Warning, ...]
    blocked: str                         # "" or a block code; the UI disables the choice

@dataclass(frozen=True)
class SourceSummary:
    name: str
    pid: str | None
    vid_a: str | None
    vid_b: str | None
    load: Position
    play_time: int
    saved_at: int
    game_mode: int
    platform: Platform
    where_label: str                     # "vault: full backup 2026-10-05" / "live: Steam slot 4"

@dataclass(frozen=True)
class CloudInfo:
    steam_cloud: bool | None             # None for Xbox
    cross_save_on: bool | None           # None = unknown (accountdata not read)
    xbox_sync_busy: bool                 # containers.index changed < 60 s ago (2.8 step 1)

@dataclass(frozen=True)
class AccountChoice:
    account: LiveAccount
    label: str
    blocked: str                         # e.g. CROSS_PLATFORM_GATED, XGS_READ_ONLY
    warnings: tuple[Warning, ...]        # e.g. OTHER_ACCOUNT

@dataclass(frozen=True)
class EvictionPicker:
    source: SourceSummary
    account: LiveAccount                 # the account shown (2.3.1)
    cloud: CloudInfo
    candidates: tuple[SlotCandidate, ...]    # 15, slot order
    suggested_slot: int | None
    other_accounts: tuple[AccountChoice, ...]
    warnings: tuple[Warning, ...]        # plan-level (game running, recovery pending, ...)
    fingerprint: str
```

#### Plans and outcomes

```python
@dataclass(frozen=True)
class MemberWrite:
    position: Position
    vid: str                             # data sha256, written verbatim
    pid: str | None
    play_time: int
    meta_ts: int                         # timestamp written into the meta (now, F5b)
    source_meta_ts: int | None           # the captured meta time before the stamp
    meta_plain_sha256: str               # plaintext as written (after the timestamp stamp)
    meta_disk_sha256: str                # bytes as written (Steam: encrypted for `ordinal`)
    disk_time: float                     # Steam mtime / Xbox FILETIME (unix seconds)
    ordinal: int | None                  # Steam storage ordinal; None on Xbox
    copy_of: Position | None             # F4: this position holds a copy of V
    origin: Mapping[str, object]         # Location.to_dict() of the bytes' source

@dataclass(frozen=True)
class Eviction:
    position: Position
    vid: str | None                      # None: file present but unreadable (raw preserve)
    pid: str | None
    name: str
    play_time: int
    saved_at: int
    durable_before: bool

@dataclass(frozen=True)
class TargetPlan:
    target: LiveTarget
    label: str
    cloud_mode: Literal["steam-cloud", "steam-local", "xbox"]
    before: Literal["empty", "occupied", "mixed", "partial", "unreadable"]
    writes: tuple[MemberWrite, ...]      # always 2; 0 only for keep (vault-only)
    evictions: tuple[Eviction, ...]      # for keep: the members copied to the vault

@dataclass(frozen=True)
class Estimate:
    preserve_bytes: int                  # 0 when everything is already durable
    snapshot_bytes: int                  # only objects the store does not hold yet (usually ~0)
    write_bytes: int
    seconds: float

@dataclass(frozen=True)
class PlacePlan:
    plan_id: str                         # random; not an op_id (op_id is minted by execute)
    kind: Literal["place", "place_into_empty", "restore_older", "swap", "promote", "undo",
                  "panic_restore", "keep"]
    targets: tuple[TargetPlan, ...]      # one per slot: 1, 2 (swap), up to 15 (panic restore)
    source: SourceSummary | None         # None for swap / keep / panic_restore
    undo_of: str | None
    warnings: tuple[Warning, ...]
    confirmations: frozenset[str]        # codes the caller must echo back in `confirmed`
    blocked: tuple[Warning, ...]         # non-empty -> execute refuses
    outcome: str                         # "Swap into Steam slot 7"
    fingerprint: str                     # 2.8 step 1
    estimate: Estimate
    created_at: float

@dataclass(frozen=True)
class UndoOffer:
    op_id: str
    label: str                           # "Undo: put 'Polestar' back into Steam slot 7"

@dataclass(frozen=True)
class OpOutcome:
    op_id: str | None
    status: Literal["done", "rolled_back", "failed", "refused", "stale"]
    outcome: str                         # "Moved 'Main Save' into Steam slot 7. 'Polestar' is in the vault."
    warnings: tuple[Warning, ...]
    undo: UndoOffer | None
    verify: Literal["pending", "n/a"]
    error: str | None
    code: str | None = None              # 'refused' / 'failed': GAME_RUNNING, BUSY, VAULT_SPACE, PRESERVE_FAILED:<class>, ...
    replan: PlacePlan | None = None      # status 'stale': the fresh plan to show

@dataclass(frozen=True)
class Progress:
    step: Literal["checking", "preserving", "snapshot", "writing", "verifying", "recording"]
    done: int
    total: int
```

`RecoveryItem`, `VerifyItem` and `OpSummary` are defined in 2.10, 2.12 and 2.13;
`EmptySlotGuide` in 2.4.2.

### 2.3 Planner

#### 2.3.1 Which account the picker shows

1. The account named by the caller (the UI's account switch), else
2. the same-platform account where the source's pid is live (restore-older is the likely intent),
   else
3. the account the source pair was captured from (`prefer`), if it is a configured live account
   of the same platform, else
4. the first enabled, writable account of the source's platform (`sources` order).

Every other configured account appears in `other_accounts` with its block reason or warnings:
other platform -> `CROSS_PLATFORM_GATED` (until F6 opens, 3.5); same platform, an account other
than the capture account -> `OTHER_ACCOUNT` (confirm, 3.3); xgs-only Xbox folder ->
`XGS_READ_ONLY`.

#### 2.3.2 Candidate rows

Data comes from D1's `ReadModel.live_board()` (vids, `MetaSnap`, pins, line names; hashed by
D1's scanner with its per-machine hash cache), never from the Tk thread, and never by decoding
save data. Per slot:

| Field | Rule |
|---|---|
| `state` | `empty`: neither position present. `partial`: a Steam data file without its meta (or the reverse), or an Xbox record whose blob is missing. `unreadable`: a present member whose meta does not decrypt/parse. `mixed`: both present and readable with different pids. `occupied`: otherwise. |
| `newer` | the position with the higher `(meta_ts, play_time)`; ties -> A (same rule as `SlotView.newest` once P1 is fixed) |
| `durable` | `all(is_durable(vid))` over present members; `unreadable` members count as not durable |
| `kind` | `noop` when the slot holds exactly the source pair (same vids in the same positions); `restore_older` when occupant pid == source pid; `place_into_empty` when empty; else `place` |
| `outcome` | `place_into_empty`: "Put into empty Steam slot 1"; `place`: "Swap into Steam slot 7"; `restore_older`: "Go back to this version in Steam slot 7" (source has less play than the occupant) or "Use this version in Steam slot 7" (otherwise); `noop`: "Already in Steam slot 7" |
| `blocked` | `XGS_READ_ONLY` (whole account), `ALREADY_LIVE` (noop), `RECOVERY_PENDING` (open op on this account) |

#### 2.3.3 Warning codes

Severity `confirm` means the plan lists the code in `confirmations` and `execute` refuses
unless the caller echoes it; `block` means `execute` refuses.

| Code | Severity | Where | Condition | Text (template) |
|---|---|---|---|---|
| `NOT_IN_VAULT` | info | candidate | a present member is not durable | "Not in the vault yet - it will be copied there first." |
| `EMPTY_SLOT` | info | candidate | state empty | "Emptied outside the app - checked after the next launch." |
| `PLAY_TIME_AHEAD` | confirm | candidate (restore_older) | occupant play time > source play time | "Steam slot 7 has 3h12m more play than this version. That newer save stays in the vault." |
| `PINNED` | warn | candidate | occupant pid pinned | "You pinned this save." |
| `MIXED_SLOT` | warn | candidate | state mixed | "This slot holds two different saves. Both will be kept in the vault." |
| `PARTIAL_SLOT` | warn | candidate | state partial | "Part of this slot is missing. What is there will be kept in the vault." |
| `UNREADABLE_MEMBER` | warn | candidate | state unreadable | "One file in this slot cannot be read. It will be kept in the vault as it is." |
| `PENDING_UPLOAD` | info | candidate | Xbox record Modified/Created | "This slot has changes Xbox has not uploaded yet." |
| `EXPEDITION_ACTIVE` | warn | candidate | occupant meta GameMode == 6 | "Expedition in progress. If the season ends while it is in the vault, it becomes a normal save when you put it back." |
| `EXPEDITION_SOURCE` | warn | plan | source GameMode == 6 | "Expedition save. If its season has ended, the game turns it into a normal save when you load it." |
| `TWIN_LIVE` | confirm | candidate | source pid live in another slot of this account and this slot is not that one | "This save is already in Steam slot 4. A second copy makes two separate branches." |
| `LIVE_ELSEWHERE` | info | plan | source pid live on another account/platform; `data["cross_platform"]` = D1 `Line.cross_platform` | "This save is also in Xbox slot 4." |
| `ALREADY_LIVE` | block | candidate | kind noop | "This version is already in Steam slot 7." |
| `CROSS_PLATFORM_GATED` | block | account | F6 not open | "Moving saves between Steam and Xbox is not available yet." |
| `OTHER_ACCOUNT` | confirm | account | the capture account is known and differs from the target account (F7b, 3.3) | "This save comes from another account. Its bases may not be editable until the game takes them over when you play it here." |
| `XGS_READ_ONLY` | block | account | Xbox folder without `containers.index` | "This Xbox folder can only be read." |
| `GAME_RUNNING` | block | plan | `game_probe() is True` | "No Man's Sky is running. Close it first." |
| `GAME_STATE_UNKNOWN` | confirm | plan | `game_probe() is None` | "Could not check whether No Man's Sky is running. Make sure it is closed." |
| `CROSS_SAVE_ON` | warn | plan | accountdata CrossSaves true (if known) | "Hello Games cross-save is on: the game may ask which copy to keep." |
| `STEAM_CLOUD_OTHER_DEVICE` | info | plan | cloud mode steam-cloud | "If Steam asks at the next launch, choose the copy on this PC." |
| `XBOX_SYNC_BUSY` | block (soft) | plan | `containers.index` mtime < 60 s ago | "Xbox is still syncing. Try again in a minute." |
| `XBOX_QUOTA` | warn / block | plan | wgs total after the op > 200 MB / > 256 MB | "Xbox cloud space would be nearly full / over the 256 MB limit." |
| `VAULT_UPGRADE_NEEDED` | block | plan | `core.store.vault_mode(vault_root) != "store"` (D3 upgrade not done, running or failed) | "Upgrade the vault first: Tools > Vault upgrade... Nothing was changed." |
| `VAULT_UNAVAILABLE` | block | plan | vault root missing or not writable; D3 `StoreUnavailable` | "The vault is not reachable. Nothing was changed." |
| `VAULT_SPACE` | block | plan | vault free space < (`StoreLocator.space_needed_for_preserve` + `VersionStore.space_needed` of the snapshot) x 1.5 + 64 MB, or live drive free space < 2 x write bytes; D3 `InsufficientSpace` | "Not enough free disk space." |
| `SOURCE_UNREADABLE` | block | plan | locator cannot open/verify the source | "That version cannot be read from the vault." |
| `RECOVERY_PENDING` | block | plan | an open op on any target account (2.10) | "An earlier move was interrupted. Resolve it first." |
| `STALE_SOURCE_MIXED` | warn | plan | source pair's members have different pids | "The saved slot held two different saves; only the chosen one is used." |

Codes used only by one flow are defined with it: Swap `SWAP_EMPTY_SIDE` (2.4); empty slots
`EMPTY_SLOT_MAY_REFILL` (2.4.2); undo `PLAYED_SINCE`, `ALREADY_UNDONE`, `UNDO_STALE`,
`UNDO_WOULD_EMPTY` (2.11); F6 `SOURCE_OLD_FORMAT`, `UNSUPPORTED_META`, `SOURCE_NO_ID` (3.5.1);
outcome codes `BUSY`, `PRESERVE_FAILED:<class>`, `SNAPSHOT_FAILED`, `RECORDING_PENDING` (2.14).

#### 2.3.4 Suggested slot (C30, refined)

```python
def suggest(cands: list[SlotCandidate]) -> tuple[int | None, str]:
    usable = [c for c in cands if not c.blocked]
    same = [c for c in usable if c.kind == "restore_older"]
    if same:   # rewind the line in place; if it is live twice, the most recently played copy
        return max(same, key=lambda c: (c.last_played, -c.target.slot)).target.slot, "same_line"
    empty = [c for c in usable if c.kind == "place_into_empty"]
    if empty:
        return min(c.target.slot for c in empty), "empty"
    pool = [c for c in usable if c.kind == "place" and not c.pinned]
    safe = [c for c in pool if c.durable]
    if safe:
        return min(safe, key=lambda c: (c.last_played, c.target.slot)).target.slot, "in_vault_least_recent"
    if pool:   # still safe: preserve-before-replace copies it first (I2)
        return min(pool, key=lambda c: (c.last_played, c.target.slot)).target.slot, "least_recent"
    return None, ""
```

A pinned slot holding the source's own line is still suggested ("same_line"): pinning means
"do not evict this save for another one"; going back to an older version of the same save keeps
the newer one in the vault and is what the user asked for. The pin shows as a warning.

An empty slot is not a risky suggestion. Steam Cloud's refill risk applies while a slot stays
empty; a Place writes new files, and overwrites made outside the game stick (R8 E1). Its row
therefore carries only `EMPTY_SLOT` (info), and verification (2.12) reports a refill if one
happens. The owner confirms this once in a run (7.2).

#### 2.3.5 `plan_place` (read-only)

```python
def plan_place(ctx, source, target) -> PlacePlan:
    rt = resolve_target(ctx, target)                 # C29, 2.3.6; raises TargetError
    src = load_source(ctx, source)                   # metas via locator.open(vid, prefer); data
                                                     # length + vid existence only (no full read)
    gates = transfer_gates(ctx, src, rt)             # VAULT_UPGRADE_NEEDED, F6, F7b, XGS_READ_ONLY
    cur = read_slot(rt)                              # pre-image candidate: hashes via D1 cache
    kind = classify(src, cur)                        # place | place_into_empty | restore_older | noop
    writes = pairmeta.compute(src, rt, now=int(ctx.clock()))                    # 2.5
    evictions = [eviction(m, ctx.locator) for m in cur.present]
    warnings = candidate_warnings(...) + plan_warnings(ctx, rt, src, writes)
    return PlacePlan(..., fingerprint=fingerprint(rt, cur, src), estimate=estimate(...))
```

`fingerprint` = sha256 over, per target, for each position: `exists`, size, `st_mtime_ns`,
on Xbox the raw `containers.index` bytes; plus the source vids and `load`. `execute` recomputes
it under the live-op lock; any difference returns `status="stale"` with a fresh plan (2.8).

#### 2.3.6 Target resolution (C29)

`resolve_target` is the only way any write path obtains a live folder:

1. `platform in ("steam", "xbox")`, `1 <= slot <= 15`.
2. Exactly one configured source with `role == "live"`, `enabled`, `writable`,
   `platform == target.platform`, `Path(source.path).name == target.account_key` and, when
   `target.instance` is set, `sha1(resolved parent path)[:8] == target.instance` (D1's
   disambiguator for two Proton prefixes holding the same `st_<steamid64>`). Zero ->
   `TargetError("unknown live account")`; more than one -> `TargetError("ambiguous live
   folder")` (the read model always sets `instance` in that case, so only a hand-made target can
   hit it).
3. The folder exists, is not inside the vault root, is not the vault root, and
   `savedir.platform_of(folder) == target.platform` (for an empty Steam account folder: name
   `st_<digits>` or `DefaultUser`).
4. Xbox: `containers.index` present (else `XGS_READ_ONLY`), its header parses, and the folder's
   parent is named `wgs`.
5. `cloud_mode` from `steam_autocloud.vdf` presence; `xgs_twin` from `msstore.xgs_twin`.

The resolved folder path never leaves core (UIs get labels only).

### 2.4 The operations

Every live operation is one `op` and runs through the same `execute` (2.8): resolve, preserve,
snapshot, journal, write, validate, record. They differ only in what they write. Every one
**replaces**; none deletes a save file or leaves a slot empty (F8, I9).

| Operation | Preserved first | Writes | Undo (2.11) |
|---|---|---|---|
| **Place** (T occupied) | both present members of T | A and B of T (2.5) | Place the evicted pair back |
| **PlaceIntoEmpty** | - | A and B of T | blocked: it would empty T (`UNDO_WOULD_EMPTY`, the steps of 2.4.2) |
| **Restore-older** (T holds the source's pid) | both members (the newer tip) | A and B of T | Place the evicted pair back |
| **Swap** (both occupied) | both slots | the 4 positions; each pair keeps its positions, metas stamped now (2.5) and re-keyed on Steam | Swap back |
| **Promote** (make position p load) | both members (keeps the exact old metas) | A and B of T: same data, metas stamped now with p newest (2.5) | Place the old pair back with its old loading position |
| **Undo** of op X | T's current content when it differs from what X wrote | the inverse of X | redo X (an undo is an op) |
| **Keep a copy** (2.4.1) | - | nothing live; a vault entry of T | none needed (nothing live changed) |

A Swap with an empty side would vacate a slot and is blocked: `SWAP_EMPTY_SIDE` (block) "Steam
slot 3 is empty, and the app never empties a slot. Use Move to live to put a save into it."

Rules shared by all of them:

- **Both positions are written** whenever a slot is filled (I5). A source with one member, or
  with members of two pids, fills the other position with a copy of V (2.5). No operation ever
  leaves a mixed slot (P6).
- **Data is verbatim.** Only metas change, and only by: Steam re-key (ordinal of the destination
  position), the timestamp stamp of 2.5, and F6 conversion (3.5).
- **Never touched:** `accountdata.hg` / `mf_accountdata.hg` / the Xbox `AccountData` record and
  its xgs folder, `steam_autocloud.vdf`, `cache/`, any other slot, any other account's folder or
  `containers.index`, and every file the op did not create (I7).
- A **cross-folder Swap** (two accounts of one platform) is one op over two targets with two
  snapshots and the `OTHER_ACCOUNT` confirmation (F7b). A cross-platform Swap is two transfers,
  gated by F6.

#### 2.4.1 Keep a copy (vault only)

`plan_keep(ctx, target)` plans a copy of one live slot into the vault, for a slot whose save has
no vault copy yet (D6 offers it on the board and in the empty-slot steps). The plan has kind
`keep`, no writes, the slot's present members as `evictions`, `NOT_IN_VAULT` per member that is
not durable, and the blocks `VAULT_UPGRADE_NEEDED`, `VAULT_UNAVAILABLE`, `VAULT_SPACE`;
the game may run (nothing live is written; D3's stable-read check refuses a file that changes
while it is read). `execute` takes the live-op lock, re-checks the fingerprint (`stale` on a
change), journals `preparing`, calls `VersionStore.add_from_live(target_dir, platform=...,
account_key=..., kind="extract", label="kept: <slot label>", scope=CaptureScope(slots=(n,)),
op_id=op_id, reason="keep")`, appends an oplog line (`op: "keep"`, `entry_id` = the new entry)
and `done`. No `record_op` (no live change); D1's scanner picks the entry up as a capture.
Outcome: "'Main Save' from Steam slot 7 is in the vault."

#### 2.4.2 Emptying a slot and empty slots (F8)

The app never empties a slot. `empty_slot_guide(ctx, target)` returns what the UI shows when the
owner asks to empty a slot and on the card of an empty slot (read-only):

```python
@dataclass(frozen=True)
class EmptySlotGuide:
    target: LiveTarget
    label: str                           # "Steam slot 7"
    cloud_mode: Literal["steam-cloud", "steam-local", "xbox"]
    occupied: bool
    durable: bool                        # every present member has a vault copy; else the UI
                                         # offers Keep a copy (2.4.1) first
    refill: Warning | None               # EMPTY_SLOT_MAY_REFILL; None on steam-local
    steps: tuple[str, ...]               # numbered manual steps for this cloud mode
```

| Cloud mode | `refill` text (`EMPTY_SLOT_MAY_REFILL`, info) | `steps` |
|---|---|---|
| steam-cloud | "While this slot is empty, Steam Cloud may put a save back into it at the next game start." | 1 Close No Man's Sky. 2 In Steam: No Man's Sky > Properties > General, turn off "Keep game saves in the Steam Cloud". 3 In the save folder delete the four files of this slot (`save13.hg`, `mf_save13.hg`, `save14.hg`, `mf_save14.hg`). 4 Start the game once and quit. 5 Turn Steam Cloud back on; if Steam asks, keep the copy on this PC. (today's text, `operations.py:629-634`, with the file names) |
| steam-local | none | 1 Close No Man's Sky. 2 In the save folder delete the four files of this slot (names as above). |
| xbox | "While this slot is empty, Xbox sync may put a save back into it." | 1 Start No Man's Sky. 2 Delete the save in the game's own load menu. 3 Quit and let Xbox sync finish. (INFERRED: Xbox slot records cannot be edited by hand; the owner confirms these steps once, 7.2) |

The board's empty-slot marker and card take `refill` from here; the next D1 scan shows whether the
slot stayed empty (D1's journal reports a `resurrected` event if Steam Cloud refilled it).

### 2.5 What is written: positions, metas and times

`pairmeta.compute(src, rt, now) -> tuple[MemberWrite, MemberWrite]` is a pure function (no I/O)
so every case is unit-testable. It implements the owner's F4a (the version with its own same-id
partner) and F5b (stamp "now"; the original time stays in the vault record).

```python
def compute(src: SourcePair, rt: ResolvedTarget, now: int):
    L = src.load                                   # position V was captured in
    O = other(L)
    V = src.member(L)                              # bytes + meta plaintext (locator.open(prefer))
    P = src.member(O)                              # partner or None
    use_partner = P is not None and P.pid == V.pid
    Pw = P if use_partner else V                   # O position gets the partner or a copy of V

    # 1) meta timestamps (F5b): V = now, the other position = now - 1
    #    (metas without a timestamp field keep their bytes, 2.5.2)
    ts_L, ts_O = now, now - 1

    # 2) metas: timestamp stamp, then platform encoding
    plain_L = with_ts(V.meta_plain, ts_L, rt.platform)
    plain_O = with_ts(Pw.meta_plain, ts_O, rt.platform)
    if rt.platform == "steam":
        disk_L = meta.encrypt(plain_L, storage_ordinal(file_no(slot, L)))   # re-key
        disk_O = meta.encrypt(plain_O, storage_ordinal(file_no(slot, O)))
        assert meta.is_valid_for(disk_L, ...) and meta.is_valid_for(disk_O, ...)
    else:
        disk_L, disk_O = plain_L, plain_O          # plaintext; same bytes to wgs and xgs

    # 3) on-disk times: V newest by mtime / FILETIME as well, in every cloud mode
    t_L, t_O = float(now), float(now - 1)
    return (MemberWrite(position=L, source_meta_ts=V.meta_ts, ...),
            MemberWrite(position=O, source_meta_ts=Pw.meta_ts,
                        copy_of=None if use_partner else L, ...))
```

`MemberWrite` gains `source_meta_ts: int | None` (the captured meta time before the stamp); the
oplog line carries it, and the source version keeps its original meta in the vault entry it came
from (D1's op record points there through `OpLineage.source`). The placed live copy therefore is
a meta-only variant of the version: same `vid`, different meta (D1 `MetaSnap.meta_sha`).

#### 2.5.1 Why these rules

- **Positions are kept as captured.** The captured pair goes back in its positions; the default
  `load` is the newer member, which R7 2.4 measured as also the most progressed (581/581 pairs).
- **"V newest by every signal"** (C23, R8 C5): meta timestamp (what the load screen sorts by,
  DESIGN.md), Steam file mtime order (what Steam Cloud and any mtime fallback see) and Xbox
  FILETIME order (what today's app and possibly the Xbox app use when the meta has no time)
  all put L above O. Strict, not equal: a tie has no defined winner.
- **"Now" everywhere** (F5b): the load screen shows the placed save as the newest, and a file
  stamped older than the cloud's copy cannot be replaced by it at the next launch (project rule,
  `operations.py:253-257`; R8 G5). Xbox FILETIME = now follows R8 Q8 (the record's local write
  time). The meta and the disk times agree, so the unknown "meta time far older than the file"
  case never arises.
- **A copy of V in O** (single-member or mixed source) is stamped `now - 1`, so V loads and the
  copy is the fallback the game offers - the same playthrough, never a different one.

#### 2.5.2 Edge cases

| Case | Rule |
|---|---|
| V's meta has no timestamp field (Xbox 280-B Waypoint meta, pre-Waypoint Steam) | meta bytes unchanged; order by disk time only (the game has nothing else to sort by) |
| partner newer than V or with more play time (the user picked the restore point) | partner written with `now - 1`; V loads |
| source pair members have different pids | treated as single-member (copy of V); `STALE_SOURCE_MIXED` warning |
| Promote of position p | `src` = the slot's own pair with `load = p`; both metas stamped (p = now) |
| Undo | the evicted pair goes back through `compute` too: stamped now, its old loading position as `load` (2.11) |
| Panic restore | not `compute`: the snapshot's slot files are restored byte-exact, metas included; disk times now (4.3) |
| F6 transfer | after step 2, `convert.steam_to_xbox` / `xbox_to_steam` replace the platform encoding (3.5) |

### 2.6 Slot writers

Both writers split into a pure `prepare` (computes names, bytes and the pre-image, writes
nothing), an `apply`, a `finalize` and an exact `rollback`. The journal's `writing` event (2.7)
records the prepared plan **before** `apply` creates the first file, so recovery always knows
every file the op may have created.

#### 2.6.1 Steam (`liveops/steamwrite.py`)

```python
@dataclass(frozen=True)
class FileState:
    sha256: str
    size: int
    mtime_ns: int

@dataclass(frozen=True)
class SteamFileWrite:
    name: str                       # "save13.hg" / "mf_save13.hg"
    sha256: str
    mtime: float
    temp_name: str                  # ".tmp_<op4>_save13.hg.part" (in the target folder)

@dataclass(frozen=True)
class SteamWritePlan:
    folder: Path
    files: tuple[SteamFileWrite, ...]          # in swap order
    pre: Mapping[str, FileState | None]        # every name the op touches; None = absent

def prepare(folder: Path, writes: Sequence[tuple[MemberWrite, bytes, bytes]],
            slot: int, op_tag: str) -> SteamWritePlan: ...
def stage(plan: SteamWritePlan, payload: Mapping[str, bytes]) -> None: ...
def swap(plan: SteamWritePlan, faults: FaultInjector | None) -> None: ...
def rollback(plan: SteamWritePlan, pre_bytes: Mapping[str, bytes]) -> None: ...
def sweep(folder: Path, op_tag: str) -> list[str]: ...
```

- **Stage:** every new file is written as `.tmp_<op4>_<name>.part` in the target folder (same
  volume, so `os.replace` is atomic), flushed and fsynced, then re-read and compared by sha256.
  Nothing visible to the game or Steam has changed yet.
- **Swap order:** O data, O meta, L data, L meta - the meta that makes V the loading member is the
  last rename. After each rename `os.utime(name, (t, t))`.
- **Bounded retry:** `os.replace` onto a file another process holds open fails on Windows
  (VERIFIED 9.5: `PermissionError`, winerror 5; Steam client or an antivirus scan can hold a
  save briefly). Retry 5 times, 200 ms apart, per file; then abort the swap and roll back the
  files already swapped.
- **No deletes.** A slot is only ever overwritten (F8).
- **Rollback (exact):** for every name in `pre`: if it existed, write its pre-op bytes
  (atomic) and set its pre-op `mtime_ns` from the journal's `pre` record; if it did not, delete
  it (a file this op created). Pre-op bytes come from the op's store snapshot
  (`VersionStore.read_file(snapshot_id, name)`), checked against `pre[name].sha256`; fallback:
  the preserve entry (`StoreLocator.open(vid, prefer=<preserved location>)`; data verbatim, and a
  Steam meta re-encrypted from its plaintext with the same ordinal is byte-identical because
  XXTEA here is deterministic). Then `sweep`. Under Steam Cloud this restores exactly the state
  Steam's manifest already knows, and deleting a file the op created is safe because Steam syncs
  only at game launch and exit, so it never saw that file (R8 E1).

#### 2.6.2 Xbox (`msstore.py` additions)

Today's `write_save` rewrites `containers.index` once per member, deletes the superseded blobs
immediately, and rewrites every other record from float seconds. VERIFIED (9.3): none of the 7
real indexes survives `parse_containers_index` + `build_containers_index` byte-identically;
FILETIMEs drift by up to 16 ticks (1.6 us) on 28/217 records and on every header. The new
primitives edit the index at the record-bytes level (VERIFIED prototype, 9.4):

```python
@dataclass(frozen=True)
class XboxRecordWrite:
    identifier: str                 # "Slot7Auto" / "Slot7Manual"
    data: bytes
    meta: bytes
    when: float                     # FILETIME as unix seconds; converted once to an int

@dataclass(frozen=True)
class XboxPairPlan:
    folder: Path
    pre_index_sha256: str
    pre_header_raw: bytes                       # index bytes 12..first record (ids, time, state, footer)
    pre_records_raw: Mapping[str, bytes | None] # identifier -> raw record, None = absent
    new_header_raw: bytes                       # FILETIME + state patched as integers
    new_records_raw: Mapping[str, bytes]
    new_files: tuple[str, ...]                  # relative "<DIRGUID>/<GUID>", ".../container.<n>"
    new_dirs: tuple[str, ...]                   # record dirs created for new identifiers
    superseded: tuple[str, ...]                 # old blobs + old container.<n>, deleted in finalize
    xgs: bool

def prepare_pair(folder: Path, writes: Sequence[XboxRecordWrite]) -> XboxPairPlan: ...
                                                # reads only; GUIDs pre-generated
def apply_pair(plan: XboxPairPlan, payload: Mapping[str, tuple[bytes, bytes]],
               faults: FaultInjector | None = None) -> None: ...
def finalize_pair(plan: XboxPairPlan) -> None: ...
def rollback_pair(plan: XboxPairPlan) -> None: ...
def split_index(raw: bytes) -> tuple[bytes, list[tuple[str, bytes]]]: ...
def join_index(header: bytes, records: Sequence[tuple[str, bytes]]) -> bytes: ...
```

- **apply_pair:** (1) write each identifier's new data and meta blobs and its next
  `container.<n>` under the pre-generated GUID names (atomic per file; existing record dirs, or
  a new dir for a new identifier); (2) write **one** new `containers.index` (`os.replace`):
  untouched records copied raw, our records rebuilt (state Modified, or Created for a new
  identifier; FILETIME from an int), header FILETIME and state patched as integers; both
  positions flip together; (3) mirror both identifiers into xgs when the twin exists (wgs first,
  then xgs, as today; read-back compare).
- **finalize_pair** (after validation): delete `superseded` (the replaced blobs of our own two
  records, now unreferenced; never a record). Until then the old blobs stay on disk,
  unreferenced, so an exact rollback needs nothing from the vault.
- **rollback_pair:** re-read the index; write `pre_header_raw` + for our identifiers the
  `pre_records_raw` (dropping records that were absent) + every other record as it is now;
  delete `new_files` and empty `new_dirs`; restore the xgs folders of our identifiers from the
  old blobs (or remove them if the identifier was absent). VERIFIED in the prototype for an
  existing and a new identifier: index byte-identical, wgs tree identical, xgs identical. The
  records go back to their old sync states (e.g. Synced), so an aborted op leaves nothing for the
  Xbox app to upload.
- **Concurrency guard:** `prepare_pair` stores `pre_index_sha256`; `apply_pair` re-reads the index
  immediately before step 2 and aborts (nothing referenced yet; new blobs deleted) if it changed:
  the Xbox app may still be writing sync states right after the game exits (R8 E2). The planner
  also blocks with `XBOX_SYNC_BUSY` while the index's mtime is under 60 s old.
- `write_save` and `mark_modified` are re-implemented on the same raw editing (fixes P-X1 for the
  classic paths too); `delete_save` too while the classic Clear still exists (phases 0-1; it
  becomes the steps of 2.4.2 in phase 2). The parser keeps each record's raw bytes and integer
  FILETIME (`_Container.raw`, `_Container.filetime`).

### 2.7 The operation journal

#### 2.7.1 Location and format

- File: `<vault>/organizer/ops.jsonl` (D1 reserved it for D5). Append-only, one JSON object per
  line, UTF-8 with `ensure_ascii=True`, compact separators. Paths inside are relative to the
  target folder; account folder names appear only as `account_key` (never displayed unredacted).
- Lives in the vault, beside the preserved versions and the snapshot it refers to, because
  recovery needs all three. A vault on a removable drive is covered by a local marker (2.7.4).
- Each append is one short transaction under D1's store lock (`safety.exclusive_lock`, D1
  convention: the lock is held per read-modify-write, never across a whole operation):
  `f.write(line)` once, `flush`, `os.fsync`. On POSIX the directory is fsynced when the file is
  created. The whole operation is serialised separately by the **live-op lock** (2.16).
- Reader: parse line by line; an unparsable **last** line is a torn append (crash during the
  write) and is ignored with a log line; an unparsable middle line is logged and skipped. Never
  rewritten in place (no compaction in v1; ~3-6 KB per op).
- Separate from D1's `journal.jsonl` (live observations) and from the vault's `oplog.jsonl`
  (audit, 2.13).

#### 2.7.2 Events

Every line: `{"j": 1, "op_id", "seq", "event", "utc", "machine"}` plus the event's payload.

| Event | When | Payload (abridged) |
|---|---|---|
| `preparing` | `execute` starts, before any vault write | `kind`, `app`, `targets[] {platform, account_key, slot}`, `source {vid_a, vid_b, load, prefer}`, `undo_of`, `note`, `confirmed[]`, `plan_digest` |
| `writing` | after preserve + snapshot + writer prepare, **before the first live write** (the write-ahead intent) | per target: `cloud_mode`, `snapshot_id`, `preserved[] {position, vid, location, already}`, `pre {A, B: {vid, pid, data: FileState or null, meta: FileState or null}}`, `post {A, B: MemberWrite}`, `steam {files[] {name, temp, sha256, mtime}}` or `xbox {pre_index_sha256, pre_header_b64, pre_records_b64{id: b64 or null}, new_records_b64{}, new_files[], new_dirs[], superseded[], xgs}` |
| `validated` | post-write validation passed | per target `ok` |
| `done` | oplog line and lineage recorded | `oplog: true`, `lineage: true` |
| `rolled_back` | automatic rollback succeeded | `stage` (`apply`/`validate`), `error`, `restored: true` |
| `failed` | rollback failed or could not be verified | `stage`, `error`, `rollback_error` |
| `abandoned` | ended without a live change | `reason`: `not_started`, `preserve_failed`, `snapshot_failed`, `game_started`, `stale`, `superseded`, `user_left` |
| `recovered` | a recovery choice was applied (followed by `done` or `rolled_back`) | `choice`: `forward`/`back` |
| `verify` | post-launch verification changed state (2.12) | `target`, `state`, `detail`, `observed {vid_a, vid_b}` |

Status of an op = its last lifecycle event (all but `verify`). **Open** ops: last event
`preparing`, `writing`, `validated` or `failed`. Verify state of a target = its last `verify`
event, `pending` after `done`.

Example `writing` line for a Steam Place (wrapped here; one line on disk):

```json
{"j":1,"op_id":"op-20261016T101502Z-3fa2","seq":2,"event":"writing","utc":"2026-10-16T10:15:03.120Z",
 "machine":"m-6f1c2a","targets":[{"target":{"platform":"steam","account_key":"st_<steamid64>","instance":"","slot":7},
 "cloud_mode":"steam-cloud","snapshot_id":"snapshot-steam-20261016-131502",
 "preserved":[{"position":"A","vid":"<vid A old>","location":{"kind":"store","ref":"preserve-steam-20261016-131502","slot":7,"member":"A"},"already":false},
              {"position":"B","vid":"<vid B old>","location":{"kind":"store","ref":"preserve-steam-20261016-131502","slot":7,"member":"B"},"already":false}],
 "pre":{"A":{"vid":"<vid A old>","pid":"<pid16>","data":{"sha256":"<vid A old>","size":1800000,"mtime_ns":1791900000000000000},"meta":{"sha256":"<sha>","size":432,"mtime_ns":1791900000000000000}},"B":{...}},
 "post":{"A":{"position":"A","vid":"<vid A new>","pid":"<pid16>","meta_ts":1792500903,"source_meta_ts":1791500000,"meta_plain_sha256":"...","meta_disk_sha256":"...","disk_time":1792500903.0,"ordinal":14,"copy_of":null,"origin":{...}},"B":{...}},
 "steam":{"files":[{"name":"save14.hg","temp":".tmp_3fa2_save14.hg.part","sha256":"...","mtime":1792500902.0},
                   {"name":"mf_save14.hg","temp":".tmp_3fa2_mf_save14.hg.part","sha256":"...","mtime":1792500902.0},
                   {"name":"save13.hg","temp":".tmp_3fa2_save13.hg.part","sha256":"...","mtime":1792500903.0},
                   {"name":"mf_save13.hg","temp":".tmp_3fa2_mf_save13.hg.part","sha256":"...","mtime":1792500903.0}]}}]}
```

#### 2.7.3 Relation to the other records

| Record | Owner | Role for an op |
|---|---|---|
| `organizer/ops.jsonl` | D5 | state machine of the op: intent, progress, outcome, verification; the source of truth for recovery, undo and verification |
| `oplog.jsonl` (vault root) | D5 writes, everyone reads | one v2 line per finished op (`done`, `rolled_back`, `failed`): the audit trail (2.13) |
| `organizer/lineage.json` + `journal.jsonl` | D1 | `record_op` gives D1 the binding "T holds pair P since op_id" (C16) and the evicted pair; D1's live journal then observes the game's later versions and emits events that D5's verification consumes |
| store entries | D3 | the preserve entry (kind `preserve`, the evicted members) and the pre-op snapshot (kind `snapshot`, the whole folder); both durable, never pruned |

#### 2.7.4 Local marker (vault on another drive)

Right after the `writing` event, `execute` writes
`platform.user_cache_dir(host)/vaults/<vault_id>/open-ops/<op_id>.json` (D1's per-machine,
per-vault folder) = `{op_id, vault_id, vault_root, labels[], utc}` (atomic), and
deletes it after the terminal event. At startup the app lists `vaults/*/open-ops/`; a marker
whose vault is not reachable yields a `vault_missing` recovery item: "A move into Steam slot 7 was
interrupted and its records are on the vault drive. Connect it before playing." The marker holds
no save data; losing it (cache cleaned) loses only that hint. A per-machine id
(`platform.user_data_dir(host)/machine-id`, random, deliberately not in the cache folder) tags
every event so a vault shared between PCs (D11) never recovers another machine's op here.

### 2.8 Execute

```python
def execute(ctx, plan, confirmed, progress=None, *, note="") -> OpOutcome:
    if plan.blocked:
        return refused(plan)
    missing = plan.confirmations - confirmed
    if missing:
        raise ConfirmationRequired(missing)
    if plan.kind == "keep":
        return execute_keep(ctx, plan, note)                            # 2.4.1: no live write
    with liveop_lock(ctx, timeout=10):                                  # one live write at a time (2.16);
                                                                        # timeout -> refused(code="BUSY")
        # 1 re-check what can change between plan and execute (none of these writes anything)
        if vault_mode(ctx.vault.root) != "store":                       # D3 core.store.vault_mode
            return refused(plan, code="VAULT_UPGRADE_NEEDED")
        if fingerprint_now(ctx, plan) != plan.fingerprint:
            return stale(replan(ctx, plan))
        if open_ops_on(ctx, plan.accounts()):
            return refused(plan, code="RECOVERY_PENDING")
        if (why := vault_space_problem(ctx, plan.estimate)):           # VAULT_UNAVAILABLE / VAULT_SPACE
            return refused(plan, code=why)
        if (why := game_problem(ctx, confirmed)):                       # GAME_RUNNING / unconfirmed unknown
            return refused(plan, code=why)
        ctx.lineage.observe(scan_targets(ctx, plan), src="scan",        # D1: journal the pre-op state
                            skip=busy_targets(ctx))
        op_id = new_op_id(ctx.clock)                                    # section 6 format
        jr.append(ctx, preparing(op_id, plan, confirmed, note))         # short store-lock txn
        try:
            # 2 the source: open, verify, compute final bytes
            payload = load_and_verify_source(ctx, plan)                 # sha256 == vid, chunk total
            # 3 preserve every member that will be replaced
            preserved = preserve_targets(ctx, plan, op_id)              # StoreLocator.preserve
            # 4 whole-folder pre-op snapshot per target folder, in the store
            snaps = {t: ctx.versions.add_from_live(folder(t), platform=t.platform,
                                                   account_key=t.account_key, kind="snapshot",
                                                   label=f"auto: {plan.kind}", scope=CaptureScope(),
                                                   op_id=op_id, reason=plan.kind)
                     for t in plan.folders()}
            # 5 writers prepare (reads the pre-image; compares with the plan fingerprint)
            wplans = prepare_writers(ctx, plan, payload, op_id)
        except Abandon as why:                                          # nothing live written
            jr.append(ctx, abandoned(op_id, why.reason))
            return stale(replan(ctx, plan)) if why.reason == "stale" else refused(plan, code=why.code)
        jr.append(ctx, writing(op_id, plan, preserved, snaps, wplans))  # WRITE-AHEAD
        marker.write(ctx, op_id, plan)
        probe = ctx.game_probe()                                        # I1: last check, no write yet
        if probe is True or (probe is None and "GAME_STATE_UNKNOWN" not in confirmed):
            jr.append(ctx, abandoned(op_id, "game_started")); marker.remove(ctx, op_id)
            return refused(plan, code="GAME_RUNNING" if probe else "GAME_STATE_UNKNOWN")
        try:
            apply_writers(ctx, wplans, payload)                         # 2.6
            validate(ctx, plan, wplans)                                 # 2.9.1
        except Exception as exc:
            return rollback_and_report(ctx, op_id, plan, wplans, snaps, exc)   # 2.9.2
        jr.append(ctx, validated(op_id))
        finalize_writers(wplans)                                        # Xbox superseded, Steam sweep
        record(ctx, op_id, plan, preserved, snaps, note)                # 2.13; errors -> stays 'validated'
        ctx.lineage.observe(scan_targets(ctx, plan), src="verify",      # D1: the post-write state
                            skip=busy_targets(ctx) - set(plan.target_list()))
        jr.append(ctx, done(op_id)); marker.remove(ctx, op_id)
    return OpOutcome(op_id, "done", outcome_text(plan), undo=undo_offer(plan, op_id),
                     verify="pending", warnings=..., error=None)
```

Notes:

- **Preserve** calls `ctx.locator.preserve([LiveMember(target, position, expected_vid), ...],
  op_id=op_id, reason=kind)` once per target slot. D1's `VaultLocator` hands it to D3's
  `StoreLocator.preserve`: it re-reads the live slot, keeps both members, writes one store entry
  of kind `preserve` (id `preserve-<plat>-<stamp>`; written every time, ~3 KB of manifest when the
  objects already exist) and verifies every object it references before returning durable
  locations. Error mapping, each an `Abandon` (nothing live written): `PreserveMismatch` or
  `SourceChanged` -> `stale` with a fresh plan; `PreserveFailed("store-unavailable")` ->
  `VAULT_UPGRADE_NEEDED`; `StoreUnavailable` -> `VAULT_UNAVAILABLE`; `InsufficientSpace` ->
  `VAULT_SPACE`; D1 `LockTimeout` -> `BUSY` (retry notice); any other `StoreError` ->
  `PRESERVE_FAILED:<class>`. An unreadable member is preserved as raw files (its `vid` is still
  the data sha256).
- **Snapshot** = the whole target folder (Steam `*.hg` + vdf; Xbox the whole account folder) as a
  store entry of kind `snapshot`. The store deduplicates, so it costs only the objects it does
  not hold yet (after preserve, usually none). It is the exact pre-image for rollback (2.9.2),
  the `snapshot_id` of the oplog line and the source of a panic restore (4.3). Store snapshots are
  durable and never pruned. Failure -> `abandoned(snapshot_failed)`, code `SNAPSHOT_FAILED`.
- **Locks:** the live-op lock is the outer lock for the whole call; D1's store lock is taken
  only inside short transactions (journal appends, `record_op`, `observe`) and D3 takes it only
  for each manifest commit. Neither is ever held across another call (2.16).
- **Progress** is reported at each numbered step; the GUI shows it without blocking Tk (C31) and
  must not let the app close while `execute` runs (the GUI worker is a daemon thread today,
  `gui.py:1368`; closing would kill a write mid-way).
- **No game override.** Today's `allow_game_running` / GUI "Proceed anyway (risky)?"
  (`gui.py:1308-1311`) does not exist for organizer ops: the game would overwrite the slot at its
  next auto-save and the Xbox index can change under us. Unknown (`None`) is a confirmation.

### 2.9 Validation and slot-scoped rollback

#### 2.9.1 Validation (after the writes, before anything is recorded as done)

| Check | Steam | Xbox |
|---|---|---|
| bytes | for each written position: `sha256(data) == vid`, `sha256(meta file) == meta_disk_sha256` | blobs referenced by the new records: same two hashes |
| meta | decrypts with the position's ordinal (header `0xEEEEEEBE`); `SizeDecompressed == lz4_block.stats(data).total_decompressed`; meta id (0x15C) == pid when pid is a SaveUniversalId | (after P1) id 0x118 == pid; for 360-B / format 2004 metas `u32 0x10 == chunk total` (it held the compressed size in the Worlds I era, libNOM.io; VERIFIED 41/42 stream metas, 9.1) |
| order | `SlotView.newest.label == L`; `mtime(L) > mtime(O)` | `SlotView.newest.label == L`; meta time L > O; FILETIME L > O |
| records | - | our records: state Modified/Created, FILETIME as planned (integer); every other record raw-identical to the pre-image |
| mirror | - | when xgs exists: `xgs/<id>/data` and `meta` byte-equal to the wgs blobs |
| containment | every other `*.hg` and the vdf unchanged (name, size, `mtime_ns` vs the snapshot manifest); no `.tmp_<op4>_*` left; no file of the folder is missing | header identity unchanged (process id, account id); no record removed |

#### 2.9.2 Rollback (automatic, slot-scoped, exact)

On any exception in apply or validation:

1. Steam: `steamwrite.rollback` - every touched name gets its pre-op bytes and pre-op
   `mtime_ns` back (`os.utime(ns=...)`) or is deleted if it did not exist; temp files swept.
   Xbox: `msstore.rollback_pair` - index back to the pre-image bytes, new blobs and dirs
   deleted, xgs restored from the old blobs.
2. Verify the pre-image: Steam `FileState` (sha256, size, `mtime_ns`) per name; Xbox
   `sha256(containers.index) == pre_index_sha256` and xgs equal.
3. Success -> journal `rolled_back`; oplog v2 line with `status: "rolled_back"`; lineage
   `record_op(status="rolled_back")`; outcome `rolled_back` with the error ("Nothing changed in
   Steam slot 7. The move failed: ..."). Failure -> journal `failed`; the outcome offers the
   **panic restore** of this folder from the op's snapshot (4.3) and the item stays open (2.10).

Other slots, accountdata and the vdf are never part of a rollback - unlike today's undo-based
recovery, which restores the whole folder including accountdata (R8 B4, R1 E11).

### 2.10 Recovery of an interrupted operation (next start)

`pending_recovery(ctx)` runs at app start (CLI: `nmsvault recover`), before any new live op, and
whenever the UI asks. It only reads, except for the automatic cases below.

```python
@dataclass(frozen=True)
class RecoveryItem:
    op_id: str
    kind: str
    labels: tuple[str, ...]                 # "Steam slot 7"
    state: Literal["not_started", "complete", "torn", "superseded", "failed",
                   "other_machine", "vault_missing"]
    recommended: Literal["forward", "back", "leave", "none"]
    choices: tuple[str, ...]
    text: str
    started: str                            # local ISO time
```

Classification of an open op, per target, by comparing the live files with the journal:

- Steam: each touched name is `pre` (matches `pre` FileState, or absent when pre was absent),
  `post` (matches the planned sha256), or `other`; `.tmp_<op4>_*` files are noted.
- Xbox: index `pre` (sha256 == `pre_index_sha256`), `post` (our records raw == planned and the
  rest raw == pre), or `other`; each xgs identifier `pre`/`post`/`other`.

| Last event | Live state | Action |
|---|---|---|
| `preparing` | (nothing live can have changed) | automatic: `abandoned(not_started)`; one-line notice |
| `writing` | all `pre` | automatic: sweep the op's temp / unreferenced new files; `abandoned(not_started)` |
| `writing` | all `post` (crash after the last write) | recommended **forward**: validate, finalize, record, `done` |
| `writing` | mix of `pre` and `post` only (torn) | choice **forward** (recommended when the source and the preserved copies open and re-verify) or **back** |
| `writing` | any `other` (the game or another tool wrote the slot since) | **leave** only: preserve the slot's current content if not durable, `abandoned(superseded)`; the evicted save is in the vault; explain |
| `validated` | (writes verified) | automatic forward: finalize + record (both idempotent by `op_id`), `done` |
| `failed` | any | choices **back** (retry rollback), **panic restore** of the folder from the op's snapshot (4.3), **leave** |
| any, other `machine` | - | `other_machine`: "This move was started on another computer"; no action |
| marker only, vault not reachable | - | `vault_missing` (2.7.4) |

- **Forward** re-derives the post bytes from the source (`locator.open` + `pairmeta`) and
  requires them to hash exactly to the journal's `post`; Steam re-stages and swaps only names
  not yet `post`; Xbox re-applies with the recorded GUID names when the index is `pre`, or only
  mirrors xgs + finalizes when it is `post`. Then validation, recording, `recovered` + `done`.
- **Back** = the exact rollback of 2.9.2 using the recorded pre-image, then `recovered` +
  `rolled_back`.
- The game must be closed (same guard as execute); with the game running, items stay open and
  the UI says "Close No Man's Sky to finish".
- While an op is open on an account, new live ops there are blocked (`RECOVERY_PENDING`); the rest
  of the app works.
- Nothing is decided by time: an op interrupted months ago classifies the same way.

### 2.11 Undo (slot-scoped, non-destructive, multi-level; C24)

`plan_undo(ctx, op_id)` builds an ordinary plan from the op's journal record and the slot's
**current** content, so an undo can never overwrite progress silently and never touches another
slot. Per target of the op, the current pair is classified against what the op wrote (`post`)
and what it evicted (`pre`):

| Current content of T | Class | Undo plan for T |
|---|---|---|
| exactly the written pair (same vids per position) | `same` | Place the evicted pair back (kind `undo`); the written pair came from the vault, so preserve copies nothing |
| same pid as the written pair, play time >= (D1 binding: a descendant) | `progressed` | Place the evicted pair back; preserve first stores the newer versions (D1 records them as children of V); confirmation `PLAYED_SINCE`: "You played 1h05m in Steam slot 7 since the move. That progress stays in the vault as the newest version of 'Main Save'." |
| exactly the evicted pair (a cloud revert already undid it) | `already` | blocked `ALREADY_UNDONE`: "Steam slot 7 already holds 'Polestar' again." |
| empty (emptied outside the app since) | `empty` | PlaceIntoEmpty the evicted pair |
| anything else (another pid, or the same pid with less play) | `stale` | blocked `UNDO_STALE`: "Steam slot 7 has changed since (it now holds 'X'). Use Move to live to put 'Polestar' where you want it." The UI may open the picker with the evicted pair as the source. |

Per op kind:

- **place / restore_older / promote**: as the table. The evicted pair goes back from its preserve
  entry in its positions, with the position that was loading before as `load`, and like every
  pair write it is stamped now (2.5, F5b); its original meta times stay in the preserve entry and
  the oplog. A slot that held one member before gets that member in both positions (2.5, I5) -
  never a mixed slot, never a deletion.
- **place_into_empty**: the inverse would empty T, so the undo is blocked: `UNDO_WOULD_EMPTY`
  (block) "Undoing this would empty Steam slot 1. The app never empties a slot; see the steps to
  do it yourself." The UI then shows `empty_slot_guide(T)` (2.4.2), which offers Keep a copy
  (2.4.1) first when the slot's current content is not durable.
- **swap**: swap back when both slots are `same` or `progressed` (progress travels with its
  save); any `stale` slot blocks the whole undo with the per-slot explanation.
- **undo**: the inverse of an undo is the original op again (redo), same rules.
- Multi-level: `op_history(target)` lists the target's ops newest first; the UI offers Undo for
  the newest `done` op of each target (the non-modal "Done - Undo" of C30) and from the history
  list for older ones. Each undo is a new op with `undo_of`, so it is itself undoable.
- Whole-folder undo survives only as the **panic restore** (4.3).

### 2.12 Post-launch verification (C25)

A placed save can be undone outside the app: Steam Cloud offers its copy at the next launch and
the player may pick it (R8 R1); Xbox sync or a console session can do the same (R8 R2). Every
`done` op therefore enters verification per target.

**Evidence D5 uses** (all read-only):

| Source | Gives |
|---|---|
| D1 `LineageRecorder.op_outcome(op_id, target)` and `JournalEvent`s from each `observe` | `confirmed` (a later observation shows a recorded child of the placed pair: the game saved on top of it), `reverted` (the slot holds the evicted pair again), `superseded` (a later op), `unexpected` with the `deleted` / `replaced` events (emptied outside the app, another pid); an unchanged placed pair stays `pending` in D1 |
| Xbox record sync state of our identifiers | Modified/Created after the write -> Synced later = uploaded |
| Steam `remotecache.vdf` (optional reader, 2.12.2) | our files' SHA-1 present and synced = uploaded |
| game sessions seen by this app | `safety.is_game_running()` results recorded by each GUI refresh / CLI call into `user_cache_dir/vaults/<vault_id>/game-seen.json` (`last_seen_running` UTC) |

**States** per (op, target):

| State | Rule | UI (alert = shown until dismissed) |
|---|---|---|
| `pending` | no evidence yet | slot badge "Waiting for the next game launch to confirm" |
| `confirmed` | a child of the placed pair is live (detail `played`); or the placed pair is still live after a game session seen after the op (`unchanged`); or uploaded (Xbox Synced, Steam manifest) (`uploaded`) | none; tick in the op history |
| `reverted` | the slot holds the evicted pair, or another version of the evicted pid that is not a descendant of the placed pair | alert: "Steam Cloud put the old save back in Steam slot 7. 'Main Save' is safe in the vault." Actions: Put it back (a new `plan_place` of the placed pair), Dismiss. Hint: "When Steam asks at launch, choose the copy on this PC." |
| `deleted` | the slot is empty while the placed pair was expected | alert: "Steam slot 7 is empty again. 'Main Save' is safe in the vault." Action: Put it back |
| `replaced` | another pid (the game started a new save there, or another tool) | info in the history |
| `superseded` | a later op on the target | none |
| `expired` | still `pending` after 30 days | none |

```python
@dataclass(frozen=True)
class VerifyItem:
    op_id: str
    target: LiveTarget
    label: str                           # "Steam slot 7"
    state: Literal["pending", "confirmed", "reverted", "deleted", "replaced", "superseded",
                   "expired"]
    detail: str                          # played | unchanged | uploaded | ...
    text: str
    actions: tuple[Literal["place_again", "show_steps", "dismiss"], ...]   # show_steps: the
                                         # "choose the copy on this PC" hint of a revert
    since: str                           # local ISO time of the op

def update_verification(ctx, events) -> list[VerifyItem]:
    """Called after every D1 observe (GUI refresh, app start, CLI 'scan'). For each (op, target)
    whose verify state is pending or alerting: derive the state from the evidence above; on a
    change append a `verify` event to ops.jsonl (short store-lock txn). Returns the items the UI
    should show (alerts + pending badges)."""
```

Dismissing an alert appends `verify {state, dismissed: true}`; the history keeps it. "Put it
back" never reuses the old op: it is a new Place from the vault (the placed versions are durable:
they came from the vault, and the store keeps everything).

#### 2.12.1 `confirmed`: D1 and D5 agree

D5's own `observe(src="verify")` right after the write holds the placed pair. D1's `op_outcome`
therefore reports `confirmed` only when a later observation shows a recorded child of the placed
pair; an unchanged placed pair stays `pending` in D1. D5 adds the evidence D1 does not have: a
game session seen after the op (`unchanged`) or an upload (`uploaded`).

#### 2.12.2 Steam `remotecache.vdf` reader (optional, phase 2b)

`steamcloud.read_manifest(steam_root: Path, accountid32: int) -> dict[str, ManifestEntry]`
parses `<steam_root>/userdata/<accountid32>/275850/remotecache.vdf` (format R8 E1: per file
`size`, `localtime`, `time`, `remotetime`, `sha` SHA-1, `syncstate`). Stdlib only (DESIGN.md D6);
the Steam root comes from `platform.steam_roots(host)` (DESIGN.md D7); `accountid32 = steamid64 -
76561197960265728` from the folder name. Used only for `uploaded` evidence and a planner
`PENDING_UPLOAD` hint on Steam; never displayed (the path holds the account id). Absent or
unparsable -> no evidence, never an error.

### 2.13 Oplog record v2 and lineage recording

#### 2.13.1 The oplog line

One line per op that reached `done`, `rolled_back` or `failed` (none for `abandoned`: nothing
changed), plus one per `keep`, appended to `<vault>/oplog.jsonl` as **one** `write` + `flush` +
`fsync` (VERIFIED 9.2 C: a torn last line makes today's `read_oplog` raise, so a partial line must
never be left). The first six keys are today's (`operations.py:958-966`), so today's readers
(classic history, CLI) read it unchanged; the rest are additive (R1 E4).

```json
{"ts": "2026-10-16T13:15:04", "op": "place", "entry_id": "preserve-steam-20261016-131503",
 "snapshot_id": "snapshot-steam-20261016-131503",
 "detail": "place 'Main Save' into steam slot 7; 'Polestar' kept in the vault",
 "changed": ["save14.hg", "mf_save14.hg", "save13.hg", "mf_save13.hg"],
 "v": 2, "op_id": "op-20261016T101503Z-3fa2", "utc": "2026-10-16T10:15:04Z",
 "app": "nms-save-vault 0.4.0", "kind": "place", "status": "done", "undo_of": null,
 "targets": [{"platform": "steam", "account_key": "st_<steamid64>", "instance": "", "slot": 7,
   "cloud_mode": "steam-cloud", "snapshot_id": "snapshot-steam-20261016-131503",
   "written": [{"position": "A", "vid": "<64 hex>", "pid": "<16 hex>", "play_time": 1900000,
                "meta_ts": 1792500904, "source_meta_ts": 1791500000, "disk_time": 1792500904.0,
                "ordinal": 14, "meta_plain_sha256": "<64 hex>", "meta_disk_sha256": "<64 hex>",
                "copy_of": null,
                "origin": {"kind": "store", "ref": "full-steam-20261005-174129", "slot": 9, "member": "A"}},
               {"position": "B", "...": "..."}],
   "evicted": [{"position": "A", "vid": "<64 hex>", "pid": "<16 hex>", "play_time": 100000,
                "meta_ts": 1791900000, "preserved_at": {"kind": "store", "ref": "preserve-steam-20261016-131503", "slot": 7, "member": "A"},
                "already_durable": false}, {"position": "B", "...": "..."}]}],
 "confirmed": ["PLAY_TIME_AHEAD"], "note": "", "error": null}
```

| Key | Rule |
|---|---|
| `ts` | local ISO seconds, as today |
| `op` | the D5 kind (`place`, `place_into_empty`, `restore_older`, `swap`, `promote`, `undo`, `panic_restore`, `keep`) |
| `entry_id` | the `preserve-` store entry of the first target, or the new entry of a `keep`; else null |
| `snapshot_id` | the store snapshot entry (kind `snapshot`, whole folder) of the first target's folder; each target lists its own; null for `keep` |
| `detail` | ASCII; save names allowed, never a folder name, path or account id (today's lines embed folder names that contain account ids, R1 E4) |
| `changed` | file names (Steam) or identifiers (Xbox) written, as today (never a deletion) |
| `targets[].account_key` | internal key; redacted on any display or export |
| `status` | `done`, `rolled_back`, `failed`; `error` holds the message for the latter two |

Field set = R7 2.8 items 1-5 and R8 4.6 (op id, UTC, app version, target, per written member
vid/pid/meta hashes/meta time and the source's meta time/disk time/ordinal/origin as a version
reference, per evicted member
hashes/id/where preserved/already durable, snapshot, cloud mode, confirmed warnings, note,
`undo_of`). R7 item 6 (the later "confirmed" mark) lives in `ops.jsonl` `verify` events and D1's
journal, not in the append-only oplog.

#### 2.13.2 Lineage

After validation, per target: `ctx.lineage.record_op(OpLineage(op_id, kind, at, target,
placed=PairKey(post A vid, post B vid), evicted=PairKey(pre) or None,
source=<Location of the placed bytes>, preserved={vid: Location}, status, undo_of, note,
app))` - D1 2.6.6; idempotent on `(op_id, target)`, so a Swap makes two calls and recovery may
repeat them. A `rolled_back` op is recorded with `status="rolled_back"` (D1 keeps the previous
binding). A panic restore records one call per written slot with kind `panic_restore` (D1 binds
the slot but marks no version placed or evicted); Keep a copy records none. Order: oplog line, then `record_op`, then the `done` journal event; on restart a
`validated` op re-runs the missing steps (the oplog writer first scans the file tail for the
`op_id`).

```python
@dataclass(frozen=True)
class OpSummary:                         # op_history() rows for the UIs
    op_id: str
    kind: str
    when: str                            # local time
    labels: tuple[str, ...]
    text: str                            # "Moved 'Main Save' into Steam slot 7; 'Polestar' to the vault"
    status: Literal["done", "rolled_back", "failed", "abandoned", "open"]
    verify: str                          # worst verify state over the op's targets
    undo_of: str | None
```

### 2.14 Errors and what the user is told

`execute` returns an `OpOutcome` for every operational result and raises only for caller errors
(`ConfirmationRequired`, `TargetError`) and genuine bugs (which still leave a consistent journal:
the rollback runs in a `finally`-style handler before the exception propagates). All new
exceptions subclass `operations.OperationError`, so today's front-end handlers
(`gui.py:1317-1318`) keep working.

| Condition | Status / exception | Live change | Message (ASCII) |
|---|---|---|---|
| plan has `blocked` items | `refused` | none | the block warning's text |
| missing confirmation | `ConfirmationRequired(codes)` | none | (caller bug; UI re-asks) |
| target invalid / ambiguous | `TargetError` | none | "That live slot is not available." |
| live state or source changed since the plan | `stale` + `replan` | none | "Steam slot 7 changed since you opened this. Please check again." |
| game running (True) at either check | `refused`, code `GAME_RUNNING` | none | "No Man's Sky is running. Close it first." |
| open op on the account | `refused`, `RECOVERY_PENDING` | none | "An earlier move was interrupted. Resolve it first." |
| another live write running (lock timeout) | `refused`, `BUSY` | none | "Another operation is still running. Try again in a moment." |
| vault not upgraded to the version store | `refused`, `VAULT_UPGRADE_NEEDED` | none | "Upgrade the vault first: Tools > Vault upgrade... Nothing was changed." |
| vault missing / full | `refused`, `VAULT_UNAVAILABLE` / `VAULT_SPACE` | none | "The vault is not reachable. Nothing was changed." / "Not enough free disk space. Nothing was changed." |
| preserve failed otherwise | `refused`, `PRESERVE_FAILED:<class>` | none | "The current save could not be copied to the vault (<reason>). Nothing was changed." |
| snapshot failed | `refused`, `SNAPSHOT_FAILED` | none | "The safety snapshot failed. Nothing was changed." |
| write or validation failed, rollback verified | `rolled_back` | none (exact pre-image) | "Nothing changed in Steam slot 7. The move failed: <reason>." |
| rollback failed | `failed` | unknown, journal open | "The move failed and Steam slot 7 could not be put back automatically. Use Recover (recommended) or restore the folder from the safety snapshot." |
| recording failed after a verified write | `done` + warning `RECORDING_PENDING` | done | "Done. The history will finish updating at the next start." |

Every caught exception is logged with `op_id`, stage, target label and the exception (project
rule: surface, never swallow). Logs never contain account ids or full paths (redacted).

### 2.15 Safety invariants

| # | Invariant | Enforced by | Tested by (5) |
|---|---|---|---|
| I1 | No live write unless the game check is False (or None and confirmed) both at execute start and immediately before the first live write; organizer ops have no override | `game_problem` at start, the second probe after `writing` | T-G1..G3 |
| I2 | Every member that will be replaced is durable and hash-verified in the vault before the first live write | `StoreLocator.preserve` (D3 2.8), `Abandon` on any error | T-P1..P4 |
| I3 | Data bytes are written verbatim: `sha256 == vid` on the staged bytes and after the write | writers + validation | T-W1, T-V1 |
| I4 | Only metas change, and only by re-key, the 2.5 timestamp rules, or F6 conversion | `pairmeta`, `convert` (pure, property-tested) | T-M1..M8 |
| I5 | Filling a slot always writes both positions; no op leaves a mixed slot | `pairmeta` (copy of V), planner | T-M3, T-F2 |
| I6 | The member that must load is newest by meta time, Steam mtime and Xbox FILETIME | `pairmeta` + validation `order` row | T-M2, T-V2 |
| I7 | Never touched: accountdata (both platforms), `steam_autocloud.vdf`, `cache/`, other slots, other accounts, other `containers.index` records, files the op did not create | writers only take the slot's names / identifiers; validation `containment` row | T-V3, T-X3 |
| I8 | Every write is staged, then swapped, then validated; a failure rolls the slot back exactly | writers 2.6, 2.9.2 | T-C1..C14 |
| I9 | No D5 path deletes a live save file or leaves a slot empty, on any platform; the only deletions are a rollback removing a file the same op created and `finalize_pair` removing the replaced blobs of the op's own Xbox records | writers have no delete input; no Evict; Swap with an empty side and undo of a fill are blocked | T-NODEL |
| I10 | One live write at a time, organizer and classic alike (phase 1 on); an open op blocks new live ops on its account | live-op lock, `RECOVERY_PENDING` | T-R5, T-L1, T-L2 |
| I11 | Write-ahead: the `writing` event (with every file the op may create) is fsynced before the first live write | `execute` order, `prepare` before `apply` | T-C1..C14 |
| I12 | Every live op takes a whole-folder store snapshot of each target folder before the first live write; the oplog `snapshot_id` names it | `execute` step 4, `oplog2` writer | T-O1, T-O3 |
| I13 | Recovery never writes a slot whose content matches neither the pre- nor the post-image | recovery classification | T-R3 |
| I14 | UI text is ASCII and redacted; no account id, folder name or path in messages, logs or the oplog `detail` | `Warning.text` builder, `tests/test_ui_text.py` | T-U1 |

### 2.16 Concurrency and performance

**Locks.**

- **Live-op lock** `<vault>/organizer/.liveop.lock` (D5's; an in-process `threading.Lock`, then
  `safety.exclusive_lock`), held for a whole live `execute`, a Keep a copy and a recovery action.
  Timeout 10 s -> `BUSY` with a retry notice; nothing else becomes read-only. It serialises the
  GUI and the CLI, two app instances, and two machines sharing one vault.
- **Classic writes (phases 0-1).** From phase 1 today's live writers (`repopulate_slot`,
  `restore_full`, `clear_slot`, `promote_member`, `undo_last`) run inside
  `liveops.liveop_lock(ctx)` too: D1's phase-1 recording hook takes it around each call (D1
  2.6.7). So two front ends or two instances can never write one live folder at once; a timeout
  refuses the classic op with "Another operation is still running." (an `OperationError`).
  Phase 2 re-routes these ops through `execute` (4.1).
- **Store lock** (D1, `<vault>/organizer/.lock`): not re-entrant (a nested transaction on one
  thread is a bug), held for milliseconds per transaction: each `ops.jsonl` append, `record_op`,
  `observe`. D3 takes it only for each manifest commit (preserve, snapshot, keep). A
  `LockTimeout` fails that one transaction: before the `writing` event it is an `Abandon` with
  code `BUSY`; after a verified write (recording) the op ends `done` with `RECORDING_PENDING` and
  recovery finishes the recording at the next start. The organizer never turns read-only for the
  session because of one timeout.
- **Order:** the live-op lock first, then the store lock, never the reverse. No code holding the
  store lock calls into D5, and D3 never calls D1 while holding it, so there is no deadlock.
- **Busy targets.** `liveops.busy_targets(ctx) -> frozenset[LiveTarget]` = targets of open ops.
  D1's `observe(scan, *, src, skip)` takes the skip set: every refresh passes `busy_targets()`,
  so a GUI refresh during a write never journals a torn slot as a game event; D5's post-write
  `observe(src="verify")` passes the busy targets minus its own op's targets, so the post-write
  state is journaled at the op (V-12).
- **GUI.** `execute` runs on a worker thread; the Tk thread never reads save files (C31); the
  window cannot close while it runs; refresh timers pause.

**Performance** (INFERRED from measured rates: R1 2h scans 18-43 ms per live folder, R7 2.9
hashing ~1.7 GB/s warm, D3 2.8 preserve 10-30 ms of I/O plus fsyncs and 0.17 s for a whole Steam
snapshot (VERIFIED by D3), R2 2.5 `tasklist` 175 ms):

| Step | Typical (SSD, warm) | Notes |
|---|---|---|
| `eviction_picker` | < 50 ms + game probe 175 ms | from D1's live board; probe off the Tk thread |
| `plan_place` | 30-80 ms | metas only; source data not read |
| source open + verify | 10-60 ms | reads 2-28 MB, sha256, chunk-header walk |
| preserve | 10-40 ms | one ~3 KB manifest even when the objects are already stored |
| whole-folder store snapshot | 0.1-0.3 s | hashes ~54-68 MB (R8 R7); writes only objects the store lacks |
| stage + swap / apply | 50-200 ms | 4 files or 2 blobs pairs, fsync each |
| validate + D1 observe | 60-150 ms | re-hash written members, rescan the folder |
| journal + oplog appends | 10-40 ms | 5-6 fsynced appends |
| **total** | **~0.7-1.1 s** incl. two game probes (HDD 2-5 s) | progress shown at each step |
| `pending_recovery` at start | < 50 ms | folds `ops.jsonl` (3-6 KB per op) |

Disk per op: the preserve entry adds the evicted members only when the store lacks them (0 when
they are stored) plus its manifest; the snapshot adds only objects the store lacks (usually none
after preserve) plus a manifest - instead of today's 54-68 MB folder copy per op.

## 3. Owner decisions

All five forks are decided (01-consolidation 4a); only the decided behaviour is implemented.

### 3.1 F4 - member policy: (a) the version with its own partner

The captured pair goes back in its captured positions with V loading (2.5); a source with one
member, or with members of two pids, gets a copy of V in the other position. Both in-game load
entries are real (auto-save and restore point), and no slot is ever mixed (I5).

- Rejected (b) V in both positions: the player loses the restore point as an in-game fallback.
- Rejected (c) V + delete the other member (libNOM.io's Copy): under Steam Cloud the deleted
  member comes back and the slot becomes mixed (R8 Q1c, A4).

### 3.2 F5 - meta timestamp: (b) stamp "now"

Every pair write stamps V = now and the other position = now - 1, in the meta and on disk, in
every cloud mode (2.5); undo too (2.11). The load screen shows the placed save as the newest; its
original in-game date stays in the vault (the source entry's meta, which D1's op record points
at; `source_meta_ts` in the oplog line). The live copy is a meta-only variant of the version
(same `vid`). The panic restore is exempt: it restores a recorded folder state byte-exact (4.3).

- Rejected (a) keep V's original time: honest load-screen date, but not the owner's choice.

### 3.3 F7 - another account's save: (b) allow, with one warning

Whenever the capture account (2.1) is known and differs from the target account, the plan
carries `OTHER_ACCOUNT` (confirm): "This save comes from another account. Its bases may not be
editable until the game takes them over when you play it here." No ownership id is read, stored
or compared (owner Q6); an unknown capture account gives no warning. In game, the game appends
the new identity and re-owns bases and discoveries on first play (VERIFIED for cross-save
deliveries, R8 D5; not always complete, `..A74F`). Only the slot's files or records are written,
never `steam_autocloud.vdf` or another account's `containers.index` (I7).

- Rejected (a) same person only: needs hashed ownership ids in the index.
- Rejected (c) block: stops moves between the owner's own accounts.

### 3.4 F8 - the app only replaces

No operation deletes a live save file or leaves a slot empty (I9): there is no Evict; a Swap
with an empty side is blocked (`SWAP_EMPTY_SIDE`); the undo of a fill into an empty slot is
blocked (`UNDO_WOULD_EMPTY`); the panic restore leaves slots that are absent from the snapshot
alone and lists them (4.3); the classic Clear becomes the manual steps (4.1). Emptying a slot is
the owner's manual procedure, shown by `empty_slot_guide` (2.4.2) together with the warning that
Steam Cloud or Xbox sync may refill an empty slot (`EMPTY_SLOT_MAY_REFILL`). Reason: under Steam
Cloud a file deleted while the game is closed comes back at the next launch (VERIFIED twice in
the owner's own oplog, R8 E1; R7 2.6 row 7).

- Rejected: delete after the owner confirms that Steam Cloud is off - the app cannot check it.
- Rejected: delete and detect the resurrection - the app would pretend a delete worked until the
  next launch proves otherwise.

### 3.5 F6 - Steam <-> Xbox transfer: (a), last phase, after the owner's runs T1/T2

**Status (2026-10-18): T1 and T2 passed; the gate is lifted; G1 is a built-in record.** Both
directions are open (record in `liveops/transfer.py`: runs T1/T2, meta format 2004, Steam 432 B
<-> Xbox 360 B, BaseVersion 4228). A transfer is now an ordinary Place: it may replace an
occupied slot (the evicted pair is preserved into the vault first), needs no env switch and no
fresh backup, and Undo of a replace puts the evicted version back (a same-platform place). A
transfer into an empty slot has no Undo (F8). The text below is the original plan, kept as the
record.

Verbatim data (same `0xFEEDA1E5` stream; PLATFORM token "Win|Final" on both PC builds, R8 D5)
plus a field-by-field meta conversion (3.5.2); the game re-owns the save on first play (VERIFIED
for cross-save deliveries; UNTESTED for a copy that did not pass Hello Games' servers, hence the
runs). Until a direction passes its run, `CROSS_PLATFORM_GATED` stays and the picker says "Use
Hello Games cross-save in the game (up to 5 saves)." Undo of a transfer is a transfer back (same
gates); everything else is an ordinary Place.

- Rejected (b) libNOM.io-style ownership rewrite: re-serialises the JSON (breaks "never
  recompress") and needs upkeep per game version (R8 Q4b).
- Rejected (c) never: the owner wants it after the runs.

#### 3.5.1 Gates (each failure blocks unless the row says warn or confirm)

| Gate | Rule | Code |
|---|---|---|
| G1 validated | the direction has a passing record, built into `liveops/transfer.py` (`VALIDATED`: T1 Steam -> Xbox, T2 Xbox -> Steam, 2026-10-18, format 2004, BaseVersion 4228). A direction or format without a record stays shut. No per-vault file, no env switch | `CROSS_PLATFORM_GATED` |
| G2 stream data | first 4 bytes `E5 A1 ED FE` and a clean chunk walk whose total equals the meta size; refuses single-LZ4 (Xbox before 4.52), HGSAVEV2 (4.52-5.00) and plain JSON (Steam before 3.60) | `SOURCE_OLD_FORMAT`: "This save uses an older format. Load and save it once in the game first." |
| G3 meta format | Steam 432 B / format 2004 <-> Xbox 360 B / 2004. Worlds I (Steam 384 / Xbox 296, format 2003) refused until a run T2b covers it: no Steam 384-B meta exists in the owner's data to test against (VERIFIED 9.1) | `UNSUPPORTED_META` |
| G4 identity | meta id != 0 and equals the id in the data's first chunk | `SOURCE_NO_ID` |
| G5 other account | a transfer always comes from another account: the F7b confirmation (3.3) | `OTHER_ACCOUNT` (confirm) |
| G6 Xbox quota | Steam -> Xbox: wgs total after the op <= 256 MB (warn above 200 MB; 67.7 MB used today, R8 D8) | `XBOX_QUOTA` |
| G7 cross-save | Hello Games cross-save on -> warn | `CROSS_SAVE_ON` |

#### 3.5.2 Meta conversion (`liveops/convert.py`, pure functions)

Field map (R8 D7, extended by the VERIFIED survey 9.1):

| Field | Steam plaintext 432 B | Xbox 360 B | Steam -> Xbox | Xbox -> Steam |
|---|---|---|---|---|
| header | 0x00 `0xEEEEEEBE` | - | dropped | set |
| meta format | 0x04 and 0x168 | 0x124 | copy | copy to both |
| hash area | 0x08-0x37 (zero, all 156) | - | dropped | zero |
| SizeDecompressed | 0x38 | 0x10 | chunk total of the data (== 0x38, validated) | chunk total |
| SizeDisk | 0x3C (stale in 12/156) | - | dropped | `len(data)` |
| profile hash | 0x40 (zero, all 156) | - | dropped | zero |
| BaseVersion, GameMode, Season, TotalPlayTime | 0x44, 0x48, 0x4A, 0x4C | 0x00, 0x04, 0x06, 0x08 | copy | copy |
| unnamed u32 | 0x54 (== SizeDecompressed in 144/144 game-written metas; stale in the 12 editor-touched ones) | - | dropped | chunk total |
| SaveName, SaveSummary (128 B each, bytes after the NUL kept) | 0x58, 0xD8 | 0x14, 0x94 | copy | copy |
| Difficulty (u32) | 0x158 | 0x114 | copy | copy |
| SaveUniversalId (u64) | 0x15C | 0x118 | copy | copy |
| Timestamp (u32) | 0x164 | 0x120 | per F5 | per F5 |
| Difficulty tag (64 B) | 0x16C | 0x128 | copy | copy |
| tail | 0x1AC-0x1AF (zero) | - | dropped | zero |

```python
def steam_to_xbox(plain: bytes, data: bytes, ts: int) -> bytes:        # -> 360 B plaintext
def xbox_to_steam(meta: bytes, data: bytes, ts: int, ordinal: int) -> bytes:   # -> 432 B encrypted
```

Both refuse anything but the G3 lengths and formats, validate the result (Xbox: re-parse with
the fixed `parse_ms_meta`; Steam: `is_valid_for(out, ordinal)` and `parse(decrypt(out))` fields
equal the source's), and never touch the data. Round-trip property, testable read-only on the
owner's metas: `steam_to_xbox(xbox_to_steam(x)) == x` for every Xbox 360-B game-written meta
whose 0x10 equals the chunk total, and `xbox_to_steam(steam_to_xbox(s))` decrypts to `s` for the
144 game-written Steam metas (the 12 editor-touched ones differ exactly at 0x3C and 0x54, which
the converter recomputes - expected).

The Xbox side then is an ordinary record write (Created or Modified, FILETIME now, xgs mirror);
the Steam side an ordinary file write with the destination ordinal and mtime now under Cloud.
VERIFIED context: 0 of 195 versions exist byte-identical on both platforms (9.1) - the game
re-writes a save when cross-save delivers it, so the vault has no precedent for a verbatim
cross-platform copy. That is why the owner runs are mandatory.

#### 3.5.3 Owner validation runs

Run only by the owner, with a throwaway save, after a verified full backup of both live
folders; the app never runs them by itself.

**T1 Steam -> Xbox.**

1. On Steam start a new throwaway game; play ~5 minutes; build a base computer and 3 parts;
   discover one planet; save at a beacon; quit. Close Steam's sync prompt if any.
2. Close the Xbox app; wait 60 s (sync settles, R8 E2).
3. `set NMSVAULT_ALLOW_UNVALIDATED_TRANSFER=1` and run
   `nmsvault place --from live:steam:<slot> --to xbox:<empty slot> --validation-run T1`.
   The app records the op and the converted meta hash.
4. Start NMS (Game Pass). Check: (a) the slot shows the right name, play time and date;
   (b) it loads; (c) the base is editable (move a part) and the discovery is listed; (d) save at
   a beacon; quit.
5. In the app: rescan. Check: (e) the new version descends from the placed one (verification
   `confirmed: played`); (f) once D4 has indexed the new version, its bases' owner platform
   (`ptk`, a platform code, not an id) reads Xbox.
6. Wait for the Xbox app to show the save as synced; start NMS again: (g) no conflict dialog,
   the save is intact.

Pass = (a)-(e) and (g); (f) recorded when available.

**T2 Xbox -> Steam** - the mirror image, with Steam Cloud **on**, plus: (h) after quitting and
relaunching through Steam, the placed save is still there (Steam Cloud accepted it; verification
`confirmed: uploaded` when the manifest reader is enabled).

**T2b Worlds I** - T2 with a 296-B Xbox source, only if such a save is still wanted (8 live
Xbox metas are 296 B today, R8 A5); opens G3 for 296 <-> 384.

Recording (decided 2026-10-18): the passing runs are a built-in record in code, not a per-vault
file and not a CLI command; other users' vaults would never hold the owner's runs. A new
direction or meta format needs a new record. The transfer re-closes by itself if the game
introduces a new meta length or format (G3 refuses unknown values).

## 4. Migration of today's operations

### 4.1 Today's operations

| Today (`operations.py`) | Decision | New behaviour |
|---|---|---|
| `repopulate_slot` (306-364; Xbox 492-522) | **re-route** | Adapter: `live_dir` must resolve to a configured live account (C29, 2.3.6) or the call is refused; source = the vault entry's pair (locator) or, for an uncatalogued folder (CLI `--from <folder>`), `PairSource(folder_slot=(dir, slot))` whose bytes are read and verified directly; then `plan_place` + `execute`. Always writes both positions (P6), journals, preserves. Returns today's `OpResult` shape for existing callers. |
| `restore_entry` for an extract (191-221) | **re-route** | Place of the extract's pair into the same slot number of the given account |
| `promote_member` (367-400; Xbox 440-463) | **re-route** | `plan_promote(target, position)`; fixes the Xbox no-op (needs P1) and the old Steam mtime under Cloud (R8 G5) |
| `plan_clear_slot` + `clear_slot` (671-760) | **retire** in phase 2 | "Empty this slot" shows `empty_slot_guide` (2.4.2) and deletes nothing (F8). Phases 0-1: today's code with the P5 interim fix, under the live-op lock from phase 1 (2.16) |
| `extract_slot` (280-303) | keep | vault-only; in store mode D3 writes it as a store entry (`add_from_live(kind="extract")`), the same call as Keep a copy (2.4.1) |
| `restore_full` (224-274; Xbox 525-560) | keep as the **panic restore** (4.3) | whole folder, with the P4/P7 fixes; never deletes a slot |
| `undo_last` (881-919) | **retire** as "Undo" | the new Undo is slot-scoped (2.11); `undo_last` becomes `panic_restore` of the newest snapshot **of that folder** (4.3) |
| `snapshot_live` (134-145) | keep until phase 2 | phase 0 removes its pruning (P2); from phase 2 `execute` takes store snapshots directly (2.8) and the classic paths get D3's store snapshot |
| `create_full_backup`, `import_backup`, `import_vault`, `preview_vault_import` | keep | vault-only; D3 decides where the bytes go (store entries in store mode) |
| `msstore.write_save` / `mark_modified` (and `delete_save` while the classic Clear exists) | keep names | re-implemented on record-bytes editing (P-X1, 2.6.2) |

### 4.2 Older app versions

Not supported on an upgraded vault (owner Q2: users cannot and should not go back to older app
versions); D5 writes nothing for them. The oplog keeps today's six leading keys only so that
today's own readers keep working until phase 2 replaces them (2.13.1).

### 4.3 The panic restore (today's whole-folder undo and restore, kept and fixed)

```python
def plan_panic_restore(ctx, account: LiveAccount, snapshot_id: str | None = None, *,
                       restore_accountdata: bool = False) -> PlacePlan: ...   # kind "panic_restore"
```

- Target is a resolved live account (C29), never the GUI's "active live" (P3/P4 class).
- Source: a whole-folder snapshot or full backup of the same folder, from D3's
  `list_vault_entries(vault, store)`: kind `snapshot` or `full`, `source == account_key`, same
  platform, and on Xbox the entry's `containers.index` account id equals the live one (P7).
  Default: the newest such snapshot (P4: today it takes the newest of any folder,
  `operations.py:893-905`). Its files are read through D3's `open_entry_folder(...)` (the legacy
  folder until it is folded, else a verified temporary materialization).
- First every live member that is not durable is preserved (up to 15 slots, one `preserve` call
  per slot) and a fresh store snapshot is taken, so a panic restore is itself undoable (its undo =
  panic restore of that fresh snapshot).
- Writes slot files / slot records only, byte-exact as in the source, metas included (it restores
  a recorded state, so the F5b stamp does not apply); disk times are now under Steam Cloud and on
  Xbox. `steam_autocloud.vdf` is never copied (P7); accountdata is kept unless
  `restore_accountdata=True` (warning: "rolls back unlocks and settings", R8 B4); on Xbox the live
  index header identity is kept and only slot records are taken from the source (raw), with the
  xgs mirror updated per identifier.
- Slots absent from the source are left alone and listed in the outcome: "Steam slot 12 was
  empty in the snapshot and was left as it is." (F8: no deletion).
- Journaled like any op (pre: the fresh snapshot; post: the source's slot files), so a crash
  mid-restore is recovered forward or back.

### 4.4 Prerequisites: what is fixed first and how it is tested

| # | Change | Test (prove the bug, then the fix) |
|---|---|---|
| **P1 parse** | `msstore` constants `MS_META_LEN_WAYPOINT = 280`, `MS_META_LEN_WORLDS_I = 296`, `MS_META_LEN_WORLDS_II = 360` (libNOM.io `PlatformMicrosoft.cs:22-25`); `parse_ms_meta`: names when `len >= 280`; difficulty u8 at 0x114 for 280, u32 for 296/360; id 0x118, timestamp 0x120, format 0x124 for 296/360; difficulty tag 0x128 for 360. `_build_member` fills `MetaInfo.timestamp` / `slot_identifier` from them. `playtime` takes the id from the meta when non-zero (no chunk decode; R7 2.3) | synthetic 280/296/360 metas parse field by field (fails today for all three); `make_wgs_account` builds 360-B (and a 296-B variant) metas with non-zero id/time instead of 432-B Steam-length ones (`conftest.py:70-116`); read-only test on the live wgs (skip when absent): every Worlds-era meta has time != 0 and id == the data's id (VERIFIED over the 44 distinct Xbox metas in vault + live: time != 0 in 44/44, id == data id in 42/44, the 2 others are pre-Worlds saves with no stream id, 9.1); the newest member by meta time has >= play time |
| **P1 setter** | `set_ms_meta_timestamp(meta, ts)` writes u32 0x120 for 296/360-B metas of format 2003/2004 and **raises** otherwise (today it silently returns the input, `msstore.py:346`) | exactly 4 bytes change; the Xbox promote test asserts the meta order flips (today it does not, R8 A5) |
| **P-X1** (new) | index parse keeps each record's raw bytes and integer FILETIME; `build_containers_index` writes untouched records raw; `write_save` / `delete_save` / `mark_modified` use record-bytes editing | parse + rebuild is byte-identical for synthetic indexes with sub-second FILETIMEs (fails today); read-only on the real index (0/7 identical today, VERIFIED 9.3) |
| P3 | `gui.on_promote` uses the clicked row's folder (`gui.py:1539`); finally replaced by `plan_promote(target)` | R1 E9 script as a GUI test |
| P4 | `undo_last` only considers snapshots whose `source` is the target folder's name and platform; finally the panic restore (4.3) | R1 E3 (two same-platform folders): no cross-folder restore |
| P5 | interim: `_latest_vault_copy` matches by playthrough id + play time, not slot number; final: the classic Clear is retired (4.1) and "in the vault" is `is_durable(vid)` everywhere | R1 E10 scenario: no false "safe" |
| P6 | interim: `repopulate_slot` refuses a one-member source when the destination holds the other member ("would leave a mixed slot; use Move to live"); final: Place writes both positions | R1 E1 scenario refuses (Steam and Xbox) |
| P7 | `restore_full` never copies `steam_autocloud.vdf`; Xbox refuses a backup whose index account id differs; accountdata kept unless asked | two `st_` sandbox folders: vdf and accountdata unchanged; Xbox foreign index refused |
| oplog | `append_oplog`: one write + flush + fsync; `read_oplog`: skip an unparsable last line (log it) | torn-line fixture: the new reader skips it, today's reader raises (9.2 C) |

P1 and P-X1 block every Xbox organizer operation (member order, identity, exact rollback);
P3-P7 make today's flows safe until they are re-routed and become regression tests afterwards.

### 4.5 Entry points

**GUI** (today's labels -> organizer route; D6 decides the look):

| Today | Becomes |
|---|---|
| backup slot menu "Copy slot N into live slot N" / "...into a live slot..." (`CopySlotDialog`, `gui.py:1478-1526`) | "Move to live..." = the eviction picker (C30) |
| live slot menu "Replace ... from anywhere..." | "Replace with..." = the picker entered from the slot (version chooser, then `plan_place`) |
| "Make ... the one the game loads (promote)" (`gui.py:1528-1542`) | `plan_promote` on the clicked slot (P3 gone by construction); hidden on the member already loading (R2 D7) |
| "Clear slot..." (`gui.py:1455-1476`) | "Empty this slot (manual steps)..." = `empty_slot_guide` (2.4.2); nothing is deleted (F8) |
| (new) live slot menu | "Keep a copy in the vault now" = `plan_keep` (2.4.1), offered when the slot is not in the vault |
| toolbar Undo (`gui.py:1631-1640`) | per-slot "Undo" of the newest op + the op history; the old global undo becomes "Restore whole folder from a snapshot..." (4.3) |
| backup root "Restore all of ..." (`gui.py:1403-1437`) | "Restore whole folder..." (4.3); extract roots go to "Move to live..." |
| modal "Done" (`gui.py:1326-1333`) | non-modal "Done - Undo" (C30) |
| "Proceed anyway (risky)?" (`gui.py:1308-1311`) | removed for organizer ops (2.8) |
| (new) start-up | recovery items (2.10) and verification alerts (2.12) |

**CLI** (`cli.py`):

| Command | Becomes |
|---|---|
| `repopulate --from X --src-slot N --to-slot M` | adapter to `place`; prints the plan; asks unless `--yes`; `--force` now only confirms `GAME_STATE_UNKNOWN` (a running game always refuses) |
| `promote --slot N --member A/B` | `plan_promote` |
| `clear N` | prints the manual steps of `empty_slot_guide` (2.4.2); deletes nothing |
| `restore <extract-id>` | `place` |
| `restore <full-id>` (`--no-mirror`) | panic restore of that snapshot/backup (4.3); `--no-mirror` = leave slots absent from it alone |
| `undo` | slot-scoped undo of the newest op on `--to` (or the only target); `undo --op <op_id>`; `undo --whole-folder` = panic restore |
| new `place`, `swap`, `keep`, `ops`, `recover`, `panic-restore`, (phase 5) `transfer-validated` | 2.2 API |

Targets are written `steam:7`, `xbox:4`, or `steam:<account>:7` when a platform has more than
one live account; sources `--from <entry-id> --src-slot N [--member A|B]`, `--from live:steam:4`, or `--from vid:<prefix>` (`--from` as in today's `extract` and `repopulate`).
Every command prints the plan's warnings and the outcome text from core; non-interactive runs
need `--yes` plus `--confirm CODE` for each confirmation code.

### 4.6 Documentation to update when implemented

`DESIGN.md`: the operation semantics (both positions, newest by every signal, journal,
verification), the Xbox meta facts (296/360 B, id 0x118, time 0x120) and the FILETIME rule;
remove the stale "SizeDisk == file size" line (`DESIGN.md:91`, R1 pre-work list). `README.md`:
features, "the app replaces, never empties a slot" with the manual steps, the real
`uv run pytest` count.

## 5. Test plan

Risk-based: the most tests go where a bug loses or mixes a save - the write/rollback/recovery
path, member order, and the Xbox index. pytest only; every write test runs in `tmp_path`
sandboxes built from synthetic saves (project rule); real data only read-only and skipped when
absent. Keep fixture paths short (Windows MAX_PATH, R1 G4).

### 5.1 Fixtures

| Fixture | Builds | Notes |
|---|---|---|
| `steam_live(tmp, slots, cloud=True)` | `st_76561190000000001/` with synthetic pairs (promote today's `steam_save` helper, `test_playtime.py:43-63`, to `conftest.py`; add `pid`, `play`, `ts`, member choice), `accountdata.hg` / `mf_accountdata.hg`, `steam_autocloud.vdf` when `cloud`, a `cache/` file | both variants (with / without the vdf) for every Steam execute test |
| `xbox_live(tmp, slots, xgs=True, meta_len=360)` | the fixed `make_wgs_account` (real 360 / 296-B metas, non-zero id 0x118 and time 0x120, sub-second FILETIMEs, an `AccountData` record) plus the `xgs` twin | both variants (with / without xgs) |
| `vault_with(tmp, pairs)` | a vault in store mode: a real D3 `VersionStore` in `tmp_path` (real `safety.exclusive_lock`) holding entries with known pairs, and D1's `VaultLocator` over D3's `StoreLocator` | unit tests that isolate D5 may use `FakeLocator` (D1's ABC: `open` verifies sha256, `preserve` writes a preserve-shaped entry and re-reads it); D1's locator conformance suite also runs on it |
| `legacy_vault(tmp)` | a vault without a store (`vault_mode == "legacy"`) | `VAULT_UPGRADE_NEEDED`; the phase-1 classic-lock tests |
| `op_ctx(...)` | `OpContext` with a scripted `game_probe` (a list of answers), fixed `clock`, `FaultInjector`, a fake `LineageRecorder` (records calls) for unit tests, the real `ops.jsonl` writer | a second `op_ctx` on the same vault = "after restart"; the integration suite (5.3) uses D1's real recorder |
| `tree_hash(path)` | sha256 + `mtime_ns` of every file under a folder | the "nothing changed" assertion |

`FaultInjector.at(point)` raises `SimulatedCrash(BaseException)` - not an `Exception`, so no
handler in the code under test can catch it, exactly like a kill. Named points live in the
executor and writers (`after_preparing`, `in_preserve`, `after_snapshot`, `after_writing`,
`staged:<n>`, `swapped:<n>`, `before_index`, `after_index`, `xgs:<id>`, `after_validated`,
`after_oplog`, `after_record`).

### 5.2 Tests by area

| ID | Area | Scenario | Assert |
|---|---|---|---|
| T-M1 | pairmeta | common Place of a full pair, Steam cloud / local / Xbox | positions kept; metas stamped L = now, O = now - 1; `source_meta_ts` = the captured times; Steam metas re-keyed for `file_no + 1` of each position and `is_valid_for`; disk times now / now - 1 |
| T-M2 | pairmeta | V is the older member (user choice, Promote) | partner stamped now - 1; order holds on meta time, mtime and FILETIME |
| T-M3 | pairmeta | one-member source; mixed-pid source | the other position gets a copy of V stamped now - 1; `copy_of` set; never two pids |
| T-M4 | pairmeta | the undo of a place | the evicted pair goes back in its positions with its old loading member as `load`, stamped now |
| T-M5 | pairmeta | each cloud mode | the same meta stamps and disk times in all three modes |
| T-M6 | pairmeta | meta-only variant | written `vid` == source `vid`; `meta_plain_sha256` differs from the source's; `source_meta_ts` recorded in the oplog line |
| T-M7 | pairmeta | Xbox 280-B meta (no time field) | meta bytes unchanged; order by FILETIME only; the setter is not called |
| T-M8 | P1 | setter on 296/360-B metas | exactly 4 bytes at 0x120 differ; 280 raises |
| T-P0 | planner | every `plan_*`, `eviction_picker` and `empty_slot_guide` call | `tree_hash` of live folders and vault unchanged; no `ops.jsonl` created |
| T-P1 | preserve | Place over a live-only pair | a `preserve` store entry and the store snapshot exist before the first live write (fault at `after_writing` and inspect); their objects re-hash to the vids |
| T-P2 | preserve | vault without a store; vault root missing | `refused`, `VAULT_UPGRADE_NEEDED` / `VAULT_UNAVAILABLE`; live `tree_hash` unchanged |
| T-P3 | preserve | the slot changes between plan and execute | `stale` with a fresh plan; nothing written |
| T-P4 | preserve | vault full (patched `shutil.disk_usage`) | `refused`, `VAULT_SPACE`; live unchanged |
| T-B1 | planner | candidate states empty / occupied / mixed / partial / unreadable | `state`, `kind`, `durable`, warnings per 2.3.3; an empty candidate carries only `EMPTY_SLOT` (info) |
| T-B2 | planner | suggestion order | same line > lowest empty > durable least recent > least recent > none (all pinned) |
| T-B3 | planner | blocks and confirmations | xgs-only account, cross-platform, vault not upgraded, game running, open op block; `OTHER_ACCOUNT` (confirm) when the capture account is known and differs, none when it is the same or unknown |
| T-F1 | flows | Place, PlaceIntoEmpty, restore-older, Promote on each fixture variant | post-image hashes; `SlotView.newest == L`; oplog v2 line; ops.jsonl `preparing, writing, validated, done`; `record_op` once per target; `note` reaches the `preparing` event, the oplog line and `OpLineage.note` |
| T-F2 | flows | Place from a one-member / mixed source into a full slot | both positions written, one pid (P6 regression, R1 E1) |
| T-F3 | flows | Swap both occupied; Swap with an empty side | both slots exchanged, stamped and re-keyed, one `op_id`, two `record_op` calls; `SWAP_EMPTY_SIDE` blocks and nothing is written |
| T-K1 | keep | `plan_keep` + `execute` on Steam and Xbox, game running and not | an `extract` store entry holding the slot's vids; live `tree_hash` unchanged; oplog `keep`; no `record_op` |
| T-E1 | empty slots | `empty_slot_guide` per cloud mode, occupied and empty | `refill` for steam-cloud and xbox only; the steps name the slot's four files on Steam; `durable` matches `is_durable` |
| T-NODEL | invariant I9 | every path - place, place_into_empty, restore_older, swap, promote, undo, panic_restore, keep, recovery forward and back, the re-routed classic ops - on every fixture variant, with spies on `os.remove`, `os.unlink`, `Path.unlink`, `shutil.rmtree` and on `containers.index` record states | no live save file or Xbox record is removed except files the same op created (rollback) and the superseded blobs of the op's own records; no slot is empty after an op unless it was empty before |
| T-G1 | game | probe True at execute start | `refused`; nothing written; no `writing` event |
| T-G2 | game | probe flips to True after `writing`, before the first live write | `abandoned(game_started)`; live unchanged; the preserve entry stays (harmless) |
| T-G3 | game | probe None | without the code: `ConfirmationRequired`; with it: proceeds |
| T-W1 | Steam writer | stage + swap | temp files fsynced and verified; final names only after all four staged; no temp left |
| T-W2 | Steam writer | `os.replace` fails (a handle held open; Windows only, skip elsewhere) | retried; persistent failure -> files already swapped rolled back exactly |
| T-X1 | Xbox writer | `split_index` / `join_index` on synthetic indexes with sub-second FILETIMEs | byte-identical (today's parse + build is not, 9.3) |
| T-X2 | Xbox writer | `apply_pair` | one index write (spy on `os.replace`); both records flip; states Modified / Created |
| T-X3 | Xbox writer | other records, header identity, `AccountData` | raw-identical after the op |
| T-X4 | Xbox writer | `rollback_pair` for an existing and a new identifier | index byte-identical; wgs and xgs trees identical (the 9.4 prototype as a test) |
| T-X5 | Xbox writer | index changes between `prepare_pair` and the index write | aborted; new blobs deleted; nothing referenced |
| T-V1 | validation | post-write checks | each row of 2.9.1 fails on a corrupted file (inject `corrupt_after_swap`) |
| T-V2 | rollback | validation failure on each fixture variant | exact pre-image (sha256, size, `mtime_ns`; Xbox index bytes), Steam bytes read from the store snapshot; `rolled_back`; oplog line `status: rolled_back` naming the store snapshot |
| T-V3 | containment | any op | other slots, accountdata, vdf, `cache/` unchanged (`tree_hash` minus the slot's files) |
| T-C1..C15 | crash matrix | a `SimulatedCrash` at each point: `after_preparing` (C1), `in_preserve` (C2), `after_snapshot` (C3), `after_writing` (C4), Steam `staged:2` (C5), `swapped:1..4` (C6-C9), Xbox `before_index` (C10), `after_index` (C11), `xgs:<first>` (C12), `after_validated` (C13), `after_oplog` (C14), torn `writing` line in `ops.jsonl` (C15) | after restart `pending_recovery` classifies as 2.10 says; both **forward** and **back** (where offered) end exactly in the post- or pre-image; no temp files or unreferenced blobs; exactly one oplog line per finished op; `record_op` idempotent |
| T-R1..R6 | recovery | all post; torn; `other` (another tool rewrites the slot); other machine id; open op blocks the same account only; marker without vault | recommended choice per table; `other` -> nothing written (I13); `RECOVERY_PENDING` only on that account; `vault_missing` item |
| T-UN1..UN8 | undo | same / progressed / already / empty / stale; undo of PlaceIntoEmpty; undo of a Swap; undo of an undo (redo) | per 2.11; the undo of a fill is blocked with `UNDO_WOULD_EMPTY` and writes nothing; progressed versions preserved before the overwrite; undo never touches another slot |
| T-VR1 | verification | Steam: write the evicted bytes back with their old mtimes (what a "cloud" pick does) | `reverted` alert; "Put it back" plans a Place of the placed pair |
| T-VR2 | verification | a child of the placed pair appears (same pid, more play) | D1 `op_outcome` `confirmed`; D5 `confirmed: played` |
| T-VR3 | verification | Xbox record state Modified -> Synced (raw edit) | `confirmed: uploaded` |
| T-VR4 | verification | another pid appears; the slot is emptied outside the app | `replaced`; `deleted` |
| T-VR5 | verification | game seen running after the op, slot unchanged | D1 stays `pending`; D5 `confirmed: unchanged`; without a session D5 stays `pending` |
| T-VR6 | verification | 31 days without evidence | `expired`, no alert |
| T-O1 | oplog | v2 line | the six leading keys with today's meaning; `snapshot_id` names the store snapshot of the target folder, `entry_id` the preserve entry; `source_meta_ts` present; `detail` has no folder name |
| T-O3 | oplog | torn last line | new `read_oplog` skips it with a log line; the D5 writer never produces one (single write) |
| T-L1 | locks | two contexts run `execute` at once (threads and processes) | the second gets `BUSY`; no deadlock with `preserve`, `record_op`, `observe` and D3's manifest commits taking the store lock |
| T-L2 | locks | a classic write (phase-1 hook) while an `execute` holds the live-op lock, and the reverse, from two processes | the later one is refused with "Another operation is still running."; never two writers in one live folder |
| T-L3 | locks | store-lock timeout inside `execute`: before `writing`; during recording | `BUSY`, nothing live written; `done` + `RECORDING_PENDING`, finished by recovery at the next start |
| T-T1 | targets | unknown account, duplicate folder name without `instance`, vault-inside path, xgs-only | `TargetError` / `XGS_READ_ONLY` |
| T-U1 | UI text | every `Warning.text`, outcome, guide step and recovery/verify text | ASCII (extend `tests/test_ui_text.py` to the new package); no account id or path |
| T-RD1 | real data (read-only, skip when absent) | P1 parser on the live wgs; P-X1 rebuild of the live index; F6 convert round trips (phase 5) | expectations of 4.4 / 3.5.2; `tree_hash` before == after |
| T-6x | F6 (phase 5) | gates G1-G4 and G6 block, G5 asks, G7 warns; convert unit + property tests | 3.5 |

### 5.3 Cross-design integration suite (risky path end to end)

`tests/integration/test_liveops_e2e.py`, part of the normal `uv run pytest` run in CI. It builds
synthetic Steam (cloud and local) and Xbox (with xgs) live folders and a vault upgraded to store
mode by D3's real upgrade (copy-aside, then `store/FORMAT`), and uses the real D1 organizer store,
journal and recorder, the real D3 `VersionStore` / `StoreLocator` and D5 - no fakes between the
three designs.

- Scenarios: place, place into empty, restore-older, swap, promote, keep, undo (same and
  progressed), panic restore; every crash point C1-C14 with forward and with back; a simulated
  cloud revert (the evicted bytes and old mtimes written back); a game child (a new version of
  the placed pid with more play time).
- Asserted together after each scenario: the live tree (`tree_hash`); the store entries (one
  preserve and one snapshot per op, every object verifies in D3's fsck); the `ops.jsonl` events;
  the oplog lines; D1's journal edges (a recorded edge from the placed pair to the child, none on
  a revert) and `op_outcome` (`pending`, then `confirmed` or `reverted`); D5's `VerifyItem`
  states; and the T-NODEL spies.

### 5.4 What the tests deliberately do not cover

The game's and the clouds' real behaviour (which member loads when signals disagree, Xbox
conflict dialogs, ownership migration of a non-server copy, whether a Place into a slot emptied
by hand stays under Steam Cloud, the Xbox in-game delete): those are the owner runs T1-T6 of R8 6
and the two runs of 7.2, of which T1/T2 gate F6 (3.5.3). Verification (2.12) exists precisely to
catch what those runs might prove wrong.

## 6. Implementation plan

One step = one commit with its tests (project rule: done means committed, suite green). Sizes:
S < 150 lines of code, M 150-400, L 400-900 (tests extra). "Needs" names the sibling pieces.

### Phase 0 - prerequisites (independent of D1-D4; can ship as bug-fix releases)

| Step | Content | Size | Tests | Needs |
|---|---|---|---|---|
| 0.1 | P1 parse: Xbox meta lengths 280/296/360, id/time/format/names/difficulty; `_build_member`; `playtime` uses the meta id; `make_wgs_account` real lengths with non-zero id/time (and a 296-B variant); existing Xbox tests updated where they asserted the old zeros | M | P1 rows of 4.4, T-RD1 (parser) | - |
| 0.2 | P1 setter: `set_ms_meta_timestamp` for 296/360, raises otherwise; Xbox promote now orders by meta | S | T-M8, promote order test | 0.1 |
| 0.3 | P-X1: record-bytes `containers.index` editing (raw records, integer FILETIMEs); `write_save` / `delete_save` / `mark_modified` on it | M | T-X1, T-X3, T-RD1 (index) | - |
| 0.4 | oplog: single-write fsynced append; tolerant reader | S | T-O3 | - |
| 0.5 | P3: promote uses the clicked row's folder | S | R1 E9 as a GUI test | - |
| 0.6 | P4: `undo_last` filtered to the target folder's own snapshots | S | R1 E3 | - |
| 0.7 | P7: no vdf copy; Xbox foreign-index refusal; keep accountdata by default (CLI flag `--with-accountdata`, GUI checkbox off) | M | P7 row of 4.4 | - |
| 0.8 | P5 interim: clear plan by playthrough id + play time | S | R1 E10 | - |
| 0.9 | P6 interim: refuse a one-member source into a slot holding the other member | S | R1 E1 | - |

P2 (stop pruning) is a phase-0 step of 01-consolidation; D5 depends on no retention.

### Phase 1 - one live writer at a time (with D1 phase 1)

| Step | Content | Size | Tests | Needs |
|---|---|---|---|---|
| 1.1 | `liveops.liveop_lock` (in-process lock + `safety.exclusive_lock` on `.liveop.lock`); D1's phase-1 recording hook takes it around every classic live write (`repopulate_slot`, `restore_full`, `clear_slot`, `promote_member`, `undo_last`) | S | T-L2 | D1 hook (2.6.7), `safety.exclusive_lock` |

### Phase 2 - live operations

Needs D1 phase 1 (ids, store + lock, read model, recorder with `observe(..., skip)`) and D3's
phase-2 store in store mode, reached after the copy-aside and the upgrade (`StoreLocator.preserve`,
`add_from_live`, `read_file`, `list_vault_entries`, `open_entry_folder`, `vault_mode`).

| Step | Content | Size | Tests | Needs |
|---|---|---|---|---|
| 2.1 | `core/liveops/` skeleton: `model.py` (2.2 dataclasses, `to_dict`), `errors.py`, `OpContext`, `Policies` | S | dataclass round trips | D1 ids |
| 2.2 | `targets.py`: `resolve_target`, `resolve_account` (C29, `instance`) | S | T-T1 | D1 `LiveTarget` |
| 2.3 | `pairmeta.py` (F4a, F5b) | M | T-M1..M7 | 0.2 |
| 2.4 | `steamwrite.py`: prepare / stage / swap (retry) / rollback / sweep | M | T-W1, T-W2 | - |
| 2.5 | `msstore`: `prepare_pair` / `apply_pair` / `finalize_pair` / `rollback_pair` | M | T-X2, T-X4, T-X5 | 0.3 |
| 2.6 | `opjournal.py`: events, fold, tolerant reader, markers, machine id, `busy_targets` | M | journal unit tests, T-C15, T-L1, T-L3 | D1 store, 1.1 |
| 2.7 | planner: `eviction_picker`, `plan_place`, `plan_promote`, warnings, suggestion, fingerprint, the vault-mode gate | L | T-P0, T-B1..B3 | D1 read model, D3 `is_durable`, `vault_mode` |
| 2.8 | `execute` for place / place_into_empty / restore_older / promote: preserve, store snapshot, journal, writers, validation, rollback, oplog v2, `record_op`, D1 `observe` with `skip` | L | T-F1, T-F2, T-G1..G3, T-P1..P4, T-V1..V3, T-O1 | D3 preserve and snapshots, D1 recorder |
| 2.9 | recovery: `pending_recovery`, `resolve_recovery`, startup hook (GUI and CLI `recover`) | L | T-C1..C14, T-R1..R6 | 2.8 |
| 2.10 | `plan_swap` (`SWAP_EMPTY_SIDE`), `plan_keep`, `empty_slot_guide` | M | T-F3, T-K1, T-E1 | 2.8 |
| 2.11 | undo: `plan_undo` (incl. `UNDO_WOULD_EMPTY`), `op_history`, `OpSummary` | M | T-UN1..UN8 | 2.8 |
| 2.12 | verification: game-seen recorder, `update_verification`, `VerifyItem` | M | T-VR1..VR6 | D1 `op_outcome`, events |
| 2.13 | re-route classic ops (repopulate, restore extract, promote) through `liveops`; the classic Clear becomes the guide; CLI `place` / `swap` / `keep` / `undo` / `ops` / `recover` / `clear` (steps); update tests that assert old internals; retire the P5/P6 interims | M | existing suites, CLI tests, T-NODEL | 2.8-2.11 |
| 2.14 | panic restore (4.3) replacing `undo_last` / full `restore` entry points | M | panic-restore tests incl. a crash point, T-NODEL | 2.8, D3 listing |
| 2.15 | classic-tab GUI wiring: picker dialog on the old `CopySlotDialog` slot, non-modal Done-Undo, recovery prompt, verification alerts, the empty-slot steps, close blocked during execute | M | GUI tests via the `gui_app` root | D6 order of work |
| 2.16 | cross-design integration suite (5.3) | M | 5.3 | D1, D3, 2.8-2.14 |
| 2.17 | DESIGN.md and README (4.6) | S | real test count | all |

Rough total: phase 0 about 0.9 kLOC + tests; phase 2 about 4 kLOC + tests.

### Phase 2b (optional, small)

| Step | Content | Size | Tests |
|---|---|---|---|
| 2b.1 | Steam `remotecache.vdf` reader (2.12.2): `uploaded` evidence, Steam `PENDING_UPLOAD` | S/M | parser fixtures; T-VR3 Steam variant |
| 2b.2 | `CROSS_SAVE_ON` from the accountdata `CrossSaves` flag (read with D4's `decode_save`; no index change) | S | flag fixtures |

### Phase 5 - Steam <-> Xbox (F6a)

| Step | Content | Size | Tests |
|---|---|---|---|
| 5.1 | `convert.py` (3.5.2) | M | unit + property tests; T-RD1 round trips on the owner's metas (read-only) |
| 5.2 | gates G1-G7, `transfer-validation.json`, `nmsvault transfer-validated`, the run-only env flag | M | T-6x |
| 5.3 | owner runs T1 and T2 (and T2b if wanted) | owner | 3.5.3 |
| 5.4 | open each validated direction (`policies.cross_platform = "validated"`) | S | gate tests flip |

## 7. Risks and open questions

### 7.1 Risks

| # | Risk | Likelihood / impact | Mitigation in this design | Residual |
|---|---|---|---|---|
| K1 | Steam Cloud offers its copy at the next launch (another PC, a Steam Deck) and the owner picks it | medium / the placed save leaves live | evicted and placed versions stay durable; `reverted` alert with "Put it back"; the plan's hint says which copy to choose | the owner must act once |
| K2 | Xbox app still writing `containers.index` just after the game exits (uploads 10-30 s later, R8 E2) | medium / lost sync state or our write overwritten | `XBOX_SYNC_BUSY` for 60 s after the index changed; index re-read right before our single write; rollback on any doubt | a sync write inside our ~1 s window is caught by validation (other records must be raw-identical) |
| K3 | Windows file locks (antivirus, Steam client) during the swap | low-medium / failed op | 5 retries; exact rollback | repeated failures need a retry by the owner |
| K4 | Old blobs kept until validation; a kill before recovery and a game launch in between | low / extra unreferenced files in a container folder | recovery deletes exactly the files the journal lists | the Xbox app's view of stray files is unknown (believed ignored, as `_resolve_blobs` ignores them) |
| K5 | The vault upgrade is not done or failed | low / no organizer moves | `VAULT_UPGRADE_NEEDED` blocks with the way out; the classic tab keeps working | the owner finishes the upgrade (D3) |
| K6 | Two branches of one save live at once (twins) - cross-save and server behaviour unknown (R8 R3) | medium once "use an older version" is easy | `TWIN_LIVE` confirmation; D1 lineage keeps branches apart | unknown game-side effect |
| K7 | Expedition saves converting when placed after their season (R8 B5) | low / changed save | warnings `EXPEDITION_SOURCE`, `EXPEDITION_ACTIVE` | by design of the game |
| K8 | The load screen dates a placed save "now" (F5b) | certain / by decision | the original date stays in the vault (`source_meta_ts`, the source entry's meta) | none |
| K9 | App killed mid-write (Task Manager, power) | low / torn slot | write-ahead journal, exact rollback, recovery at next start; marker for a missing vault drive | the owner must not launch the game before reopening the app - the recovery prompt says so |
| K10 | F6 ownership of a copy that did not pass Hello Games' servers | unknown | gated behind T1/T2 | - |
| K11 | A slot the owner empties by hand fills again (Steam Cloud, Xbox sync) | medium / the slot is not empty | `EMPTY_SLOT_MAY_REFILL` and the steps (2.4.2); D1 reports the refill | the owner repeats the steps |

### 7.2 Open questions

The owner forks are decided (section 3). `ops.jsonl` is kept forever (F9). Open:

- Q1 "Put it back" after a cloud revert: one click with the original plan, or show the plan
  again? Recommended: show it again (the slot may have changed).
- Owner run E1 (review V-21): under Steam Cloud, place a save into a slot emptied by hand, launch
  the game, quit, relaunch: the placed save must still be there (expected: overwrites stick,
  R8 E1).
- Owner run E2: on Xbox, delete a throwaway save in the game's load menu, quit, let sync finish,
  restart: the slot must stay empty. Until it passes, the Xbox steps of 2.4.2 are marked "not
  yet confirmed".

For D6: block app exit while `execute` runs; surfaces for recovery items and verification
alerts; the confirmation flow (`confirmations` echoed back as `confirmed`); the account switch in
the picker; `empty_slot_guide` on the empty-slot card and the "Empty this slot" window; Keep a
copy; the "Note for this move" field (`execute(note=...)`); `LIVE_ELSEWHERE.data["cross_platform"]`.

## 8. Deviations from 01-consolidation section 6

1. **`LiveTarget`** carries D1's `instance` disambiguator (two Proton prefixes with the same
   folder name); UIs may pass D1's opaque `token`. D5 resolves it in core (2.3.6).
2. **Locator** as refined by D1/D3: `preserve(Sequence[LiveMember], *, op_id, reason) ->
   list[Location]` (D3's `StoreLocator`) and `open(vid, prefer)`; D5 needs `prefer` to place the
   meta exactly as captured.
3. **Edge kinds**: D5 follows D1 - place, swap, restore-older, promote and undo are operation
   kinds linked by `op_id`, not edge kinds; reverts are journal events.
4. **No Evict** (F8): section 6 lists Place/Swap/Evict; D5 implements no Evict and no deletion
   path; emptying a slot is the owner's manual procedure (2.4.2).
5. **New entry points**: `plan_keep` (vault-only Keep a copy), `empty_slot_guide`, the `note`
   argument of `execute`, `liveop_lock` for the classic writers (phase 1).
6. **New files**: `<vault>/organizer/ops.jsonl` (reserved by D1), `<vault>/organizer/.liveop.lock`,
   `<vault>/organizer/transfer-validation.json` (phase 5); per machine
   `user_cache_dir/vaults/<vault_id>/open-ops/` and `game-seen.json`, and
   `user_data_dir/machine-id`.
7. **`msstore` write primitives** change to record-bytes editing (P-X1), a new prerequisite not
   listed in section 3 of 01-consolidation (found here, 9.3).

No request to a sibling design is open: D1 provides `observe(..., skip)` and the `confirmed`
rule of 2.12.1 and takes the live-op lock in its phase-1 hook; D3 provides preserve, snapshots,
`read_file`, the listing and `vault_mode`; D4 only its decoder (phase 2b).

## 9. Evidence index

All scripts are D5's, in `<scratchpad>\D5\scripts\`, outputs `<scratchpad>\D5\*.out.txt`,
sandboxes in `<scratchpad>\D5\sbx\` (`<scratchpad>` = the session scratch folder under
`%LOCALAPPDATA%\Temp\claude\...`). Real files were only opened for reading; every write went to
the sandbox. Run from the repo root with `PYTHONUTF8=1 uv run python -I <script> ...`.

| # | Script(s) | Shows |
|---|---|---|
| 9.1 | `meta_regions.py`, `steam_0x54.py`, `steam_0x54b.py`, `xplat_same_bytes.py` (input: R7's `inventory.json`) | distinct metas: Steam 432 B x156, Xbox 360 B x36, 296 B x8; the only unknown non-zero Steam field is u32 0x54 = SizeDecompressed (144/156; the 12 others are editor-touched, where 0x3C and 0x54 are stale and 0x38 still equals the chunk total 156/156); Steam 0x08-0x37, 0x40 and the tail are zero; Xbox metas have no unknown non-zero bytes; Xbox time 0x120 non-zero 44/44, id 0x118 == data id 42/44 (2 pre-Worlds); 0 of 195 versions exist byte-identical on both platforms |
| 9.2 | `oplog_compat_exp.py` (oplog readers of today's code and of released versions) | a torn last line makes `read_oplog` raise `JSONDecodeError`, so an append must be a single write (case C) |
| 9.3 | `index_roundtrip.py`, `index_roundtrip2.py` (read-only on the live and vault `containers.index` files) | parse + rebuild is byte-identical for 0/7 indexes: the header FILETIME drifts in 7/7 and record FILETIMEs in 28/217 (max 16 ticks) because of the float round trip |
| 9.4 | `xbox_pair_write_proto.py` (synthetic wgs + xgs) | both Xbox members written with one index replace; untouched records raw-identical; rollback leaves the index byte-identical and the wgs and xgs trees identical, for an existing and a new identifier |
| 9.5 | `replace_open_file.py` | `os.replace` onto a file another handle holds open fails on Windows (`PermissionError`, winerror 5) |

Read-only proof (VERIFIED after all experiments): the vault's `catalog.json` (sha256
`a3a58014...a045b9`) and `oplog.jsonl` (`29b3ece9...598aaa`) equal R1's baseline;
`playthrough-ids.json` is unchanged (41,480 B, 2026-10-05 17:41); no file under the vault, the
live `st_*` folders or the Xbox `SystemAppData` tree was modified today.
