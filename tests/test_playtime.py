"""Total play time: one row per playthrough, however many copies of it exist.

The whole value of the report is the deduplication, so that is what these pin: the two
members of a slot, the same save in a dozen dated backups, and the same run carried to
another platform by the NMS cloud all have to collapse onto one row carrying the highest
play time seen.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from nms_save_vault.core import formats, meta, playtime, slotmap
from nms_save_vault.core.catalog import Vault


def _vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    return v


def lz4_literals(payload: bytes) -> bytes:
    lit = len(payload)
    if lit < 15:
        return bytes([lit << 4]) + payload
    rest = lit - 15
    cont = bytearray()
    while rest >= 255:
        cont.append(255)
        rest -= 255
    cont.append(rest)
    return bytes([0xF0]) + bytes(cont) + payload


def chunked(payload: bytes) -> bytes:
    block = lz4_literals(payload)
    return struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(payload), 0) + block


def steam_save(folder: Path, slot: int, member: int, name: str, play: int, ident: int, ts: int) -> None:
    """Write one Steam save pair (data + encrypted meta) into ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    fno = slotmap.slot_file_numbers(slot)[member]
    payload = b'{"F2P":4737,"<h0":{"Pk4":"' + name.encode("utf-8") + b'","WmU":"0x%X"}}' % ident
    (folder / slotmap.data_filename(fno)).write_bytes(chunked(payload))

    plain = bytearray(formats.META_LEN_WORLDS_II)
    struct.pack_into("<I", plain, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT, formats.META_FORMAT_4)
    struct.pack_into("<I", plain, formats.OFF_SIZE_DECOMPRESSED, len(payload))
    struct.pack_into("<I", plain, formats.OFF_SIZE_DISK, len(chunked(payload)))
    struct.pack_into("<H", plain, formats.OFF_GAME_MODE, 1)
    struct.pack_into("<Q", plain, formats.OFF_TOTAL_PLAY_TIME, play)
    nm = name.encode("utf-8")
    plain[formats.OFF_SAVE_NAME : formats.OFF_SAVE_NAME + len(nm)] = nm
    struct.pack_into("<I", plain, formats.OFF_DIFFICULTY, 2)
    struct.pack_into("<Q", plain, formats.OFF_SLOT_IDENTIFIER, ident)
    struct.pack_into("<I", plain, formats.OFF_TIMESTAMP, ts)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT_TAIL, formats.META_FORMAT_4)
    ordinal = slotmap.storage_ordinal(fno)
    (folder / slotmap.meta_filename(fno)).write_bytes(meta.encrypt(bytes(plain), ordinal))


# --- identity ----------------------------------------------------------------


def test_the_playthrough_id_is_read_out_of_the_data_blob():
    """Xbox metas leave the id field 0, so the id has to come from the save data."""
    raw = chunked(b'{"F2P":4737,"<h0":{"WmU":"0xDDDAD1BDD39F9C72"}}')
    assert playtime._id_from_bytes(raw, 0) == 0xDDDAD1BDD39F9C72


def test_a_save_without_the_id_field_reports_none():
    """Pre-Waypoint saves predate it; they key the player block '6f=' and have no id."""
    raw = chunked(b'{"F2P":4146,"6f=":{"Pk4":"Voyagers"}}')
    assert playtime._id_from_bytes(raw, 0) is None


def test_an_unframed_old_save_is_read_with_the_size_from_its_meta():
    payload = b'{"F2P":4146,"<h0":{"WmU":"0x1234ABCD"}}' + b" " * 40
    assert playtime._id_from_bytes(lz4_literals(payload), len(payload)) == 0x1234ABCD


def test_the_id_cache_decodes_a_file_once(tmp_path, monkeypatch):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Cached", 3600, 0xABCDEF, 1000)
    calls = []
    real = playtime._id_from_data

    def counting(path, expected):
        calls.append(path)
        return real(path, expected)

    monkeypatch.setattr(playtime, "_id_from_data", counting)
    cache = playtime._IdCache(tmp_path / "ids.json")
    data = live / slotmap.data_filename(slotmap.slot_file_numbers(1)[0])
    st = data.stat()
    for _ in range(3):
        assert cache.get_or_read(data, st.st_size, st.st_mtime, 0) == 0xABCDEF
    assert len(calls) == 1

    cache.flush()
    assert playtime._IdCache(tmp_path / "ids.json").get_or_read(data, st.st_size, st.st_mtime, 0) == 0xABCDEF
    assert len(calls) == 1          # a second run does not decode it again either


# --- the report ---------------------------------------------------------------


def test_both_members_and_every_backup_count_once(tmp_path):
    """One playthrough: two members, plus an older copy of it in the vault."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Main", 7200, 0xAAAA, 2000)
    steam_save(live, 1, 1, "Main", 7100, 0xAAAA, 1900)
    backup = tmp_path / "bk"
    steam_save(backup, 1, 0, "Main", 3600, 0xAAAA, 1000)

    vault = _vault(tmp_path)
    from nms_save_vault.core import operations as ops

    ops.import_backup(vault, backup, label="yesterday")
    report = playtime.collect(vault, [live])

    assert len(report.playthroughs) == 1
    p = report.playthroughs[0]
    assert p.play_time == 7200 and p.copies == 3        # highest wins, not the sum
    assert report.total_play_time == 7200
    assert report.saves == 3 and p.identified


def test_a_renamed_save_is_still_one_playthrough_under_its_current_name(tmp_path):
    live = tmp_path / "live"
    steam_save(live, 2, 0, "New Name", 9000, 0xBBBB, 5000)
    old = tmp_path / "old"
    steam_save(old, 7, 0, "Old Name", 4000, 0xBBBB, 1000)   # different slot, too

    vault = _vault(tmp_path)
    from nms_save_vault.core import operations as ops

    ops.import_backup(vault, old, label="before the rename")
    report = playtime.collect(vault, [live])

    assert len(report.playthroughs) == 1
    assert report.playthroughs[0].name == "New Name"
    assert report.playthroughs[0].play_time == 9000


def test_the_same_run_on_two_platforms_is_one_playthrough(tmp_path, make_wgs_account, monkeypatch):
    """An NMS cloud save carried to another platform keeps its id, so it must not double."""
    ident = 0xDECA9E5054EFD19E
    live = tmp_path / "steam"
    steam_save(live, 3, 0, "The Cartographers", 30000, ident, 4000)
    xbox = make_wgs_account(tmp_path / "xb", [("Slot3Auto", '{"a":1}', "The Cartographers", "", 29000, 3000)])
    # The wgs meta carries no id; the real one lives in the blob, which the fixture cannot
    # build at this size -- so stand in for the decode with the id that save would hold.
    monkeypatch.setattr(playtime, "_id_from_data", lambda path, expected: ident)

    report = playtime.collect(_vault(tmp_path), [live, xbox], use_cache=False)

    assert len(report.playthroughs) == 1
    p = report.playthroughs[0]
    assert sorted(p.platforms) == ["steam", "xbox"] and p.cross_platform
    assert p.play_time == 30000


def test_saves_without_an_id_fall_back_to_name_and_are_flagged(tmp_path, make_wgs_account, monkeypatch):
    xbox = make_wgs_account(tmp_path / "xb", [
        ("Slot1Auto", '{"a":1}', "Voyagers", "", 30865, 1000),
        ("Slot2Auto", '{"b":2}', "Singularity", "", 21623, 1000),
    ])
    monkeypatch.setattr(playtime, "_id_from_data", lambda path, expected: None)

    report = playtime.collect(_vault(tmp_path), [xbox], use_cache=False)

    assert len(report.playthroughs) == 2
    assert report.unidentified == 2
    assert report.total_play_time == 30865 + 21623


def test_an_unreadable_folder_does_not_sink_the_report(tmp_path):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Main", 1000, 0xCCCC, 1000)
    report = playtime.collect(_vault(tmp_path), [live, tmp_path / "does-not-exist"])
    assert len(report.playthroughs) == 1 and report.folders == 1
