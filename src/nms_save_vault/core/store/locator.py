"""``StoreLocator``: D1's ``VersionLocator`` over the version store (D3 2.7, 2.8).

A store location is ``(StoreRef(entry id), slot, member)`` of an entry that holds the version,
because the meta (and its Steam ordinal) belongs to that entry. Every stored version is durable:
nothing removes entries or objects. Reads verify everything and never write store data; a
failed read is noted in ``state/damage.json`` and the next location is tried.
"""
from __future__ import annotations

import logging
import struct
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .. import formats
from .. import meta as metacrypt
from ..organizer.model import LiveTarget, StoreRef, Vid
from .d1 import Location, LiveMember, OpenedVersion, PreserveFailed, VersionLocator, VersionUnavailable
from .errors import EntryCorrupt, ObjectCorrupt, ObjectMissing
from .objects import sha256

if TYPE_CHECKING:
    from .versionstore import VersionStore

log = logging.getLogger(__name__)


class StoreLocator(VersionLocator):
    def __init__(self, store: "VersionStore", *,
                 live_dir: Callable[[LiveTarget], Path]) -> None:
        self.store = store
        self.live_dir = live_dir

    def locate(self, vid: Vid) -> list[Location]:
        """Every entry, slot and member holding ``vid``, newest entry first."""
        out = []
        for entry_id, slot, member in self.store.vid_refs(vid):
            entry = self.store.entry(entry_id)
            out.append(Location(vid, "store", StoreRef(entry_id), slot, member,  # type: ignore[arg-type]
                                entry.platform, durable=True))
        return out

    def open(self, vid: Vid, prefer: Location | None = None) -> OpenedVersion:
        """The version's bytes and its meta from ``prefer`` (when it is a store location),
        else from the newest entry holding it; every byte verified. A location of another
        kind in ``prefer`` is left to its own reader."""
        candidates = self.locate(vid)
        if prefer is not None and prefer.kind == "store" and isinstance(prefer.ref, StoreRef):
            candidates = [prefer] + [c for c in candidates if c != prefer]
        problems = []
        for loc in candidates:
            try:
                return self._read(vid, loc)
            except VersionUnavailable as exc:
                problems.append(f"{loc.ref.key} slot {loc.slot}{loc.member}: {exc}")
        raise VersionUnavailable(f"version {vid[:8]} is not readable from the store"
                                 + (": " + "; ".join(problems) if problems else ""))

    def _read(self, vid: Vid, loc: Location) -> OpenedVersion:
        try:
            entry = self.store.entry(loc.ref.key)
        except (KeyError, EntryCorrupt) as exc:
            raise VersionUnavailable(f"entry {loc.ref.key}: {exc}") from exc
        m = entry.member(loc.slot, loc.member)
        if m is None or m.vid != vid:
            raise VersionUnavailable(f"entry {entry.id} has no such member")
        if entry.platform == "steam" and m.ordinal is None:
            raise VersionUnavailable("its meta was unreadable when it was kept")
        current = None
        try:
            current = entry.file(m.data)
            data = self.store.file_bytes(current)
            current = entry.file(m.meta)
            raw_meta = self.store.file_bytes(current)
            plain = _plain_meta(entry.platform, raw_meta, m.ordinal, current.sha256)
            if m.meta_plain_sha256 is not None and sha256(plain) != m.meta_plain_sha256:
                raise ObjectCorrupt(current.sha256, "the meta does not decrypt to what was kept")
        except (ObjectMissing, ObjectCorrupt) as exc:
            self.store.record_damage(entry.id, current,
                                     "missing" if isinstance(exc, ObjectMissing) else "corrupt",
                                     exc.detail)
            raise
        return OpenedVersion(data, plain, entry.platform, m.meta_format, loc)

    def is_durable(self, vid: Vid) -> bool:
        """True when an entry holds ``vid`` and its bytes are there with the right size (inline,
        or an object of that size)."""
        for entry_id, slot, member in self.store.vid_refs(vid):
            m = self.store.entry(entry_id).member(slot, member)    # type: ignore[arg-type]
            fe = self.store.entry(entry_id).file(m.data)
            if fe.inline is not None or self.store.objects.size_of(fe.sha256) == fe.size:
                return True
        return False

    def preserve(self, members: Sequence[LiveMember], *, op_id: str,
                 reason: str) -> list[Location]:
        raise PreserveFailed("keeping live saves in the store is not available yet")


def _plain_meta(platform: str, raw: bytes, ordinal: int | None, digest: str) -> bytes:
    """A Steam meta decrypted with the ordinal it was kept with (its header must come out
    right); an Xbox meta as stored."""
    if platform != "steam":
        return raw
    try:
        plain = metacrypt.decrypt(raw, ordinal)  # type: ignore[arg-type]
        header = struct.unpack_from("<I", plain, formats.OFF_HEADER)[0]
    except (struct.error, ValueError) as exc:
        raise ObjectCorrupt(digest, f"the meta does not decrypt ({exc})") from exc
    if header != formats.META_HEADER:
        raise ObjectCorrupt(digest, "the meta does not decrypt with its ordinal")
    return plain
