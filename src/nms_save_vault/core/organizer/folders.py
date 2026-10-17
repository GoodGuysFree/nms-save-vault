"""The read side over folders (D1 2.11, D2's ``FolderLocator``): live folders and catalog
entry folders, as the last scan saw them. It reads; it never keeps anything (``preserve``
belongs to the version store, phase 2)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .. import meta as metacrypt
from .. import safety, savedir
from .locator import (
    LiveMember,
    Location,
    OpenedVersion,
    PreserveFailed,
    VersionLocator,
    VersionUnavailable,
)
from .model import EntryRef, LiveRef, SourceRef, StoreRef, Vid
from .scanner import ScanResult

_ORDER = {"store": 0, "durable": 1, "external": 2, "live": 3}


class FolderLocator(VersionLocator):
    """``folders`` maps each live folder and entry the scan saw to its folder on this
    machine."""

    def __init__(self, scan: ScanResult, folders: Mapping[SourceRef, Path]) -> None:
        self.scan = scan
        self.folders = dict(folders)

    def locate(self, vid: Vid) -> list[Location]:
        info = self.scan.versions.get(vid)
        if info is None:
            return []
        out = []
        for loc in info.locations:
            if isinstance(loc.where, StoreRef):
                continue
            kind = "live" if isinstance(loc.where, LiveRef) else "entry"
            durable = loc.managed and loc.entry_kind not in ("live", "inplace")
            out.append(Location(vid, kind, loc.where, loc.slot, loc.member, info.platform,
                                durable, external=loc.entry_kind == "inplace"))
        return sorted(out, key=_rank)

    def open(self, vid: Vid, prefer: Location | None = None) -> OpenedVersion:
        candidates = self.locate(vid)
        if prefer is not None:
            candidates = [prefer] + [c for c in candidates if c != prefer]
        problems = []
        for loc in candidates:
            folder = self.folders.get(loc.ref)
            if folder is None:
                continue
            try:
                return self._read(vid, loc, folder)
            except (OSError, ValueError) as exc:
                problems.append(f"{_name(loc.ref)}: {exc}")
        raise VersionUnavailable("; ".join(problems) or "no readable copy")

    @staticmethod
    def _read(vid: Vid, loc: Location, folder: Path) -> OpenedVersion:
        view = savedir.scan_any(folder)
        mv = view.slots[loc.slot].members["AB".index(loc.member)]
        if not mv.exists:
            raise ValueError("the save is no longer there")
        data = mv.data_path.read_bytes()
        if safety.sha256_bytes(data) != vid:
            raise ValueError("the save there has changed")
        blob = mv.meta_path.read_bytes()
        if loc.platform == "steam":
            if mv.ordinal_used is None:
                raise ValueError("its meta cannot be read")
            plain = metacrypt.decrypt(blob, mv.ordinal_used)
        else:
            plain = blob
        meta_format = mv.info.meta_format if mv.info else 0
        return OpenedVersion(data, plain, loc.platform, meta_format, loc)

    def preserve(self, members: Sequence[LiveMember], *, op_id: str,
                 reason: str) -> list[Location]:
        raise PreserveFailed("store-unavailable")

    def is_durable(self, vid: Vid) -> bool:
        return any(loc.durable for loc in self.locate(vid))


def _rank(loc: Location) -> tuple:
    group = ("store" if isinstance(loc.ref, StoreRef) else "durable" if loc.durable
             else "external" if loc.external else "live")
    return (_ORDER[group], str(loc.ref), loc.slot, loc.member)


def _name(ref: SourceRef) -> str:
    if isinstance(ref, EntryRef):
        return ref.entry_id
    if isinstance(ref, LiveRef):
        return f"{ref.platform} live folder"
    return ref.key
