"""core.saveedit (CL5): a save rebuilt after a content edit - data compressed again, meta sizes
set as the game sets them, everything else in the meta kept."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from nms_save_vault.core import formats, lz4_block, meta, msstore, savedir, savejson
from nms_save_vault.core import saveedit, savepatch, slotmap
from nms_save_vault.core.hashname import key as K

from .conftest import chunked, steam_save

UNITS = ("PlayerStateData", "Units")


def payload(units: int) -> bytes:
    return json.dumps({K("PlayerStateData"): {K("Units"): units, K("SaveName"): "Main"}},
                      separators=(",", ":")).encode() + b"\x00"


def steam_member(folder: Path, *, units: int = 5, base_version: int = 4233):
    steam_save(folder, 1, 0, "Main", 3600, 0x1234, 1_790_000_000, payload=payload(units),
               base_version=base_version)
    m = savedir.scan(folder).slots[1].a
    data = Path(m.data_path).read_bytes()
    plain = meta.decrypt(Path(m.meta_path).read_bytes(), m.ref.storage_ordinal)
    return m, data, plain


def test_a_steam_save_takes_an_edit_and_its_meta_gets_the_game_s_sizes(tmp_path):
    m, data, plain = steam_member(tmp_path / "st_1")
    out = saveedit.rebuild("steam", data, plain, [savepatch.Replace(UNITS, b"99999")])
    root = savejson.decode_save(out.data).root
    assert root[K("PlayerStateData")][K("Units")] == 99999
    assert (out.size_decompressed, out.size_disk) == (len(payload(99999)), len(out.data))
    u = lambda off: struct.unpack_from("<I", out.meta, off)[0]
    assert u(0x38) == u(0x54) == out.size_decompressed and u(0x3C) == len(out.data)
    changed = {i - i % 4 for i in range(len(plain)) if plain[i] != out.meta[i]}
    assert changed <= {0x38, 0x3C, 0x54}
    before, after = meta.parse(plain, 0), meta.parse(out.meta, 0)
    assert (after.slot_identifier, after.timestamp, after.save_name, after.total_play_time) == (
        before.slot_identifier, before.timestamp, before.save_name, before.total_play_time)


def test_a_rebuilt_steam_save_reads_back_as_a_valid_save_once_written(tmp_path):
    m, data, plain = steam_member(tmp_path / "st_1")
    out = saveedit.rebuild("steam", data, plain, [savepatch.Replace(UNITS, b"7")])
    folder = tmp_path / "rebuilt"
    folder.mkdir()
    ordinal = m.ref.storage_ordinal
    (folder / slotmap.data_filename(m.ref.file_no)).write_bytes(out.data)
    (folder / slotmap.meta_filename(m.ref.file_no)).write_bytes(meta.encrypt(out.meta, ordinal))
    again = savedir.scan(folder).slots[1].a
    assert again.valid and again.info.size_decompressed == out.size_decompressed
    assert again.info.size_disk == len(out.data)


def test_an_xbox_save_takes_an_edit_and_its_size_field_follows(tmp_path, make_wgs_account):
    text = payload(5).decode()
    folder = make_wgs_account(tmp_path, [("Slot1Auto", text, "Main", "", 60, 1_790_000_000)])
    m = msstore.scan(folder).slots[1].a
    data, stored = Path(m.data_path).read_bytes(), Path(m.meta_path).read_bytes()
    stored = bytearray(stored)
    struct.pack_into("<I", stored, msstore.MS_OFF_BASE_VERSION, 4233)
    out = saveedit.rebuild("xbox", data, bytes(stored), [savepatch.Replace(UNITS, b"123")])
    fields = msstore.parse_ms_meta(out.meta)
    assert fields["size_field"] == out.size_decompressed == len(payload(123))
    assert savejson.decode_save(out.data).root[K("PlayerStateData")][K("Units")] == 123
    assert {k: v for k, v in fields.items() if k != "size_field"} == {
        k: v for k, v in msstore.parse_ms_meta(bytes(stored)).items() if k != "size_field"}


def test_a_save_of_a_newer_game_is_refused(tmp_path):
    _m, data, plain = steam_member(tmp_path / "st_1", base_version=99_999)
    with pytest.raises(saveedit.EditRefused, match="newer"):
        saveedit.rebuild("steam", data, plain, [savepatch.Replace(UNITS, b"1")])


def test_a_meta_that_disagrees_with_its_data_is_refused(tmp_path):
    _m, data, plain = steam_member(tmp_path / "st_1")
    wrong = bytearray(plain)
    struct.pack_into("<I", wrong, formats.OFF_SIZE_DECOMPRESSED, 1)
    with pytest.raises(saveedit.EditRefused, match="does not match its own data"):
        saveedit.rebuild("steam", data, bytes(wrong), [savepatch.Replace(UNITS, b"1")])


@pytest.mark.parametrize("data, why", [
    (lz4_block.compress_block(payload(5)), "old save container"),
    (lz4_block.HGSAVEV2_HEADER + b"rest", "old save container"),
    (chunked(payload(5) + b"stale"), "exactly one NUL"),
    (chunked(payload(5)[:-1]), "exactly one NUL"),
])
def test_an_old_or_odd_container_is_refused(data, why):
    with pytest.raises(saveedit.EditRefused, match=why):
        saveedit.body_of(data)


def test_metas_this_core_does_not_rebuild_are_refused(tmp_path):
    _m, data, plain = steam_member(tmp_path / "st_1")
    edit = [savepatch.Replace(UNITS, b"1")]
    with pytest.raises(saveedit.EditRefused, match="432-byte"):
        saveedit.rebuild("steam", data, plain[:384], edit)
    with pytest.raises(saveedit.EditRefused, match="not decrypted"):
        saveedit.rebuild("steam", data, bytes(len(plain)), edit)
    with pytest.raises(saveedit.EditRefused, match="360-byte"):
        saveedit.rebuild("xbox", data, b"\x00" * 296, edit)
    with pytest.raises(saveedit.EditRefused, match="platform"):
        saveedit.rebuild("ps5", data, plain, edit)


def test_an_edit_that_cannot_be_made_stops_the_rebuild(tmp_path):
    _m, data, plain = steam_member(tmp_path / "st_1")
    with pytest.raises(savepatch.PatchRefused):
        saveedit.rebuild("steam", data, plain, [savepatch.Replace(("Nope",), b"1")])



def test_rebuild_names_an_old_container_as_old(tmp_path):
    _m, _data, plain = steam_member(tmp_path / "st_1")
    with pytest.raises(saveedit.EditRefused, match="old save container"):
        saveedit.rebuild("steam", lz4_block.compress_block(payload(5)), plain,
                         [savepatch.Replace(UNITS, b"1")])


def test_a_raw_nul_never_reaches_a_rebuilt_save(tmp_path):
    _m, data, plain = steam_member(tmp_path / "st_1")
    with pytest.raises(savepatch.PatchRefused, match="NUL"):
        saveedit.rebuild("steam", data, plain,
                         [savepatch.Replace(("PlayerStateData", "SaveName"), b'"Ma\x00in"')])


@pytest.mark.slow
def test_a_real_steam_save_is_rebuilt_with_one_edit(live_save_dir):
    """Read-only: a real save's Units raised by one; sizes and every other meta field right."""
    view = savedir.scan(live_save_dir)
    m = next(s.newest for s in view.occupied_slots if s.newest is not None and s.newest.valid)
    data = Path(m.data_path).read_bytes()
    plain = meta.decrypt(Path(m.meta_path).read_bytes(), m.ref.storage_ordinal)
    root = savejson.decode_save(data).root
    where = ("BaseContext",) + UNITS if K("BaseContext") in root else UNITS
    body = saveedit.body_of(data)
    old = int(savepatch.value_bytes(body, where))
    out = saveedit.rebuild("steam", data, plain, [savepatch.Replace(where, str(old + 1).encode())])
    assert lz4_block.decompress(out.data) == savepatch.apply(
        body, [savepatch.Replace(where, str(old + 1).encode())]) + b"\x00"
    assert meta.parse(out.meta, 0).size_decompressed == out.size_decompressed
