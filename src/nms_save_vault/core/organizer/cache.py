"""The scanner's per-machine cache (D1 2.4): what was learnt about files and entries, so
an unchanged file is not hashed again and an unchanged entry not rescanned.

Derived data only, rebuildable at any time, kept beside the app's other per-machine data
(``platform.user_cache_dir``) and never in the vault. Keys are absolute paths (files) and
entry ids; values are content hashes, which stay true wherever the vault is opened from.
A cache that cannot be read is simply started again; one that cannot be written is
logged. The last writer wins.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import NamedTuple

from .. import platform as host_platform
from .. import safety
from .model import Capture, ModelError, capture_from_json, capture_to_json, is_vid, pid_kind

log = logging.getLogger(__name__)

SCHEMA = 2
FILENAME = "scan-cache.json"

_HEX_ID = re.compile(r"0|[1-9A-F][0-9A-F]{0,15}")


def cache_dir(vault_root: Path, vault_id: str | None,
              host: host_platform.Host | None = None) -> Path:
    """``<user cache>/vaults/<vault_id>/``, shared by everything per-machine about one vault.
    A vault without an organizer store yet is keyed by its resolved location instead."""
    if vault_id is None:
        digest = hashlib.sha256(str(Path(vault_root).resolve()).encode("utf-8")).hexdigest()
        vault_id = f"nostore-{digest[:16]}"
    return host_platform.user_cache_dir(host) / "vaults" / vault_id


class FileHit(NamedTuple):
    vid: str
    whole: bool | None      # the live check's verdict; None when never checked (entry copies)
    meta_sha: str           # the meta the verdict was reached with: it ties sizes to chunks


class ScanCache:
    """``path`` None keeps the cache in memory only (``--no-cache``)."""

    def __init__(self, path: Path | None, vault_id: str | None, *,
                 read_only: bool = False) -> None:
        self.path = path
        self.vault_id = vault_id
        self.read_only = read_only     # used, never saved (the operation hook)
        self._files: dict[str, list] = {}        # path -> [size, mtime_ns, vid, whole, meta_sha]
        self._entries: dict[str, dict] = {}      # entry id -> {"fp", "captures"}
        self._ids: dict[str, str] = {}           # vid -> data id hex, "0" for none
        self._legacy: dict[str, str] = {}        # vid -> legacy pid fixed at first sight
        self._dirty = False

    @classmethod
    def load(cls, path: Path, vault_id: str | None, *, read_only: bool = False) -> ScanCache:
        cache = cls(path, vault_id, read_only=read_only)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cache
        except OSError as exc:
            log.warning("scan cache not read, starting afresh: %s", exc.strerror)
            return cache
        try:
            data = json.loads(raw.decode("utf-8"))
            if data.get("schema") != SCHEMA or data.get("vault_id") != vault_id:
                return cache
            files, entries, ids, legacy = (data["files"], data["entries"], data["ids"],
                                           data["legacy"])
            if not (all(_file_ok(v) for v in files.values())
                    and all(isinstance(v, dict) and isinstance(v.get("fp"), str)
                            and isinstance(v.get("captures"), list) for v in entries.values())
                    and all(is_vid(k) and isinstance(v, str) and _HEX_ID.fullmatch(v)
                            for k, v in ids.items())
                    and all(is_vid(k) and _is_legacy(v) for k, v in legacy.items())):
                raise ValueError("an entry is not in the expected form")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("scan cache damaged, starting afresh: %s", exc)
            return cache
        cache._files, cache._entries, cache._ids, cache._legacy = files, entries, ids, legacy
        return cache

    # --- files ----------------------------------------------------------------

    def file(self, path: Path, size: int, mtime_ns: int) -> FileHit | None:
        """What is known of a data file that has not changed since, else None."""
        hit = self._files.get(str(path))
        if hit is None or hit[0] != size or hit[1] != mtime_ns:
            return None
        return FileHit(hit[2], hit[3], hit[4])

    def put_file(self, path: Path, size: int, mtime_ns: int, vid: str, whole: bool | None,
                 meta_sha: str) -> None:
        value = [size, mtime_ns, vid, whole, meta_sha]
        if self._files.get(str(path)) != value:
            self._files[str(path)] = value
            self._dirty = True

    # --- entries --------------------------------------------------------------

    def entry(self, entry_id: str, fingerprint: str) -> tuple[Capture, ...] | None:
        hit = self._entries.get(entry_id)
        if hit is None or hit["fp"] != fingerprint:
            return None
        try:
            return tuple(capture_from_json(c) for c in hit["captures"])
        except ModelError as exc:
            log.warning("scan cache entry %s dropped: %s", entry_id, exc)
            return None

    def put_entry(self, entry_id: str, fingerprint: str, captures: tuple[Capture, ...]) -> None:
        self._entries[entry_id] = {"fp": fingerprint,
                                   "captures": [capture_to_json(c) for c in captures]}
        self._dirty = True

    # --- ids ------------------------------------------------------------------

    def data_id(self, vid: str) -> int | None:
        """The id found in a version's data (0 = it has none), or None when not looked up."""
        hit = self._ids.get(vid)
        return int(hit, 16) if hit is not None else None

    def put_data_id(self, vid: str, ident: int) -> None:
        self._ids[vid] = f"{ident:X}" if ident else "0"
        self._dirty = True

    def legacy_pid(self, vid: str) -> str | None:
        """The legacy pid a version got when first seen: it names the slot it was in then,
        so a save without an id keeps its line when it moves."""
        return self._legacy.get(vid)

    def put_legacy_pid(self, vid: str, pid: str) -> None:
        if self._legacy.get(vid) != pid:
            self._legacy[vid] = pid
            self._dirty = True

    # --- upkeep ---------------------------------------------------------------

    def prune(self, paths: set[str], entry_ids: set[str]) -> None:
        """After a full scan: forget files and entries that were not seen. Ids stay: the id
        of given bytes never changes."""
        for key in [k for k in self._files if k not in paths]:
            del self._files[key]
            self._dirty = True
        for key in [k for k in self._entries if k not in entry_ids]:
            del self._entries[key]
            self._dirty = True

    def save(self) -> None:
        """Write the cache if anything changed. A failure is logged, never raised: the cache
        is only an optimisation."""
        if self.path is None or self.read_only or not self._dirty:
            return
        data = {"schema": SCHEMA, "vault_id": self.vault_id, "files": self._files,
                "entries": self._entries, "ids": self._ids, "legacy": self._legacy}
        try:
            safety.atomic_write_bytes(
                self.path, json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        except OSError as exc:
            log.warning("scan cache not saved: %s", exc.strerror)
            return
        self._dirty = False


def _file_ok(value: object) -> bool:
    return (isinstance(value, list) and len(value) == 5 and isinstance(value[0], int)
            and isinstance(value[1], int) and is_vid(value[2])
            and (value[3] is None or isinstance(value[3], bool)) and isinstance(value[4], str))


def _is_legacy(value: object) -> bool:
    try:
        return isinstance(value, str) and pid_kind(value) == "legacy"
    except ModelError:
        return False
