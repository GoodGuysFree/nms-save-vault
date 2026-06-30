"""Microsoft / Xbox ('wgs') reader, tested against a synthetic but spec-accurate fixture.

No real Game Pass install is needed: we hand-build a valid containers.index + container
blob + a 0xFEEDA1E5 data blob + a plaintext MS meta, then assert the reader decodes it.
"""
from __future__ import annotations

import struct
import uuid

import pytest

from nms_save_vault.core import formats, msstore, savedir


def _lp_utf16(s: str) -> bytes:
    return struct.pack("<i", len(s)) + s.encode("utf-16-le")


def _fixed_utf16(s: str, total_bytes: int) -> bytes:
    raw = s.encode("utf-16-le")
    return raw + b"\x00" * (total_bytes - len(raw))


def _guid_bytes(seed: int) -> bytes:
    return bytes((seed + i) % 256 for i in range(16))


def _guid_name(raw: bytes) -> str:
    return uuid.UUID(bytes_le=raw).hex.upper()


def _data_blob(payload: bytes) -> bytes:
    """A valid single-chunk 0xFEEDA1E5 stream (current Worlds format)."""
    block = bytes([len(payload) << 4]) + payload  # LZ4 literals-only block
    return struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(payload), 0) + block


def _ms_meta_worlds(name: str, summary: str, *, play: int, size: int, timestamp: int) -> bytes:
    b = bytearray(formats.META_LEN_WORLDS_II)  # 360
    struct.pack_into("<I", b, 0, 4700)        # base version
    struct.pack_into("<H", b, 4, 1)           # game mode
    struct.pack_into("<H", b, 6, 0)           # season
    struct.pack_into("<Q", b, 8, play)        # total play time
    struct.pack_into("<I", b, 16, size)       # decompressed size
    nm = name.encode("utf-8")                 # meta strings are UTF-8 (NUL-terminated)
    b[20 : 20 + len(nm)] = nm
    sm = summary.encode("utf-8")
    b[148 : 148 + len(sm)] = sm
    struct.pack_into("<I", b, 276, 1)         # difficulty
    struct.pack_into("<Q", b, 280, 0)         # slot identifier
    struct.pack_into("<I", b, 288, timestamp)
    struct.pack_into("<I", b, 292, formats.META_FORMAT_4)
    return bytes(b)


def _build_wgs(tmp_path, saves):
    """saves: list of (identifier, payload_text, name, summary, play, timestamp). Returns account dir."""
    acct = tmp_path / "wgs" / ("0000000000000001_29070100B936489ABCE8B9AF3980429C")
    acct.mkdir(parents=True)

    index = struct.pack("<i", msstore.CONTAINERSINDEX_HEADER)
    index += struct.pack("<q", len(saves))            # container count
    index += _lp_utf16("proc")                        # process identifier
    index += struct.pack("<q", 0)                     # last write (filetime)
    index += struct.pack("<i", 2)                     # sync state
    index += _lp_utf16("acct")                        # account identifier
    index += struct.pack("<q", msstore.CONTAINERSINDEX_FOOTER)

    for i, (ident, payload, name, summary, play, ts) in enumerate(saves):
        dir_guid = _guid_bytes(100 + i)
        blobdir = acct / _guid_name(dir_guid)
        blobdir.mkdir()
        data_guid, meta_guid = _guid_bytes(1 + i * 2), _guid_bytes(2 + i * 2)

        data = _data_blob(payload.encode())
        (blobdir / _guid_name(data_guid)).write_bytes(data)
        (blobdir / _guid_name(meta_guid)).write_bytes(
            _ms_meta_worlds(name, summary, play=play, size=len(payload), timestamp=ts)
        )

        container = struct.pack("<ii", msstore.BLOBCONTAINER_HEADER, 2)
        container += _fixed_utf16("data", 128) + _guid_bytes(200 + i) + data_guid
        container += _fixed_utf16("meta", 128) + _guid_bytes(210 + i) + meta_guid
        (blobdir / "container.1").write_bytes(container)

        index += _lp_utf16(ident) + _lp_utf16("") + _lp_utf16("synchex")
        index += bytes([1])                           # extension -> container.1
        index += struct.pack("<i", 2)                 # sync state (not deleted)
        index += dir_guid                             # directory guid
        index += struct.pack("<q", 0)                 # last modified
        index += struct.pack("<q", 0)                 # empty
        index += struct.pack("<q", len(data))         # total size

    (acct / "containers.index").write_bytes(index)
    return acct


def test_guid_name_matches_dotnet_n_format():
    raw = _guid_bytes(1)
    assert _guid_name(raw) == uuid.UUID(bytes_le=raw).hex.upper()
    assert len(_guid_name(raw)) == 32 and _guid_name(raw).isupper()


def test_parse_ms_meta_fields():
    blob = _ms_meta_worlds("Hello", "On Planet", play=3600, size=2048, timestamp=1782240635)
    info = msstore.parse_ms_meta(blob)
    assert info["name"] == "Hello"
    assert info["summary"] == "On Planet"
    assert info["total_play_time"] == 3600
    assert info["timestamp"] == 1782240635
    assert info["meta_format"] == formats.META_FORMAT_4


def test_scan_microsoft_reads_slots(tmp_path):
    acct = _build_wgs(
        tmp_path,
        [
            ("Slot1Auto", '{"a":1}', "Xbox Save One", "On Planet (Test)", 7200, 1782240635),
            ("Slot1Manual", '{"a":1}', "Xbox Save One", "In a base", 7100, 1781000000),
            ("Slot3Auto", '{"bb":2}', "Third Slot", "Space station", 99999, 1782000000),
            ("AccountData", '{"acc":1}', "", "", 0, 0),
        ],
    )
    assert msstore.is_microsoft_save_dir(acct)
    assert savedir.looks_like_save_dir(acct)

    view = msstore.scan(acct)
    assert view.account_present

    s1 = view.slots[1]
    assert s1.occupied
    assert s1.a.exists and s1.a.save_name == "Xbox Save One"      # Slot1Auto -> member A
    assert s1.b.exists and s1.b.save_name == "Xbox Save One"      # Slot1Manual -> member B
    assert s1.a.info.total_play_time == 7200
    # newest is the one with the larger timestamp (the Auto save here)
    assert s1.newest.label == "A"
    # size_decompressed is taken from the data chunk structure
    assert s1.a.info.size_decompressed == len('{"a":1}')

    s3 = view.slots[3]
    assert s3.occupied and s3.a.save_name == "Third Slot"
    assert not view.slots[2].occupied


def test_scan_any_dispatches_to_microsoft(tmp_path):
    acct = _build_wgs(tmp_path, [("Slot2Manual", '{"x":9}', "MS via scan_any", "", 60, 100)])
    view = savedir.scan_any(acct)
    assert view.slots[2].occupied
    assert view.slots[2].b.save_name == "MS via scan_any"   # Manual -> member B


def test_scan_preserves_wgs_identity(tmp_path):
    """The scan must be non-lossy: it keeps the full wgs on-disk identity (dir/blob GUIDs,
    container number, sync state, sync-time) so a future writer can rewrite saves in place."""
    acct = _build_wgs(tmp_path, [("Slot1Auto", '{"a":1}', "Save", "summary", 10, 5)])
    view = msstore.scan(acct)

    # index-level identity (containers.index header)
    assert view.xbox_index is not None
    assert view.xbox_index.process_id == "proc"
    assert view.xbox_index.account_id == "acct"
    assert view.xbox_index.container_count == 1

    # per-member identity (i == 0 in the fixture)
    x = view.slots[1].a.xbox
    assert x is not None
    assert x.identifier == "Slot1Auto"
    assert x.dir_guid == _guid_name(_guid_bytes(100))
    assert x.extension == 1
    assert x.sync_state == 2
    assert x.sync_time == "synchex"
    assert x.has_second_identifier is False
    assert x.data_local_guid == _guid_name(_guid_bytes(1))
    assert x.meta_local_guid == _guid_name(_guid_bytes(2))
    assert x.data_cloud_guid == _guid_name(_guid_bytes(200))
    assert x.meta_cloud_guid == _guid_name(_guid_bytes(210))
    assert x.blob_container_file.name == "container.1"
    # the recorded local GUIDs actually name the on-disk blob files
    assert (x.directory / x.data_local_guid).is_file()
    assert (x.directory / x.meta_local_guid).is_file()


def test_steam_scan_has_no_xbox_identity(tmp_path):
    """A Steam scan leaves the Xbox carriers empty (no regression / no false positives)."""
    view = savedir.scan(tmp_path)  # no containers.index here -> Steam path
    assert view.xbox_index is None
    assert view.slots[1].a.xbox is None


# --- serialization (round-trip the wgs writers against the readers) ---------------------


def test_build_lp_string_roundtrips_non_bmp():
    """The length prefix must count UTF-16 code units, not Python chars, so astral chars
    (emoji) survive _read_lp_string instead of splitting the surrogate pair."""
    for s in ("", "ascii", "Slot1Auto", "Hi\U0001F600", "emoji\U0001F600here", "汉字"):
        blob = msstore._build_lp_string(s)
        decoded, end = msstore._read_lp_string(blob, 0)
        assert decoded == s
        assert end == len(blob)


def test_guid_and_filetime_inverses():
    raw = _guid_bytes(7)
    assert msstore._guid_bytes_from_name(_guid_name(raw)) == raw
    for unix in (0.0, 1782240635.0):
        assert msstore._filetime_to_unix(msstore._unix_to_filetime(unix)) == unix


def test_build_blob_container_roundtrip():
    data_local, meta_local = _guid_name(_guid_bytes(1)), _guid_name(_guid_bytes(2))
    data_cloud, meta_cloud = _guid_name(_guid_bytes(3)), _guid_name(_guid_bytes(4))
    blob = msstore.build_blob_container(data_local, meta_local, data_cloud, meta_cloud)

    assert len(blob) == msstore.BLOBCONTAINER_TOTAL_LENGTH
    assert struct.unpack_from("<i", blob, 0)[0] == msstore.BLOBCONTAINER_HEADER
    assert struct.unpack_from("<i", blob, 4)[0] == 2
    assert blob[8 : 8 + 8].decode("utf-16-le") == "data"
    assert msstore._guid_name(blob[0x88:0x98]) == data_cloud
    assert msstore._guid_name(blob[0x98:0xA8]) == data_local
    assert blob[0xA8 : 0xA8 + 8].decode("utf-16-le") == "meta"
    assert msstore._guid_name(blob[0x128:0x138]) == meta_cloud
    assert msstore._guid_name(blob[0x138:0x148]) == meta_local


def test_containers_index_roundtrip(tmp_path):
    """Parse -> rebuild -> parse yields identical structures (header + every record)."""
    saves = [
        ("Slot1Auto", '{"a":1}', "A", "sa", 10, 5),
        ("Slot3Manual", '{"bb":2}', "B", "sb", 20, 7),
        ("AccountData", '{"acc":1}', "", "", 0, 0),
    ]
    acct = _build_wgs(tmp_path, saves)
    info1, conts1 = msstore.parse_containers_index(acct)

    rebuilt = msstore.build_containers_index(info1, conts1)
    (acct / "containers.index").write_bytes(rebuilt)
    info2, conts2 = msstore.parse_containers_index(acct)

    assert info2 == info1
    assert conts2 == conts1   # dataclass field-wise equality (incl. resolved blob paths/GUIDs)
    # and the whole-folder scan still decodes the same way after the rewrite
    view = msstore.scan(acct)
    assert view.slots[1].a.save_name == "A"
    assert view.slots[3].b.save_name == "B"


# --- writer (Phase 2b): mutate a wgs save in a sandbox fixture --------------------------


def test_write_save_updates_in_place(tmp_path):
    """Writing a save rotates its blob GUIDs, bumps container.<n>, flips sync states, writes
    the data bytes verbatim, and leaves exactly one container file with only the new blobs."""
    acct = _build_wgs(tmp_path, [
        ("Slot1Auto", '{"a":1}', "Old Name", "old", 100, 5),
        ("AccountData", '{"acc":1}', "", "", 0, 0),
    ])
    before = msstore.scan(acct).slots[1].a.xbox
    old_data_guid, old_meta_guid, old_ext = before.data_local_guid, before.meta_local_guid, before.extension

    payload = b'{"new":42}'
    new_data = _data_blob(payload)
    msstore.write_save(
        acct, "Slot1Auto", new_data,
        _ms_meta_worlds("New Name", "new summary", play=2000, size=len(payload), timestamp=999),
        when=123456.0,
    )

    a = msstore.scan(acct).slots[1].a
    assert a.exists and a.save_name == "New Name" and a.info.total_play_time == 2000
    x = a.xbox
    assert x.extension == old_ext + 1                     # container.<n> bumped 1 -> 2
    assert x.data_local_guid != old_data_guid             # blob GUID rotated
    assert x.sync_state == msstore.BLOB_SYNC_MODIFIED
    assert msstore.scan(acct).xbox_index.sync_state == msstore.INDEX_SYNC_MODIFIED
    # data bytes written verbatim
    assert (x.directory / x.data_local_guid).read_bytes() == new_data
    # old data AND meta blobs unlinked; exactly one container.* file remains
    assert not (x.directory / old_data_guid).exists()
    assert not (x.directory / old_meta_guid).exists()
    assert sorted(p.name for p in x.directory.glob("container.*")) == [f"container.{x.extension}"]
    # AccountData was left untouched
    assert msstore.scan(acct).account_present


def test_write_save_second_write_rotates_and_cleans(tmp_path):
    """A second write advances the extension again, deletes the FIRST write's blobs, and
    bumps the index last-write time."""
    acct = _build_wgs(tmp_path, [("Slot1Auto", '{"a":1}', "One", "", 1, 1)])
    msstore.write_save(acct, "Slot1Auto", _data_blob(b'{"v":1}'),
                       _ms_meta_worlds("V1", "", play=1, size=7, timestamp=1), when=100.0)
    x1 = msstore.scan(acct).slots[1].a.xbox            # ext now 2

    d2 = _data_blob(b'{"v":2}')
    msstore.write_save(acct, "Slot1Auto", d2,
                       _ms_meta_worlds("V2", "", play=2, size=7, timestamp=2), when=200.0)
    view = msstore.scan(acct)
    x2 = view.slots[1].a.xbox
    assert x2.extension == x1.extension + 1            # 2 -> 3
    assert view.slots[1].a.save_name == "V2"
    assert (x2.directory / x2.data_local_guid).read_bytes() == d2
    assert not (x2.directory / x1.data_local_guid).exists()   # first write's blobs gone
    assert not (x2.directory / x1.meta_local_guid).exists()
    assert sorted(p.name for p in x2.directory.glob("container.*")) == [f"container.{x2.extension}"]
    assert view.xbox_index.last_write == 200.0         # global index timestamp updated


def test_write_save_creates_new_slot(tmp_path):
    acct = _build_wgs(tmp_path, [("Slot1Auto", '{"a":1}', "Existing", "", 100, 5)])
    payload = b'{"z":9}'
    msstore.write_save(
        acct, "Slot5Manual", _data_blob(payload),
        _ms_meta_worlds("Fresh", "", play=10, size=len(payload), timestamp=1),
        when=222.0, create_if_missing=True,
    )
    view = msstore.scan(acct)
    assert view.slots[5].occupied and view.slots[5].b.save_name == "Fresh"   # Manual -> B
    assert view.slots[5].b.xbox.sync_state == msstore.BLOB_SYNC_CREATED
    assert view.slots[5].b.xbox.has_second_identifier is False               # single-id (5.50+) tree
    assert view.slots[1].a.save_name == "Existing"                            # untouched


def test_write_save_undeletes_a_deleted_record(tmp_path):
    """Writing content into a Deleted index record must un-delete it (Modified), else the
    Xbox app would garbage-collect the freshly-written save."""
    acct = _build_wgs(tmp_path, [("Slot2Manual", '{"a":1}', "Gone", "", 1, 1)])
    info, conts = msstore.parse_containers_index(acct)
    conts[0].sync_state = msstore.SYNC_STATE_DELETED
    (acct / "containers.index").write_bytes(msstore.build_containers_index(info, conts))

    payload = b'{"back":1}'
    msstore.write_save(acct, "Slot2Manual", _data_blob(payload),
                       _ms_meta_worlds("Back", "", play=5, size=len(payload), timestamp=9), when=300.0)
    view = msstore.scan(acct)
    assert view.slots[2].occupied and view.slots[2].b.save_name == "Back"
    assert view.slots[2].b.xbox.sync_state == msstore.BLOB_SYNC_MODIFIED


def test_write_save_new_save_mirrors_double_identifier(tmp_path):
    """A created save copies the tree's identifier convention: if siblings use the pre-5.50
    double-identifier form, the new record does too."""
    acct = _build_wgs(tmp_path, [("Slot1Auto", '{"a":1}', "Existing", "", 1, 1)])
    info, conts = msstore.parse_containers_index(acct)
    conts[0].second_identifier = conts[0].identifier        # pre-5.50 double-identifier tree
    (acct / "containers.index").write_bytes(msstore.build_containers_index(info, conts))

    payload = b'{"n":1}'
    msstore.write_save(acct, "Slot4Auto", _data_blob(payload),
                       _ms_meta_worlds("New", "", play=1, size=len(payload), timestamp=1),
                       when=10.0, create_if_missing=True)
    _info, conts2 = msstore.parse_containers_index(acct)
    new_rec = next(c for c in conts2 if c.identifier == "Slot4Auto")
    assert new_rec.second_identifier == "Slot4Auto"         # mirrored the double-identifier form


def test_write_save_missing_without_create_raises(tmp_path):
    acct = _build_wgs(tmp_path, [("Slot1Auto", '{"a":1}', "X", "", 1, 1)])
    with pytest.raises(ValueError):
        msstore.write_save(acct, "Slot9Auto", _data_blob(b"{}"), b"\x00" * 360, when=1.0)
