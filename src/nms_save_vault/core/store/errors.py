"""Errors of the version store. Every one is an ``OperationError``, so the front ends already
show its message; messages are ASCII and written for the owner (D3 2.14)."""
from __future__ import annotations

from ..operations import OperationError


class StoreError(OperationError):
    pass


class StoreUnavailable(StoreError):
    """The vault folder is gone (a removed drive), FORMAT is unreadable, or the store cannot be
    written here (a read-only medium, no file locking)."""


class StoreBusy(StoreError):
    """The vault lock stayed taken past its timeout: only this write failed; try again."""

    def __init__(self, message: str = "The vault is busy. Try again.") -> None:
        super().__init__(message)


class StoreNotUpgraded(StoreError):
    """The vault is not in store mode: still in the old layout, or being upgraded."""


class StoreTooNew(StoreError):
    """FORMAT asks for a newer app: to read the store (refused), or to write it (read-only)."""

    def __init__(self, message: str = (
            "This vault was upgraded by a newer NMS Save Vault. Update the app.")) -> None:
        super().__init__(message)


class InsufficientSpace(StoreError):
    def __init__(self, needed: int) -> None:
        mb = -(-needed // (1024 * 1024))
        super().__init__(f"Not enough free space in the vault drive (needs {mb} MB).")
        self.needed = needed


class Cancelled(StoreError):
    """The caller cancelled; nothing was committed."""


class ObjectMissing(StoreError):
    def __init__(self, digest: str, detail: str = "missing") -> None:
        super().__init__(f"A stored copy ({digest[:12]}) is damaged ({detail}). Run Check vault.")
        self.digest = digest


class ObjectCorrupt(StoreError):
    def __init__(self, digest: str, detail: str = "its bytes do not match") -> None:
        super().__init__(f"A stored copy ({digest[:12]}) is damaged ({detail}). Run Check vault.")
        self.digest = digest


class EntryCorrupt(StoreError):
    """An entry manifest does not parse, fails its checksum, or breaks the schema."""


class SourceChanged(StoreError):
    def __init__(self, name: str, *, vanished: bool = False) -> None:
        what = "disappeared" if vanished else "changed"
        super().__init__(f"A save ({name}) {what} while it was being copied. Is the game running?")
        self.name = name
        self.vanished = vanished
