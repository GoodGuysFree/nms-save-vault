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
from pathlib import Path

from .. import platform as host_platform
from .. import safety
from .model import Capture, ModelError, capture_from_json, capture_to_json, is_vid

log = logging.getLogger(__name__)

SCHEMA = 1
FILENAME = "scan-cache.json"


def cache_dir(vault_root: Path, vault_id: str | None,
              host: host_platform.Host | None = None) -> Path:
    """``<user cache>/vaults/<vault_id>/``, shared by everything per-machine about one vault.
    A vault without an organizer store yet is keyed by its resolved location instead."""
    if vault_id is None:
        digest = hashlib.sha256(str(Path(vault_root).resolve()).encode("utf-8")).hexdigest()
        vault_id = f"nostore-{digest[:16]}"
    return host_platform.user_cache_dir(host) / "vaults" / vault_id


class ScanCache:
    """``path`` None keeps the cache in memory only (``--no-cache``)."""

    def __init__(self, path: Path | None, vault_id: str | None) -> None:
        self.path = path
        self.vault_id = vault_id
        self._files: dict[str, list] = {}        # path -> [size, mtime_ns, vid, ok]
        self._entries: dict[str, dict] = {}      # entry id -> {"fp", "captures"}
        self._ids: dict[str, str] = {}           # vid -> data id hex, "0" for none
        self._dirty = False

    @classmethod
    def load(cls, path: Path, vault_id: str | None) -> ScanCache:
        cache = cls(path, vault_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cache
        except OSError as exc:
            log.warning("scan cache not read, starting afresh: %s", exc)
            return cache
        try:
            data = json.loads(raw.decode("utf-8"))
            if data.get("schema") != SCHEMA or data.get("vault_id") != vault_id:
                return cache
            cache._files = {k: v for k, v in data["files"].items() if _file_ok(v)}
            cache._entries = {k: v for k, v in data["entries"].items()
                              if isinstance(v, dict) and isinstance(v.get("fp"), str)
                              and isinstance(v.get("captures"), list)}
            cache._ids = {k: v for k, v in data["ids"].items()
                          if is_vid(k) and isinstance(v, str)}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("scan cache damaged, starting afresh: %s", exc)
            return cls(path, vault_id)
        return cache

    # --- files ----------------------------------------------------------------

    def file(self, path: Path, size: int, mtime_ns: int) -> tuple[str, bool | None] | None:
        """``(vid, validated)`` for a file that has not changed, else None. ``validated`` is
        None when the file was never checked (entry copies are not)."""
        hit = self._files.get(str(path))
        if hit is None or hit[0] != size or hit[1] != mtime_ns:
            return None
        return hit[2], hit[3]

    def put_file(self, path: Path, size: int, mtime_ns: int, vid: str, ok: bool | None) -> None:
        value = [size, mtime_ns, vid, ok]
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

    # --- data ids -------------------------------------------------------------

    def data_id(self, vid: str) -> int | None:
        """The id found in a version's data (0 = it has none), or None when not looked up."""
        hit = self._ids.get(vid)
        return int(hit, 16) if hit is not None else None

    def put_data_id(self, vid: str, ident: int) -> None:
        self._ids[vid] = f"{ident:X}" if ident else "0"
        self._dirty = True

    # --- upkeep ---------------------------------------------------------------

    def prune(self, paths: set[str], entry_ids: set[str]) -> None:
        """After a full scan: forget files and entries that were not seen."""
        for key in [k for k in self._files if k not in paths]:
            del self._files[key]
            self._dirty = True
        for key in [k for k in self._entries if k not in entry_ids]:
            del self._entries[key]
            self._dirty = True

    def save(self) -> None:
        """Write the cache if anything changed. A failure is logged, never raised: the cache
        is only an optimisation."""
        if self.path is None or not self._dirty:
            return
        data = {"schema": SCHEMA, "vault_id": self.vault_id, "files": self._files,
                "entries": self._entries, "ids": self._ids}
        try:
            safety.atomic_write_bytes(
                self.path, json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        except OSError as exc:
            log.warning("scan cache not saved: %s", exc)
            return
        self._dirty = False


def _file_ok(value: object) -> bool:
    return (isinstance(value, list) and len(value) == 4 and isinstance(value[0], int)
            and isinstance(value[1], int) and is_vid(value[2])
            and (value[3] is None or isinstance(value[3], bool)))
