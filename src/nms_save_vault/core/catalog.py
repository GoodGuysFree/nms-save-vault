"""The vault (managed storage) and the backup catalog.

Hybrid model: pre-existing backups are catalogued *in place*; app-created backups,
auto-snapshots and single-slot extracts live under the vault root. Each entry caches a
per-slot summary (computed by scanning + decoding metas) so the UI is fast.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import savedir
from .savedir import SaveDirView

CATALOG_VERSION = 1

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
        ms.timestamp = mv.effective_timestamp  # falls back to mtime for Xbox saves
        ms.base_version = mv.info.base_version
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


class Vault:
    """Managed storage root + catalog of all known backups."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.backups_dir = self.root / "backups"
        self.snapshots_dir = self.root / "snapshots"
        self.extracts_dir = self.root / "extracts"
        self.catalog_path = self.root / "catalog.json"
        self.oplog_path = self.root / "oplog.jsonl"
        self._entries: list[CatalogEntry] = []
        self._loaded = False

    def ensure(self) -> None:
        for d in (self.root, self.backups_dir, self.snapshots_dir, self.extracts_dir):
            d.mkdir(parents=True, exist_ok=True)

    def load(self) -> "Vault":
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
        tmp = self.catalog_path.with_name(self.catalog_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.replace(tmp, self.catalog_path)

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
        self._entries = [e for e in self._entries if e.id != entry.id]
        self._entries.append(entry)
        self.save()

    def remove(self, entry_id: str) -> None:
        if not self._loaded:
            self.load()
        self._entries = [e for e in self._entries if e.id != entry_id]
        self.save()

    def new_id(self, kind: str, when: datetime | None = None, tag: str | None = None) -> str:
        when = when or datetime.now()
        stamp = f"{when:%Y%m%d-%H%M%S}"
        base = f"{kind}-{tag}-{stamp}" if tag else f"{kind}-{stamp}"
        existing = {e.id for e in self.entries}
        if base not in existing:
            return base
        i = 2
        while f"{base}-{i}" in existing:
            i += 1
        return f"{base}-{i}"

    def append_oplog(self, record: dict) -> None:
        self.ensure()
        with open(self.oplog_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def read_oplog(self) -> list[dict]:
        if not self.oplog_path.is_file():
            return []
        out = []
        for line in self.oplog_path.read_text("utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

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
        if savedir.looks_like_save_dir(here):
            found.append(here)
            dirnames[:] = []  # don't descend into a save dir
    return found
