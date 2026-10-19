"""Steam <-> Xbox meta conversion (D5 3.5.2): synthetic metas, a property sweep, and the
read-only round trip on the owner's real metas (slow, skipped when absent, writes nothing)."""
from __future__ import annotations

import random
import struct
from pathlib import Path

import pytest

from nms_save_vault.core import formats, gameversion, meta, msstore, savedir, slotmap
from nms_save_vault.core.liveops import convert
from nms_save_vault.core.liveops.convert import ConvertUnsupported

from .conftest import chunked, xbox_meta

PID = 0xDDDAD1BDD39F9C72
TS = 1_790_000_123


def _steam_plain(data: bytes, *, name="Main", summary="in orbit", play=7200, ident=PID,
                 ts=TS, difficulty=2, base=4220, season=0, mode=1) -> bytearray:
    p = bytearray(formats.META_LEN_WORLDS_II)
    struct.pack_into("<I", p, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", p, formats.OFF_META_FORMAT, formats.META_FORMAT_4)
    total = len(data) and convert.chunk_total(data)
    struct.pack_into("<I", p, formats.OFF_SIZE_DECOMPRESSED, total)
    struct.pack_into("<I", p, formats.OFF_SIZE_DISK, len(data))
    struct.pack_into("<I", p, formats.OFF_BASE_VERSION, base)
    struct.pack_into("<H", p, formats.OFF_GAME_MODE, mode)
    struct.pack_into("<H", p, formats.OFF_SEASON, season)
    struct.pack_into("<Q", p, formats.OFF_TOTAL_PLAY_TIME, play)
    struct.pack_into("<I", p, 0x54, total)
    nm, sm = name.encode(), summary.encode()
    p[formats.OFF_SAVE_NAME:formats.OFF_SAVE_NAME + len(nm)] = nm
    p[formats.OFF_SAVE_SUMMARY:formats.OFF_SAVE_SUMMARY + len(sm)] = sm
    struct.pack_into("<I", p, formats.OFF_DIFFICULTY, difficulty)
    struct.pack_into("<Q", p, formats.OFF_SLOT_IDENTIFIER, ident)
    struct.pack_into("<I", p, formats.OFF_TIMESTAMP, ts)
    struct.pack_into("<I", p, formats.OFF_META_FORMAT_TAIL, formats.META_FORMAT_4)
    p[0x16C:0x16C + 64] = bytes(range(64))
    return p


def _data(n: int = 300) -> bytes:
    return chunked(b'{"a":"' + b"x" * n + b'"}')


def test_steam_to_xbox_maps_every_field():
    data = _data()
    plain = bytes(_steam_plain(data, name="Main", summary="in orbit"))
    out = convert.steam_to_xbox(plain, data, 1_800_000_000)
    assert len(out) == 360
    f = msstore.parse_ms_meta(out)
    assert (f["name"], f["summary"], f["slot_id"], f["total_play_time"], f["difficulty"]) == \
        ("Main", "in orbit", PID, 7200, 2)
    assert (f["timestamp"], f["meta_format"], f["size_field"], f["base_version"],
            f["game_mode"]) == (1_800_000_000, 2004, convert.chunk_total(data), 4220, 1)
    assert out[0x128:0x168] == bytes(range(64))


def test_round_trip_steam_xbox_steam_keeps_every_mapped_field():
    data = _data()
    plain = bytes(_steam_plain(data))
    back = convert.xbox_to_steam_plain(convert.steam_to_xbox(plain, data, TS), data, TS)
    assert back == plain


def test_round_trip_xbox_steam_xbox():
    data = _data()
    x = xbox_meta("Voyager", "far away", play=99, size=convert.chunk_total(data), pid=PID,
                  ts=TS, difficulty=3)
    assert convert.steam_to_xbox(convert.xbox_to_steam_plain(x, data, TS), data, TS) == x


def test_xbox_to_steam_encrypts_for_the_ordinal_and_recomputes_sizes():
    data = _data()
    x = xbox_meta("Voyager", play=5, size=12345, pid=PID, ts=TS)       # a stale 0x10
    ordinal = slotmap.storage_ordinal(slotmap.slot_file_numbers(4)[0])
    disk = convert.xbox_to_steam(x, data, TS + 5, ordinal)
    assert len(disk) == 432 and meta.is_valid_for(disk, ordinal)
    info = meta.parse(meta.decrypt(disk, ordinal), ordinal)
    assert (info.size_decompressed, info.size_disk, info.timestamp) == \
        (convert.chunk_total(data), len(data), TS + 5)
    assert struct.unpack_from("<I", meta.decrypt(disk, ordinal), 0x54)[0] == info.size_decompressed


def test_names_keep_the_bytes_after_the_nul():
    data = _data()
    plain = _steam_plain(data)
    plain[formats.OFF_SAVE_NAME + 40:formats.OFF_SAVE_NAME + 44] = b"junk"
    out = convert.steam_to_xbox(bytes(plain), data, TS)
    assert out[0x14 + 40:0x14 + 44] == b"junk"
    assert convert.xbox_to_steam_plain(out, data, TS) == bytes(plain)


@pytest.mark.parametrize("length", [280, 296])
def test_unknown_xbox_meta_lengths_are_refused(length):
    x = xbox_meta("W", ts=TS, pid=PID, length=length)
    with pytest.raises(ConvertUnsupported) as err:
        convert.xbox_to_steam_plain(x, _data(), TS)
    assert err.value.code == "CONVERT_UNSUPPORTED" and err.value.reason == "meta_length"


@pytest.mark.parametrize("length", [formats.META_LEN_WORLDS_I, formats.META_LEN_WAYPOINT,
                                    formats.META_LEN_VANILLA, 433])
def test_unknown_steam_meta_lengths_are_refused(length):
    with pytest.raises(ConvertUnsupported) as err:
        convert.steam_to_xbox(bytes(length), _data(), TS)
    assert err.value.reason == "meta_length"


def test_older_meta_format_is_refused():
    data = _data()
    plain = _steam_plain(data)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT, formats.META_FORMAT_3)
    with pytest.raises(ConvertUnsupported) as err:
        convert.steam_to_xbox(bytes(plain), data, TS)
    assert err.value.reason == "meta_format"
    x = xbox_meta("W", ts=TS, pid=PID, fmt=formats.META_FORMAT_3)
    with pytest.raises(ConvertUnsupported):
        convert.xbox_to_steam_plain(x, data, TS)


def test_hgsavev2_and_other_data_are_refused():
    good = _data()
    plain = bytes(_steam_plain(good))
    x = xbox_meta("W", ts=TS, pid=PID)
    for bad in (b"HGSAVEV2" + b"\0" * 40, b'{"json": true}', b"", good + b"trailing"):
        with pytest.raises(ConvertUnsupported) as err:
            convert.steam_to_xbox(plain, bad, TS)
        assert err.value.reason in ("data_format", "data_walk")
        with pytest.raises(ConvertUnsupported):
            convert.xbox_to_steam_plain(x, bad, TS)


def test_game_newer_is_refused_both_ways():
    data = _data()
    newer = gameversion.KNOWN["steam"].base_version + 1
    with pytest.raises(ConvertUnsupported) as err:
        convert.steam_to_xbox(bytes(_steam_plain(data, base=newer)), data, TS)
    assert err.value.reason == "GAME_NEWER"
    x = bytearray(xbox_meta("W", ts=TS, pid=PID))
    struct.pack_into("<I", x, 0, gameversion.KNOWN["xbox"].base_version + 1)
    with pytest.raises(ConvertUnsupported) as err:
        convert.xbox_to_steam_plain(bytes(x), data, TS)
    assert err.value.reason == "GAME_NEWER"


def test_steam_meta_with_unmapped_data_or_wrong_size_is_refused():
    data = _data()
    for offset in (0x10, 0x40, 0x1AC):                      # hash area, profile hash, tail
        plain = _steam_plain(data)
        plain[offset] = 1
        with pytest.raises(ConvertUnsupported) as err:
            convert.steam_to_xbox(bytes(plain), data, TS)
        assert err.value.reason == "unmapped_field"
    plain = _steam_plain(data)
    struct.pack_into("<I", plain, formats.OFF_SIZE_DECOMPRESSED, 7)
    with pytest.raises(ConvertUnsupported) as err:
        convert.steam_to_xbox(bytes(plain), data, TS)
    assert err.value.reason == "size_mismatch"


def test_an_encrypted_steam_meta_is_refused():
    data = _data()
    disk = meta.encrypt(bytes(_steam_plain(data)), 3)
    with pytest.raises(ConvertUnsupported) as err:
        convert.steam_to_xbox(disk, data, TS)
    assert err.value.reason == "meta_header"


def test_property_round_trips_over_random_metas():
    rng = random.Random(20261018)
    for _ in range(60):
        data = _data(rng.randint(1, 4000))
        name = "".join(rng.choice("abcXYZ 09-_é") for _ in range(rng.randint(0, 40)))
        plain = bytes(_steam_plain(
            data, name=name, summary=name[::-1], play=rng.getrandbits(40),
            ident=rng.getrandbits(64), ts=rng.getrandbits(32), difficulty=rng.getrandbits(16),
            base=rng.randint(1, gameversion.KNOWN["steam"].base_version),
            season=rng.randint(0, 20), mode=rng.randint(0, 6)))
        ts = rng.getrandbits(32)
        x = convert.steam_to_xbox(plain, data, ts)
        assert convert.xbox_to_steam_plain(x, data, ts) == \
            meta.set_timestamp(plain, ts)
        assert convert.steam_to_xbox(convert.xbox_to_steam_plain(x, data, ts), data, ts) == x


# --- T-RD1: the owner's real metas, read-only ---------------------------------------------


@pytest.mark.slow
def test_real_metas_round_trip_in_memory(live_save_dir, live_wgs_dir):
    """Converts every real meta in memory and asserts the round trip. Reads only."""
    steam_ok = steam_refused = steam_stale = 0
    for mv in _members(live_save_dir):
        data, raw = Path(mv.data_path).read_bytes(), Path(mv.meta_path).read_bytes()
        if len(raw) != formats.META_LEN_WORLDS_II or mv.ordinal_used is None:
            steam_refused += 1
            continue
        plain = meta.decrypt(raw, mv.ordinal_used)
        ts = struct.unpack_from("<I", plain, formats.OFF_TIMESTAMP)[0]
        try:
            x = convert.steam_to_xbox(plain, data, ts)
        except ConvertUnsupported:
            steam_refused += 1
            continue
        back = convert.xbox_to_steam_plain(x, data, ts)
        diff = {i for i in range(len(plain)) if plain[i] != back[i]}
        assert diff <= set(range(0x3C, 0x40)) | set(range(0x54, 0x58)), sorted(diff)[:8]
        steam_stale += bool(diff)
        steam_ok += 1
    xbox_ok = xbox_refused = xbox_size = 0
    for mv in _members(live_wgs_dir):
        data, x = Path(mv.data_path).read_bytes(), Path(mv.meta_path).read_bytes()
        ts = msstore.parse_ms_meta(x)["timestamp"]
        try:
            steam = convert.xbox_to_steam_plain(x, data, ts)
        except ConvertUnsupported:
            xbox_refused += 1
            continue
        back = convert.steam_to_xbox(steam, data, ts)
        diff = {i for i in range(len(x)) if x[i] != back[i]}
        assert diff <= set(range(0x10, 0x14)), sorted(diff)[:8]      # the stale size field
        xbox_size += bool(diff)
        xbox_ok += 1
    print(f"T-RD1 steam ok={steam_ok} refused={steam_refused} stale_fields_recomputed={steam_stale}"
          f" xbox ok={xbox_ok} refused={xbox_refused} size_field_recomputed={xbox_size}")
    assert steam_ok + xbox_ok > 0


def _members(folder: Path):
    view = savedir.scan_any(folder)
    for slot in view.slots.values():
        for mv in slot.members:
            if mv.exists and mv.valid:
                yield mv
