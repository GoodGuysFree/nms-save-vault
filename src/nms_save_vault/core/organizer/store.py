"""The organizer store: ``<vault>/organizer/``, the user data and recorded history that
travel with the vault (D1 2.3).

Files: ``organizer.json`` (header: schema, ``vault_id``), ``journal.jsonl`` (append-only
records), and JSON documents such as ``notes.json`` and ``lineage.json``. Every write
happens inside :meth:`OrganizerStore.transaction`, under the store lock ``.lock``, held for
one read-modify-write only. Readers take no lock: documents are replaced atomically and the
journal only grows, a torn last line being ignored until the next append seals it off.

On disk everything is pure ASCII (``ensure_ascii``), so no reader can mis-decode it; text
stays Unicode through ``\\u`` escapes. No absolute path is ever stored.
"""
from __future__ import annotations

import copy
import errno
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ... import __version__
from .. import safety

log = logging.getLogger(__name__)

SCHEMA = 1
DIRNAME = "organizer"
HEADER = "organizer.json"
JOURNAL = "journal.jsonl"
NOTES = "notes.json"
LINEAGE = "lineage.json"
LOCK = ".lock"
LIVEOP_LOCK = ".liveop.lock"    # taken around every live write, always before the store lock

#: The ``kind`` each JSON document declares; a file of another kind is not ours to rewrite.
KINDS = {
    HEADER: "nmsvault.organizer",
    NOTES: "nmsvault.organizer.notes",
    LINEAGE: "nmsvault.organizer.lineage",
}
RECORD_TYPES = ("obs", "op")

APP = f"nms-save-vault {__version__}"
NEWER_SCHEMA = "This vault's organizer data was written by a newer version of the app"
BUSY = "Another NMS Save Vault window is saving - try again"

_VAULT_ID = re.compile(r"[0-9a-f]{32}")


class StoreBusy(Exception):
    """The store lock stayed taken past the timeout. Only this transaction failed."""

    def __init__(self) -> None:
        super().__init__(BUSY)


class StoreReadOnly(Exception):
    """A write was asked of a store that is read-only; ``problems`` says why."""


def store_root(vault_root: Path) -> Path:
    return Path(vault_root) / DIRNAME


def liveop_lock_path(vault_root: Path) -> Path:
    return store_root(vault_root) / LIVEOP_LOCK


@dataclass
class _Doc:
    stamp: tuple[int, int, int] | None     # (st_ino, st_mtime_ns, st_size) when read
    data: dict | None


class OrganizerStore:
    """One vault's organizer folder, opened by this process.

    ``create`` makes the folder and header now when absent; otherwise the first transaction
    does. ``read_only`` forces a store that never writes (the read-only smoke test, listings).
    The store also turns read-only by itself on a newer schema, a file that does not parse
    or a medium that refuses writes; it then still shows everything and records nothing.
    """

    def __init__(self, vault_root: Path, *, create: bool, read_only: bool = False,
                 clock: Callable[[], float] = time.time, lock_timeout: float = 10.0) -> None:
        self.vault_root = Path(vault_root)
        self.root = store_root(self.vault_root)
        self.clock = clock
        self.lock_timeout = lock_timeout
        self.read_only = read_only
        self.problems: list[str] = []
        self.vault_id: str | None = None
        self._cache_lock = threading.Lock()
        self._docs: dict[str, _Doc] = {}
        self._journal: list[dict] = []
        self._journal_stamp: tuple[int, int] | None = None   # (st_ino, offset read through)
        self._journal_tail: bytes = b""
        self._load_header()
        if create and self.vault_id is None and not self.read_only:
            with self.transaction():
                pass

    # --- reading (no lock) ----------------------------------------------------

    def journal_records(self) -> Sequence[dict]:
        """Every readable journal record in file order. Cached; only the new tail is read."""
        with self._cache_lock:
            return tuple(self._refresh_journal())

    def document(self, name: str) -> dict:
        """A copy of a JSON document (``notes.json``, ``lineage.json``); an empty one of its
        kind when the file does not exist yet."""
        with self._cache_lock:
            doc = self._refresh_doc(name)
        return copy.deepcopy(doc) if doc is not None else _empty_doc(name)

    # --- writing --------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[StoreTx]:
        """Take the store lock, re-read whatever changed, yield a mutable view; on a normal
        exit write the changed documents atomically and append the new journal records in
        one fsynced write. An exception discards everything. Not re-entrant."""
        if self.read_only:
            raise StoreReadOnly("; ".join(self.problems) or "the organizer is read-only")
        with ExitStack() as stack:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                handle = stack.enter_context(
                    safety.exclusive_lock(self.root / LOCK, timeout=self.lock_timeout))
            except safety.LockTimeout as exc:
                raise StoreBusy() from exc
            except RuntimeError as exc:
                raise RuntimeError("organizer store lock is not re-entrant") from exc
            except OSError as exc:
                if not isinstance(exc, PermissionError) and exc.errno != errno.EROFS:
                    raise
                self._turn_read_only(f"the vault cannot be written: {exc.strerror}")
                raise StoreReadOnly(self.problems[-1]) from exc
            if handle.warning:
                self._problem(handle.warning)
            self._load_header()
            if self.read_only:
                raise StoreReadOnly("; ".join(self.problems))
            if self.vault_id is None:
                self._write_header()
            with self._cache_lock:
                journal = list(self._refresh_journal(locked=True))
            tx = StoreTx(self, journal)
            yield tx
            self._commit(tx)

    def _commit(self, tx: StoreTx) -> None:
        if self.read_only:      # a document read during the transaction turned out unwritable
            raise StoreReadOnly("; ".join(self.problems))
        for name, doc in tx._docs.items():
            raw = _dump_doc(doc)
            if raw != tx._read_as[name]:
                self._replace(name, raw)
        if tx._pending:
            data = b"".join(_dump_record(r) for r in tx._pending)
            _append(self.root / JOURNAL, data)

    def _replace(self, name: str, raw: bytes) -> None:
        """Atomic replace, after keeping the current file as ``<name>.prev``."""
        path = self.root / name
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current is not None:
            safety.atomic_write_bytes(self.root / f"{name}.prev", current)
        safety.atomic_write_bytes(path, raw)

    def _write_header(self) -> None:
        header = {"schema": SCHEMA, "kind": KINDS[HEADER], "vault_id": uuid.uuid4().hex,
                  "created": int(self.clock()), "app": APP}
        safety.atomic_write_bytes(self.root / HEADER, _dump_doc(header))
        self.vault_id = header["vault_id"]

    # --- loading --------------------------------------------------------------

    def _load_header(self) -> None:
        with self._cache_lock:
            doc = self._refresh_doc(HEADER)
        if doc is None:
            return
        vault_id = doc.get("vault_id")
        if not isinstance(vault_id, str) or not _VAULT_ID.fullmatch(vault_id):
            self._turn_read_only(f"{HEADER}: no valid vault_id")
            return
        self.vault_id = vault_id

    def _refresh_doc(self, name: str) -> dict | None:
        """The parsed document, re-read when the file changed. Call under ``_cache_lock``."""
        path = self.root / name
        try:
            st = path.stat()
        except FileNotFoundError:
            self._docs[name] = _Doc(None, None)
            return None
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        cached = self._docs.get(name)
        if cached is not None and cached.stamp == stamp:
            return cached.data
        data = self._parse_doc(name, path.read_bytes())
        self._docs[name] = _Doc(stamp, data)
        return data

    def _parse_doc(self, name: str, raw: bytes) -> dict | None:
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:   # UnicodeDecodeError is a ValueError too
            self._turn_read_only(f"{name} does not parse ({exc}); it is left untouched")
            return None
        if not isinstance(data, dict) or data.get("kind") != KINDS.get(name):
            self._turn_read_only(f"{name} is not an organizer {name} file; it is left untouched")
            return None
        schema = data.get("schema")
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            self._turn_read_only(f"{name} has no valid schema; it is left untouched")
            return None
        if schema > SCHEMA:
            self._turn_read_only(NEWER_SCHEMA)
        return data

    def _refresh_journal(self, *, locked: bool = False) -> list[dict]:
        """Parse what was appended since the last read. Call under ``_cache_lock``.

        A last line without its newline is skipped. Read under the store lock (``locked``)
        it can only be the torn end of a crashed append and is reported; read without the
        lock it may be an append still in progress."""
        path = self.root / JOURNAL
        try:
            st = path.stat()
        except FileNotFoundError:
            self._journal, self._journal_stamp, self._journal_tail = [], None, b""
            return self._journal
        ino, offset = self._journal_stamp or (st.st_ino, 0)
        if ino != st.st_ino or st.st_size < offset:      # replaced or cut: start over
            self._journal, offset, self._journal_tail = [], 0, b""
        if st.st_size != offset + len(self._journal_tail):
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
            lines = data.split(b"\n")
            self._journal_tail = lines.pop()    # after the last newline: "" or unfinished
            for raw in lines:
                offset += len(raw) + 1
                record = self._parse_record(raw)
                if record is not None:
                    self._journal.append(record)
            self._journal_stamp = (st.st_ino, offset)
        if self._journal_tail and locked:
            self._problem(f"{JOURNAL}: the last line is incomplete and was ignored")
        return self._journal

    def _parse_record(self, raw: bytes) -> dict | None:
        if not raw.strip():
            return None
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            self._problem(f"{JOURNAL}: a damaged line was skipped ({exc})")
            return None
        if not _valid_envelope(record):
            self._problem(f"{JOURNAL}: a line that is not a journal record was skipped")
            return None
        return record

    def _problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)
            log.warning("organizer store: %s", text)

    def _turn_read_only(self, reason: str) -> None:
        self.read_only = True
        self._problem(reason)


class StoreTx:
    """The mutable view a transaction yields. Documents are copies; journal records are
    queued and written when the transaction ends."""

    def __init__(self, store: OrganizerStore, journal: list[dict]) -> None:
        self._store = store
        self._journal = journal
        self._pending: list[dict] = []
        self._docs: dict[str, dict] = {}
        self._read_as: dict[str, bytes] = {}    # each document as first handed out
        # Running maxima, so appending many records (a bootstrap) costs no rescan each.
        self._last_seq = max((r["seq"] for r in journal), default=0)
        self._last_at = max((r["at"] for r in journal), default=0)

    @property
    def vault_id(self) -> str:
        assert self._store.vault_id is not None
        return self._store.vault_id

    def doc(self, name: str) -> dict:
        """The document to read and change in place; written at the end if it changed."""
        if name not in KINDS or name == HEADER:
            raise ValueError(f"not an organizer document: {name}")
        if name not in self._docs:
            doc = self._store.document(name)
            if self._store.read_only:   # newer schema or does not parse: never rewritten
                raise StoreReadOnly("; ".join(self._store.problems))
            self._docs[name], self._read_as[name] = doc, _dump_doc(doc)
        return self._docs[name]

    def journal(self) -> Sequence[dict]:
        """Every record, as re-read under the lock, plus the ones this transaction added."""
        return tuple(self._journal) + tuple(self._pending)

    def append(self, record: dict, *, at: int | None = None) -> dict:
        """Queue one record (``t`` plus its own fields) and return it with ``id``, ``seq`` and
        ``at`` assigned. ``at`` defaults to now, never earlier than any record so far, so the
        order never goes backwards when the clock does; backfill passes its own time."""
        if record.get("t") not in RECORD_TYPES or {"id", "seq", "at"} & record.keys():
            raise ValueError("a journal record is its type and fields; id, seq, at are assigned")
        seq = self._last_seq + 1
        if at is None:
            at = max(int(self._store.clock()), self._last_at)
        full = {"t": record["t"], "id": f"{self.vault_id[:8]}-{seq}", "seq": seq, "at": at}
        full.update((k, v) for k, v in record.items() if k != "t")
        if not _valid_envelope(full):
            raise ValueError("not a valid journal record")
        _dump_record(full)          # refuse what cannot be written before anything is
        self._pending.append(full)
        self._last_seq, self._last_at = seq, max(self._last_at, at)
        return full


def _valid_envelope(record: object) -> bool:
    if not isinstance(record, dict) or record.get("t") not in RECORD_TYPES:
        return False
    seq, at = record.get("seq"), record.get("at")
    return (isinstance(record.get("id"), str) and _is_uint(seq) and seq > 0
            and _is_uint(at))


def _is_uint(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _empty_doc(name: str) -> dict:
    return {"schema": SCHEMA, "kind": KINDS[name]}


def _dump_doc(doc: dict) -> bytes:
    return (json.dumps(doc, ensure_ascii=True, allow_nan=False, indent=1) + "\n").encode("ascii")


def _dump_record(record: dict) -> bytes:
    text = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return (text + "\n").encode("ascii")


def _append(path: Path, data: bytes) -> None:
    """One fsynced append; a line torn by an earlier crash is sealed off first."""
    with open(path, "a+b", buffering=0) as f:
        size = f.seek(0, os.SEEK_END)
        if size:
            f.seek(size - 1)
            if f.read(1) != b"\n":
                data = b"\n" + data
        pending = memoryview(data)
        while pending:                 # appends go to the end whatever the position
            pending = pending[f.write(pending):]
        os.fsync(f.fileno())
