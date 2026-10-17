"""The version store's root: ``<vault>/store/`` with its FORMAT file, the vault mode, opening
with the compatibility checks, and the sweep of stale temp files (D3 2.2, 2.5, 2.12).

The store holds no absolute path and no machine name: everything under ``store/`` is named by a
hash or an entry id (D11).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ... import __version__
from .. import safety
from .errors import StoreNotUpgraded, StoreTooNew, StoreUnavailable
from .manifest import FileEntry
from .objects import ObjectStore, sha256

log = logging.getLogger(__name__)

VaultMode = Literal["legacy", "upgrading", "store"]
FaultHook = Callable[[str], None]   # tests: raise at a named point to simulate a crash

STORE_DIR = "store"
FORMAT_NAME = "FORMAT"
STORE_FORMAT = "nmsvault-store"
STORE_VERSION = 1
HASH_NAME = "sha256"
FANOUT = 2
INLINE_MAX = 8192
TMP_MAX_AGE = 3600          # own temp files older than this are leftovers, never work in progress

OBJECTS, ENTRIES, QUARANTINE, TMP, STATE = "objects", "entries", "quarantine", "tmp", "state"
UPGRADE_JSON = "upgrade.json"

_STORE_ID = re.compile(r"[0-9a-f]{32}")


def store_root(vault_root: str | Path) -> Path:
    return Path(vault_root) / STORE_DIR


def vault_mode(vault_root: str | Path) -> VaultMode:
    """"upgrading" while ``store/state/upgrade.json`` exists (an upgrade or a rollback runs or
    was interrupted); else "store" when ``store/FORMAT`` exists; else "legacy". No lock."""
    root = store_root(vault_root)
    if (root / STATE / UPGRADE_JSON).exists():
        return "upgrading"
    if (root / FORMAT_NAME).is_file():
        return "store"
    return "legacy"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(when: datetime) -> str:
    return _aware(when).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo is not None else when.astimezone()   # naive = local time


@dataclass(frozen=True)
class StoreFormat:
    version: int
    read_compat: int
    write_compat: int
    store_id: str
    hash: str
    fanout: int
    inline_max: int
    created_utc: str

    def to_bytes(self) -> bytes:
        body = {"format": STORE_FORMAT, "version": self.version, "read_compat": self.read_compat,
                "write_compat": self.write_compat, "store_id": self.store_id,
                "hash": self.hash, "fanout": self.fanout, "inline_max": self.inline_max,
                "created_utc": self.created_utc}
        return (json.dumps(body, indent=1, sort_keys=True) + "\n").encode("ascii")


def read_format(root: Path) -> StoreFormat:
    """Parse and check ``store/FORMAT``. Raises StoreUnavailable when it cannot be read or is
    malformed, StoreTooNew when reading the store needs a newer app."""
    try:
        d = json.loads((root / FORMAT_NAME).read_bytes().decode("ascii"))
    except (OSError, ValueError) as exc:
        raise StoreUnavailable(f"The vault is not reachable (store FORMAT unreadable: {exc}).") from exc
    try:
        if not isinstance(d, dict) or d.get("format") != STORE_FORMAT:
            raise ValueError("not a store FORMAT file")
        ints = {k: d[k] for k in ("version", "read_compat", "write_compat", "fanout", "inline_max")}
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in ints.values()):
            raise ValueError("a number field is not a number")
        if not isinstance(d["store_id"], str) or not _STORE_ID.fullmatch(d["store_id"]):
            raise ValueError("bad store_id")
        if not isinstance(d["hash"], str) or not isinstance(d["created_utc"], str):
            raise ValueError("bad hash or created_utc")
    except (KeyError, ValueError) as exc:
        raise StoreUnavailable(f"The vault is not reachable (store FORMAT malformed: {exc}).") from exc
    fmt = StoreFormat(d["version"], d["read_compat"], d["write_compat"], d["store_id"], d["hash"],
                      d["fanout"], d["inline_max"], d["created_utc"])
    if fmt.read_compat > STORE_VERSION:
        raise StoreTooNew()
    if fmt.hash != HASH_NAME or fmt.fanout != FANOUT:
        raise StoreTooNew()     # a layout this version does not know, whatever read_compat says
    return fmt


def create_store(vault_root: str | Path, *, clock: Callable[[], datetime] = utc_now) -> StoreFormat:
    """Create an empty store: its folders first, FORMAT last, since FORMAT appearing is what
    turns store mode on. The upgrade's commit step (D3 4.1 step 7) and a new vault use it; the
    caller holds the vault lock. Refuses a vault that already has a store."""
    root = store_root(vault_root)
    if (root / FORMAT_NAME).exists():
        raise StoreUnavailable("The vault already has a version store.")
    for name in (OBJECTS, ENTRIES, QUARANTINE, TMP, STATE):
        (root / name).mkdir(parents=True, exist_ok=True)
    fmt = StoreFormat(STORE_VERSION, STORE_VERSION, STORE_VERSION, uuid.uuid4().hex, HASH_NAME,
                      FANOUT, INLINE_MAX, iso_utc(clock()))
    safety.atomic_write_bytes(root / FORMAT_NAME, fmt.to_bytes(), temp_prefix=".FORMAT.tmp_")
    safety.fsync_dir(root)
    return fmt


ReadOnlyReason = Literal["too-new", "medium", "no-lock"]


class VersionStore:
    """One vault's version store. Open it with :meth:`open` (or :func:`open_store`)."""

    def __init__(self, vault_root: Path, fmt: StoreFormat, *,
                 lock: Callable[[], AbstractContextManager], app_version: str,
                 clock: Callable[[], datetime], live_roots: Sequence[Path],
                 faults: FaultHook | None) -> None:
        self.vault_root = vault_root
        self.root = store_root(vault_root)
        self.format = fmt
        self.store_id = fmt.store_id
        self.inline_max = fmt.inline_max
        self.app_version = app_version
        self.clock = clock
        self.live_roots = tuple(Path(p) for p in live_roots)
        self.read_only_reason: ReadOnlyReason | None = None
        self.warnings: list[str] = []
        self._lock = lock
        self._faults = faults
        self.objects = ObjectStore(self.root / OBJECTS, self.root / TMP, self.root / QUARANTINE,
                                   faults=faults)

    @property
    def read_only(self) -> bool:
        return self.read_only_reason is not None

    def store_file(self, path: str, data: bytes, mtime_ns: int) -> FileEntry:
        """The record of one captured file: kept inline when at most ``inline_max`` bytes, else
        put as an object (deduplicated and verified)."""
        digest = sha256(data)
        if len(data) <= self.inline_max:
            return FileEntry(path, len(data), digest, mtime_ns, inline=data)
        self.objects.put(data, digest)
        return FileEntry(path, len(data), digest, mtime_ns)

    def file_bytes(self, fe: FileEntry) -> bytes:
        """A file's bytes, verified against its record."""
        if fe.inline is not None:
            return fe.inline
        return self.objects.get(fe.sha256, fe.size)

    @classmethod
    def open(cls, vault_root: str | Path, *, lock: Callable[[], AbstractContextManager],
             app_version: str, clock: Callable[[], datetime] = utc_now,
             live_roots: Sequence[Path] = (), faults: FaultHook | None = None) -> "VersionStore":
        """Open the store of a vault in store mode. Raises StoreUnavailable (vault gone, FORMAT
        unreadable), StoreNotUpgraded (legacy or upgrading vault), StoreTooNew (needs a newer
        app to read). Opens read-only when only writing needs a newer app, the medium cannot be
        written, or the file system cannot lock."""
        vault_root = Path(vault_root)
        if not vault_root.is_dir():
            raise StoreUnavailable("The vault is not reachable.")
        _require_store_mode(vault_root)
        store = cls(vault_root, read_format(store_root(vault_root)), lock=lock,
                    app_version=app_version, clock=clock, live_roots=live_roots, faults=faults)
        if store.format.write_compat > STORE_VERSION:
            store._set_read_only("too-new", "This vault was upgraded by a newer NMS Save Vault; "
                                 "it opens read-only. Update the app to write to it.")
        else:
            store._probe_writable()
        return store

    # --- internals shared by the later steps ------------------------------------------------

    def _set_read_only(self, reason: ReadOnlyReason, text: str) -> None:
        if self.read_only_reason is None:
            self.read_only_reason = reason
        self.warnings.append(text)
        log.warning(text)

    def _probe_writable(self) -> None:
        tmp = self.root / TMP
        probe = tmp / f"probe.{os.getpid()}.{secrets.token_hex(4)}.part"
        try:
            tmp.mkdir(exist_ok=True)
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            self._set_read_only("medium", f"The vault cannot be written here ({exc.strerror or exc}); "
                                "it opens read-only.")
            return
        self._sweep_tmp()
        try:
            with self._lock() as handle:
                warning = getattr(handle, "warning", None)
        except safety.LockTimeout:
            return      # busy is not unsupported: a timeout never makes the store read-only
        if warning:
            self._set_read_only("no-lock", "The vault's drive does not support file locking; "
                                "it opens read-only.")

    def _sweep_tmp(self) -> None:
        """Remove this app's leftovers in ``store/tmp`` older than an hour: ``*.part`` files and
        ``mat-*`` folders. Younger ones may be another process's work in progress."""
        cutoff = time.time() - TMP_MAX_AGE
        try:
            items = list((self.root / TMP).iterdir())
        except OSError as exc:
            log.warning("could not list the store's temp folder: %s", exc)
            return
        for p in items:
            try:
                if p.lstat().st_mtime >= cutoff:
                    continue
                if p.is_file() and p.name.endswith(".part"):
                    p.unlink()
                elif p.is_dir() and not p.is_symlink() and p.name.startswith("mat-"):
                    shutil.rmtree(p)
                else:
                    continue
                log.info("removed the stale store temp item %s", p.name)
            except OSError as exc:
                log.warning("could not remove the stale store temp item %s: %s", p.name, exc)

    def _require_store_mode(self) -> None:
        _require_store_mode(self.vault_root)

    def _require_writable(self) -> None:
        self._require_store_mode()
        if self.read_only_reason == "too-new":
            raise StoreTooNew()
        if self.read_only_reason is not None:
            raise StoreUnavailable(self.warnings[0] if self.warnings else "The vault is read-only.")

    def _fault(self, point: str) -> None:
        if self._faults is not None:
            self._faults(point)


def _require_store_mode(vault_root: Path) -> None:
    mode = vault_mode(vault_root)
    if mode == "upgrading":
        raise StoreNotUpgraded("The vault is being upgraded.")
    if mode != "store":
        raise StoreNotUpgraded("Upgrade the vault first (Vault > Upgrade the vault).")


def vault_lock(vault_root: str | Path) -> Callable[[], AbstractContextManager]:
    """D1's vault lock, the one every vault commit takes (D3 2.12)."""
    from ..organizer import store as organizer_store

    path = organizer_store.store_root(Path(vault_root)) / organizer_store.LOCK
    return lambda: safety.exclusive_lock(path)


def open_store(vault_root: str | Path, *, live_roots: Sequence[Path] = (),
               clock: Callable[[], datetime] = utc_now) -> VersionStore:
    """Open a vault's store with the app's defaults: D1's vault lock and this app's version."""
    return VersionStore.open(vault_root, lock=vault_lock(vault_root), app_version=__version__,
                             clock=clock, live_roots=live_roots)
