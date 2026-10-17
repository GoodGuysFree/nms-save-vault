"""Live-slot operations: move to live (Place / Swap with preserve-before-replace), undo,
recovery and verification (docs/organizer/designs/D5-live-operations.md).

Front ends call only what this package exports; no UI reads save files or chooses a
write target itself.
"""
from __future__ import annotations

from .context import OpContext, Policies, VersionStore
from .errors import (
    ConfirmationRequired,
    LiveChanged,
    LiveOpError,
    RollbackError,
    TargetError,
    WriteError,
)
from .faults import FaultInjector, SimulatedCrash
from .model import (
    AccountChoice,
    CloudInfo,
    CloudMode,
    EmptySlotGuide,
    Estimate,
    EvictionPicker,
    Eviction,
    LiveAccount,
    MemberFacts,
    MemberWrite,
    OpOutcome,
    OpSummary,
    PairSource,
    PlacePlan,
    Position,
    Progress,
    RecoveryItem,
    ResolvedTarget,
    SlotCandidate,
    SourceSummary,
    TargetPlan,
    UndoOffer,
    VerifyItem,
    Warning,
)
from .opjournal import busy_targets, liveop_lock
from .targets import resolve_target

__all__ = [
    "AccountChoice", "CloudInfo", "CloudMode", "ConfirmationRequired", "EmptySlotGuide",
    "Estimate", "EvictionPicker", "Eviction", "FaultInjector", "LiveAccount", "LiveChanged",
    "LiveOpError",
    "MemberFacts", "MemberWrite", "OpContext", "OpOutcome", "OpSummary", "PairSource",
    "PlacePlan", "Policies", "Position", "Progress", "RecoveryItem", "ResolvedTarget",
    "RollbackError", "SimulatedCrash", "SlotCandidate", "SourceSummary", "TargetError",
    "TargetPlan", "UndoOffer", "VerifyItem", "VersionStore", "Warning", "WriteError",
    "busy_targets", "liveop_lock", "resolve_target",
]
