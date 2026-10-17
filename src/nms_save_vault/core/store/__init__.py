"""The content-addressed version store under ``<vault>/store/`` (design D3)."""
from .capture import CaptureScope
from .errors import (
    Cancelled,
    EntryCorrupt,
    InsufficientSpace,
    ObjectCorrupt,
    ObjectMissing,
    SourceChanged,
    StoreBusy,
    StoreError,
    StoreNotUpgraded,
    StoreTooNew,
    StoreUnavailable,
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
