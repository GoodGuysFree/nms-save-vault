"""``StoreLocator``: D1's ``VersionLocator`` over the version store (D3 2.7, 2.8), and the one
provider of ``preserve`` (owner decision Q1).

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

from .. import formats, lz4_block
from .. import meta as metacrypt
from ..organizer.model import LiveTarget, StoreRef, Vid
from . import capture
from .capture import CaptureScope
from .d1 import Location, LiveMember, OpenedVersion, PreserveFailed, VersionLocator, VersionUnavailable
from .errors import EntryCorrupt, ObjectCorrupt, ObjectMissing, PreserveMismatch, StoreError
from .manifest import MemberRecord, StoreEntry
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

    # --- preserve (D3 2.8) ----------------------------------------------------------------

    def preserve(self, members: Sequence[LiveMember], *, op_id: str,
                 reason: str) -> list[Location]:
        """Keep each member's live save durably before a live write replaces it: one
        ``preserve`` entry per slot, holding both members of the slot (a one-member copy
        restored later would leave a mixed slot), written every time, then every file of it
        re-read and checked. One location per member, in order. Raises PreserveFailed
        (PreserveMismatch when a slot no longer holds the expected version)."""
        kept: dict[tuple[LiveTarget, str], Location] = {}
        for target, group in _by_target(members).items():
            entry = self._preserve_slot(target, group, op_id=op_id, reason=reason)
            for lm in group:
                kept[(target, lm.member)] = Location(
                    lm.expected_vid, "store", StoreRef(entry.id), target.slot, lm.member,
                    target.platform, durable=True)
        return [kept[(lm.target, lm.member)] for lm in members]

    def _preserve_slot(self, target: LiveTarget, group: Sequence[LiveMember], *, op_id: str,
                       reason: str) -> StoreEntry:
        expected = {lm.member: lm.expected_vid for lm in group}

        def check(records: list[MemberRecord]) -> str:
            found = {m.member: m for m in records if m.slot == target.slot}
            for member, vid in expected.items():
                if member not in found or found[member].vid != vid:
                    raise PreserveMismatch(
                        f"Slot {target.slot} changed since the operation was planned. "
                        "Nothing was written; try again.")
            name = found[group[0].member].name or "<unnamed>"
            return f"kept: slot {target.slot} ({name})"

        try:
            entry = self.store.add_from_live(
                self.live_dir(target), platform=target.platform, account_key=target.account_key,
                kind="preserve", label=f"kept: slot {target.slot}",
                scope=CaptureScope(slots=(target.slot,)), op_id=op_id, reason=reason,
                finish=check)
        except PreserveFailed:
            raise
        except StoreError as exc:
            raise PreserveFailed(str(exc)) from exc
        self._verify_kept(entry)
        return entry

    def _verify_kept(self, entry: StoreEntry) -> None:
        """Re-read every file of a new preserve entry (deduplicated ones too) and check that the
        stored bytes still say what the records say: hashes, Steam metas decrypting with their
        ordinals, and each member's validity."""
        current = None
        try:
            for current in entry.files:
                self.store.file_bytes(current)
            for m in entry.members:
                current = entry.file(m.meta)
                if entry.platform != "steam" or m.ordinal is None:
                    continue    # nothing to decrypt: the hashes above are the whole check
                data = self.store.file_bytes(entry.file(m.data))
                plain = _plain_meta("steam", self.store.file_bytes(current), m.ordinal,
                                    current.sha256)
                if (m.meta_plain_sha256 is not None and sha256(plain) != m.meta_plain_sha256) \
                        or _steam_valid(plain, m.ordinal, data) != m.valid:
                    raise ObjectCorrupt(current.sha256, "the kept copy does not check out")
        except (ObjectMissing, ObjectCorrupt) as exc:
            self.store.record_damage(entry.id, current,
                                     "missing" if isinstance(exc, ObjectMissing) else "corrupt",
                                     exc.detail)
            raise PreserveFailed(str(exc)) from exc

    def space_needed_for_preserve(self, members: Sequence[LiveMember]) -> int:
        """Bytes a :meth:`preserve` of ``members`` can add to the vault drive: the slots' files
        too big to be inline, less the requested versions the store already holds, plus a
        margin for the manifests."""
        total = PRESERVE_MARGIN
        for target, group in _by_target(members).items():
            try:
                plan = capture.plan(self.live_dir(target), target.platform, kind="preserve",
                                    scope=CaptureScope(slots=(target.slot,)), live=True)
            except StoreError:
                continue        # nothing there to keep; preserve itself will say so
            need = plan.object_bytes(self.store.inline_max)
            for lm in group:
                size = self._stored_size(lm.expected_vid)
                if size is not None and size > self.store.inline_max:
                    need -= size
            total += max(0, need)
        return total

    def _stored_size(self, vid: Vid) -> int | None:
        for entry_id, slot, member in self.store.vid_refs(vid):
            entry = self.store.entry(entry_id)
            fe = entry.file(entry.member(slot, member).data)        # type: ignore[arg-type,union-attr]
            if fe.inline is not None or self.store.objects.size_of(fe.sha256) == fe.size:
                return fe.size
        return None


PRESERVE_MARGIN = 64 * 1024


def _by_target(members: Sequence[LiveMember]) -> dict[LiveTarget, list[LiveMember]]:
    groups: dict[LiveTarget, list[LiveMember]] = {}
    for lm in members:
        group = groups.setdefault(lm.target, [])
        if all(other.member != lm.member for other in group):
            group.append(lm)
    return groups


def _steam_valid(plain: bytes, ordinal: int, data: bytes) -> bool:
    """A Steam member's ``valid`` recomputed from stored bytes (the capture's rule: header ok
    and SizeDecompressed equal to the chunk total)."""
    info = metacrypt.parse(plain, ordinal)
    st = lz4_block.stats(data)
    return bool(info.valid and st.chunk_count and st.total_decompressed == info.size_decompressed)


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
