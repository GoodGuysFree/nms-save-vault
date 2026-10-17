"""The version store: ``<vault>/store/`` with its FORMAT file, the vault mode, opening with the
compatibility checks, the sweep of stale temp files, adding entries and reading them back
(D3 2.2, 2.5, 2.6, 2.12).

The store holds no absolute path and no machine name: everything under ``store/`` is named by a
hash or an entry id (D11). Objects are written before and outside the vault lock; only the
manifest commit runs under it, for milliseconds.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ... import __version__
from .. import catalog, discover, safety, savedir
from ..organizer.model import Platform
from ..state import AppState
from . import capture
from .capture import CaptureScope
from .damage import DamageRecord, read_damage, write_damage
from .damage import merge as merge_damage
from .errors import (
    Cancelled,
    EntryCorrupt,
    InsufficientSpace,
    ObjectMissing,
    StoreBusy,
    StoreError,
    StoreNotUpgraded,
    StoreTooNew,
    StoreUnavailable,
)
from .manifest import (
    ENTRY_KINDS,
    MAX_ENTRY_ID,
    EntryKind,
    FileEntry,
    MemberRecord,
    StoreEntry,
    is_entry_id,
)
from .materialize import MaterializeResult, check_destination, files_to_write, write_entry
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
SPACE_MARGIN = 64 * 1024 * 1024
FRESH_SECONDS = 2.0         # a folder time younger than this may hide a same-tick change

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


def _parse_local(text: str) -> datetime:
    """A catalog v1 time (local, naive, ISO seconds)."""
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not a time: {text!r}") from exc


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
    created = not root.is_dir()
    for name in (OBJECTS, ENTRIES, QUARANTINE, TMP, STATE):
        (root / name).mkdir(parents=True, exist_ok=True)
    safety.fsync_dir(root)                    # the folders exist before FORMAT says so
    if created:
        safety.fsync_dir(Path(vault_root))
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
                 clock: Callable[[], datetime], live_roots: Sequence[Path] | None,
                 faults: FaultHook | None, app_state: AppState | None = None) -> None:
        self.vault_root = vault_root
        self.root = store_root(vault_root)
        self.format = fmt
        self.store_id = fmt.store_id
        self.inline_max = fmt.inline_max
        self.app_version = app_version
        self.clock = clock
        # None: discover the live folders on this machine whenever they are needed.
        self._live_roots = None if live_roots is None else tuple(Path(p) for p in live_roots)
        self._app_state = app_state
        self.read_only_reason: ReadOnlyReason | None = None
        self.warnings: list[str] = []
        self._lock = lock
        self._faults = faults
        self.objects = ObjectStore(self.root / OBJECTS, self.root / TMP, self.root / QUARANTINE,
                                   faults=faults)
        self._index: dict[str, StoreEntry] = {}
        self._vids: dict[str, list[tuple[str, int, str]]] = {}   # vid -> (entry, slot, member)
        self._corrupt: dict[str, str] = {}          # manifest file name -> why it is unreadable
        self._index_guard = threading.Lock()
        self._entries_seen: int | None = None       # entries/ mtime_ns at the last listing

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

    # --- reading (no lock: manifests are immutable and appear atomically) ---------------------

    def entries(self) -> list[StoreEntry]:
        """Every readable entry, oldest first. Unreadable manifests are skipped (and logged);
        :attr:`corrupt_manifests` names them."""
        self._refresh()
        return sorted(self._index.values(), key=lambda e: (e.created_utc, e.id))

    def entry(self, entry_id: str) -> StoreEntry:
        """The entry; KeyError when there is none, EntryCorrupt when its manifest is bad."""
        self._refresh()
        if entry_id in self._index:
            return self._index[entry_id]
        if f"{entry_id}.json" in self._corrupt:
            raise EntryCorrupt(f"entry {entry_id}: {self._corrupt[f'{entry_id}.json']}")
        raise KeyError(f"the store has no entry {entry_id!r}")

    def has_entry(self, entry_id: str) -> bool:
        self._refresh()
        return entry_id in self._index

    def read_file(self, entry_id: str, path: str) -> bytes:
        """One file of an entry, verified."""
        return self.file_bytes(self.entry(entry_id).file(path))

    # --- materialize / export ------------------------------------------------------------------

    def materialize(self, entry_id: str, dest: str | Path, *, slots: Iterable[int] | None = None,
                    account_files: bool = True, cache: bool = True,
                    keep_mtimes: bool = True) -> MaterializeResult:
        """Write an entry (or some of its slots) into ``dest``, an empty or absent folder outside
        the store and the live save folders; every file is verified after writing."""
        dest = Path(dest)
        check_destination(dest, refuse_inside=(self.root,), live=self.live_folders())
        return self._materialize(entry_id, dest, slots=slots, account_files=account_files,
                                 cache=cache, keep_mtimes=keep_mtimes)

    @contextmanager
    def materialized(self, entry_id: str, *, slots: Iterable[int] | None = None,
                     account_files: bool = True, cache: bool = True) -> Iterator[Path]:
        """A verified temporary folder holding the entry, on the store's volume; removed on
        exit, also after an error. For reading only. A lock on ``<folder>.lock`` is held for
        as long as the folder is in use, so no other process's sweep removes it."""
        dest = self.root / TMP / f"mat-{entry_id}-{secrets.token_hex(4)}"
        lock_file = _mat_lock(dest)
        try:
            with safety.exclusive_lock(lock_file, timeout=0):
                try:
                    self._materialize(entry_id, dest, slots=slots, account_files=account_files,
                                      cache=cache, keep_mtimes=True)
                    yield dest
                finally:
                    if dest.exists():
                        try:
                            shutil.rmtree(dest)
                        except OSError as exc:
                            log.warning("could not remove the temporary folder %s: %s",
                                        dest.name, exc)
        finally:
            _unlink_quietly(lock_file)

    def export_entry(self, entry_id: str, dest: str | Path, *, slots: Iterable[int] | None = None,
                     account_files: bool = True, cache: bool = True,
                     keep_mtimes: bool = True) -> MaterializeResult:
        """Materialize to a folder outside the vault (and the live save folders), for other
        tools or another machine."""
        dest = Path(dest)
        check_destination(dest, refuse_inside=(self.vault_root,), live=self.live_folders())
        return self._materialize(entry_id, dest, slots=slots, account_files=account_files,
                                 cache=cache, keep_mtimes=keep_mtimes)

    def _materialize(self, entry_id: str, dest: Path, *, slots: Iterable[int] | None,
                     account_files: bool, cache: bool, keep_mtimes: bool) -> MaterializeResult:
        entry = self.entry(entry_id)
        if slots is not None:
            slots = tuple(slots)
        files = files_to_write(self, entry, slots=slots, account_files=account_files, cache=cache)
        return write_entry(self, entry, files, dest, keep_mtimes=keep_mtimes)

    def live_folders(self) -> discover.LiveFolders:
        """The live save folders (with their xgs twins) nothing may be written into: the ones
        given at open, else the configured live sources (``app_state``) plus the ones
        discovered on this machine."""
        roots = (self._live_roots if self._live_roots is not None
                 else discover.all_live_paths(self._app_state))
        return discover.live_folders(roots)

    @property
    def corrupt_manifests(self) -> dict[str, str]:
        self._refresh()
        return dict(self._corrupt)

    def _refresh(self, *, force: bool = False) -> None:
        """Parse manifests not seen yet, when ``entries/`` changed since the last look (its
        modification time, D3 2.7) or ``force``. Manifests never change once written, so a
        name already indexed (or found corrupt) is not read again."""
        try:
            st = os.stat(self.root / ENTRIES)
        except OSError as exc:
            raise StoreUnavailable(f"The vault is not reachable ({exc.strerror or exc}).") from exc
        if not force and st.st_mtime_ns == self._entries_seen:
            return
        try:
            names = os.listdir(self.root / ENTRIES)
        except OSError as exc:
            raise StoreUnavailable(f"The vault is not reachable ({exc.strerror or exc}).") from exc
        with self._index_guard:
            # A folder time this fresh may not have moved for a manifest added within the same
            # clock tick (FAT keeps 2 s): look again next time.
            fresh = time.time() - st.st_mtime < FRESH_SECONDS
            self._entries_seen = None if fresh else st.st_mtime_ns
            for name in names:
                if not name.endswith(".json") or name[:-5] in self._index or name in self._corrupt:
                    continue
                try:
                    raw = (self.root / ENTRIES / name).read_bytes()
                except OSError as exc:
                    log.warning("could not read the store manifest %s: %s", name, exc)
                    continue
                try:
                    entry = StoreEntry.from_bytes(raw)
                    if entry.id != name[:-5]:
                        raise EntryCorrupt(f"names entry {entry.id}")
                except EntryCorrupt as exc:
                    log.warning("the store manifest %s is damaged: %s", name, exc)
                    self._corrupt[name] = str(exc)
                    continue
                self._index_entry(entry)

    def _index_entry(self, entry: StoreEntry) -> None:
        """Add an entry to the in-memory index (call under ``_index_guard``)."""
        if entry.id in self._index:
            return
        self._index[entry.id] = entry
        for m in entry.members:
            self._vids.setdefault(m.vid, []).append((entry.id, m.slot, m.member))

    def vid_refs(self, vid: str) -> tuple[tuple[str, int, str], ...]:
        """Every (entry id, slot, member) holding ``vid``, newest entry first. In memory."""
        self._refresh()
        with self._index_guard:
            refs = list(self._vids.get(vid, ()))
        return tuple(sorted(refs, key=lambda r: (self._index[r[0]].created_utc, r[0], r[1], r[2]),
                            reverse=True))

    def record_damage(self, entry_id: str, fe: FileEntry | None, problem: str,
                      detail: str) -> None:
        """Note in ``state/damage.json`` that a file of an entry (or, with ``fe`` None, its
        manifest) could not be read back intact. Best effort: a read-only store or a busy lock
        only logs it."""
        record = DamageRecord(entry_id, fe.path if fe else "", fe.sha256 if fe else "",
                              problem, detail, iso_utc(self.clock()), iso_utc(self.clock()))
        log.warning("damaged store entry %s %s: %s (%s)", entry_id, record.path, problem, detail)
        if self.read_only:
            return
        try:
            with self._lock():
                write_damage(self.root, merge_damage(read_damage(self.root), record))
        except (safety.LockTimeout, OSError, RuntimeError) as exc:
            log.warning("could not record the damage in the store's damage list: %s", exc)

    def damage(self) -> list[DamageRecord]:
        return read_damage(self.root)

    def fsck(self, *, deep: bool = False, repair: bool = False,
             fallbacks: Sequence[Path] = ()) -> "FsckReport":
        """Check the store (D3 2.11): quick, ``deep`` (re-hash everything), ``repair``
        (re-store damaged files from good copies elsewhere). Nothing is ever deleted."""
        from .fsck import fsck

        return fsck(self, deep=deep, repair=repair, fallbacks=fallbacks)

    # --- writing ------------------------------------------------------------------------------

    def add_from_folder(self, folder: str | Path, *, kind: EntryKind, label: str,
                        source: Mapping[str, Any], scope: CaptureScope = CaptureScope(),
                        entry_id: str | None = None, origin: Mapping[str, Any] | None = None,
                        created_local: str | None = None, op_id: str | None = None,
                        reason: str = "", cancel: Callable[[], bool] | None = None,
                        progress: Callable[[int, int], None] | None = None) -> StoreEntry:
        """Keep a folder (a legacy entry's, an import, a materialized entry) as a store entry.
        ``entry_id`` keeps a given id (fold, copy-in): an entry with that id and the same files
        is returned as is; different files get ``<id>-L``."""
        folder = Path(folder)
        return self._add(folder, platform=savedir.platform_of(folder), account_key=None,
                         live=False, kind=kind, label=label, source=source, scope=scope,
                         entry_id=entry_id, origin=origin, created_local=created_local,
                         op_id=op_id, reason=reason, cancel=cancel, progress=progress)

    def add_from_live(self, target_dir: str | Path, *, platform: Platform, account_key: str,
                      kind: EntryKind, label: str, scope: CaptureScope,
                      op_id: str | None = None, reason: str = "",
                      finish: Callable[[list[MemberRecord]], str | None] | None = None
                      ) -> StoreEntry:
        """Keep (part of) a live save folder; it is only read. ``finish`` sees the member
        records before the commit: it may refuse (raise) or return the label to use."""
        target_dir = Path(target_dir)
        if target_dir.is_dir() and savedir.platform_of(target_dir) != platform:
            raise StoreError(f"That folder is not a {platform} save folder.")
        source = {"type": "live", "platform": platform, "account_key": account_key}
        return self._add(target_dir, platform=platform, account_key=account_key, live=True,
                         kind=kind, label=label, source=source, scope=scope, entry_id=None,
                         origin=None, created_local=None, op_id=op_id, reason=reason,
                         cancel=None, progress=None, finish=finish)

    def _add(self, folder: Path, *, platform: Platform, account_key: str | None, live: bool,
             kind: EntryKind, label: str, source: Mapping[str, Any], scope: CaptureScope,
             entry_id: str | None, origin: Mapping[str, Any] | None, created_local: str | None,
             op_id: str | None, reason: str, cancel: Callable[[], bool] | None,
             progress: Callable[[int, int], None] | None,
             finish: Callable[[list[MemberRecord]], str | None] | None = None) -> StoreEntry:
        if kind not in ENTRY_KINDS:
            raise ValueError(f"not an entry kind: {kind!r}")
        if entry_id is not None and not is_entry_id(entry_id):
            raise ValueError(f"not an entry id: {entry_id!r}")
        if created_local is not None:
            _parse_local(created_local)
        self._require_writable()
        if not folder.is_dir():
            raise StoreError("The folder to keep is not reachable.")
        plan = capture.plan(folder, platform, kind=kind, scope=scope, live=live)
        self._require_space(plan.object_bytes(self.inline_max))
        records: dict[str, FileEntry] = {}
        stats = {}
        total = len(plan.files) + len(plan.made)
        done = 0
        sources = [(rel, path, None) for rel, path in plan.files]
        made = [(rel, None, item) for rel, item in plan.made.items()]
        for rel, path, item in sources + made:
            if path is not None:
                data, st = capture.read_stable(path, rel)
                mtime_ns = st.st_mtime_ns
            else:
                data, mtime_ns = item
            records[rel] = self.store_file(rel, data, mtime_ns)
            if path is not None and capture.wants_stats(platform, rel):
                stats[rel] = capture.stats_of(data)
            del data
            done += 1
            if progress is not None:
                progress(done, total)
            if cancel is not None and cancel():
                raise Cancelled("Cancelled. Nothing was kept.")
        members, notes, absent = capture.describe_members(folder, platform, records, stats,
                                                          plan.slots)
        plan.require_consistent(records)
        if finish is not None:
            label = finish(members) or label
        for note in notes:
            log.warning("keeping a copy of %s: %s", folder.name, note)
        fields = dict(kind=kind, label=label, reason=reason, op_id=op_id,
                      app_version=self.app_version, platform=platform, account_key=account_key,
                      source=dict(source), scope=plan.scope,
                      origin=None if origin is None else dict(origin),
                      files=list(records.values()), members=members,
                      account=capture.account_paths(plan, records), xbox=plan.xbox, notes=notes,
                      absent=absent)
        return self._commit(fields, entry_id=entry_id, created_local=created_local)

    def _require_space(self, needed: int) -> None:
        try:
            free = shutil.disk_usage(self.root).free
        except OSError as exc:
            raise StoreUnavailable(f"The vault is not reachable ({exc.strerror or exc}).") from exc
        if free < needed + SPACE_MARGIN:
            raise InsufficientSpace(needed + SPACE_MARGIN)

    def _commit(self, fields: dict[str, Any], *, entry_id: str | None,
                created_local: str | None) -> StoreEntry:
        """The commit point: under the vault lock, re-check the mode, settle the id (unique
        across the catalog and the store) and write the manifest."""
        now = _aware(self.clock())
        local = now.astimezone().replace(tzinfo=None)
        # A kept time (fold, copy-in: the legacy entry's) is when the entry was made, in UTC too.
        made = _aware(_parse_local(created_local)) if created_local else now
        fields = dict(fields, created_utc=iso_utc(made),
                      created_local=created_local or local.isoformat(timespec="seconds"))
        try:
            with self._lock():
                self._require_store_mode()
                self._refresh(force=True)           # ids are settled against every manifest
                if entry_id is not None:
                    existing = self._index.get(entry_id)
                    if existing is not None and _same_files(existing.files, fields["files"]):
                        return existing
                    if existing is not None or self._manifest_path(entry_id).exists():
                        entry_id = self._free_id(entry_id, suffix="-L", first_suffix="",
                                                 catalog_ids=self._catalog_ids())
                else:
                    stamp = f"{fields['kind']}-{fields['platform']}-{local:%Y%m%d-%H%M%S}"
                    entry_id = self._free_id(stamp, catalog_ids=self._catalog_ids())
                entry = StoreEntry.new(id=entry_id, **fields)
                self._write_manifest(entry)
        except safety.LockTimeout as exc:
            raise StoreBusy() from exc
        self._check_present(entry)
        return entry

    def _free_id(self, stem: str, *, suffix: str = "", catalog_ids: frozenset[str] = frozenset(),
                 first_suffix: str = "-") -> str:
        """``stem + suffix``, else with ``-2``, ``-3``... after it (``2``... with
        ``first_suffix=""``): the first not taken in the catalog or the store. The stem is
        shortened as needed so no id is longer than an id may be."""
        def taken(candidate: str) -> bool:
            return (candidate in catalog_ids or candidate in self._index
                    or self._manifest_path(candidate).exists())

        def fit(end: str) -> str:
            return stem[:MAX_ENTRY_ID - len(end)] + end

        if not taken(fit(suffix)):
            return fit(suffix)
        n = 2
        while taken(fit(f"{suffix}{first_suffix}{n}")):
            n += 1
        return fit(f"{suffix}{first_suffix}{n}")

    def _catalog_ids(self) -> frozenset[str]:
        try:
            return frozenset(e.id for e in catalog.Vault(self.vault_root).load().entries)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StoreError(f"The vault catalog cannot be read ({exc}).") from exc

    def _manifest_path(self, entry_id: str) -> Path:
        return self.root / ENTRIES / f"{entry_id}.json"

    def _write_manifest(self, entry: StoreEntry) -> None:
        raw = entry.to_bytes()
        dest = self._manifest_path(entry.id)
        # Not named after the id: a long id would push the temp path past Windows' limit.
        tmp = self.root / TMP / f"entry.{os.getpid()}.{secrets.token_hex(4)}.part"
        try:
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            self._fault("entry.before_replace")
            os.replace(tmp, dest)                       # COMMIT POINT
            safety.fsync_dir(dest.parent)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        with self._index_guard:
            self._index_entry(entry)
        self._fault("entry.after_replace")

    def _check_present(self, entry: StoreEntry) -> None:
        """Every object the new entry references is on disk with its size (a medium that lost a
        rename shows here, not at the next read)."""
        for fe in entry.files:
            if fe.inline is None and self.objects.size_of(fe.sha256) != fe.size:
                raise ObjectMissing(fe.sha256, f"{fe.path} of the new entry {entry.id}")

    @classmethod
    def open(cls, vault_root: str | Path, *, lock: Callable[[], AbstractContextManager],
             app_version: str, clock: Callable[[], datetime] = utc_now,
             live_roots: Sequence[Path] | None = None,
             faults: FaultHook | None = None,
             app_state: AppState | None = None) -> "VersionStore":
        """Open the store of a vault in store mode. Raises StoreUnavailable (vault gone, FORMAT
        unreadable), StoreNotUpgraded (legacy or upgrading vault), StoreTooNew (needs a newer
        app to read). Opens read-only when only writing needs a newer app, the medium cannot be
        written, or the file system cannot lock. ``live_roots``: the live save folders nothing
        is ever written into; None means the live sources of ``app_state`` plus the ones
        discovered on this machine."""
        vault_root = Path(vault_root)
        if not vault_root.is_dir():
            raise StoreUnavailable("The vault is not reachable.")
        _require_store_mode(vault_root)
        store = cls(vault_root, read_format(store_root(vault_root)), lock=lock,
                    app_version=app_version, clock=clock, live_roots=live_roots, faults=faults,
                    app_state=app_state)
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
        warning = None
        try:
            with self._lock() as handle:
                warning = getattr(handle, "warning", None)
        except safety.LockTimeout:
            pass        # busy is not unsupported: a timeout never makes the store read-only
        if warning:
            self._set_read_only("no-lock", "The vault's drive does not support file locking; "
                                "it opens read-only.")
            return      # without locks the sweep could not tell a folder in use from a leftover
        self._sweep_tmp()

    def _sweep_tmp(self) -> None:
        """Remove this app's leftovers in ``store/tmp`` older than an hour: ``*.part`` files and
        ``mat-*`` folders. Younger ones may be another process's work in progress; a ``mat-*``
        folder is removed only when its lock is free (no live :meth:`materialized` holds it)."""
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
                    if not _remove_if_unlocked(p, _mat_lock(p), shutil.rmtree):
                        continue
                elif p.is_file() and p.name.startswith("mat-") and p.name.endswith(".lock") \
                        and not p.with_suffix("").exists():
                    if not _remove_if_unlocked(None, p, None):
                        continue
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


def _mat_lock(folder: Path) -> Path:
    return folder.with_name(folder.name + ".lock")


def _remove_if_unlocked(item: Path | None, lock_file: Path,
                        remove: Callable[[Path], None] | None) -> bool:
    """Remove ``item`` (with ``remove``) and then ``lock_file``, only when the lock is free
    now. False when someone holds it: the item is in use."""
    try:
        with safety.exclusive_lock(lock_file, timeout=0):
            if item is not None and remove is not None:
                remove(item)
    except (safety.LockTimeout, RuntimeError):     # another process; or this very thread
        return False
    _unlink_quietly(lock_file)
    return True


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:      # Windows: another process opened it meanwhile; harmless
        log.info("could not remove the lock file %s: %s", path.name, exc)


def _same_files(a: Sequence[FileEntry], b: Sequence[FileEntry]) -> bool:
    key = lambda f: (f.path, f.size, f.sha256)   # noqa: E731
    return sorted(map(key, a)) == sorted(map(key, b))


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


def open_store(vault_root: str | Path, *, live_roots: Sequence[Path] | None = None,
               app_state: AppState | None = None,
               clock: Callable[[], datetime] = utc_now) -> VersionStore:
    """Open a vault's store with the app's defaults: D1's vault lock and this app's version.
    ``app_state``: the app's configuration (its live sources are never written into)."""
    return VersionStore.open(vault_root, lock=vault_lock(vault_root), app_version=__version__,
                             clock=clock, live_roots=live_roots, app_state=app_state)
