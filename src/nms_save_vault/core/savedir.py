"""Read-only model of an NMS save folder: slots, their two members, and decoded metas.

Tolerant by design: handles missing files, partially-present slots, and metas that were
manually moved between slots (decrypts with the file's mapped ordinal first, then falls
back to auto-detection and flags the mismatch).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import formats, meta
from .formats import (
    ACCOUNT_DATA_NAME,
    ACCOUNT_META_NAME,
    ACCOUNT_STORAGE_ORDINAL,
    MAX_SAVE_SLOTS,
)
from .meta import MetaInfo
from .slotmap import SaveFileRef, slot_file_numbers


@dataclass
class MemberView:
    """One save (a slot member: 'A' or 'B') as found on disk."""

    ref: SaveFileRef
    data_path: Path
    meta_path: Path
    exists: bool
    data_size: int = 0
    data_mtime: float = 0.0
    ordinal_used: int | None = None
    info: MetaInfo | None = None
    note: str = ""

    @property
    def slot(self) -> int:
        return self.ref.slot

    @property
    def label(self) -> str:
        return self.ref.member_label

    @property
    def mapped_ordinal(self) -> int:
        return self.ref.storage_ordinal

    @property
    def valid(self) -> bool:
        return self.info is not None and self.info.valid

    @property
    def moved(self) -> bool:
        """True if the meta is encrypted for a different slot than its filename implies."""
        return self.ordinal_used is not None and self.ordinal_used != self.ref.storage_ordinal

    @property
    def save_name(self) -> str:
        return self.info.save_name if self.info else ""

    @property
    def timestamp(self) -> int:
        return self.info.timestamp if self.info else 0


@dataclass
class SlotView:
    slot: int
    a: MemberView
    b: MemberView

    @property
    def members(self) -> tuple[MemberView, MemberView]:
        return (self.a, self.b)

    @property
    def present_members(self) -> list[MemberView]:
        return [m for m in (self.a, self.b) if m.exists]

    @property
    def occupied(self) -> bool:
        return bool(self.present_members)

    @property
    def newest(self) -> MemberView | None:
        """The member the game treats as current: highest meta timestamp, then mtime."""
        candidates = [m for m in self.present_members if m.valid] or self.present_members
        if not candidates:
            return None
        return max(candidates, key=lambda m: (m.timestamp, m.data_mtime))

    @property
    def display_name(self) -> str:
        n = self.newest
        if n and n.save_name:
            return n.save_name
        return "<unnamed>" if self.occupied else "<empty>"


@dataclass
class SaveDirView:
    path: Path
    slots: dict[int, SlotView]
    account_present: bool
    stray_files: list[str] = field(default_factory=list)

    @property
    def occupied_slots(self) -> list[SlotView]:
        return [s for s in self.slots.values() if s.occupied]


def _load_member(folder: Path, file_no: int) -> MemberView:
    ref = SaveFileRef(file_no)
    dp = folder / ref.data_name
    mp = folder / ref.meta_name
    data_exists = dp.is_file()
    meta_exists = mp.is_file()
    mv = MemberView(ref=ref, data_path=dp, meta_path=mp, exists=data_exists and meta_exists)

    if not mv.exists:
        if data_exists != meta_exists:
            mv.note = "incomplete: missing " + ("meta" if data_exists else "data") + " file"
        return mv

    stat = dp.stat()
    mv.data_size = stat.st_size
    mv.data_mtime = stat.st_mtime
    blob = mp.read_bytes()
    try:
        if meta.is_valid_for(blob, ref.storage_ordinal):
            ordinal, plain = ref.storage_ordinal, meta.decrypt(blob, ref.storage_ordinal)
        else:
            ordinal, plain = meta.decrypt_autodetect(blob, ref.storage_ordinal)
        mv.ordinal_used = ordinal
        mv.info = meta.parse(plain, ordinal)
        if ordinal != ref.storage_ordinal:
            mv.note = f"meta keyed for ordinal {ordinal} (moved without re-key)"
    except Exception as exc:  # noqa: BLE001 - report, never raise during a scan
        mv.note = f"meta unreadable: {exc}"
    return mv


def scan(path: str | Path) -> SaveDirView:
    """Scan a folder and return a structured, read-only view of its slots."""
    folder = Path(path)
    slots: dict[int, SlotView] = {}
    for slot in range(1, MAX_SAVE_SLOTS + 1):
        fa, fb = slot_file_numbers(slot)
        slots[slot] = SlotView(slot=slot, a=_load_member(folder, fa), b=_load_member(folder, fb))
    account = (folder / ACCOUNT_DATA_NAME).is_file() and (folder / ACCOUNT_META_NAME).is_file()
    return SaveDirView(path=folder, slots=slots, account_present=account)


def looks_like_save_dir(path: str | Path) -> bool:
    """True if the folder contains at least one save meta file (for import validation)."""
    folder = Path(path)
    if not folder.is_dir():
        return False
    return any(folder.glob("mf_save*.hg")) or (folder / ACCOUNT_META_NAME).is_file()
