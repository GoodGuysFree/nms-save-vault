"""Value types of the live operations (D5 2.2): what the planner returns, what ``execute``
takes and reports, and the rows of recovery, verification and history.

Pure: no file access. Every public type is a frozen dataclass with ``to_dict()``, a
JSON-safe form for the CLI's ``--json``. Text fields are ASCII and already redacted when
they are built; a path never leaves core, so a ``Path`` renders as its last component only.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

from ..organizer.locator import Location
from ..organizer.model import LiveRef, LiveTarget, Platform, target_to_json

Position = Literal["A", "B"]          # A = auto-save (file 2k-1), B = restore point (file 2k)
CloudMode = Literal["steam-cloud", "steam-local", "gog", "xbox"]
Severity = Literal["info", "warn", "confirm", "block"]
SlotState = Literal["empty", "occupied", "mixed", "partial", "unreadable"]
PlanKind = Literal["place", "place_into_empty", "restore_older", "swap", "promote", "undo",
                   "panic_restore", "keep"]

#: A live account folder: D1's LiveRef, which also carries the ``instance`` disambiguator.
LiveAccount = LiveRef


def to_json_value(value: Any) -> Any:
    """``value`` as plain JSON data (dicts, lists, strings, numbers, booleans, None)."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, LiveTarget):
        return target_to_json(value)
    if isinstance(value, Location):
        return {"vid": value.vid, **value.to_json()}
    if isinstance(value, Path):
        return value.name
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_json_value(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(to_json_value(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [to_json_value(v) for v in value]
    raise TypeError(f"no JSON form for {type(value).__name__}")


class _JsonForm:
    def to_dict(self) -> dict[str, Any]:
        return to_json_value(self)


# --- inputs -------------------------------------------------------------------


@dataclass(frozen=True)
class PairSource(_JsonForm):
    """The version the user picked: a captured pair and the position that must load."""

    vid_a: str | None
    vid_b: str | None
    load: Position | None = None          # None = the newer member by (meta_ts, play_time)
    prefer: Location | None = None        # where the pair was captured (its metas as captured)
    folder_slot: tuple[Path, int] | None = None   # classic adapter only (D5 4.1): a folder the
                                                  # locator does not know; bytes read directly


@dataclass(frozen=True)
class ResolvedTarget:
    """A live slot resolved to its folder. Internal: never shown, so it has no ``to_dict``."""

    target: LiveTarget
    source_id: str                        # state.json Source.id
    folder: Path
    cloud_mode: CloudMode
    xgs_twin: Path | None
    label: str                            # "Steam slot 7" (+ the account's label when needed)


# --- the eviction picker (what "Move to live..." renders) -----------------------


@dataclass(frozen=True)
class Warning(_JsonForm):
    code: str                             # stable code (D5 2.3.3)
    severity: Severity
    text: str                             # one ASCII sentence, redacted
    data: Mapping[str, object]            # numbers for formatting: seconds, slot, bytes


@dataclass(frozen=True)
class MemberFacts(_JsonForm):
    vid: str | None
    pid: str | None
    name: str
    play_time: int                        # seconds, meta TotalPlayTime
    saved_at: int                         # unix, meta timestamp (Steam 0x164, Xbox 0x120)
    game_mode: int
    difficulty: int
    season: int
    size: int                             # data bytes
    durable: bool                         # locator.is_durable(vid)
    sync_state: str                       # Xbox 'Synced'|'Modified'|'Created'|'Deleted'; Steam ''


@dataclass(frozen=True)
class SlotCandidate(_JsonForm):
    target: LiveTarget
    label: str                            # "Steam slot 7"
    state: SlotState
    a: MemberFacts | None
    b: MemberFacts | None
    newer: Position | None                # the member the game loads now
    pid: str | None                       # occupant (newer member)
    line_name: str                        # D1 read-model name of pid (fallback: meta name)
    play_time: int                        # newer member
    last_played: int                      # newer member's meta timestamp
    durable: bool                         # every present member is durable
    vault_copy: Literal["durable", "partial", "outside-vault", "none"]
    pinned: bool                          # occupant's pid is pinned
    same_line: bool                       # occupant pid == source pid
    suggested: bool
    suggest_reason: Literal["same_line", "empty", "in_vault_least_recent", "least_recent", ""]
    kind: Literal["place_into_empty", "place", "restore_older", "noop"]
    outcome: str                          # button text, e.g. "Swap into Steam slot 7"
    warnings: tuple[Warning, ...]
    blocked: str                          # "" or a block code; the UI disables the choice


@dataclass(frozen=True)
class SourceSummary(_JsonForm):
    name: str
    pid: str | None
    vid_a: str | None
    vid_b: str | None
    load: Position
    play_time: int
    saved_at: int
    game_mode: int
    platform: Platform
    where_label: str                      # "vault: full backup 2026-10-05" / "live: Steam slot 4"


@dataclass(frozen=True)
class CloudInfo(_JsonForm):
    steam_cloud: bool | None              # None for Xbox
    cross_save_on: bool | None            # None = unknown (accountdata not read)
    xbox_sync_busy: bool                  # containers.index changed < 60 s ago


@dataclass(frozen=True)
class AccountChoice(_JsonForm):
    account: LiveAccount
    label: str
    blocked: str                          # e.g. CROSS_PLATFORM_GATED, XGS_READ_ONLY
    warnings: tuple[Warning, ...]         # e.g. OTHER_ACCOUNT


@dataclass(frozen=True)
class EvictionPicker(_JsonForm):
    source: SourceSummary
    account: LiveAccount                  # the account shown
    cloud: CloudInfo
    candidates: tuple[SlotCandidate, ...]     # 15, slot order
    suggested_slot: int | None
    other_accounts: tuple[AccountChoice, ...]
    warnings: tuple[Warning, ...]         # plan-level (game running, recovery pending, ...)
    fingerprint: str


# --- plans and outcomes --------------------------------------------------------


@dataclass(frozen=True)
class MemberWrite(_JsonForm):
    """One position of a slot as an operation writes it."""

    position: Position
    vid: str                              # data sha256, written verbatim
    pid: str | None
    play_time: int
    meta_ts: int                          # the time written into the meta (now, F5b); 0 = none
    source_meta_ts: int | None            # the captured meta time before the stamp
    meta_plain_sha256: str                # plaintext as written (after the stamp)
    meta_disk_sha256: str                 # bytes as written (Steam: encrypted for `ordinal`)
    disk_time: float                      # Steam mtime / Xbox FILETIME (unix seconds)
    ordinal: int | None                   # Steam storage ordinal; None on Xbox
    copy_of: Position | None              # this position holds a copy of the loading member
    origin: Mapping[str, object]          # where the bytes came from (Location JSON form)


@dataclass(frozen=True)
class Eviction(_JsonForm):
    position: Position
    vid: str | None                       # None: file present but unreadable (raw preserve)
    pid: str | None
    name: str
    play_time: int
    saved_at: int
    durable_before: bool


@dataclass(frozen=True)
class TargetPlan(_JsonForm):
    target: LiveTarget
    label: str
    cloud_mode: CloudMode
    before: SlotState
    writes: tuple[MemberWrite, ...]       # always 2; 0 only for keep (vault-only)
    evictions: tuple[Eviction, ...]       # for keep: the members copied to the vault
    source: PairSource | None = None      # what is written here (execute reads it again)


@dataclass(frozen=True)
class Estimate(_JsonForm):
    preserve_bytes: int                   # 0 when everything is already durable
    snapshot_bytes: int                   # only objects the store does not hold yet
    write_bytes: int
    seconds: float


@dataclass(frozen=True)
class PlacePlan(_JsonForm):
    plan_id: str                          # random; not an op_id (execute mints that)
    kind: PlanKind
    targets: tuple[TargetPlan, ...]       # one per slot: 1, 2 (swap), up to 15 (panic restore)
    source: SourceSummary | None          # None for swap / keep / panic_restore
    undo_of: str | None
    warnings: tuple[Warning, ...]
    confirmations: frozenset[str]         # codes the caller must echo back in `confirmed`
    blocked: tuple[Warning, ...]          # non-empty -> execute refuses
    outcome: str                          # "Swap into Steam slot 7"
    fingerprint: str
    estimate: Estimate
    created_at: float
    restores: str | None = None           # panic_restore: the snapshot or backup it puts back
    via_undo: bool = False                # panic_restore started by the whole-folder Undo


@dataclass(frozen=True)
class UndoOffer(_JsonForm):
    op_id: str
    label: str                            # "Undo: put 'Polestar' back into Steam slot 7"


@dataclass(frozen=True)
class OpOutcome(_JsonForm):
    op_id: str | None
    status: Literal["done", "rolled_back", "failed", "refused", "stale"]
    outcome: str
    warnings: tuple[Warning, ...]
    undo: UndoOffer | None
    verify: Literal["pending", "n/a"]
    error: str | None
    code: str | None = None               # 'refused' / 'failed': GAME_RUNNING, BUSY, ...
    replan: PlacePlan | None = None       # status 'stale': the fresh plan to show


@dataclass(frozen=True)
class Progress(_JsonForm):
    step: Literal["checking", "preserving", "snapshot", "writing", "verifying", "recording"]
    done: int
    total: int


# --- empty slots, recovery, verification, history -----------------------------------


@dataclass(frozen=True)
class EmptySlotGuide(_JsonForm):
    target: LiveTarget
    label: str
    cloud_mode: CloudMode
    occupied: bool
    durable: bool                         # every present member has a vault copy
    refill: Warning | None                # EMPTY_SLOT_MAY_REFILL; None on steam-local
    steps: tuple[str, ...]                # numbered manual steps for this cloud mode


@dataclass(frozen=True)
class RecoveryItem(_JsonForm):
    op_id: str
    kind: str
    labels: tuple[str, ...]
    state: Literal["not_started", "complete", "torn", "superseded", "failed", "other_machine",
                   "vault_missing"]
    recommended: Literal["forward", "back", "leave", "none"]
    choices: tuple[str, ...]
    text: str
    started: str                          # local ISO time


@dataclass(frozen=True)
class VerifyItem(_JsonForm):
    op_id: str
    target: LiveTarget
    label: str
    state: Literal["pending", "confirmed", "reverted", "deleted", "replaced", "superseded",
                   "expired"]
    detail: str                           # played | unchanged | uploaded | ...
    text: str
    actions: tuple[Literal["place_again", "show_steps", "dismiss"], ...]
    since: str                            # local ISO time of the op


@dataclass(frozen=True)
class OpSummary(_JsonForm):
    op_id: str
    kind: str
    when: str                             # local time
    labels: tuple[str, ...]
    text: str
    status: Literal["done", "rolled_back", "failed", "abandoned", "open"]
    verify: str                           # worst verify state over the op's targets
    undo_of: str | None
