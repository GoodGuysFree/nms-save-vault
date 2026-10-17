"""Checking the version store (D3 2.11): quick, deep, and repair.

* quick: FORMAT readable; every manifest parses with a matching checksum (inline payloads
  included); every referenced object is there with its size; temp leftovers; objects no entry
  references; ``state/migration.json`` agrees with ``entries/``.
* deep: quick, plus every object re-hashed; every Steam meta decrypts with its recorded ordinal
  and its member's validity comes out as recorded; every Xbox index parses and names its
  members.
* repair: a missing or corrupt object is looked for among files of the right size in the old
  backup folders, hand-made in-place folders, live folders, the pre-upgrade copy and any folder
  the owner names; bytes with the right sha256 are stored again (a corrupt object is
  quarantined first, never deleted). What cannot be repaired is listed in
  ``state/damage.json``. Own temp leftovers older than an hour are removed.

fsck never deletes an object or a manifest.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import catalog, lz4_block, msstore
from .. import meta as metacrypt
from . import capture
from .errors import ObjectCorrupt, ObjectMissing, StoreError
from .listing import read_migration
from .manifest import FileEntry, StoreEntry
from .objects import sha256
from .versionstore import STATE, TMP, read_format

if TYPE_CHECKING:
    from .versionstore import VersionStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FsckProblem:
    kind: str           # format, manifest, missing, corrupt, member, index, migration
    entry_id: str
    path: str
    digest: str
    detail: str         # ASCII, for the owner
    repaired: bool = False


@dataclass(frozen=True)
class FsckReport:
    deep: bool
    entries: int
    objects: int
    unreferenced: int             # objects no entry references (an interrupted capture's)
    tmp_leftovers: int
    problems: tuple[FsckProblem, ...]

    @property
    def ok(self) -> bool:
        return all(p.repaired for p in self.problems)

    @property
    def repaired(self) -> int:
        return sum(1 for p in self.problems if p.repaired)


def fsck(store: "VersionStore", *, deep: bool = False, repair: bool = False,
         fallbacks: Sequence[Path] = ()) -> FsckReport:
    if repair:
        store._require_writable()
        store._sweep_tmp()
    problems: list[FsckProblem] = []
    try:
        read_format(store.root)
    except StoreError as exc:
        return FsckReport(deep, 0, 0, 0, 0, (FsckProblem("format", "", "FORMAT", "", str(exc)),))
    for name, why in sorted(store.corrupt_manifests.items()):
        problems.append(FsckProblem("manifest", name[:-5], "", "", why))
        store.record_damage(name[:-5], None, "manifest", why)
    entries = store.entries()
    refs: dict[str, list[tuple[StoreEntry, str, int]]] = defaultdict(list)
    for entry in entries:
        for fe in entry.files:
            if fe.inline is None:
                refs[fe.sha256].append((entry, fe.path, fe.size))
    present = set(store.objects.digests())

    bad: dict[str, tuple[str, str]] = {}           # digest -> (kind, detail)
    for digest, holders in refs.items():
        size = holders[0][2]
        on_disk = store.objects.size_of(digest)
        if on_disk is None:
            bad[digest] = ("missing", "the stored copy is missing")
        elif on_disk != size:
            bad[digest] = ("corrupt", f"{on_disk} bytes instead of {size}")
        elif deep:
            try:
                store.objects.get(digest, size)
            except ObjectMissing:
                bad[digest] = ("missing", "the stored copy is missing")
            except ObjectCorrupt as exc:
                bad[digest] = ("corrupt", exc.detail)
            except (StoreError, OSError) as exc:
                bad[digest] = ("corrupt", f"the stored copy cannot be read ({exc})")

    repaired: set[str] = set()
    failed: dict[str, str] = {}
    if repair and bad:
        repaired, failed = _repair(store, {d: refs[d][0][2] for d in bad}, bad, fallbacks)
    for digest, (kind, detail) in sorted(bad.items()):
        if digest in failed:
            detail = f"{detail}; the repair failed: {failed[digest]}"
        for entry, path, _size in refs[digest]:
            fixed = digest in repaired
            problems.append(FsckProblem(kind, entry.id, path, digest,
                                        "re-stored from a good copy" if fixed else detail, fixed))
            if not fixed:
                store.record_damage(entry.id, entry.file(path), kind, detail)
    if deep:
        # The objects were re-hashed just above: the member check reads them without hashing.
        problems += _check_members(store, entries, set(bad) - repaired)
    problems += _check_migration(store)

    tmp_left = sum(1 for p in _list(store.root / TMP)
                   if p.name.endswith(".part") or (p.name.startswith("mat-") and p.is_dir()))
    return FsckReport(deep, len(entries), len(present | repaired),
                      len(present - set(refs)), tmp_left, tuple(problems))


def _check_members(store: "VersionStore", entries: Iterable[StoreEntry],
                   skip: set[str]) -> list[FsckProblem]:
    """Deep: what the records say about each save still follows from the stored bytes."""
    problems = []
    checked: dict[tuple[str, str, int], bool] = {}
    for entry in entries:
        if entry.platform == "xbox" and entry.xbox is not None:
            problems += _check_index(store, entry)
        for m in entry.members:
            data_fe, meta_fe = entry.file(m.data), entry.file(m.meta)
            if entry.platform != "steam" or m.ordinal is None or {data_fe.sha256,
                                                                  meta_fe.sha256} & skip:
                continue
            key = (data_fe.sha256, meta_fe.sha256, m.ordinal)
            try:
                if key not in checked:
                    checked[key] = _steam_valid(_raw(store, meta_fe), m.ordinal,
                                                _raw(store, data_fe))
                valid = checked[key]
            except (OSError, ValueError) as exc:
                valid = None
                detail = str(exc)
            else:
                detail = "the save does not check out as it did when it was kept"
            if valid is not m.valid:
                problems.append(FsckProblem("member", entry.id, m.data, m.vid,
                                            f"slot {m.slot}{m.member}: {detail}"))
    return problems


def _raw(store: "VersionStore", fe: FileEntry) -> bytes:
    """A file's bytes already verified by this run: inline, or its object read as it is."""
    return fe.inline if fe.inline is not None else store.objects.path_for(fe.sha256).read_bytes()


def _steam_valid(raw_meta: bytes, ordinal: int, data: bytes) -> bool:
    info = metacrypt.parse(metacrypt.decrypt(raw_meta, ordinal), ordinal)
    if not info.valid:
        raise ValueError("the meta does not decrypt with its recorded ordinal")
    return capture.steam_valid(info, lz4_block.stats(data))


def _check_index(store: "VersionStore", entry: StoreEntry) -> list[FsckProblem]:
    path = entry.xbox["index"]
    try:
        _header, records, _trailer = msstore.split_index(_raw(store, entry.file(path)))
    except (OSError, ValueError) as exc:
        return [FsckProblem("index", entry.id, path, entry.file(path).sha256,
                            f"the Xbox save index does not parse ({exc})")]
    named = {identifier for identifier, _rec in records}
    return [FsckProblem("index", entry.id, path, entry.file(path).sha256,
                        f"the Xbox save index does not name {m.xbox_identifier}")
            for m in entry.members if m.xbox_identifier not in named]


def _check_migration(store: "VersionStore") -> list[FsckProblem]:
    """Every old backup the fold reports as folded is a store entry."""
    path = store.root / STATE / "migration.json"
    try:
        d = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        return [FsckProblem("migration", "", "state/migration.json", "", f"unreadable ({exc})")]
    fold = d.get("fold") if isinstance(d, dict) else None
    states = fold.get("entries") if isinstance(fold, dict) else None
    if not isinstance(states, dict):
        return []
    return [FsckProblem("migration", entry_id, "", "",
                        "recorded as folded but the store has no such entry")
            for entry_id, rec in sorted(states.items())
            if isinstance(rec, dict) and rec.get("status") in ("folded", "verified")
            and not store.has_entry(entry_id)]


# --- repair -----------------------------------------------------------------------------------


def _repair(store: "VersionStore", sizes: dict[str, int], bad: dict[str, tuple[str, str]],
            fallbacks: Sequence[Path]) -> tuple[set[str], dict[str, str]]:
    """Re-store every digest of ``sizes`` found among the candidate folders. Returns the
    digests fixed and, for each whose re-store failed, why (the others are left as they are)."""
    wanted_sizes = set(sizes.values())
    by_size: dict[int, list[Path]] = defaultdict(list)
    own = store.root.resolve()
    for root in candidate_folders(store, fallbacks):
        for dirpath, dirs, names in os.walk(root):
            if Path(dirpath).resolve() == own:
                dirs[:] = []            # never the store's own objects or quarantine
                continue
            for name in names:
                p = Path(dirpath) / name
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size in wanted_sizes:
                    by_size[size].append(p)
    fixed: set[str] = set()
    failed: dict[str, str] = {}
    for digest, size in sizes.items():
        for candidate in by_size.get(size, ()):
            try:
                data = candidate.read_bytes()
            except OSError:
                continue
            if sha256(data) != digest:
                continue
            try:
                if bad[digest][0] == "corrupt" and store.objects.has(digest):
                    store.objects.quarantine(digest, "fsck found it corrupt")
                store.objects.put(data, digest)
            except (OSError, StoreError) as exc:     # one failure never stops the check
                failed[digest] = str(exc)
                log.warning("fsck could not re-store %s: %s", digest[:12], exc)
                break
            fixed.add(digest)
            log.info("fsck re-stored %s from a copy outside the store", digest[:12])
            break
    return fixed, failed


def candidate_folders(store: "VersionStore", fallbacks: Sequence[Path]) -> list[Path]:
    """Where good copies of stored files may be: the old backup folders, hand-made in-place
    folders, the live folders, the pre-upgrade copy, and the owner's own folders. Never the
    store itself."""

    vault = catalog.Vault(store.vault_root)
    roots = [vault.backups_dir, vault.snapshots_dir, vault.extracts_dir]
    try:
        roots += [Path(e.path) for e in vault.entries if not e.managed]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("fsck could not read the vault catalog for in-place folders: %s", exc)
    roots += list(store.live_folders())
    copy = read_migration(store.vault_root).get("copy_aside")
    if isinstance(copy, dict) and isinstance(copy.get("name"), str):
        roots.append(store.vault_root.parent / copy["name"])
    roots += [Path(f) for f in fallbacks]
    out: list[Path] = []
    for root in roots:
        if root.is_dir() and root.resolve() not in out and not _inside(root, store.root):
            out.append(root.resolve())
    return out


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _list(folder: Path) -> list[Path]:
    try:
        return list(folder.iterdir())
    except OSError:
        return []
