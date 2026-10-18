"""The vault (managed storage) and the backup catalog.

Hybrid model: pre-existing backups are catalogued *in place*; app-created backups,
auto-snapshots and single-slot extracts live under the vault root. Each entry caches a
per-slot summary (computed by scanning + decoding metas) so the UI is fast.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import formats, safety, savedir, slotmap
from .savedir import SaveDirView

log = logging.getLogger(__name__)

# catalog.json is saved through a temp file named like this. One older than a few minutes is
# a leftover of a save that was interrupted, never a save still in progress.
CATALOG_TEMP_PREFIX = ".catalog.json.tmp_"
STALE_TEMP_SECONDS = 5 * 60

CATALOG_VERSION = 1

# Marks the copy of a vault made before its upgrade to the version store (D3 4.1).
PRE_UPGRADE_COPY = "PRE-UPGRADE-COPY.txt"

# Entry kinds
KIND_FULL = "full"          # app-created full snapshot (managed)
KIND_SNAPSHOT = "snapshot"  # auto pre-operation snapshot (managed)
KIND_EXTRACT = "extract"    # single-slot extract (managed)
KIND_IMPORTED = "imported"  # imported manual backup (copied into vault)
KIND_INPLACE = "inplace"    # catalogued where it already lives


@dataclass
class MemberSummary:
    label: str
    present: bool
    name: str = ""
    summary: str = ""
    game_mode: int = 0
    season: int = 0
    play_time: int = 0
    timestamp: int = 0
    base_version: int = 0
    data_size: int = 0
    valid: bool = False
    moved: bool = False
    note: str = ""
    # Added after the first catalogs were written; entries cached before that default to 0
    # and fall back to game_mode, so an old catalog still shows something sensible.
    difficulty: int = 0

    @property
    def save_type_label(self) -> str:
        """'Auto-Save' / 'Restore-Point' from the stored 'A'/'B' letter."""
        return slotmap.save_type_label_of(self.label)

    @property
    def difficulty_label(self) -> str:
        return formats.difficulty_label(self.difficulty, self.game_mode)


@dataclass
class SlotSummary:
    slot: int
    occupied: bool
    name: str
    newest_label: str | None
    members: list[MemberSummary]


@dataclass
class CatalogEntry:
    id: str
    kind: str
    label: str
    path: str
    created: str
    source: str | None = None
    managed: bool = False
    slots: list[SlotSummary] = field(default_factory=list)
    note: str = ""

    @property
    def occupied_slots(self) -> list[SlotSummary]:
        return [s for s in self.slots if s.occupied]


# --- summary building --------------------------------------------------------


def _member_summary(mv) -> MemberSummary:
    ms = MemberSummary(
        label=mv.label,
        present=mv.exists,
        data_size=mv.data_size,
        valid=mv.valid,
        moved=mv.moved,
        note=mv.note,
    )
    if mv.info is not None:
        ms.name = mv.info.save_name
        ms.summary = mv.info.save_summary
        ms.game_mode = mv.info.game_mode
        ms.season = mv.info.season
        ms.play_time = mv.info.total_play_time
        ms.timestamp = mv.effective_timestamp  # falls back to mtime when the meta has none
        ms.base_version = mv.info.base_version
        ms.difficulty = mv.info.difficulty
    return ms


def _slot_summary(sv) -> SlotSummary:
    newest = sv.newest
    return SlotSummary(
        slot=sv.slot,
        occupied=sv.occupied,
        name=sv.display_name,
        newest_label=(newest.label if newest else None),
        members=[_member_summary(m) for m in sv.members],
    )


def summarize(view: SaveDirView, slots=None) -> list[SlotSummary]:
    keys = sorted(view.slots) if slots is None else sorted(slots)
    return [_slot_summary(view.slots[k]) for k in keys if k in view.slots]


# --- (de)serialization -------------------------------------------------------


def _entry_from_dict(d: dict) -> CatalogEntry:
    slots = [
        SlotSummary(
            slot=s["slot"],
            occupied=s["occupied"],
            name=s["name"],
            newest_label=s.get("newest_label"),
            members=[MemberSummary(**m) for m in s.get("members", [])],
        )
        for s in d.get("slots", [])
    ]
    return CatalogEntry(
        id=d["id"],
        kind=d["kind"],
        label=d.get("label", ""),
        path=d["path"],
        created=d["created"],
        source=d.get("source"),
        managed=d.get("managed", False),
        slots=slots,
        note=d.get("note", ""),
    )


class OplogRead(list):
    """The readable op-log records, oldest first, plus where the log is damaged. ``lines``
    holds each record's line number, so damage can be placed relative to a record."""

    def __init__(self, records=(), damaged_lines=(), lines=()):
        super().__init__(records)
        self.damaged_lines: list[int] = list(damaged_lines)
        self.lines: list[int] = list(lines)

    @property
    def warning(self) -> str:
        """One sentence for the user about damage, or '' when there is none."""
        if not self.damaged_lines:
            return ""
        lines = ", ".join(str(n) for n in self.damaged_lines[:5])
        if len(self.damaged_lines) > 5:
            lines += f" and {len(self.damaged_lines) - 5} more"
        return (
            f"The undo log (oplog.jsonl) has damaged lines ({lines}); they were skipped, so "
            "the operations they recorded cannot be undone."
        )


class Vault:
    """Managed storage root + catalog of all known backups."""

    def __init__(self, root: str | Path, *, read_only: bool = False):
        self.root = Path(root)
        # A read-only vault is only looked at: loading tidies nothing and nothing is saved
        # (the organizer's read-only mode and its checks against a real vault).
        self.read_only = read_only
        self.backups_dir = self.root / "backups"
        self.snapshots_dir = self.root / "snapshots"
        self.extracts_dir = self.root / "extracts"
        self.catalog_path = self.root / "catalog.json"
        self.oplog_path = self.root / "oplog.jsonl"
        self._entries: list[CatalogEntry] = []
        self._loaded = False

    def ensure(self) -> None:
        if self.read_only:
            raise PermissionError(f"the vault at {self.root.name} is opened read-only")
        for d in (self.root, self.backups_dir, self.snapshots_dir, self.extracts_dir):
            d.mkdir(parents=True, exist_ok=True)
        from .store.versionstore import start_new_vault

        start_new_vault(self.root)      # a brand-new vault starts in store mode (D3 4.1)

    def load(self) -> "Vault":
        if not self.read_only:
            self._remove_stale_temp_files()
        if self.catalog_path.is_file():
            data = json.loads(self.catalog_path.read_text("utf-8"))
            self._entries = [_entry_from_dict(e) for e in data.get("entries", [])]
        else:
            self._entries = []
        self._loaded = True
        return self

    def save(self) -> None:
        self.ensure()
        data = {"version": CATALOG_VERSION, "entries": [asdict(e) for e in self._entries]}
        # Temp file, fsync, os.replace: a crash leaves the old catalog or the new one, whole.
        safety.atomic_write_bytes(
            self.catalog_path, json.dumps(data, indent=2).encode("utf-8"), temp_prefix=CATALOG_TEMP_PREFIX
        )

    def _remove_stale_temp_files(self) -> None:
        """Delete temp files an interrupted catalog save left behind (only those, and only
        once they are too old to belong to a save in progress)."""
        if not self.root.is_dir():
            return
        cutoff = datetime.now().timestamp() - STALE_TEMP_SECONDS
        for tmp in self.root.glob(f"{CATALOG_TEMP_PREFIX}*.part"):
            try:
                if tmp.is_file() and tmp.stat().st_mtime < cutoff:
                    tmp.unlink()
                    log.warning("removed %s, left by an interrupted catalog save", tmp.name)
            except OSError as exc:
                log.warning("could not remove %s, left by an interrupted catalog save: %s", tmp.name, exc)

    @property
    def entries(self) -> list[CatalogEntry]:
        if not self._loaded:
            self.load()
        return list(self._entries)

    def get(self, entry_id: str) -> CatalogEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def upsert(self, entry: CatalogEntry) -> None:
        if not self._loaded:
            self.load()
        self._commit([e for e in self._entries if e.id != entry.id] + [entry])

    def remove(self, entry_id: str) -> None:
        if not self._loaded:
            self.load()
        self._commit([e for e in self._entries if e.id != entry_id])

    def _commit(self, entries: list[CatalogEntry]) -> None:
        """Make ``entries`` the catalog and save it. If the save fails, memory goes back to
        what the file still says, so the two never disagree."""
        before = self._entries
        self._entries = entries
        try:
            self.save()
        except BaseException:
            self._entries = before
            raise

    def new_id(
        self, kind: str, when: datetime | None = None, tag: str | None = None, folder: Path | None = None
    ) -> str:
        """A fresh entry id; with ``folder``, also one that names no existing ``folder/<id>``."""
        when = when or datetime.now()
        stamp = f"{when:%Y%m%d-%H%M%S}"
        base = f"{kind}-{tag}-{stamp}" if tag else f"{kind}-{stamp}"
        existing = {e.id for e in self.entries}

        def taken(candidate: str) -> bool:
            return candidate in existing or (folder is not None and (folder / candidate).exists())

        if not taken(base):
            return base
        i = 2
        while taken(f"{base}-{i}"):
            i += 1
        return f"{base}-{i}"

    @staticmethod
    def tag_of(entry_id: str) -> str | None:
        """The tag :meth:`new_id` put into an id ('snapshot-steam-20261017-120000' ->
        'steam'), or None for an id made without one."""
        parts = entry_id.split("-")
        if len(parts) >= 4 and not parts[1].isdigit() and parts[2].isdigit():
            return parts[1]
        return None

    def append_oplog(self, record: dict) -> None:
        """Append one record as one complete line, fsynced, so a crash can lose the record
        but never leave half a line in front of later ones. A line torn by an earlier crash
        is never extended: the record starts on a fresh line. One handle checks the last
        byte and writes, and every byte is written even if the OS takes them in pieces."""
        self.ensure()
        data = (json.dumps(record) + "\n").encode("utf-8")
        with open(self.oplog_path, "a+b", buffering=0) as f:
            size = f.seek(0, os.SEEK_END)
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    data = b"\n" + data
            pending = memoryview(data)
            while pending:                 # appends go to the end whatever the position
                pending = pending[f.write(pending):]
            os.fsync(f.fileno())

    def read_oplog(self) -> OplogRead:
        """Every readable record, oldest first, and where the log is damaged. A line that is
        not a record -- torn by a crash mid-write, invalid UTF-8, or otherwise damaged -- is
        logged and skipped, never raised: one bad line must not make the whole history
        unreadable. Each record's line number is kept, so damage can be placed relative to
        it (undo must not act on an operation older than a damaged line)."""
        if not self.oplog_path.is_file():
            return OplogRead()
        records: list[dict] = []
        record_lines: list[int] = []
        damaged: list[int] = []
        for number, raw in enumerate(self.oplog_path.read_bytes().split(b"\n"), start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
            except ValueError as exc:      # UnicodeDecodeError is a ValueError too
                log.warning("skipped line %d of %s, not a valid record: %s", number, self.oplog_path.name, exc)
                damaged.append(number)
                continue
            if not isinstance(record, dict):
                log.warning("skipped line %d of %s, not a record object", number, self.oplog_path.name)
                damaged.append(number)
                continue
            records.append(record)
            record_lines.append(number)
        return OplogRead(records, damaged, lines=record_lines)

    def make_entry_from_dir(self, entry_id, kind, label, path, *, managed, source=None, slots=None, note="") -> CatalogEntry:
        """Scan ``path`` and build a catalog entry with a cached slot summary."""
        view = savedir.scan_any(path)
        return CatalogEntry(
            id=entry_id,
            kind=kind,
            label=label,
            path=str(Path(path)),
            created=datetime.now().isoformat(timespec="seconds"),
            source=source,
            managed=managed,
            slots=summarize(view, slots),
            note=note,
        )


def looks_like_vault_dir(path: str | Path) -> bool:
    """True if ``path`` is an NMS Save Vault root. The authoritative marker is
    ``catalog.json`` (written the first time the vault gains an entry); a vault copied
    elsewhere carries it along with its ``backups``/``snapshots``/``extracts`` folders."""
    return (Path(path) / "catalog.json").is_file()


def discover_save_dirs(root: str | Path, exclude=(), max_depth: int = 3) -> list[Path]:
    """Find folders under ``root`` (to a bounded depth) that look like NMS save dirs."""
    root = Path(root)
    excluded = {Path(e).resolve() for e in exclude}
    found: list[Path] = []
    root_resolved = root.resolve()
    for dirpath, dirnames, _files in os.walk(root):
        here = Path(dirpath)
        depth = len(here.resolve().relative_to(root_resolved).parts)
        if depth >= max_depth:
            dirnames[:] = []
        if here.resolve() in excluded:
            dirnames[:] = []
            continue
        if here != root and (looks_like_vault_dir(here) or (here / PRE_UPGRADE_COPY).is_file()):
            # A vault's own folders (or a pre-upgrade copy of one, D3 4.1) are its entries,
            # never backups of their own.
            dirnames[:] = []
            continue
        if savedir.looks_like_save_dir(here):
            found.append(here)
            dirnames[:] = []  # don't descend into a save dir
    return found
