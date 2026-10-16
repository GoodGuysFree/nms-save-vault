# D5 - Live-slot operations: move to live, swap, evict, undo, recovery, verification

Status: design, wave 1. Author: D5 subagent, 2026-10-16. Subject S5 (C23-C26, C29-C30; forks
F4-F8). Binding inputs: `01-consolidation.md` sections 2, 5 and 6; R8 section 4 (the draft spec
this document turns into a design). Audience: the implementing agents (completeness wins) and the
parent. No production code was written; every experiment ran on synthetic sandboxes or as a
read-only probe (Evidence index at the end).

Tags: VERIFIED = run or read by D5; SOURCED = cited from code or research; INFERRED = reasoning.

## 0. Owner summary

1. "Move to live" puts any saved version into the live slot you pick; what was there is copied to the vault and checked first.
2. Both halves of a slot (auto-save, restore point) are replaced together; the version you picked is the one the game loads.
3. Each move is logged before it starts; after a crash the app finishes or reverses it. Undo restores only that slot and keeps what you played since.
4. After your next game launch the app checks that Steam Cloud or Xbox did not bring the old save back.
5. Your choices, my picks: keep the restore point (yes), keep the real date (yes), other accounts only if yours (yes), no emptying slots under Steam Cloud without your steps (yes).
6. Steam <-> Xbox moves come last, after you test one each way with a throwaway save.

## Contents

1. Scope, non-goals, dependencies
2. Design: model and API (2.1-2.3), flows (2.4), slot writing (2.5-2.6), the operation journal
   (2.7), execute (2.8), validation and rollback (2.9), recovery (2.10), undo (2.11), post-launch
   verification (2.12), oplog v2 and lineage (2.13), errors (2.14), safety invariants (2.15),
   concurrency and performance (2.16)
3. Options: F4 member policy, F5 timestamps, F7 cross-account, F8 deletions under Steam Cloud,
   F6 Steam <-> Xbox transfer
4. Migration and compatibility
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
- **Swap(T1, T2)** - exchange two live slots of one platform.
- **Evict(T)** - free a slot (deletion; governed by F8).
- **Promote(T, member)** - make the other member of a slot load; re-routed through the same
  machinery (fixes Xbox promote being a no-op, R8 A5).
- **Undo(op)** - slot-scoped and non-destructive (C24); multi-level per target.
- The **operation journal** (write-ahead intent, completion, verification state) and **recovery**
  of an interrupted operation on the next start.
- **Post-launch verification** (C25): Steam Cloud / Xbox reverts and resurrections.
- **Oplog record v2** and the call into D1's lineage recorder.
- The **eviction-picker data** the UIs render (C30) and the explicit-target rule (C29).
- What happens to today's operations and their GUI/CLI entry points (section 4).
- Phase-5 **Steam <-> Xbox transfer** (F6): meta conversion spec, gates, owner runs T1/T2.

### 1.2 Non-goals

- UI layout and visuals (D6 Tk, D7 web). D5 defines entry points, plan objects, confirmation
  codes and message texts; the UIs decide how they look.
- Version identity, lineage edges, live-journal observation, notes and pins (D1).
- Where preserved bytes live, retention and pruning (D2 / D3).
- Content facts (D4). D5 asks D4 for one extra fact (ownership identities, F7).
- Cross-account transfer of **another person's** save with an ownership rewrite (libNOM.io-style
  JSON rewrite): it breaks the verbatim rule (R8 Q4b). Not designed.
- Pre-Frontiers / pre-Worlds formats as transfer sources (refused, 3.5).
- Writing while NMS.exe runs: organizer operations have **no override** (2.15 I1).

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
| P2 age-based snapshot pruning | D2's version-aware pruning; plus the D5 rule "keep the snapshot named by the newest snapshot-bearing oplog record" (4.2) | D2 | old-app undo fall-through (9.2 E) |
| D1 ids, `LiveTarget`, `PairKey`, read model | live board (vids, MetaSnap, pins, line names) | D1 | planner |
| D1 `LineageRecorder.record_op`, `expected_pair`, `Journal.observe` events | lineage binding (C16), verification events | D1 | 2.12, 2.13 |
| D1 organizer store + lock (`safety.exclusive_lock`) | `ops.jsonl` (reserved for D5 by D1), one writer at a time | D1 | journal |
| D2/D3 `VersionLocator` | `open(vid, prefer)`, `preserve(LiveMember...)`, `is_durable(vid)`, `locate(vid)` | D2/D3 | preserve-before-replace |
| D2/D3 snapshots | whole-folder pre-op snapshot as today (`snapshot_live`) | D2/D3 | rule "auto-snapshot first"; old-app undo |
| D4 facts | `save.owners` from `UsedDiscoveryOwnersV2` (request, 8) | D4 | F7a only |
| UI (D6/D7) | consumes 2.2 objects; must block app exit during `execute`; shows recovery and verification items | D6/D7 | - |

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
| durable | `VersionLocator.is_durable(vid)`: an exact, hash-verified, non-prunable copy exists in the vault (D2 decision 3: managed full/extract/imported, never a snapshot) |

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
def plan_evict(ctx: OpContext, target: LiveTarget) -> PlacePlan: ...
def plan_promote(ctx: OpContext, target: LiveTarget, position: Position) -> PlacePlan: ...
def plan_undo(ctx: OpContext, op_id: str) -> PlacePlan: ...
def plan_panic_restore(ctx: OpContext, account: LiveAccount, snapshot_id: str | None = None, *,
                       restore_accountdata: bool = False) -> PlacePlan: ...      # 4.3
def execute(ctx: OpContext, plan: PlacePlan, confirmed: frozenset[str],
            progress: Callable[[Progress], None] | None = None) -> OpOutcome: ...
def pending_recovery(ctx: OpContext) -> list[RecoveryItem]: ...
def resolve_recovery(ctx: OpContext, op_id: str,
                     choice: Literal["forward", "back", "leave"]) -> OpOutcome: ...
def update_verification(ctx: OpContext, events: Sequence[JournalEvent]) -> list[VerifyItem]: ...
def verification_items(ctx: OpContext) -> list[VerifyItem]: ...
def op_history(ctx: OpContext, target: LiveTarget | None = None,
               limit: int = 50) -> list[OpSummary]: ...
def resolve_target(ctx: OpContext, target: LiveTarget) -> ResolvedTarget: ...
def busy_targets(ctx: OpContext) -> frozenset[LiveTarget]: ...          # 2.16, for D1 observe
def open_snapshot_ids(ctx: OpContext) -> frozenset[str]: ...           # 2.7.4, for D2 retention
```

All `plan_*` and `eviction_picker` calls are **read-only** (no file is created, not even the
journal). `execute` re-checks everything that can change between plan and execute (2.8 step 1).
Every returned object is a frozen dataclass with `to_dict()` (JSON-safe; the web UI option W
serialises it verbatim). Every text field is ASCII and already passed through `aliases.redact`;
no text contains a path or an account id.

#### Context and policies

```python
@dataclass
class OpContext:
    vault: Vault                         # catalog.Vault (oplog, snapshots)
    store: OrganizerStore                # D1: paths, vault_id, lock()
    locator: VersionLocator              # D2/D3
    lineage: LineageRecorder             # D1: record_op, expected_pair
    readmodel: ReadModel                 # D1: live_board(), line names, pins
    facts: FactsProvider | None          # D4 (optional; F7a ownership check)
    sources: Sequence[state.Source]      # configured live sources (state.json)
    policies: Policies
    host: platform.Host                  # D7: env and home arrive as arguments
    app_version: str
    machine_id: str                      # random per machine, user_data_dir(host)/machine-id
    game_probe: Callable[[], bool | None] = safety.is_game_running
    clock: Callable[[], float] = time.time
    faults: FaultInjector | None = None  # tests only (5.3); None in production

@dataclass(frozen=True)
class Policies:                          # one field per owner fork; defaults = recommendations
    member: Literal["pair", "twice"] = "pair"                                  # F4 (3.1)
    timestamp: Literal["keep", "now"] = "keep"                                 # F5 (3.2)
    cross_account: Literal["same_person", "warn", "block"] = "same_person"     # F7 (3.3)
    steam_cloud_delete: Literal["overwrite_only", "refuse_guided",
                                "delete_detect"] = "refuse_guided"             # F8 (3.4)
    cross_platform: Literal["off", "validated", "never"] = "off"               # F6 (3.5)
```

`Policies` is a constant chosen at build time from the owner's answers, not a user setting.
Only the chosen option of each fork is implemented; the field exists so tests can pin it and a
later change of mind is one constant.

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
    vault_copy: str                      # D1 BoardSlot.vault_copy: durable|partial|prunable-only|
                                         # outside-vault|none (finer than `durable`, for the UI)
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
    blocked: str                         # e.g. CROSS_PLATFORM_GATED, OTHER_PERSON, XGS_READ_ONLY
    warnings: tuple[Warning, ...]

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
    meta_ts: int                         # timestamp written into the meta
    meta_plain_sha256: str               # plaintext as written (after any timestamp edit)
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
    writes: tuple[MemberWrite, ...]      # 2, or 0 for evict
    deletes: tuple[Position, ...]        # evict, or the vacated side of a one-sided swap
    evictions: tuple[Eviction, ...]

@dataclass(frozen=True)
class Estimate:
    preserve_bytes: int                  # 0 when everything is already durable
    snapshot_bytes: int
    write_bytes: int
    seconds: float

@dataclass(frozen=True)
class PlacePlan:
    plan_id: str                         # random; not an op_id (op_id is minted by execute)
    kind: Literal["place", "place_into_empty", "restore_older", "swap", "evict",
                  "promote", "undo", "panic_restore"]
    targets: tuple[TargetPlan, ...]      # one per slot written: 1, 2 (swap), up to 15 (panic restore)
    source: SourceSummary | None         # None for evict / swap
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
    code: str | None = None              # 'refused' / 'failed': GAME_RUNNING, BUSY, PRESERVE_FAILED:<code>, ...
    replan: PlacePlan | None = None      # status 'stale': the fresh plan to show

@dataclass(frozen=True)
class Progress:
    step: Literal["checking", "preserving", "snapshot", "writing", "verifying", "recording"]
    done: int
    total: int
```

`RecoveryItem`, `VerifyItem` and `OpSummary` are defined in 2.10, 2.12 and 2.13.

### 2.3 Planner

#### 2.3.1 Which account the picker shows

1. The account named by the caller (the UI's account switch), else
2. the same-platform account where the source's pid is live (restore-older is the likely intent),
   else
3. the account the source pair was captured from (`prefer`), if it is a configured live account
   of the same platform, else
4. the first enabled, writable account of the source's platform (`sources` order).

Every other configured account appears in `other_accounts` with its block reason or warnings:
other platform -> `CROSS_PLATFORM_GATED` (until F6 opens, 3.5); same platform other account ->
F7 policy (3.3); xgs-only Xbox folder -> `XGS_READ_ONLY`.

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
| `PLAY_TIME_AHEAD` | confirm | candidate (restore_older) | occupant play time > source play time | "Steam slot 7 has 3h12m more play than this version. That newer save stays in the vault." |
| `PINNED` | warn | candidate | occupant pid pinned | "You pinned this save." |
| `MIXED_SLOT` | warn | candidate | state mixed | "This slot holds two different saves. Both will be kept in the vault." |
| `PARTIAL_SLOT` | warn | candidate | state partial | "Part of this slot is missing. What is there will be kept in the vault." |
| `UNREADABLE_MEMBER` | warn | candidate | state unreadable | "One file in this slot cannot be read. It will be kept in the vault as it is." |
| `PENDING_UPLOAD` | info | candidate | Xbox record Modified/Created | "This slot has changes Xbox has not uploaded yet." |
| `EXPEDITION_ACTIVE` | warn | candidate | occupant meta GameMode == 6 | "Expedition in progress. If the season ends while it is in the vault, it becomes a normal save when you put it back." |
| `EXPEDITION_SOURCE` | warn | plan | source GameMode == 6 | "Expedition save. If its season has ended, the game turns it into a normal save when you load it." |
| `TWIN_LIVE` | confirm | candidate | source pid live in another slot of this account and this slot is not that one | "This save is already in Steam slot 4. A second copy makes two separate branches." |
| `LIVE_ELSEWHERE` | info | plan | source pid live on another account/platform | "This save is also in Xbox slot 4." |
| `ALREADY_LIVE` | block | candidate | kind noop | "This version is already in Steam slot 7." |
| `CROSS_PLATFORM_GATED` | block | account | F6 not open | "Moving saves between Steam and Xbox is not available yet." |
| `OTHER_ACCOUNT` | confirm | account | same platform, other account, F7 allows | "This save was played on another account. Bases may stay owned by that account until you play it here." |
| `OTHER_PERSON` | block (F7a/c) / confirm (F7b) | account | ownership identities not all the owner's (3.3) | "This save belongs to someone else's account." |
| `XGS_READ_ONLY` | block | account | Xbox folder without `containers.index` | "This Xbox folder can only be read." |
| `GAME_RUNNING` | block | plan | `game_probe() is True` | "No Man's Sky is running. Close it first." |
| `GAME_STATE_UNKNOWN` | confirm | plan | `game_probe() is None` | "Could not check whether No Man's Sky is running. Make sure it is closed." |
| `CROSS_SAVE_ON` | warn | plan | accountdata CrossSaves true (if known) | "Hello Games cross-save is on: the game may ask which copy to keep." |
| `STEAM_CLOUD_OTHER_DEVICE` | info | plan | cloud mode steam-cloud | "If Steam asks at the next launch, choose the copy on this PC." |
| `STEAM_CLOUD_DELETE` | block or confirm (F8) | plan | a delete under steam-cloud | "Steam Cloud puts deleted saves back. See the steps." |
| `XBOX_SYNC_BUSY` | block (soft) | plan | `containers.index` mtime < 60 s ago | "Xbox is still syncing. Try again in a minute." |
| `XBOX_QUOTA` | warn / block | plan | wgs total after the op > 200 MB / > 256 MB | "Xbox cloud space would be nearly full / over the 256 MB limit." |
| `VAULT_UNAVAILABLE` | block | plan | vault root missing or not writable | "The vault is not reachable. Nothing was changed." |
| `VAULT_SPACE` | block | plan | free space < estimate x 1.5 + 64 MB (vault) or < 2 x write bytes (live drive) | "Not enough free disk space." |
| `SOURCE_UNREADABLE` | block | plan | locator cannot open/verify the source | "That version cannot be read from the vault." |
| `RECOVERY_PENDING` | block | plan | an open op on any target account (2.10) | "An earlier move was interrupted. Resolve it first." |
| `STALE_SOURCE_MIXED` | warn | plan | source pair's members have different pids | "The saved slot held two different saves; only the chosen one is used." |

Codes used only by one flow are defined with it: undo `PLAYED_SINCE`, `ALREADY_UNDONE`,
`UNDO_STALE` (2.11); F8 `STEAM_CLOUD_OFF_CONFIRMED` (3.4); F6 `SOURCE_OLD_FORMAT`,
`UNSUPPORTED_META`, `SOURCE_NO_ID` (3.5.1); outcome codes `BUSY`, `PRESERVE_FAILED:<code>`,
`SNAPSHOT_FAILED`, `RECORDING_PENDING` (2.14).

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

#### 2.3.5 `plan_place` (read-only)

```python
def plan_place(ctx, source, target) -> PlacePlan:
    rt = resolve_target(ctx, target)                 # C29, 2.3.6; raises TargetError
    src = load_source(ctx, source)                   # metas via locator.open(vid, prefer); data
                                                     # length + vid existence only (no full read)
    gates = transfer_gates(ctx, src, rt)             # F6 / F7 / XGS_READ_ONLY (3.3, 3.5)
    cur = read_slot(rt)                              # pre-image candidate: hashes via D1 cache
    kind = classify(src, cur)                        # place | place_into_empty | restore_older | noop
    writes = pairmeta.compute(src, rt, ctx.policies, now=int(ctx.clock()))     # 2.5
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

Every operation is one `op` and runs through the same `execute` (2.8): resolve, preserve,
snapshot, journal, write, validate, record. They differ only in what they write and delete.

| Operation | Preserved first | Writes | Deletes | Undo (2.11) |
|---|---|---|---|---|
| **Place** (T occupied) | both present members of T | A and B of T (2.5) | - | Place the evicted pair back |
| **PlaceIntoEmpty** | - | A and B of T | - | Evict T (F8 rules) |
| **Restore-older** (T holds the source's pid) | both members (the newer tip) | A and B of T | - | Place the evicted pair back |
| **Swap** (both full) | both slots | the 4 positions, each pair keeps its positions and metas (re-keyed on Steam) | - | Swap back |
| **Swap** (one side empty = move) | the full slot | the empty slot's A and B | the vacated slot's A and B (F8) | move back |
| **Evict** | both members | - | A and B of T (F8; Xbox: records flagged Deleted) | PlaceIntoEmpty the evicted pair |
| **Promote** (make position p load) | both members (keeps the exact old metas for undo) | A and B of T: same data, re-timed metas (2.5) | - | Place the old pair (old metas) back |
| **Undo** of op X | T's current content when it differs from what X wrote | the inverse of X | the inverse of X | redo X (an undo is an op) |

Rules shared by all of them:

- **Both positions are written** whenever a slot is filled (I5). A source with one member, or
  with members of two pids, fills the other position with a copy of V (2.5). No operation ever
  leaves a mixed slot (P6).
- **Data is verbatim.** Only metas change, and only by: Steam re-key (ordinal of the destination
  position), the timestamp edits of 2.5, and F6 conversion (3.5).
- **Never touched:** `accountdata.hg` / `mf_accountdata.hg` / the Xbox `AccountData` record and
  its xgs folder, `steam_autocloud.vdf`, `cache/`, any other slot, any other account's folder or
  `containers.index`, and every file the op did not create (I7).
- A **cross-folder Swap** (two accounts of one platform) is one op over two targets with two
  snapshots, gated by F7. A cross-platform Swap is two transfers, gated by F6.

### 2.5 What is written: positions, metas and times

`pairmeta.compute(src, rt, policies, now) -> tuple[MemberWrite, MemberWrite]` is a pure function
(no I/O) so every case is unit-testable. Shown with the recommended policies F4a ("pair") and
F5a ("keep"); section 3 gives the alternatives.

```python
def compute(src: SourcePair, rt: ResolvedTarget, pol: Policies, now: int):
    L = src.load                                   # position V was captured in
    O = other(L)
    V = src.member(L)                              # bytes + meta plaintext (locator.open(prefer))
    P = src.member(O)                              # partner or None
    use_partner = (pol.member == "pair" and P is not None and P.pid == V.pid)
    Pw = P if use_partner else V                   # O position gets the partner or a copy of V

    # 1) meta timestamps: V must be newest by meta time
    #    (metas without a timestamp field skip steps 1-2 and keep their bytes, 2.5.2)
    if pol.timestamp == "keep":
        ts_L = V.meta_ts or now                    # a stored 0 means unknown -> now
        ts_O = Pw.meta_ts if (use_partner and Pw.meta_ts < ts_L) else ts_L - 1
    else:                                          # "now"
        ts_L, ts_O = now, now - 1

    # 2) metas: timestamp edit (only if changed), then platform encoding
    plain_L = with_ts(V.meta_plain, ts_L, rt.platform)
    plain_O = with_ts(Pw.meta_plain, ts_O, rt.platform)
    if rt.platform == "steam":
        disk_L = meta.encrypt(plain_L, storage_ordinal(file_no(slot, L)))   # re-key
        disk_O = meta.encrypt(plain_O, storage_ordinal(file_no(slot, O)))
        assert meta.is_valid_for(disk_L, ...) and meta.is_valid_for(disk_O, ...)
    else:
        disk_L, disk_O = plain_L, plain_O          # plaintext; same bytes to wgs and xgs

    # 3) on-disk times: V newest by mtime / FILETIME as well
    if rt.cloud_mode in ("steam-cloud", "xbox"):
        t_L, t_O = float(now), float(now - 1)
    else:                                          # steam-local: the save keeps its age
        t_L = float(ts_L)
        t_O = float(min(ts_O, ts_L - 1))
    return (MemberWrite(position=L, ...), MemberWrite(position=O, copy_of=None if use_partner else L, ...))
```

#### 2.5.1 Why these rules

- **Positions are kept as captured.** A captured pair goes back exactly as it was; when V is the
  newer member (the default) no timestamp is edited at all, so the common Place changes nothing
  but the Steam re-key (R7 2.4 measured: in 581/581 captured pairs the newer member by meta time
  also has the higher or equal play time, so the default `load` is also the most progressed).
- **"V newest by every signal"** (C23, R8 C5): meta timestamp (what the load screen sorts by,
  DESIGN.md), Steam file mtime order (what Steam Cloud and any mtime fallback see) and Xbox
  FILETIME order (what today's app and possibly the Xbox app use when the meta has no time)
  all put L above O. Strict, not equal: a tie has no defined winner.
- **Disk time = now under Steam Cloud and on Xbox.** A file stamped older than the cloud's copy
  risks being replaced by it at the next launch (project rule, `operations.py:253-257`; R8 G5).
  The meta keeps the real save date (F5a), so the in-game date stays honest. Xbox FILETIME =
  now follows R8 Q8 (the record's local write time; R8 A5 shows it already is a write/sync time,
  not the save time).
- **Steam with Cloud off** keeps the save's age on disk (today's behaviour,
  `operations.py:350-357`), in the same order.
- **A copy of V in O** (single-member or mixed source) has `ts = V.ts - 1`, so V loads and the
  copy is the fallback the game offers - the same playthrough, never a different one.

#### 2.5.2 Edge cases

| Case | Rule |
|---|---|
| V's meta has no timestamp field (Xbox 280-B Waypoint meta, pre-Waypoint Steam) | order by disk time only; `ts_*` unused; warning-free (the game has nothing else to sort by) |
| `V.meta_ts == 0` in a meta that has the field | treated as unknown: stamp `now` (as `_repopulate_slot_xbox` does today, `operations.py:508`) |
| partner newer than V (the user picked the restore point / promote) | F4a: partner re-timed to `V.ts - 1`; F4b: partner not written |
| partner has more play time but older meta time | no edit needed (meta time decides); record it |
| source pair members have different pids | treated as single-member (copy of V); `STALE_SOURCE_MIXED` warning |
| Promote of position p | `src` = the slot's own pair with `load = p`; the rules above re-time the other position (F4a) or both (F5b) |
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
    deletes: tuple[str, ...]                   # evict / vacated side (F8 permitting)
    pre: Mapping[str, FileState | None]        # every name the op touches; None = absent

def prepare(folder: Path, writes: Sequence[tuple[MemberWrite, bytes, bytes]],
            deletes: Sequence[Position], slot: int, op_tag: str) -> SteamWritePlan: ...
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
- **Deletes** (only where F8 allows, 3.4) come after the renames.
- **Rollback (exact):** for every name in `pre`: if it existed, write its pre-op bytes
  (atomic) and set its pre-op mtime; if it did not, delete it. Pre-op bytes come from the op's
  whole-folder snapshot (made with `copy2`, so mtimes are kept), checked against `pre[name].sha256`;
  fallback: the preserved versions (data verbatim; a Steam meta re-encrypted from its plaintext
  with the same ordinal is byte-identical because XXTEA here is deterministic). Then `sweep`.
  Under Steam Cloud this restores exactly the state Steam's manifest already knows, and deleting
  a file the op created is safe because Steam syncs only at game launch and exit, so it never
  saw that file (R8 E1).

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
    deleted: tuple[str, ...]                    # identifiers flagged Deleted (evict)
    xgs: bool

def prepare_pair(folder: Path, writes: Sequence[XboxRecordWrite],
                 deletes: Sequence[str] = ()) -> XboxPairPlan: ...   # reads only; GUIDs pre-generated
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
  identifier, or Deleted for an evict; FILETIME from an int), header FILETIME and state patched
  as integers; both positions flip together; (3) mirror both identifiers into xgs when the twin
  exists (wgs first, then xgs, as today; read-back compare).
- **finalize_pair** (after validation): delete `superseded`; for deleted identifiers remove the
  old blob dir (only a GUID-named child of the account folder) and the xgs identifier folder.
  Until then the old blobs stay on disk, unreferenced, so an exact rollback needs nothing from the
  vault.
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
- `write_save`, `delete_save` and `mark_modified` are re-implemented on the same raw editing
  (fixes P-X1 for the classic paths too). The parser keeps each record's raw bytes and integer
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
  (audit + old-app undo, 2.13).

#### 2.7.2 Events

Every line: `{"j": 1, "op_id", "seq", "event", "utc", "machine"}` plus the event's payload.

| Event | When | Payload (abridged) |
|---|---|---|
| `preparing` | `execute` starts, before any vault write | `kind`, `app`, `targets[] {platform, account_key, slot}`, `source {vid_a, vid_b, load, prefer}`, `undo_of`, `note`, `confirmed[]`, `plan_digest` |
| `writing` | after preserve + snapshot + writer prepare, **before the first live write** (the write-ahead intent) | per target: `cloud_mode`, `snapshot_id`, `preserved[] {position, vid, location, already}`, `pre {A, B: {vid, pid, data: FileState or null, meta: FileState or null}}`, `post {A, B: MemberWrite}`, `deletes[]`, `steam {files[] {name, temp, sha256, mtime}, deletes[]}` or `xbox {pre_index_sha256, pre_header_b64, pre_records_b64{id: b64 or null}, new_records_b64{}, new_files[], new_dirs[], superseded[], deleted[], xgs}` |
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
 "preserved":[{"position":"A","vid":"<vid A old>","location":{"kind":"entry","ref":"keep-steam-20261016-131502","slot":7,"member":"A"},"already":false},
              {"position":"B","vid":"<vid B old>","location":{"kind":"entry","ref":"keep-steam-20261016-131502","slot":7,"member":"B"},"already":false}],
 "pre":{"A":{"vid":"<vid A old>","pid":"<pid16>","data":{"sha256":"<vid A old>","size":1800000,"mtime_ns":1791900000000000000},"meta":{"sha256":"<sha>","size":432,"mtime_ns":1791900000000000000}},"B":{...}},
 "post":{"A":{"position":"A","vid":"<vid A new>","pid":"<pid16>","meta_ts":1791500000,"meta_plain_sha256":"...","meta_disk_sha256":"...","disk_time":1792500903.0,"ordinal":14,"copy_of":null,"origin":{...}},"B":{...}},
 "deletes":[],
 "steam":{"files":[{"name":"save14.hg","temp":".tmp_3fa2_save14.hg.part","sha256":"...","mtime":1792500902.0},
                   {"name":"mf_save14.hg","temp":".tmp_3fa2_mf_save14.hg.part","sha256":"...","mtime":1792500902.0},
                   {"name":"save13.hg","temp":".tmp_3fa2_save13.hg.part","sha256":"...","mtime":1792500903.0},
                   {"name":"mf_save13.hg","temp":".tmp_3fa2_mf_save13.hg.part","sha256":"...","mtime":1792500903.0}],"deletes":[]}}]}
```

#### 2.7.3 Relation to the other records

| Record | Owner | Role for an op |
|---|---|---|
| `organizer/ops.jsonl` | D5 | state machine of the op: intent, progress, outcome, verification; the source of truth for recovery, undo and verification |
| `oplog.jsonl` (vault root) | D5 writes, everyone reads | one v2 line per finished op (`done`, `rolled_back`, `failed`): audit trail and **old-app undo compatibility** (2.13) |
| `organizer/lineage.json` + `journal.jsonl` | D1 | `record_op` gives D1 the binding "T holds pair P since op_id" (C16) and the evicted pair; D1's live journal then observes the game's later versions and emits events that D5's verification consumes |
| catalog entries | D2/D3 | the preserved versions (`extract` entries, D2 decision 1) and the pre-op snapshot |

#### 2.7.4 Local marker (vault on another drive)

Right after the `writing` event, `execute` writes
`platform.user_cache_dir(host)/vaults/<vault_id>/open-ops/<op_id>.json` (D1's per-machine,
per-vault folder) = `{op_id, vault_id, vault_root, labels[], snapshot_ids[], utc}` (atomic), and
deletes it after the terminal event. At startup the app lists `vaults/*/open-ops/`; a marker
whose vault is not reachable yields a `vault_missing` recovery item: "A move into Steam slot 7 was
interrupted and its records are on the vault drive. Connect it before playing." The marker holds
no save data; losing it (cache cleaned) loses only that hint. A per-machine id
(`platform.user_data_dir(host)/machine-id`, random, deliberately not in the cache folder) tags
every event so a vault shared between PCs (D11) never recovers another machine's op here.

D2 pins the snapshots of open ops (its `pinned_entries`): `liveops.open_snapshot_ids(ctx) ->
frozenset[str]` returns the `snapshot_id`s of every open op's `writing` event plus those named
in this machine's markers.

### 2.8 Execute

```python
def execute(ctx, plan, confirmed, progress=None) -> OpOutcome:
    if plan.blocked:
        return refused(plan)
    missing = plan.confirmations - confirmed
    if missing:
        raise ConfirmationRequired(missing)
    with liveop_lock(ctx, timeout=10):                                  # one live op at a time (2.16);
                                                                        # timeout -> refused(code="BUSY")
        # 1 re-check what can change between plan and execute (none of these writes anything)
        if fingerprint_now(ctx, plan) != plan.fingerprint:
            return stale(replan(ctx, plan))
        if open_ops_on(ctx, plan.accounts()):
            return refused(plan, code="RECOVERY_PENDING")
        if (why := vault_space_problem(ctx, plan.estimate)):           # VAULT_UNAVAILABLE / VAULT_SPACE
            return refused(plan, code=why)
        if (why := game_problem(ctx, confirmed)):                       # GAME_RUNNING / unconfirmed unknown
            return refused(plan, code=why)
        ctx.lineage.observe(scan_targets(ctx, plan), src="scan")        # D1: journal the pre-op state
        op_id = new_op_id(ctx.clock)                                    # section 6 format
        jr.append(ctx, preparing(op_id, plan, confirmed))               # short store-lock txn
        try:
            # 2 the source: open, verify, compute final bytes
            payload = load_and_verify_source(ctx, plan)                 # sha256 == vid, chunk total
            # 3 preserve every member that will be replaced or deleted
            preserved = preserve_targets(ctx, plan, op_id)              # locator.preserve(...)
            # 4 whole-folder pre-op snapshot per target folder
            snaps = {t: storage_snapshot(ctx, t, reason) for t in plan.folders()}
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
        record(ctx, op_id, plan, preserved, snaps)                      # 2.13; errors -> stays 'validated'
        ctx.lineage.observe(scan_targets(ctx, plan), src="verify")     # D1: the post-write state
        jr.append(ctx, done(op_id)); marker.remove(ctx, op_id)
    storage_after_operation(ctx, op_id)                                 # D2 retention hook (2.9.5 of D2)
    return OpOutcome(op_id, "done", outcome_text(plan), undo=undo_offer(plan, op_id),
                     verify="pending", warnings=..., error=None)
```

Notes:

- **Preserve** calls `locator.preserve([LiveMember(target, position, expected_vid), ...],
  op_id=op_id, reason=kind)` once per target slot (D2 2.7: takes the store lock itself, re-reads
  the live slot and refuses `changed-since-plan`, writes both members of the slot into one
  `keep-<plat>-<stamp>` extract entry when any lacks an exact durable copy, dedupes by (vid, meta
  plaintext sha256)). Every `PreserveError` code (`live-missing`, `member-missing`,
  `changed-since-plan`, `vault-unavailable`, `vault-full`, `verify-failed`, `lock-timeout`,
  `io-error`) becomes `Abandon`; `changed-since-plan` returns `status="stale"` with a fresh plan.
  The returned `Location`s must be `durable` and not `prunable`. An unreadable member is
  preserved as raw files (its `vid` is still the data sha256).
- **Snapshot** = today's `snapshot_live` (Steam `*.hg` + vdf; Xbox the whole account folder),
  via whatever D2/D3 expose for it. It is the exact pre-image for rollback and the target of the
  v2 oplog `snapshot_id` (old-app undo, 4.2). It is taken after preserve so the evicted versions
  are durable even if the snapshot is pruned later.
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
| records | - | our records: state Modified/Created (Deleted for evict), FILETIME as planned (integer); every other record raw-identical to the pre-image |
| mirror | - | when xgs exists: `xgs/<id>/data` and `meta` byte-equal to the wgs blobs |
| deletes | deleted names absent | identifiers flagged Deleted; slot empty in `scan_any` |
| containment | every other `*.hg` and the vdf unchanged (name, size, `mtime_ns` vs the snapshot listing); no `.tmp_<op4>_*` left | header identity unchanged (process id, account id) |

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
| `writing` | any `other` (the game, an older app version or another tool wrote the slot since) | **leave** only: preserve the slot's current content if not durable, `abandoned(superseded)`; the evicted save is in the vault; explain |
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
| empty | `empty` | PlaceIntoEmpty the evicted pair |
| anything else (another pid, or the same pid with less play) | `stale` | blocked `UNDO_STALE`: "Steam slot 7 has changed since (it now holds 'X'). Use Move to live to put 'Polestar' where you want it." The UI may open the picker with the evicted pair as the source. |

Per op kind:

- **place / restore_older / promote**: as the table. The evicted pair goes back with its
  preserved metas and positions (`load` = the position that was loading before), so its in-game
  date and loading member are exactly as before; only disk times follow 2.5 (now under Steam
  Cloud and on Xbox).
- **place_into_empty**: the inverse is an Evict of T after preserving its current content;
  under Steam Cloud the F8 policy decides (3.4). A slot that held one member before is restored
  with that member in both positions (2.5, I5) - never a mixed slot, never a deletion.
- **evict**: PlaceIntoEmpty of the evicted pair when T is still empty; when T was refilled since,
  a normal Place with eviction and the `TWIN_LIVE` / `PLAY_TIME_AHEAD` checks as usual.
- **swap**: swap back when both slots are `same` or `progressed` (progress travels with its
  save); any `stale` slot blocks the whole undo with the per-slot explanation.
- **undo**: the inverse of an undo is the original op again (redo), same rules.
- Multi-level: `op_history(target)` lists the target's ops newest first; the UI offers Undo for
  the newest `done` op of each target (the non-modal "Done - Undo" of C30) and from the history
  list for older ones. Each undo is a new op with `undo_of`, so it is itself undoable.
- Whole-folder undo survives only as the **panic restore** (4.3).

### 2.12 Post-launch verification (C25)

A placed save can be undone outside the app: Steam Cloud offers its copy at the next launch and
the player may pick it (R8 R1); Xbox sync or a console session can do the same (R8 R2); a
deleted Steam file comes back (R8 E1). Every `done` op therefore enters verification per target.

**Evidence D5 uses** (all read-only):

| Source | Gives |
|---|---|
| D1 `LineageRecorder.op_outcome(op_id, target)` and `JournalEvent`s from each `observe` | `reverted` (the slot holds the evicted pair again), `superseded` (a later op), `changed` / `replaced` (another pid), `resurrected` / `deleted` events, and children of the placed pair (the game saved on top of it) |
| Xbox record sync state of our identifiers | Modified/Created after the write -> Synced later = uploaded |
| Steam `remotecache.vdf` (optional reader, 2.12.2) | our files' SHA-1 present and synced = uploaded |
| game sessions seen by this app | `safety.is_game_running()` results recorded by each GUI refresh / CLI call into `user_cache_dir/vaults/<vault_id>/game-seen.json` (`last_seen_running` UTC) |

**States** per (op, target):

| State | Rule | UI (alert = shown until dismissed) |
|---|---|---|
| `pending` | no evidence yet | slot badge "Waiting for the next game launch to confirm" |
| `confirmed` | a child of the placed pair is live (detail `played`); or the placed pair is still live after a game session seen after the op (`unchanged`); or uploaded (Xbox Synced, Steam manifest) (`uploaded`) | none; tick in the op history |
| `reverted` | the slot holds the evicted pair, or another version of the evicted pid that is not a descendant of the placed pair | alert: "Steam Cloud put the old save back in Steam slot 7. 'Main Save' is safe in the vault." Actions: Put it back (a new `plan_place` of the placed pair), Dismiss. Hint: "When Steam asks at launch, choose the copy on this PC." |
| `resurrected` | after an evict (or the deleted side of a move): files of the deleted pair are back | alert with the F8 steps (3.4); actions: Show steps, Dismiss (Clear again only under F8c) |
| `deleted` | the slot is empty while the placed pair was expected | alert: "Steam slot 7 is empty again. 'Main Save' is safe in the vault." Action: Put it back |
| `replaced` | another pid (the game started a new save there, another tool, an old app version) | info in the history |
| `superseded` | a later op on the target | none |
| `expired` | still `pending` after 30 days | none |

```python
@dataclass(frozen=True)
class VerifyItem:
    op_id: str
    target: LiveTarget
    label: str                           # "Steam slot 7"
    state: Literal["pending", "confirmed", "reverted", "resurrected", "deleted",
                   "replaced", "superseded", "expired"]
    detail: str                          # played | unchanged | uploaded | ...
    text: str
    actions: tuple[Literal["place_again", "clear_again", "show_steps", "dismiss"], ...]
    since: str                           # local ISO time of the op

def update_verification(ctx, events) -> list[VerifyItem]:
    """Called after every D1 observe (GUI refresh, app start, CLI 'scan'). For each (op, target)
    whose verify state is pending or alerting: derive the state from the evidence above; on a
    change append a `verify` event to ops.jsonl (short store-lock txn). Returns the items the UI
    should show (alerts + pending badges)."""
```

Dismissing an alert appends `verify {state, dismissed: true}`; the history keeps it. "Put it
back" never reuses the old op: it is a new Place from the vault (the placed versions are durable:
they came from the vault, and D2 keeps placed/evicted versions protected).

#### 2.12.1 Request to D1 on `op_outcome`

D5's own `observe(src="verify")` right after the write holds the placed pair, so D1's
"confirmed = the next observation holds the placed pair" would confirm every op immediately.
D5 therefore treats D1's `confirmed` as `confirmed` only when the observation shows a child of
the placed pair; an unchanged placed pair stays `pending` until a game session or an upload is
seen (rule above). Listed in section 8.

#### 2.12.2 Steam `remotecache.vdf` reader (optional, phase 2b)

`steamcloud.read_manifest(steam_root: Path, accountid32: int) -> dict[str, ManifestEntry]`
parses `<steam_root>/userdata/<accountid32>/275850/remotecache.vdf` (format R8 E1: per file
`size`, `localtime`, `time`, `remotetime`, `sha` SHA-1, `syncstate`). Stdlib only (D6); the
Steam root comes from `platform.steam_roots(host)` (D7); `accountid32 = steamid64 -
76561197960265728` from the folder name. Used only for `uploaded` evidence and a planner
`PENDING_UPLOAD` hint on Steam; never displayed (the path holds the account id). Absent or
unparsable -> no evidence, never an error.

### 2.13 Oplog record v2 and lineage recording

#### 2.13.1 The oplog line

One line per op that reached `done`, `rolled_back` or `failed` (none for `abandoned`: nothing
changed), appended to `<vault>/oplog.jsonl` as **one** `write` + `flush` + `fsync` (VERIFIED
9.2 C: a torn last line makes `read_oplog` raise in every released version, so a partial line
must never be left). The first six keys are today's (`operations.py:958-966`) so every released
version reads it; the rest are new (additive keys are ignored by old readers, R1 E4; VERIFIED
9.2 A, D).

```json
{"ts": "2026-10-16T13:15:04", "op": "place", "entry_id": "keep-steam-20261016-131503",
 "snapshot_id": "snapshot-steam-20261016-131503",
 "detail": "place 'Main Save' into steam slot 7; 'Polestar' kept in the vault",
 "changed": ["save14.hg", "mf_save14.hg", "save13.hg", "mf_save13.hg"],
 "v": 2, "op_id": "op-20261016T101503Z-3fa2", "utc": "2026-10-16T10:15:04Z",
 "app": "nms-save-vault 0.4.0", "kind": "place", "status": "done", "undo_of": null,
 "targets": [{"platform": "steam", "account_key": "st_<steamid64>", "instance": "", "slot": 7,
   "cloud_mode": "steam-cloud", "snapshot_id": "snapshot-steam-20261016-131503",
   "written": [{"position": "A", "vid": "<64 hex>", "pid": "<16 hex>", "play_time": 1900000,
                "meta_ts": 1791500000, "disk_time": 1792500904.0, "ordinal": 14,
                "meta_plain_sha256": "<64 hex>", "meta_disk_sha256": "<64 hex>", "copy_of": null,
                "origin": {"kind": "entry", "ref": "full-steam-20261005-174129", "slot": 9, "member": "A"}},
               {"position": "B", "...": "..."}],
   "evicted": [{"position": "A", "vid": "<64 hex>", "pid": "<16 hex>", "play_time": 100000,
                "meta_ts": 1791900000, "preserved_at": {"kind": "entry", "ref": "keep-steam-20261016-131503", "slot": 7, "member": "A"},
                "already_durable": false}, {"position": "B", "...": "..."}],
   "deleted": []}],
 "confirmed": ["PLAY_TIME_AHEAD"], "note": "", "error": null}
```

| Key | Rule |
|---|---|
| `ts` | local ISO seconds, as today |
| `op` | the D5 kind (`place`, `place_into_empty`, `restore_older`, `swap`, `evict`, `promote`, `undo`) |
| `entry_id` | the `keep-` entry `preserve` created (first target), else null; informational |
| `snapshot_id` | **always a whole-folder snapshot** of the first target's folder (4.2); never an extract or a slot-scoped entry |
| `detail` | ASCII; save names allowed, never a folder name, path or account id (today's lines embed folder names that contain account ids, R1 E4) |
| `changed` | file names (Steam) or identifiers (Xbox) written; `-` prefix for deletions; as today |
| `targets[].account_key` | internal key; redacted on any display or export |
| `status` | `done`, `rolled_back`, `failed`; `error` holds the message for the latter two |

Field set = R7 2.8 items 1-5 and R8 4.6 (op id, UTC, app version, target, per written member
vid/pid/meta hashes/meta time/disk time/ordinal/origin as a version reference, per evicted member
hashes/id/where preserved/already durable, snapshot, cloud mode, confirmed warnings, note,
`undo_of`). R7 item 6 (the later "confirmed" mark) lives in `ops.jsonl` `verify` events and D1's
journal, not in the append-only oplog.

#### 2.13.2 Lineage

After validation, per target: `ctx.lineage.record_op(OpLineage(op_id, kind, at, target,
placed=PairKey(post A vid, post B vid) or (None, None) for evict, evicted=PairKey(pre) or None,
source=<Location of the placed bytes>, preserved={vid: Location}, status, undo_of, note,
app))` - D1 2.6.6; idempotent on `(op_id, target)`, so a Swap makes two calls and recovery may
repeat them. A `rolled_back` op is recorded with `status="rolled_back"` (D1 keeps the previous
binding). Order: oplog line, then `record_op`, then the `done` journal event; on restart a
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
| another live op running (lock timeout) | `refused`, `BUSY` | none | "Another operation is still running." |
| vault missing / full / preserve failed | `refused`, `PRESERVE_FAILED:<code>` | none | "The current save could not be copied to the vault (vault full). Nothing was changed." |
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
| I2 | Every member that will be replaced or deleted is durable and hash-verified in the vault before the first live write | `locator.preserve` (D2 2.7), `Abandon` on any error | T-P1..P4 |
| I3 | Data bytes are written verbatim: `sha256 == vid` on the staged bytes and after the write | writers + validation | T-W1, T-V1 |
| I4 | Only metas change, and only by re-key, the 2.5 timestamp rules, or F6 conversion | `pairmeta`, `convert` (pure, property-tested) | T-M1..M8 |
| I5 | Filling a slot always writes both positions; no op leaves a mixed slot | `pairmeta` (copy of V), planner | T-M3, T-F2 |
| I6 | The member that must load is newest by meta time, Steam mtime and Xbox FILETIME | `pairmeta` + validation `order` row | T-M2, T-V2 |
| I7 | Never touched: accountdata (both platforms), `steam_autocloud.vdf`, `cache/`, other slots, other accounts, other `containers.index` records, files the op did not create | writers only take the slot's names / identifiers; validation `containment` row | T-V3, T-X3 |
| I8 | Every write is staged, then swapped, then validated; a failure rolls the slot back exactly | writers 2.6, 2.9.2 | T-C1..C14 |
| I9 | No deletion under Steam Cloud except (a) as F8 allows, (b) a rollback deleting a file this op created | writers + F8 policy | T-F8a..c |
| I10 | One live op at a time; an open op blocks new live ops on its account | live-op lock, `RECOVERY_PENDING` | T-R5, T-L1 |
| I11 | Write-ahead: the `writing` event (with every file the op may create) is fsynced before the first live write | `execute` order, `prepare` before `apply` | T-C1..C14 |
| I12 | The oplog `snapshot_id` of every v2 line names a whole-folder snapshot | `oplog2` writer | T-O1..O3 |
| I13 | Recovery never writes a slot whose content matches neither the pre- nor the post-image | recovery classification | T-R3 |
| I14 | UI text is ASCII and redacted; no account id, folder name or path in messages, logs or the oplog `detail` | `Warning.text` builder, `tests/test_ui_text.py` | T-U1 |

### 2.16 Concurrency and performance

**Locks.**

- **Live-op lock** `<vault>/organizer/.liveop.lock` (`safety.exclusive_lock`, the OS lock D1
  adds), held for a whole `execute` or recovery action, timeout 10 s -> `BUSY`. It serialises
  two app instances (and two machines sharing one vault).
- **Store lock** (D1): only per append to `ops.jsonl`; `preserve` and `record_op` take it
  themselves (D1's lock is not re-entrant, so D5 never holds it across those calls).
- **Busy targets.** `liveops.busy_targets(ctx) -> frozenset[LiveTarget]` = targets of open ops.
  D1's `observe` must skip them (record `skipped (target, "operation in progress")`), so a GUI
  refresh during a write never journals a torn slot as a game event (request, section 8).
- **GUI.** `execute` runs on a worker thread; the Tk thread never reads save files (C31); the
  window cannot close while it runs; refresh timers pause.
- **Older app versions** take no lock. The fingerprint (stale check), the preserve-time
  `expected_vid` check (D2) and recovery's `superseded` class contain what they can do.

**Performance** (INFERRED from measured rates: R1 2h scans 18-43 ms per live folder, R7 2.9
hashing ~1.7 GB/s warm, D2 2.7 preserve 30-80 ms per Steam slot, R2 2.5 `tasklist` 175 ms):

| Step | Typical (SSD, warm) | Notes |
|---|---|---|
| `eviction_picker` | < 50 ms + game probe 175 ms | from D1's live board; probe off the Tk thread |
| `plan_place` | 30-80 ms | metas only; source data not read |
| source open + verify | 10-60 ms | reads 2-28 MB, sha256, chunk-header walk |
| preserve | 0-80 ms | 0 when already durable |
| whole-folder snapshot | 0.2-0.8 s | Steam ~54 MB `*.hg`, Xbox ~68 MB (R8 R7) |
| stage + swap / apply | 50-200 ms | 4 files or 2 blobs pairs, fsync each |
| validate + D1 observe | 60-150 ms | re-hash written members, rescan the folder |
| journal + oplog appends | 10-40 ms | 5-6 fsynced appends |
| **total** | **~1-1.5 s** (HDD 3-6 s) | progress shown at each step |
| `pending_recovery` at start | < 50 ms | folds `ops.jsonl` (3-6 KB per op) |

Disk: preserve adds at most one slot pair per op (0 when durable); the snapshot is the existing
per-op cost (~54-68 MB) that D2's retention (10 per source, rescue before delete) bounds.

## 3. Options (owner forks)

Each fork is one `Policies` field (2.2). Only the chosen option is implemented; the others are
specified here so the choice can be made or changed without another design round.

### 3.1 F4 - member policy for Place

| | (a) `pair` - V with its partner (recommended) | (b) `twice` - V in both positions |
|---|---|---|
| Writes | the captured pair in its captured positions; a copy of V in the other position when the source has one member or members of two pids | V in its position, a copy of V (meta time V - 1 s) in the other |
| Meta edits | none in the common case (V is the newer member); the partner's time is lowered to V - 1 s only when the user picked the older member (or Promote) | always one (the copy's time); the partner is never written live |
| In-game load menu | both entries are real: the auto-save and the restore point with their own dates and places | two entries with the same progress |
| Undo / rollback | restores exactly what was there | same |
| Lineage | the pair stays a pair in the history | the live slot holds one version twice |
| Cloud | overwrites only | overwrites only |
| Risk | none known; R7 2.4: the newer member has >= play time in 581/581 pairs, so the default `load` is the most progressed | the player loses the restore point as an in-game fallback (it stays in the vault) |

Not designed: (c) "V + delete the other member" (libNOM.io's Copy): under Steam Cloud the
deleted member comes back and the slot becomes mixed (R8 Q1c, A4).

**Recommendation: (a).** It is the most faithful (the game gets back exactly the pair it wrote),
needs the fewest meta edits, and still never creates a mixed slot.

### 3.2 F5 - meta timestamp on Place

| | (a) `keep` - keep V's original time (recommended) | (b) `now` - re-stamp to now |
|---|---|---|
| Meta time written | V's own; partner its own (or V - 1 s, 2.5) | V = now, other = now - 1 s |
| Disk time (Steam Cloud on, Xbox) | now / now - 1 s | now / now - 1 s |
| Disk time (Steam Cloud off) | V's meta time (the save keeps its age) | now / now - 1 s |
| Load screen | shows the save's real date: tells the owner which version is live | shows "just now"; the real date survives only in the vault |
| History | placed copy = the vault version exactly (same meta) | the live copy has a new meta: a meta-only variant of the version |
| Hello Games cross-save on (owner: off) | conflict prompt possible (size/time mismatch) | same |
| Unknowns | T6 (meta time vs mtime disagreeing) does not matter: both orders agree inside the slot | none |

**Recommendation: (a).** Honest dates, no meta change at all for the common Place, and the
Steam Cloud rule (stamp the write time on disk) is met by the file times, not the meta.

### 3.3 F7 - placing another account's save (same platform)

Definitions:

- **Owner identities** = the platform ids of every configured live account (Steam: the
  steamid64 in `st_<steamid64>`; Xbox: the decimal of the account folder's 16-hex prefix, as
  libNOM.io derives it, R8 D2) plus ids the owner adds in Accounts ("this is also me").
- **Save identities** = `CommonStateData.UsedDiscoveryOwnersV2` UIDs (R8 D5): every platform
  identity that has played the save. Needs the D4 fact `save.owners` (request, section 8);
  stored in the per-machine index only, never displayed.
- **Cross-account** = the target account differs from the account the source pair was captured
  from (the capture's live account, or the vault entry's `source` folder name).

| | (a) `same_person` (recommended) | (b) `warn` | (c) `block` |
|---|---|---|---|
| Rule | allowed when every save identity is an owner identity; when D4 facts are not ready, allowed only if the capture account is one of the owner's configured accounts | always allowed | never across accounts |
| Plan | `OTHER_ACCOUNT` (confirm): "Bases may stay owned by the other account until you play it here." | `OTHER_ACCOUNT` or `OTHER_PERSON` (confirm) | `OTHER_ACCOUNT` (block) |
| Someone else's save | `OTHER_PERSON` (block) | allowed: "Bases and discoveries stay owned by the original player; editing them may not work." | blocked |
| What happens in game | the game appends the new identity and re-owns bases/discoveries on first play (VERIFIED for cross-save deliveries, R8 D5); not always complete (`..A74F`) | same; for another person UNTESTED (R8 D5 item 2) | - |
| Files | only the slot's files/records; never `steam_autocloud.vdf`, never another account's `containers.index` (I7) | same | - |

The owner's PC has a second, empty `st_` folder (R8 A1). If it is a configured account, (a)
blocks placing a save there unless that steamid is among the save's identities - which protects
a family member's account from receiving the owner's saves by mistake.

**Recommendation: (a)**, with the Accounts "this is also me" escape for a second account the
game never saw.

### 3.4 F8 - deletions under Steam Cloud

Deletions arise from: **Evict**, the **undo of a PlaceIntoEmpty**, and the vacated side of a
**move** (Swap with one empty slot). On Xbox a deletion is a `Deleted` record that propagates
(R8 E2) and is allowed under every option; on Steam with Cloud off it is allowed under every
option. The fork is only about Steam folders that hold `steam_autocloud.vdf`, where a file
deleted while the game is closed comes back at the next launch (VERIFIED twice by the owner's
own oplog, R8 E1; R7 2.6 row 7).

| | (a) `overwrite_only` | (b) `refuse_guided` (recommended) | (c) `delete_detect` |
|---|---|---|---|
| Move to live (owner's main flow) | always overwrites: the picker evicts into the vault; no deletion needed | same | same |
| Evict on Steam Cloud | not offered (`STEAM_CLOUD_DELETE` block): "Free this slot by moving another save into it." | blocked by default with the steps; second button "Steam Cloud is off for No Man's Sky - clear now" (confirmation `STEAM_CLOUD_OFF_CONFIRMED`) runs the Evict | runs; the steps are shown as information |
| Undo of a fill into an empty slot | offers "Replace with another save" or "Leave it" | as Evict | as Evict |
| Move (one side empty) | becomes a copy (`TWIN_LIVE` confirm) or is blocked | as Evict for the vacated side | deletes |
| After a delete | - | verification watches for `resurrected` (2.12) | same, plus "Clear again" |
| Honesty | never pretends | the owner does the Steam side by hand, the app does the rest and checks | the app pretends a delete worked until the next launch proves otherwise |

The steps (today's text, `operations.py:629-634`): in Steam, No Man's Sky > Properties >
General, turn off "Keep game saves in the Steam Cloud"; clear the slot; launch and quit the game
once; turn Cloud back on and keep the local copy if Steam asks. The app cannot check whether
Cloud is off (`steam_autocloud.vdf` stays), hence the explicit confirmation in (b).

**Recommendation: (b)** (= the parent's lean a + b): the main flow never deletes, and the rare
explicit "free a slot" is honest about what Steam will do.

### 3.5 F6 - Steam <-> Xbox transfer (phase 5)

| | (a) verbatim data + meta conversion, gated by owner runs (recommended, later phase) | (b) libNOM.io-style ownership rewrite | (c) never |
|---|---|---|---|
| Data | verbatim (same `0xFEEDA1E5` stream; PLATFORM token "Win\|Final" on both PC builds, R8 D5) | re-serialised JSON, owners rewritten | - |
| Meta | converted field by field (3.5.2) | rebuilt | - |
| Ownership | the game re-owns on first play (VERIFIED for cross-save deliveries; UNTESTED for a non-server copy) | rewritten by the app | Hello Games cross-save does it |
| Project rule "never recompress" | kept | broken | kept |
| Upkeep | none per game version beyond meta formats | per game version ("Update if new objects with an ownership have been added", R8 Q4b) | none |
| Status in D5 | specified below | not designed | `CROSS_PLATFORM_GATED` stays; the picker says "Use Hello Games cross-save in the game (up to 5 saves)." |

**Recommendation: (a), as phase 5, after T1 and T2 pass.** Until then the gate stays closed.

#### 3.5.1 Gates (every one must pass; each failure is a `block` warning)

| Gate | Rule | Code |
|---|---|---|
| G1 validated | `policies.cross_platform == "validated"` and `organizer/transfer-validation.json` holds a passing run for this direction (3.5.4). For the owner's own runs only: env `NMSVAULT_ALLOW_UNVALIDATED_TRANSFER=1`, target slot empty, and a full backup of the target folder younger than 30 minutes | `CROSS_PLATFORM_GATED` |
| G2 stream data | first 4 bytes `E5 A1 ED FE` and a clean chunk walk whose total equals the meta size; refuses single-LZ4 (Xbox before 4.52), HGSAVEV2 (4.52-5.00) and plain JSON (Steam before 3.60) | `SOURCE_OLD_FORMAT`: "This save uses an older format. Load and save it once in the game first." |
| G3 meta format | Steam 432 B / format 2004 <-> Xbox 360 B / 2004. Worlds I (Steam 384 / Xbox 296, format 2003) refused until a run T2b covers it: no Steam 384-B meta exists in the owner's data to test against (VERIFIED 9.1) | `UNSUPPORTED_META` |
| G4 identity | meta id != 0 and equals the id in the data's first chunk | `SOURCE_NO_ID` |
| G5 ownership | F7 policy across platforms (owner identities include both platforms' ids) | `OTHER_PERSON` |
| G6 Xbox quota | Steam -> Xbox: wgs total after the op <= 256 MB (warn above 200 MB; 67.7 MB used today, R8 D8) | `XBOX_QUOTA` |
| G7 cross-save | Hello Games cross-save on -> warn | `CROSS_SAVE_ON` |

Everything else is an ordinary Place (preserve, both positions, journal, validation, undo,
verification); undo of a transfer is a transfer back (same gates).

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
   `confirmed: played`); (f) once D4 is indexed, `UsedDiscoveryOwnersV2` lists the Xbox identity
   and the base owner is Xbox.
6. Wait for the Xbox app to show the save as synced; start NMS again: (g) no conflict dialog,
   the save is intact.

Pass = (a)-(e) and (g); (f) recorded when available.

**T2 Xbox -> Steam** - the mirror image, with Steam Cloud **on**, plus: (h) after quitting and
relaunching through Steam, the placed save is still there (Steam Cloud accepted it; verification
`confirmed: uploaded` when the manifest reader is enabled).

**T2b Worlds I** - T2 with a 296-B Xbox source, only if such a save is still wanted (8 live
Xbox metas are 296 B today, R8 A5); opens G3 for 296 <-> 384.

Recording: `nmsvault transfer-validated --direction steam-to-xbox --run T1 --result pass
--notes "..."` writes `{direction, run, result, date, app version, game build (from the save's
BaseVersion), notes}` into `organizer/transfer-validation.json`. A failed run is recorded too
and keeps the gate shut. The gate re-closes by itself if the game introduces a new meta length
or format (G3 refuses unknown values), which asks for a new run.

## 4. Migration and compatibility

### 4.1 Today's operations

| Today (`operations.py`) | Decision | New behaviour |
|---|---|---|
| `repopulate_slot` (306-364; Xbox 492-522) | **re-route** | Adapter: `live_dir` must resolve to a configured live account (C29, 2.3.6) or the call is refused; source = the catalogued entry's pair (locator) or, for an uncatalogued folder (CLI `--from <folder>`), `PairSource(folder_slot=(dir, slot))` whose bytes are read and verified directly; then `plan_place` + `execute`. Always writes both positions (P6), journals, preserves. Returns today's `OpResult` shape for existing callers. |
| `restore_entry` for an extract (191-221) | **re-route** | Place of the extract's pair into the same slot number of the given account |
| `promote_member` (367-400; Xbox 440-463) | **re-route** | `plan_promote(target, position)`; fixes the Xbox no-op (needs P1) and the old Steam mtime under Cloud (R8 G5) |
| `plan_clear_slot` + `clear_slot` (671-760) | **re-route** | `plan_evict` + `execute`; "is it in the vault" by vid (`is_durable`), not slot number (P5); Steam Cloud per F8 (3.4); the CLI/GUI texts come from the plan |
| `extract_slot` (280-303) | keep | vault-only; gains D2's verification and manifest (D2 2.8) |
| `restore_full` (224-274; Xbox 525-560) | keep as the **panic restore** (4.3) | whole folder, with the P4/P7 fixes |
| `undo_last` (881-919) | **retire** as "Undo" | the new Undo is slot-scoped (2.11); `undo_last` becomes `panic_restore` of the newest snapshot **of that folder** (4.3) |
| `snapshot_live` (134-145) | keep | called by `execute`; pruning moves to D2's post-op hook (D2 2.9.5) |
| `create_full_backup`, `import_backup`, `import_vault`, `preview_vault_import` | keep | vault-only, unchanged |
| `msstore.write_save` / `delete_save` / `mark_modified` | keep names | re-implemented on record-bytes editing (P-X1, 2.6.2) |

### 4.2 Older app versions reading what D5 writes

Realistic old versions are v0.0.9 to v0.2.8 (D2: v0.0.1-v0.0.8 already crash on the owner's
catalog). VERIFIED with their sources in a synthetic sandbox (9.2):

| What D5 writes | What an old version does | Rule D5 follows |
|---|---|---|
| `organizer/ops.jsonl`, markers, `transfer-validation.json` | never reads them; `import_vault` does not carry `organizer/` (D2 E3) | - |
| oplog v2 line with extra nested keys | parses it (A, D) | additive keys only; the six old keys keep their meaning |
| ... whose `snapshot_id` is a whole-folder snapshot | Undo restores that snapshot = the folder's state before the op (A) - today's semantics, with today's hazards (accountdata and vdf rolled back, P7) | `snapshot_id` is always a whole-folder snapshot (I12) |
| ... if `snapshot_id` named an extract | v0.0.9 wipes every other slot and accountdata; v0.1.0+ refuse (B) | never (I12) |
| a torn last line | `read_oplog` raises `JSONDecodeError`; Undo is dead until the line is repaired (C) | one write + flush + fsync per line; the new reader skips a torn last line |
| ... if the newest op's snapshot were pruned | Undo silently falls through to an older snapshot (E) | D2 never prunes the snapshot named by the newest snapshot-bearing oplog line (D2 2.9.4, adopted) |
| preserved `keep-...` entries | ordinary extracts: Restore puts the slot back in place (v0.1.0+); v0.0.9 full-restores any extract (pre-existing) | D2 decision 1 |
| catalog | no new keys from D5 | - |

Other cases: an old version used while an op is open may rewrite the slot - recovery then
classifies it `superseded` and does nothing automatic (I13). An Xbox op followed by an old
version's Undo with Steam active is refused by the old code ("coming soon"), no write. README
gets a short "downgrading" note: after organizer moves, an older version's Undo restores the
whole folder, including account data.

### 4.3 The panic restore (today's whole-folder undo and restore, kept and fixed)

```python
def plan_panic_restore(ctx, account: LiveAccount, snapshot_id: str | None = None, *,
                       restore_accountdata: bool = False) -> PlacePlan: ...   # kind "panic_restore"
```

- Target is a resolved live account (C29), never the GUI's "active live" (P3/P4 class).
- The snapshot must come from the same folder: catalog `entry.source == account_key`, same
  platform, and on Xbox the snapshot's `containers.index` account id equals the live one (P7).
  Default: the newest such snapshot (P4: today it takes the newest of any folder,
  `operations.py:893-905`).
- First every live member that is not durable is preserved (up to 15 slots, one `preserve` call
  per slot) and a fresh whole-folder snapshot is taken, so a panic restore is itself undoable
  (its undo = panic restore of that fresh snapshot).
- Writes slot files / slot records only; `steam_autocloud.vdf` is never copied (P7);
  accountdata is kept unless `restore_accountdata=True` (warning: "rolls back unlocks and
  settings", R8 B4); on Xbox the live index header identity is kept and only slot records are
  taken from the snapshot (raw), with the xgs mirror updated per identifier.
- Slots absent from the snapshot would need deletions: allowed on Xbox and Steam-local; under
  Steam Cloud handled by the F8 policy (by default those slots are left alone and listed).
- Journaled like any op (pre: the fresh snapshot; post: the source snapshot's slot files), so a
  crash mid-restore is recovered forward or back.

### 4.4 Prerequisites: what is fixed first and how it is tested

| # | Change | Test (prove the bug, then the fix) |
|---|---|---|
| **P1 parse** | `msstore` constants `MS_META_LEN_WAYPOINT = 280`, `MS_META_LEN_WORLDS_I = 296`, `MS_META_LEN_WORLDS_II = 360` (libNOM.io `PlatformMicrosoft.cs:22-25`); `parse_ms_meta`: names when `len >= 280`; difficulty u8 at 0x114 for 280, u32 for 296/360; id 0x118, timestamp 0x120, format 0x124 for 296/360; difficulty tag 0x128 for 360. `_build_member` fills `MetaInfo.timestamp` / `slot_identifier` from them. `playtime` takes the id from the meta when non-zero (no chunk decode; R7 2.3) | synthetic 280/296/360 metas parse field by field (fails today for all three); `make_wgs_account` builds 360-B (and a 296-B variant) metas with non-zero id/time instead of 432-B Steam-length ones (`conftest.py:70-116`); read-only test on the live wgs (skip when absent): every Worlds-era meta has time != 0 and id == the data's id (VERIFIED over the 44 distinct Xbox metas in vault + live: time != 0 in 44/44, id == data id in 42/44, the 2 others are pre-Worlds saves with no stream id, 9.1); the newest member by meta time has >= play time |
| **P1 setter** | `set_ms_meta_timestamp(meta, ts)` writes u32 0x120 for 296/360-B metas of format 2003/2004 and **raises** otherwise (today it silently returns the input, `msstore.py:346`) | exactly 4 bytes change; the Xbox promote test asserts the meta order flips (today it does not, R8 A5) |
| **P-X1** (new) | index parse keeps each record's raw bytes and integer FILETIME; `build_containers_index` writes untouched records raw; `write_save` / `delete_save` / `mark_modified` use record-bytes editing | parse + rebuild is byte-identical for synthetic indexes with sub-second FILETIMEs (fails today); read-only on the real index (0/7 identical today, VERIFIED 9.3) |
| P3 | `gui.on_promote` uses the clicked row's folder (`gui.py:1539`); finally replaced by `plan_promote(target)` | R1 E9 script as a GUI test |
| P4 | `undo_last` only considers snapshots whose `source` is the target folder's name and platform; finally the panic restore (4.3) | R1 E3 (two same-platform folders): no cross-folder restore |
| P5 | interim: `_latest_vault_copy` matches by playthrough id + play time, not slot number; final: `plan_evict` uses `is_durable(vid)` | R1 E10 scenario: no false "safe" |
| P6 | interim: `repopulate_slot` refuses a one-member source when the destination holds the other member ("would leave a mixed slot; use Move to live"); final: Place writes both positions | R1 E1 scenario refuses (Steam and Xbox) |
| P7 | `restore_full` never copies `steam_autocloud.vdf`; Xbox refuses a backup whose index account id differs; accountdata kept unless asked | two `st_` sandbox folders: vdf and accountdata unchanged; Xbox foreign index refused |
| oplog | `append_oplog`: one write + flush + fsync; `read_oplog`: skip an unparsable last line (log it) | torn-line fixture: new reader OK, every old reader fails (9.2 C) |

P1 and P-X1 block every Xbox organizer operation (member order, identity, exact rollback);
P3-P7 make today's flows safe until they are re-routed and become regression tests afterwards.

### 4.5 Entry points

**GUI** (today's labels -> organizer route; D6/D7 decide the look):

| Today | Becomes |
|---|---|
| backup slot menu "Copy slot N into live slot N" / "...into a live slot..." (`CopySlotDialog`, `gui.py:1478-1526`) | "Move to live..." = the eviction picker (C30) |
| live slot menu "Replace ... from anywhere..." | "Replace with..." = the picker entered from the slot (version chooser, then `plan_place`) |
| "Make ... the one the game loads (promote)" (`gui.py:1528-1542`) | `plan_promote` on the clicked slot (P3 gone by construction); hidden on the member already loading (R2 D7) |
| "Clear slot..." (`gui.py:1455-1476`) | "Free this slot..." = `plan_evict` (F8) |
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
| `clear N` | `evict` (F8) |
| `restore <extract-id>` | `place` |
| `restore <full-id>` (`--no-mirror`) | panic restore of that snapshot/backup (4.3); `--no-mirror` = leave slots absent from it alone |
| `undo` | slot-scoped undo of the newest op on `--to` (or the only target); `undo --op <op_id>`; `undo --whole-folder` = panic restore |
| new `place`, `swap`, `evict`, `ops`, `recover`, `panic-restore`, (phase 5) `transfer-validated` | 2.2 API |

Targets are written `steam:7`, `xbox:4`, or `steam:<account>:7` when a platform has more than
one live account; sources `--from <entry-id> --src-slot N [--member A|B]`, `--from live:steam:4`, or `--from vid:<prefix>` (`--from` as in today's `extract` and `repopulate`).
Every command prints the plan's warnings and the outcome text from core; non-interactive runs
need `--yes` plus `--confirm CODE` for each confirmation code.

### 4.6 Documentation to update when implemented

`DESIGN.md`: the operation semantics (both positions, newest by every signal, journal,
verification), the Xbox meta facts (296/360 B, id 0x118, time 0x120) and the FILETIME rule;
remove the stale "SizeDisk == file size" line (`DESIGN.md:91`, R1 pre-work list). `README.md`:
features, the real `uv run pytest` count, the downgrade note (4.2).

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
| `vault_with(tmp, pairs)` | a `Vault` with full backups / extracts holding known pairs, plus a locator: D2's `FolderLocator` when present, else `FakeLocator` (implements D1's ABC: `open` verifies sha256, `preserve` writes an extract-shaped entry and re-reads it) | the locator conformance suite (D1 test 10) also runs on `FakeLocator` |
| `op_ctx(...)` | `OpContext` with a scripted `game_probe` (a list of answers), fixed `clock`, `FaultInjector`, fake `LineageRecorder` (records calls), the real `ops.jsonl` writer | a second `op_ctx` on the same vault = "after restart" |
| `tree_hash(path)` | sha256 + `mtime_ns` of every file under a folder | the "nothing changed" assertion |

`FaultInjector.at(point)` raises `SimulatedCrash(BaseException)` - not an `Exception`, so no
handler in the code under test can catch it, exactly like a kill. Named points live in the
executor and writers (`after_preparing`, `in_preserve`, `after_snapshot`, `after_writing`,
`staged:<n>`, `swapped:<n>`, `before_index`, `after_index`, `xgs:<id>`, `after_validated`,
`after_oplog`, `after_record`).

### 5.2 Tests by area

| ID | Area | Scenario | Assert |
|---|---|---|---|
| T-M1 | pairmeta | common Place of a full pair, Steam cloud / local / Xbox | positions kept; no meta time edit; Steam metas re-keyed for `file_no + 1` of each position and `is_valid_for`; disk times now / now - 1 (cloud, Xbox) or meta times (local) |
| T-M2 | pairmeta | V is the older member (user choice, Promote) | F4a: partner time = V - 1 s; order holds on meta time, mtime and FILETIME |
| T-M3 | pairmeta | one-member source; mixed-pid source | the other position gets a copy of V with V - 1 s; `copy_of` set; never two pids |
| T-M4 | pairmeta | F5b | V = now, other = now - 1 on every signal |
| T-M5 | pairmeta | Steam Cloud off | mtimes = meta times, strictly ordered |
| T-M6 | pairmeta | meta time 0 | stamped now |
| T-M7 | pairmeta | Xbox 280-B meta (no time field) | order by FILETIME only; the setter is not called |
| T-M8 | P1 | setter on 296/360-B metas | exactly 4 bytes at 0x120 differ; 280 raises |
| T-P0 | planner | every `plan_*` and `eviction_picker` call | `tree_hash` of live folders and vault unchanged; no `ops.jsonl` created |
| T-P1 | preserve | Place over a live-only pair | a `keep-` extract exists before the first live write (fault at `after_writing` and inspect), re-hashes to the vids |
| T-P2 | preserve | vault root missing | `refused`, code `PRESERVE_FAILED:vault-unavailable`; live `tree_hash` unchanged |
| T-P3 | preserve | the slot changes between plan and execute | `stale` with a fresh plan; nothing written |
| T-P4 | preserve | vault full (patched `shutil.disk_usage`) | `refused`, live unchanged |
| T-B1 | planner | candidate states empty / occupied / mixed / partial / unreadable | `state`, `kind`, `durable`, warnings per 2.3.3 |
| T-B2 | planner | suggestion order | same line > lowest empty > durable least recent > least recent > none (all pinned) |
| T-B3 | planner | blocks | xgs-only account, cross-platform, F7 other person, game running, open op |
| T-F1 | flows | Place, PlaceIntoEmpty, restore-older, Promote on each fixture variant | post-image hashes; `SlotView.newest == L`; oplog v2 line; ops.jsonl `preparing, writing, validated, done`; `record_op` once per target |
| T-F2 | flows | Place from a one-member / mixed source into a full slot | both positions written, one pid (P6 regression, R1 E1) |
| T-F3 | flows | Swap both full; Swap one empty (per F8 policy) | both slots exchanged with metas re-keyed; one `op_id`, two `record_op` calls |
| T-F4 | flows | Evict on Xbox and Steam-local | slot empty; Xbox records `Deleted`, blob dirs and xgs folders removed only after validation |
| T-F8a/b/c | F8 | Evict / undo-of-fill / move on Steam Cloud under each policy | a: blocked; b: blocked unless `STEAM_CLOUD_OFF_CONFIRMED`, then deletes and arms `resurrected`; c: deletes |
| T-G1 | game | probe True at execute start | `refused`; nothing written; no `writing` event |
| T-G2 | game | probe flips to True after `writing`, before the first live write | `abandoned(game_started)`; live unchanged; the preserved entry stays (harmless) |
| T-G3 | game | probe None | without the code: `ConfirmationRequired`; with it: proceeds |
| T-W1 | Steam writer | stage + swap | temp files fsynced and verified; final names only after all four staged; no temp left |
| T-W2 | Steam writer | `os.replace` fails (a handle held open; Windows only, skip elsewhere) | retried; persistent failure -> files already swapped rolled back exactly |
| T-X1 | Xbox writer | `split_index` / `join_index` on synthetic indexes with sub-second FILETIMEs | byte-identical (today's parse + build is not, 9.3) |
| T-X2 | Xbox writer | `apply_pair` | one index write (spy on `os.replace`); both records flip; states Modified / Created |
| T-X3 | Xbox writer | other records, header identity, `AccountData` | raw-identical after the op |
| T-X4 | Xbox writer | `rollback_pair` for an existing and a new identifier | index byte-identical; wgs and xgs trees identical (the 9.4 prototype as a test) |
| T-X5 | Xbox writer | index changes between `prepare_pair` and the index write | aborted; new blobs deleted; nothing referenced |
| T-V1 | validation | post-write checks | each row of 2.9.1 fails on a corrupted file (inject `corrupt_after_swap`) |
| T-V2 | rollback | validation failure on each fixture variant | exact pre-image (sha256, size, `mtime_ns`; Xbox index bytes); `rolled_back`; oplog line `status: rolled_back` with a whole-folder `snapshot_id` |
| T-V3 | containment | any op | other slots, accountdata, vdf, `cache/` unchanged (`tree_hash` minus the slot's files) |
| T-C1..C15 | crash matrix | a `SimulatedCrash` at each point: `after_preparing` (C1), `in_preserve` (C2), `after_snapshot` (C3), `after_writing` (C4), Steam `staged:2` (C5), `swapped:1..4` (C6-C9), Xbox `before_index` (C10), `after_index` (C11), `xgs:<first>` (C12), `after_validated` (C13), `after_oplog` (C14), torn `writing` line in `ops.jsonl` (C15) | after restart `pending_recovery` classifies as 2.10 says; both **forward** and **back** (where offered) end exactly in the post- or pre-image; no temp files or unreferenced blobs; exactly one oplog line per finished op; `record_op` idempotent |
| T-R1..R6 | recovery | all post; torn; `other` (an "old app" rewrites the slot); other machine id; open op blocks the same account only; marker without vault | recommended choice per table; `other` -> nothing written (I13); `RECOVERY_PENDING` only on that account; `vault_missing` item |
| T-UN1..UN8 | undo | same / progressed / already / empty / stale; undo of PlaceIntoEmpty per F8; undo of a Swap; undo of an undo (redo) | per 2.11; progressed versions preserved before the overwrite; undo never touches another slot |
| T-VR1 | verification | Steam: write the evicted bytes back with their old mtimes (what a "cloud" pick does) | `reverted` alert; "Put it back" plans a Place of the placed pair |
| T-VR2 | verification | a child of the placed pair appears (same pid, more play) | `confirmed: played` |
| T-VR3 | verification | Xbox record state Modified -> Synced (raw edit) | `confirmed: uploaded` |
| T-VR4 | verification | after a Steam-cloud Evict (F8b confirmed) the files reappear | `resurrected` with the steps |
| T-VR5 | verification | another pid appears; the slot empties | `replaced`; `deleted` |
| T-VR6 | verification | game seen running after the op, slot unchanged | `confirmed: unchanged`; without a session it stays `pending` |
| T-VR7 | verification | 31 days without evidence | `expired`, no alert |
| T-O1 | oplog | v2 line | the six old keys present with old meaning; `snapshot_id` names a whole-folder snapshot entry; `detail` has no folder name |
| T-O2 | old apps | v0.0.9 / v0.1.0 / v0.2.8 `undo_last` after a D5 op (sources from git tags, dev-only, skipped without git) | restores the pre-op folder state; never an extract (9.2 A, B) |
| T-O3 | oplog | torn last line | new `read_oplog` skips it with a log line; the D5 writer never produces one (single write) |
| T-L1 | locks | two contexts run `execute` at once (threads and processes) | the second gets `BUSY`; no deadlock with `preserve` / `record_op` taking the store lock |
| T-T1 | targets | unknown account, duplicate folder name without `instance`, vault-inside path, xgs-only | `TargetError` / `XGS_READ_ONLY` |
| T-U1 | UI text | every `Warning.text`, outcome and recovery/verify text | ASCII (extend `tests/test_ui_text.py` to the new package); no account id or path |
| T-RD1 | real data (read-only, skip when absent) | P1 parser on the live wgs; P-X1 rebuild of the live index; F6 convert round trips (phase 5) | expectations of 4.4 / 3.5.2; `tree_hash` before == after |
| T-6x | F6 (phase 5) | each gate G1-G7 blocks; convert unit + property tests | 3.5 |

### 5.3 What the tests deliberately do not cover

The game's and the clouds' real behaviour (which member loads when signals disagree, whether
Steam reverts an old mtime, Xbox conflict dialogs, ownership migration of a non-server copy):
those are the owner runs T1-T6 of R8 6, of which T1/T2 gate F6 (3.5.3). Verification (2.12)
exists precisely to catch what those runs might prove wrong.

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

(P2 is D2's step 0.2; D5 only relies on its exemptions.)

### Phase 2 - live operations (needs D1 phase 1: ids, store + lock, read model, recorder; D2 2.1 `preserve`)

| Step | Content | Size | Tests | Needs |
|---|---|---|---|---|
| 2.1 | `core/liveops/` skeleton: `model.py` (2.2 dataclasses, `to_dict`), `errors.py`, `OpContext`, `Policies` | S | dataclass round trips | D1 ids |
| 2.2 | `targets.py`: `resolve_target`, `resolve_account` (C29, `instance`) | S | T-T1 | D1 `LiveTarget` |
| 2.3 | `pairmeta.py` (F4a / F5a; the other options only if chosen) | M | T-M1..M7 | 0.2 |
| 2.4 | `steamwrite.py`: prepare / stage / swap (retry) / rollback / sweep | M | T-W1, T-W2 | - |
| 2.5 | `msstore`: `prepare_pair` / `apply_pair` / `finalize_pair` / `rollback_pair` | M | T-X2, T-X4, T-X5 | 0.3 |
| 2.6 | `opjournal.py`: events, fold, tolerant reader, markers, machine id, `open_snapshot_ids`, `busy_targets`, live-op lock | M | journal unit tests, T-C15, T-L1 | D1 store + `safety.exclusive_lock` |
| 2.7 | planner: `eviction_picker`, `plan_place`, `plan_promote`, warnings, suggestion, fingerprint | L | T-P0, T-B1..B3 | D1 read model, D2 `is_durable` |
| 2.8 | `execute` for place / place_into_empty / restore_older / promote: preserve, snapshot, journal, writers, validation, rollback, oplog v2, `record_op`, D1 observe, D2 post-op hook | L | T-F1, T-F2, T-G1..G3, T-P1..P4, T-V1..V3, T-O1 | D2 `preserve`, D1 recorder |
| 2.9 | recovery: `pending_recovery`, `resolve_recovery`, startup hook (GUI and CLI `recover`) | L | T-C1..C14, T-R1..R6 | 2.8 |
| 2.10 | `plan_swap`, `plan_evict` with the F8 policy | M | T-F3, T-F4, T-F8a..c | 2.8 |
| 2.11 | undo: `plan_undo`, `op_history`, `OpSummary` | M | T-UN1..UN8 | 2.8 |
| 2.12 | verification: game-seen recorder, `update_verification`, `VerifyItem` | M | T-VR1..VR7 | D1 `op_outcome`, events |
| 2.13 | re-route classic ops (repopulate, restore extract, promote, clear) through `liveops`; CLI `place` / `swap` / `evict` / `undo` / `ops` / `recover`; update tests that assert old internals; retire the P5/P6 interims | M | existing suites + CLI tests | 2.8-2.11 |
| 2.14 | panic restore (4.3) replacing `undo_last` / full `restore` entry points | M | panic-restore tests incl. a crash point | 2.8 |
| 2.15 | GUI wiring until D6/D7 land: picker dialog on the old `CopySlotDialog` slot, non-modal Done-Undo, recovery prompt, verification alerts, close blocked during execute | M | GUI tests via the `gui_app` root | UI design choice |
| 2.16 | old-app compatibility harness (dev-only, skips without git tags) | S | T-O2 | 2.8 |
| 2.17 | DESIGN.md and README (4.6) | S | real test count | all |

Rough total: phase 0 about 0.9 kLOC + tests; phase 2 about 4-5 kLOC + tests.

### Phase 2b (optional, small)

| Step | Content | Size | Tests |
|---|---|---|---|
| 2b.1 | Steam `remotecache.vdf` reader (2.12.2): `uploaded` evidence, Steam `PENDING_UPLOAD` | S/M | parser fixtures; T-VR3 Steam variant |
| 2b.2 | `CROSS_SAVE_ON` from accountdata flags (needs a D4 helper) | S | flag fixtures |

### Phase 3 (with D4)

| Step | Content | Size | Tests |
|---|---|---|---|
| 3.1 | F7a: `save.owners` fact consumed; `OTHER_PERSON`; Accounts "this is also me" list | S | F7 matrix |

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
| K5 | Downgrade to an old app version after organizer moves | low / whole-folder undo incl. accountdata | `snapshot_id` rule (I12); D2 keeps the newest-oplog snapshot; README note | old behaviour stays old |
| K6 | Snapshot disk use (54-68 MB per op) | certain / disk | D2 retention (10 per source, rescue first); D3 would dedupe | none beyond D2/D3's |
| K7 | Two branches of one save live at once (twins) - cross-save and server behaviour unknown (R8 R3) | medium once "use an older version" is easy | `TWIN_LIVE` confirmation; D1 lineage keeps branches apart | unknown game-side effect |
| K8 | Expedition saves converting when placed after their season (R8 B5) | low / changed save | warnings `EXPEDITION_SOURCE`, `EXPEDITION_ACTIVE` | by design of the game |
| K9 | Meta time far older than the file time (F5a) treated specially by the game or Steam | low | orders agree inside the slot on every signal; T6 would settle it | none known |
| K10 | D1's `op_outcome` confirming too early (2.12.1) | certain if unchanged | D5 requires a child, a game session or an upload | none |
| K11 | App killed mid-write (Task Manager, power) | low / torn slot | write-ahead journal, exact rollback, recovery at next start; marker for a missing vault drive | the owner must not launch the game before reopening the app - the recovery prompt says so |
| K12 | F6 ownership of a copy that did not pass Hello Games' servers | unknown | gated behind T1/T2 | - |

### 7.2 Open questions

For the owner (designed as options, nothing waits): F4 (3.1), F5 (3.2), F7 (3.3), F8 (3.4),
F6 (3.5). Two small ones:

- Q1 "Put it back" after a cloud revert: one click with the original plan, or show the plan
  again? Recommended: show it again (the slot may have changed).
- Q2 Keep `ops.jsonl` forever (~3-6 KB per op) or trim closed ops after a year? Recommended:
  keep (it is the undo and audit source; size is negligible).

For sibling designs:

- D1: `observe` skips `liveops.busy_targets()`; `op_outcome("confirmed")` semantics (2.12.1);
  where the Accounts "this is also me" identities live (D1 notes/settings or state.json).
- D2: confirm `open_snapshot_ids()` as the source of open-op pins and the name of the post-op
  retention hook D5 calls (`storage_after_operation` here).
- D4: the `save.owners` fact (UIDs and platform tags from `UsedDiscoveryOwnersV2`, never
  displayed) for F7a; optionally accountdata flags (CrossSaves) for `CROSS_SAVE_ON`.
- D6 / D7: block app exit while `execute` runs; surfaces for recovery items and verification
  alerts; the confirmation flow (`confirmations` echoed back as `confirmed`); account switch in
  the picker.

## 8. Deviations from 01-consolidation section 6

1. **`LiveTarget`** carries D1's `instance` disambiguator (two Proton prefixes with the same
   folder name); UIs may pass D1's opaque `token`. D5 resolves it in core (2.3.6).
2. **Locator** as refined by D1/D2: `preserve(Sequence[LiveMember], *, op_id, reason) ->
   list[Location]` and `open(vid, prefer)`; D5 needs `prefer` to place the meta exactly as
   captured.
3. **Edge kinds**: D5 follows D1 - place, swap, restore-older, promote and undo are operation
   kinds linked by `op_id`, not edge kinds; reverts and resurrections are journal events.
4. **New files**: `<vault>/organizer/ops.jsonl` (reserved by D1), `<vault>/organizer/.liveop.lock`,
   `<vault>/organizer/transfer-validation.json` (phase 5); per machine
   `user_cache_dir/vaults/<vault_id>/open-ops/` and `game-seen.json`, and
   `user_data_dir/machine-id`.
5. **Requests** (interfaces D5 depends on): D1 `observe` skips busy targets; D1 `op_outcome`
   "confirmed" needs evidence after a game session (2.12.1); D4 `save.owners`; D2 pins open-op
   snapshots and the newest-oplog snapshot (already adopted by D2).
6. **`msstore` write primitives** change to record-bytes editing (P-X1), a new prerequisite not
   listed in section 3 of 01-consolidation (found here, 9.3).

## 9. Evidence index

All scripts are D5's, in `<scratchpad>\D5\scripts\`, outputs `<scratchpad>\D5\*.out.txt`,
sandboxes in `<scratchpad>\D5\sbx\` (`<scratchpad>` = the session scratch folder under
`%LOCALAPPDATA%\Temp\claude\...`). Real files were only opened for reading; every write went to
the sandbox. Run from the repo root with `PYTHONUTF8=1 uv run python -I <script> ...`.

| # | Script(s) | Shows |
|---|---|---|
| 9.1 | `meta_regions.py`, `steam_0x54.py`, `steam_0x54b.py`, `xplat_same_bytes.py` (input: R7's `inventory.json`) | distinct metas: Steam 432 B x156, Xbox 360 B x36, 296 B x8; the only unknown non-zero Steam field is u32 0x54 = SizeDecompressed (144/156; the 12 others are editor-touched, where 0x3C and 0x54 are stale and 0x38 still equals the chunk total 156/156); Steam 0x08-0x37, 0x40 and the tail are zero; Xbox metas have no unknown non-zero bytes; Xbox time 0x120 non-zero 44/44, id 0x118 == data id 42/44 (2 pre-Worlds); 0 of 195 versions exist byte-identical on both platforms |
| 9.2 | `oplog_compat_exp.py` with v0.0.9 / v0.1.0 / v0.2.8 sources (copied from D2's git-archive extracts, run with `python -S`) | A: v2 line + whole-folder snapshot -> every version's Undo restores the pre-op state; B: snapshot id = an extract -> v0.0.9 wipes the other slots and accountdata, newer versions refuse; C: torn last line -> `JSONDecodeError` in all; D: a trailing record without snapshot is skipped; E: a pruned newest snapshot -> fall-through to an older one |
| 9.3 | `index_roundtrip.py`, `index_roundtrip2.py` (read-only on the live and vault `containers.index` files) | parse + rebuild is byte-identical for 0/7 indexes: the header FILETIME drifts in 7/7 and record FILETIMEs in 28/217 (max 16 ticks) because of the float round trip |
| 9.4 | `xbox_pair_write_proto.py` (synthetic wgs + xgs) | both Xbox members written with one index replace; untouched records raw-identical; rollback leaves the index byte-identical and the wgs and xgs trees identical, for an existing and a new identifier |
| 9.5 | `replace_open_file.py` | `os.replace` onto a file another handle holds open fails on Windows (`PermissionError`, winerror 5) |

Read-only proof (VERIFIED after all experiments): the vault's `catalog.json` (sha256
`a3a58014...a045b9`) and `oplog.jsonl` (`29b3ece9...598aaa`) equal R1's baseline;
`playthrough-ids.json` is unchanged (41,480 B, 2026-10-05 17:41); no file under the vault, the
live `st_*` folders or the Xbox `SystemAppData` tree was modified today.
