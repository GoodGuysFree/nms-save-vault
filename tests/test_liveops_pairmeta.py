"""liveops.pairmeta: positions, metas and times of a pair write (D5 2.5; F4a, F5b;
T-M1..T-M7). Pure: plaintext metas in, planned members out."""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from nms_save_vault.core import formats, meta, msstore, slotmap
from nms_save_vault.core.liveops import ResolvedTarget
from nms_save_vault.core.liveops import pairmeta
from nms_save_vault.core.liveops.pairmeta import SourceMember, SourcePair, compute, default_load
from nms_save_vault.core.organizer.model import LiveTarget

from .conftest import xbox_meta
from .liveops_support import STEAM_ACCOUNT, XBOX_ACCOUNT

NOW = 1_792_500_903
PID = "DDDAD1BDD39F9C72"
OTHER_PID = "0123456789ABCDEF"


def V(n: int) -> str:
    return f"{n:064x}"


def steam_plain(name: str, play: int, ident: int, ts: int, length: int = 432) -> bytes:
    plain = bytearray(length)
    struct.pack_into("<I", plain, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT, formats.META_FORMAT_4)
    if length >= formats.META_LEN_WAYPOINT:
        struct.pack_into("<Q", plain, formats.OFF_TOTAL_PLAY_TIME, play)
        plain[formats.OFF_SAVE_NAME:formats.OFF_SAVE_NAME + len(name)] = name.encode()
        struct.pack_into("<Q", plain, formats.OFF_SLOT_IDENTIFIER, ident)
        struct.pack_into("<I", plain, formats.OFF_TIMESTAMP, ts)
    return bytes(plain)


def plain_for(platform: str, play: int, ts: int, ident: int = 0xDDDAD1BDD39F9C72,
              xbox_len: int = 360) -> bytes:
    if platform == "steam":
        return steam_plain("Main", play, ident, ts)
    return xbox_meta("Main", play=play, pid=ident, ts=ts, length=xbox_len)


def target(platform: str, slot: int = 7, cloud: str | None = None) -> ResolvedTarget:
    account = STEAM_ACCOUNT if platform == "steam" else XBOX_ACCOUNT
    mode = cloud or ("xbox" if platform == "xbox" else "steam-local")
    return ResolvedTarget(LiveTarget(platform, account, slot), "src", Path("unused"), mode, None,
                          f"slot {slot}")


def member(position: str, vid: int, platform: str, *, play: int, ts: int,
           pid: str | None = PID, ident: int = 0xDDDAD1BDD39F9C72) -> SourceMember:
    return SourceMember(position, V(vid), pid, plain_for(platform, play, ts, ident),
                        origin={"kind": "entry", "entry": "full-steam-20261005-174129",
                                "slot": 3, "member": position})


def disk_plain(platform: str, pm, slot: int = 7) -> bytes:
    if platform == "xbox":
        return pm.meta_disk
    ordinal = slotmap.storage_ordinal(slotmap.file_no(slot, slotmap.member_index(pm.write.position)))
    assert pm.write.ordinal == ordinal and meta.is_valid_for(pm.meta_disk, ordinal)
    return meta.decrypt(pm.meta_disk, ordinal)


def saved_at(platform: str, plain: bytes) -> int:
    return pairmeta.facts(platform, plain).meta_ts


def changed_offsets(a: bytes, b: bytes) -> list[int]:
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


CASES = [("steam", "steam-cloud"), ("steam", "steam-local"), ("xbox", "xbox")]


@pytest.mark.parametrize("platform,cloud", CASES)
def test_a_full_pair_keeps_its_positions_and_the_loading_member_is_newest(platform, cloud):
    """T-M1 and T-M5: the same stamps and times in every cloud mode."""
    a = member("A", 1, platform, play=3700, ts=1_791_500_000)
    b = member("B", 2, platform, play=3600, ts=1_791_400_000)
    lw, ow = compute(SourcePair(platform, "A", a, b), target(platform, cloud=cloud), NOW)

    assert (lw.write.position, ow.write.position) == ("A", "B")
    assert (lw.write.vid, ow.write.vid) == (V(1), V(2))                 # data verbatim
    assert (lw.write.meta_ts, ow.write.meta_ts) == (NOW, NOW - 1)
    assert (lw.write.disk_time, ow.write.disk_time) == (float(NOW), float(NOW - 1))
    assert (lw.write.source_meta_ts, ow.write.source_meta_ts) == (1_791_500_000, 1_791_400_000)
    assert (lw.write.copy_of, ow.write.copy_of) == (None, None)
    assert lw.write.origin["slot"] == 3 and lw.write.play_time == 3700
    for pm, src in ((lw, a), (ow, b)):
        plain = disk_plain(platform, pm)                # Steam: re-keyed for slot 7, not 3
        assert saved_at(platform, plain) == pm.write.meta_ts
        assert pm.write.meta_plain_sha256 != hashlib.sha256(src.meta_plain).hexdigest()
        offset = formats.OFF_TIMESTAMP if platform == "steam" else msstore.MS_OFF_TIMESTAMP
        assert set(changed_offsets(src.meta_plain, plain)) <= set(range(offset, offset + 4))
    if platform == "xbox":
        assert lw.write.ordinal is None


@pytest.mark.parametrize("platform", ["steam", "xbox"])
def test_the_chosen_member_loads_even_when_its_partner_is_newer(platform):
    """T-M2 (the user picked the restore point, or a promote) and T-M4 (an undo puts the
    evicted pair back with its old loading position)."""
    a = member("A", 1, platform, play=9000, ts=1_791_900_000)          # newer, more play
    b = member("B", 2, platform, play=3600, ts=1_791_400_000)
    lw, ow = compute(SourcePair(platform, "B", a, b), target(platform), NOW)
    assert (lw.write.position, lw.write.vid, lw.write.meta_ts) == ("B", V(2), NOW)
    assert (ow.write.position, ow.write.vid, ow.write.meta_ts) == ("A", V(1), NOW - 1)
    assert lw.write.disk_time > ow.write.disk_time
    assert saved_at(platform, disk_plain(platform, lw)) > saved_at(platform, disk_plain(platform, ow))


@pytest.mark.parametrize("platform", ["steam", "xbox"])
def test_a_single_member_or_mixed_source_fills_the_other_position_with_a_copy(platform):
    """T-M3: never an empty position, never two playthroughs in one slot (I5)."""
    v = member("B", 2, platform, play=3600, ts=1_791_400_000)
    lw, ow = compute(SourcePair(platform, "B", None, v), target(platform), NOW)
    assert (lw.write.position, ow.write.position) == ("B", "A")
    assert lw.write.vid == ow.write.vid == V(2)
    assert (lw.write.copy_of, ow.write.copy_of) == (None, "B")
    assert (ow.write.meta_ts, ow.write.source_meta_ts) == (NOW - 1, 1_791_400_000)

    stranger = member("A", 1, platform, play=50, ts=1_791_000_000, pid=OTHER_PID, ident=0x0123)
    lw, ow = compute(SourcePair(platform, "B", stranger, v), target(platform), NOW)
    assert ow.write.vid == V(2) and ow.write.pid == PID and ow.write.copy_of == "B"
    unknown = member("A", 1, platform, play=50, ts=1_791_000_000, pid=None)
    nameless = SourceMember("B", V(2), None, v.meta_plain)
    _lw, ow = compute(SourcePair(platform, "B", unknown, nameless), target(platform), NOW)
    assert ow.write.vid == V(2)                      # no id to match: never assume a partner


def test_the_live_copy_is_a_meta_only_variant_of_the_version():
    """T-M6: the same vid, another meta; the captured time is kept for the record."""
    a = member("A", 1, "steam", play=3700, ts=1_791_500_000)
    lw, _ow = compute(SourcePair("steam", "A", a, None), target("steam"), NOW)
    assert lw.write.vid == a.vid
    assert lw.write.meta_plain_sha256 != hashlib.sha256(a.meta_plain).hexdigest()
    assert lw.write.source_meta_ts == 1_791_500_000
    assert lw.write.to_dict()["source_meta_ts"] == 1_791_500_000


def test_metas_without_a_save_time_keep_their_bytes(monkeypatch):
    """T-M7: a 280-byte Xbox meta (and a vanilla Steam meta) has no time field: the bytes
    stay, the setter is never called, and the disk times alone order the pair."""
    def refuse(*_a):
        raise AssertionError("no save time to set")
    monkeypatch.setattr(msstore, "set_ms_meta_timestamp", refuse)
    a = SourceMember("A", V(1), PID, xbox_meta("Old", play=100, length=280))
    b = SourceMember("B", V(2), PID, xbox_meta("Old", play=90, length=280))
    lw, ow = compute(SourcePair("xbox", "A", a, b), target("xbox"), NOW)
    assert (lw.meta_disk, ow.meta_disk) == (a.meta_plain, b.meta_plain)
    assert (lw.write.meta_ts, lw.write.source_meta_ts) == (0, None)
    assert lw.write.disk_time > ow.write.disk_time

    monkeypatch.setattr(meta, "set_timestamp", refuse)
    vanilla = SourceMember("A", V(3), PID, steam_plain("", 0, 0, 0, length=formats.META_LEN_VANILLA))
    lw, _ow = compute(SourcePair("steam", "A", vanilla, None), target("steam"), NOW)
    assert meta.decrypt(lw.meta_disk, lw.write.ordinal) == vanilla.meta_plain   # re-key only


def test_unwritable_sources_are_refused():
    a = member("A", 1, "steam", play=1, ts=1_791_000_000)
    with pytest.raises(ValueError, match="transfer"):
        compute(SourcePair("steam", "A", a, None), target("xbox"), NOW)
    with pytest.raises(ValueError, match="no member B"):
        compute(SourcePair("steam", "B", a, None), target("steam"), NOW)
    garbled = SourceMember("A", V(1), PID, b"\x00" * 432)
    with pytest.raises(ValueError, match="not decrypted"):
        compute(SourcePair("steam", "A", garbled, None), target("steam"), NOW)
    longer = SourceMember("A", V(1), PID, xbox_meta("x", ts=5, length=360) + b"\x00" * 8)
    with pytest.raises(ValueError):                  # a time field this app cannot write
        compute(SourcePair("xbox", "A", longer, None), target("xbox"), NOW)


def test_the_default_member_to_load_is_the_newer_one():
    old = member("A", 1, "steam", play=500, ts=1_791_000_000)
    new = member("B", 2, "steam", play=400, ts=1_791_000_100)
    assert default_load("steam", old, new) == "B"
    same_time_more_play = member("B", 2, "steam", play=600, ts=1_791_000_000)
    assert default_load("steam", old, same_time_more_play) == "B"
    assert default_load("steam", old, member("B", 2, "steam", play=500, ts=1_791_000_000)) == "A"
    assert default_load("xbox", None, member("B", 2, "xbox", play=1, ts=1)) == "B"
    with pytest.raises(ValueError):
        default_load("steam", None, None)
