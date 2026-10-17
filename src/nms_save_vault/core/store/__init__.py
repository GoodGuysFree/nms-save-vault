"""The content-addressed version store under ``<vault>/store/`` (design D3)."""
from .capture import CaptureScope
from .errors import (
    Cancelled,
    EntryCorrupt,
    InsufficientSpace,
    ObjectCorrupt,
    ObjectMissing,
    PreserveMismatch,
    RollbackError,
    SourceChanged,
    StoreBusy,
    StoreError,
    StoreNotUpgraded,
    StoreTooNew,
    StoreUnavailable,
    UpgradeError,
)
from .damage import DamageRecord
from .fsck import FsckProblem, FsckReport
from .listing import (
    CopyAsideInfo,
    StoreStatus,
    VaultEntry,
    get_vault_entry,
    list_vault_entries,
    open_entry_folder,
    store_status,
)
from .locator import StoreLocator
from .upgrade import (
    UpgradePlan,
    UpgradeProgress,
    UpgradeResult,
    cleanup_interrupted_upgrade,
    plan_upgrade,
    run_upgrade,
    upgrade_needed,
)
from .manifest import EntryKind, FileEntry, MemberRecord, StoreEntry
from .materialize import MaterializeResult
from .objects import ObjectStore, PutResult
from .versionstore import (
    INLINE_MAX,
    STORE_VERSION,
    StoreFormat,
    VaultMode,
    VersionStore,
    create_store,
    open_store,
    read_format,
    store_root,
    vault_lock,
    vault_mode,
)
