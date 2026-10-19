"""Merging another vault's version store into this one (D3 4.8, the import of a store-mode
vault): every object an entry needs is copied and verified, then its manifest is committed. The
source is only read. An id this vault already uses for other contents is imported as
``<id>-m<n>`` and reported; importing the same source again adds nothing.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ... import __version__
from .errors import StoreError, StoreNotUpgraded
from .manifest import StoreEntry
from .versionstore import VersionStore, vault_lock, vault_mode

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeProgress:
    entries_done: int
    entries_total: int
    current: str


@dataclass(frozen=True)
class MergeReport:
    added: tuple[str, ...]                       # ids of the new entries in this vault
    renamed: tuple[tuple[str, str], ...]         # (id in the source, id here)
    same: tuple[str, ...]                        # already here: nothing added
    failed: tuple[tuple[str, str], ...]          # (id, ASCII reason); nothing of them committed
    cancelled: bool


def open_source(source_root: str | Path) -> VersionStore:
    """The store of the vault to merge from, opened only to look. StoreNotUpgraded when that
    vault has no store (or is being upgraded)."""
    root = Path(source_root)
    if vault_mode(root) != "store":
        raise StoreNotUpgraded("That vault has no version store, or is being upgraded.")
    return VersionStore.open(root, lock=vault_lock(root), app_version=__version__,
                             live_roots=(), read_only=True)


def merge_store(target: VersionStore, source: VersionStore, *,
                progress: Callable[[MergeProgress], None] | None = None,
                cancel: threading.Event | None = None) -> MergeReport:
    """Copy every entry of ``source`` this vault does not have. One entry that cannot be
    copied (a damaged object or manifest in the source) is reported and the merge goes on."""
    target._require_writable()
    entries = source.entries()
    added: list[str] = []
    renamed: list[tuple[str, str]] = []
    same: list[str] = []
    failed: list[tuple[str, str]] = [
        (name[:-5], why) for name, why in sorted(source.corrupt_manifests.items())]
    cancelled = False
    for n, entry in enumerate(entries):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if progress is not None:
            progress(MergeProgress(n, len(entries), entry.id))
        try:
            kept, status = _merge_one(target, source, entry)
        except StoreError as exc:
            log.warning("could not merge %s: %s", entry.id, exc)
            failed.append((entry.id, str(exc)))
            continue
        if status == "same":
            same.append(entry.id)
        else:
            added.append(kept.id)
            if status == "renamed":
                renamed.append((entry.id, kept.id))
    return MergeReport(tuple(added), tuple(renamed), tuple(same), tuple(failed), cancelled)


def _merge_one(target: VersionStore, source: VersionStore, entry: StoreEntry):
    """Objects first (read verified from the source, written verified here), manifest last."""
    if target.merged_copy(entry, source.store_id) is not None:
        return target.add_merged(entry, source_store_id=source.store_id)   # "same"
    wanted = {fe.sha256: fe.size for fe in entry.files if fe.inline is None}
    target._require_space(sum(size for digest, size in wanted.items()
                              if target.objects.size_of(digest) != size))
    for digest, size in wanted.items():
        # Also the ones already here: put re-hashes an existing object and heals a bad one
        # from the good copy read (verified) from the source.
        target.objects.put(source.objects.get(digest, size), digest)
    return target.add_merged(entry, source_store_id=source.store_id)
