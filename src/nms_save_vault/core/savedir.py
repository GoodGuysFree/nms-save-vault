"""Read-only model of an NMS save folder: slots, their two members, and decoded metas.

Tolerant by design: handles missing files, partially-present slots, and metas that were
manually moved between slots (decrypts with the file's mapped ordinal first, then falls
back to auto-detection and flags the mismatch).
"""
from __future__ import annotations

import hashlib
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
    # Xbox/Microsoft only: the wgs on-disk identity (msstore.XboxBlobInfo), retained so a
    # future writer can rewrite the save in place. None for Steam saves. Typed loosely to
    # avoid a circular import with msstore.
    xbox: object | None = None
    # sha256 hex of the decrypted meta (Xbox: the meta as stored); "" when it is unreadable.
    # One save's data can sit beside several metas (promote and moves re-stamp them).
    meta_sha256: str = ""

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
    def save_type_label(self) -> str:
        """'Auto-Save' or 'Restore-Point' -- what the game uses this member for."""
        return self.ref.save_type_label

    @property
    def cloud_status(self) -> str:
        """Xbox only: this save's cloud sync state, or '' when not knowable.

        Steam is deliberately blank: ``steam_autocloud.vdf`` carries only an account id and
        Steam Cloud syncs the folder as a unit, so there is no per-save state to report.
        """
        state = getattr(self.xbox, "sync_state", None)
        return formats.MS_SYNC_STATE_NAMES.get(state, "") if state is not None else ""

    @property
    def save_name(self) -> str:
        return self.info.save_name if self.info else ""

    @property
    def timestamp(self) -> int:
        """The meta save time; 0 when the meta has none (e.g. a 280-byte Xbox meta)."""
        return self.info.timestamp if self.info else 0

    @property
    def effective_timestamp(self) -> int:
        """When this save was made: the meta save time, else the file time (Xbox: the
        containers.index time). For showing, storing and comparing saves across slots and
        folders; which member of one slot is newer is :meth:`SlotView.order_time`'s call."""
        return self.timestamp or int(self.data_mtime)


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

    def _candidates(self) -> list[MemberView]:
        return [m for m in self.present_members if m.valid] or self.present_members

    def order_time(self, m: MemberView) -> float:
        """The time :attr:`newest` compares for ``m`` -- only for choosing the newer member of
        this slot: the meta save time when every candidate has one, else the file time (Xbox:
        the containers.index time) for both.

        Never one clock against the other: the index time is the local write or sync time,
        often hours or days after the save time, so mixing them picks the wrong member. To
        show a member's time or compare saves across slots, use ``effective_timestamp``.
        """
        if all(c.timestamp for c in self._candidates()):
            return m.timestamp or m.data_mtime   # only a non-candidate can lack one here
        return m.data_mtime

    @property
    def newest(self) -> MemberView | None:
        """The member the game treats as current: the later :meth:`order_time`."""
        candidates = self._candidates()
        if not candidates:
            return None
        return max(candidates, key=self.order_time)

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
    # Steam only: the folder carries steam_autocloud.vdf, so Steam Cloud is syncing it.
    # Folder-level by nature -- Steam records no per-save state anywhere on disk.
    steam_cloud: bool = False
    stray_files: list[str] = field(default_factory=list)
    # Xbox/Microsoft only: containers.index header identity (msstore.XboxIndexInfo). None
    # for Steam folders.
    xbox_index: object | None = None

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
        if mv.info.valid:
            mv.meta_sha256 = hashlib.sha256(plain).hexdigest()
        if ordinal != ref.storage_ordinal:
            mv.note = f"meta keyed for ordinal {ordinal} (moved without re-key)"
    except Exception as exc:  # noqa: BLE001 - report, never raise during a scan
        mv.note = f"meta unreadable: {exc}"
    return mv


def scan(path: str | Path) -> SaveDirView:
    """Scan a folder and return a structured, read-only view of its slots."""
    folder = Path(path)
    slots = {slot: scan_slot(folder, slot) for slot in range(1, MAX_SAVE_SLOTS + 1)}
    account = (folder / ACCOUNT_DATA_NAME).is_file() and (folder / ACCOUNT_META_NAME).is_file()
    return SaveDirView(
        path=folder,
        slots=slots,
        account_present=account,
        steam_cloud=(folder / formats.STEAM_AUTOCLOUD).is_file(),
    )


def scan_slot(path: str | Path, slot: int) -> SlotView:
    """One slot of a Steam-style folder, without reading the other fourteen."""
    fa, fb = slot_file_numbers(slot)
    return SlotView(slot=slot, a=_load_member(Path(path), fa), b=_load_member(Path(path), fb))


def looks_like_save_dir(path: str | Path) -> bool:
    """True if the folder is a Steam-style or Microsoft-style NMS save folder."""
    folder = Path(path)
    if not folder.is_dir():
        return False
    if (folder / "containers.index").is_file() or _is_xgs(folder):  # Microsoft / Xbox Game Pass
        return True
    return any(folder.glob("mf_save*.hg")) or (folder / ACCOUNT_META_NAME).is_file()


def _is_xgs(folder: Path) -> bool:
    from . import msstore  # lazy import avoids a circular dependency

    return msstore.is_xgs_save_dir(folder)


def platform_of(path: str | Path) -> str:
    """Identify a save folder's platform for labelling: ``"xbox"`` for a Microsoft /
    Game Pass ``wgs`` folder (it has a ``containers.index``) or an ``xgs`` account folder,
    otherwise ``"steam"``."""
    folder = Path(path)
    return "xbox" if (folder / "containers.index").is_file() or _is_xgs(folder) else "steam"


def scan_any(path: str | Path) -> SaveDirView:
    """Scan a folder regardless of platform (Steam ``save*.hg``, Microsoft ``wgs`` or
    ``xgs``)."""
    folder = Path(path)
    if (folder / "containers.index").is_file():
        from . import msstore  # lazy import avoids a circular dependency

        return msstore.scan(folder)
    if _is_xgs(folder):
        from . import msstore

        return msstore.scan_xgs(folder)
    return scan(folder)
