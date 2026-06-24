"""Mapping between save slots, on-disk file numbers, filenames and storage ordinals.

Verified against the user's folder:
    save.hg   -> file #1  -> ordinal 2  (PlayerState1)  -> slot 1, member A
    save2.hg  -> file #2  -> ordinal 3  (PlayerState2)  -> slot 1, member B
    save17.hg -> file #17 -> ordinal 18 (PlayerState17) -> slot 9, member A
    save30.hg -> file #30 -> ordinal 31 (PlayerState30) -> slot 15, member B

Rules:
    * file number ``f`` runs 1..30; the bare ``save.hg`` is f==1.
    * storage ordinal (XXTEA key input) = f + 1.
    * slot (1-based) k holds members A (f=2k-1) and B (f=2k).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import formats

_DATA_RE = re.compile(r"^save(\d*)\.hg$", re.IGNORECASE)
_META_RE = re.compile(r"^mf_save(\d*)\.hg$", re.IGNORECASE)

MEMBER_LABELS = ("A", "B")


def data_filename(file_no: int) -> str:
    """File number -> data filename ('save.hg' for #1, else 'saveN.hg')."""
    _check_file_no(file_no)
    return "save.hg" if file_no == 1 else f"save{file_no}.hg"


def meta_filename(file_no: int) -> str:
    """File number -> meta filename ('mf_save.hg' for #1, else 'mf_saveN.hg')."""
    return "mf_" + data_filename(file_no)


def storage_ordinal(file_no: int) -> int:
    """File number -> cTkStoragePersistent::Slot ordinal used for meta XXTEA key."""
    _check_file_no(file_no)
    return file_no + 1


def slot_of(file_no: int) -> int:
    """File number -> 1-based slot index."""
    _check_file_no(file_no)
    return (file_no + 1) // 2


def member_of(file_no: int) -> int:
    """File number -> member index within its slot (0 = 'A', 1 = 'B')."""
    _check_file_no(file_no)
    return (file_no - 1) % 2


def file_no(slot: int, member: int) -> int:
    """(1-based slot, member 0/1) -> file number."""
    if not 1 <= slot <= formats.MAX_SAVE_SLOTS:
        raise ValueError(f"slot out of range 1..{formats.MAX_SAVE_SLOTS}: {slot}")
    if member not in (0, 1):
        raise ValueError(f"member must be 0 or 1: {member}")
    return 2 * (slot - 1) + 1 + member


def member_label(member: int) -> str:
    return MEMBER_LABELS[member]


def parse_data_filename(name: str) -> int | None:
    """'saveN.hg' -> file number, or None if not a save data file."""
    m = _DATA_RE.match(name)
    if not m:
        return None
    return 1 if m.group(1) == "" else int(m.group(1))


def parse_meta_filename(name: str) -> int | None:
    """'mf_saveN.hg' -> file number, or None if not a save meta file."""
    m = _META_RE.match(name)
    if not m:
        return None
    return 1 if m.group(1) == "" else int(m.group(1))


def all_file_numbers() -> range:
    """1..MAX_SAVE_FILES."""
    return range(1, formats.MAX_SAVE_FILES + 1)


def slot_file_numbers(slot: int) -> tuple[int, int]:
    """The (A, B) file numbers for a 1-based slot."""
    return file_no(slot, 0), file_no(slot, 1)


@dataclass(frozen=True)
class SaveFileRef:
    """Identity of one save (a slot member) within a folder, independent of contents."""

    file_no: int

    @property
    def slot(self) -> int:
        return slot_of(self.file_no)

    @property
    def member(self) -> int:
        return member_of(self.file_no)

    @property
    def member_label(self) -> str:
        return member_label(self.member)

    @property
    def storage_ordinal(self) -> int:
        return storage_ordinal(self.file_no)

    @property
    def data_name(self) -> str:
        return data_filename(self.file_no)

    @property
    def meta_name(self) -> str:
        return meta_filename(self.file_no)

    def __str__(self) -> str:  # e.g. "slot 9A (save17.hg)"
        return f"slot {self.slot}{self.member_label} ({self.data_name})"


def _check_file_no(file_no: int) -> None:
    if not 1 <= file_no <= formats.MAX_SAVE_FILES:
        raise ValueError(f"file number out of range 1..{formats.MAX_SAVE_FILES}: {file_no}")
