"""What a pair write puts into a slot (D5 2.5): which bytes go to which position, with
which meta and which times. Pure: no I/O, so every case is unit-testable.

The owner's decisions:

* F4a - the version goes back with its own same-id partner, in the positions it was
  captured in; a source with one member, or with members of two playthroughs, gets a copy
  of the loading member in the other position. A slot is never left mixed.
* F5b - stamp "now": the loading member gets ``now``, the other ``now - 1``, in the meta
  and as the file / index time, so it is the newest by every signal the game or a cloud
  looks at. The original time stays in the vault record (``source_meta_ts``).

Data is never touched. A meta changes only by the time stamp and, on Steam, by being
encrypted for the destination position's storage ordinal.
"""
from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass, field

from .. import formats, meta, msstore, slotmap
from ..organizer.model import Platform
from ..safety import sha256_bytes
from .model import MemberWrite, Position, ResolvedTarget


@dataclass(frozen=True)
class SourceMember:
    """One captured member: its data's id and its meta exactly as captured."""

    position: Position                    # where it was captured
    vid: str
    pid: str | None
    meta_plain: bytes                     # Steam: decrypted plaintext; Xbox: as stored
    origin: Mapping[str, object] = field(default_factory=dict)   # Location JSON of the bytes


@dataclass(frozen=True)
class SourcePair:
    platform: Platform
    load: Position                        # the position whose member must load
    a: SourceMember | None
    b: SourceMember | None

    def member(self, position: Position) -> SourceMember | None:
        return self.a if position == "A" else self.b


@dataclass(frozen=True)
class PlannedMember:
    write: MemberWrite
    meta_disk: bytes                      # the meta file's bytes as they will be written


@dataclass(frozen=True)
class MetaFacts:
    meta_ts: int                          # 0 when the meta holds no save time
    play_time: int


def other(position: Position) -> Position:
    return "B" if position == "A" else "A"


def facts(platform: Platform, plain: bytes) -> MetaFacts:
    """Save time and play time of a meta plaintext. Raises ValueError for a Steam
    plaintext that is not a decrypted meta."""
    if platform == "steam":
        _check_steam_plain(plain)
        info = meta.parse(plain, 0)
        return MetaFacts(info.timestamp, info.total_play_time)
    fields_ = msstore.parse_ms_meta(plain)
    return MetaFacts(fields_["timestamp"], fields_["total_play_time"])


def default_load(src_platform: Platform, a: SourceMember | None,
                 b: SourceMember | None) -> Position:
    """The member that loads when the user did not pick one: the newer by (save time,
    play time); a tie goes to A."""
    if a is None or b is None:
        if a is None and b is None:
            raise ValueError("an empty pair has no member to load")
        return "A" if a is not None else "B"
    fa, fb = facts(src_platform, a.meta_plain), facts(src_platform, b.meta_plain)
    return "B" if (fb.meta_ts, fb.play_time) > (fa.meta_ts, fa.play_time) else "A"


def compute(src: SourcePair, rt: ResolvedTarget, now: int) -> tuple[PlannedMember, PlannedMember]:
    """The two members a pair write puts into ``rt``'s slot: (loading, other)."""
    platform = rt.target.platform
    if src.platform != platform:
        raise ValueError("a save moves between Steam and Xbox only by a transfer (F6)")
    load = src.load
    v = src.member(load)
    if v is None:
        raise ValueError(f"the source has no member {load} to load")
    partner = src.member(other(load))
    use_partner = (partner is not None and v.pid is not None and partner.pid == v.pid)
    written = partner if use_partner else v
    return (_member(platform, rt.target.slot, load, v, now, None),
            _member(platform, rt.target.slot, other(load), written, now - 1,
                    None if use_partner else load))


def _member(platform: Platform, slot: int, position: Position, src: SourceMember, ts: int,
            copy_of: Position | None) -> PlannedMember:
    captured = facts(platform, src.meta_plain)
    plain = src.meta_plain
    if captured.meta_ts:                  # a meta without a save time keeps its bytes
        plain = _with_ts(platform, plain, ts)
    if platform == "steam":
        ordinal = slotmap.storage_ordinal(slotmap.file_no(slot, slotmap.member_index(position)))
        disk = meta.encrypt(plain, ordinal)
        if not meta.is_valid_for(disk, ordinal):
            raise ValueError("the re-keyed meta does not decrypt for its slot")
    else:
        ordinal, disk = None, plain       # Xbox metas are plaintext, the same in wgs and xgs
    write = MemberWrite(
        position=position, vid=src.vid, pid=src.pid, play_time=captured.play_time,
        meta_ts=ts if captured.meta_ts else 0,
        source_meta_ts=captured.meta_ts or None,
        meta_plain_sha256=sha256_bytes(plain), meta_disk_sha256=sha256_bytes(disk),
        disk_time=float(ts), ordinal=ordinal, copy_of=copy_of, origin=dict(src.origin))
    return PlannedMember(write, disk)


def _with_ts(platform: Platform, plain: bytes, ts: int) -> bytes:
    if platform == "steam":
        return meta.set_timestamp(plain, ts)
    return msstore.set_ms_meta_timestamp(plain, ts)   # raises for a layout it cannot write


def _check_steam_plain(plain: bytes) -> None:
    if len(plain) not in formats.META_LENGTHS_KNOWN:
        raise ValueError(f"a Steam meta of {len(plain)} bytes")
    if struct.unpack_from("<I", plain, formats.OFF_HEADER)[0] != formats.META_HEADER:
        raise ValueError("the Steam meta is not decrypted")
