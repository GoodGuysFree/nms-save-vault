"""Microsoft / Xbox ('wgs') reader, tested against a synthetic but spec-accurate fixture.

No real Game Pass install is needed: we hand-build a valid containers.index + container
blob + a 0xFEEDA1E5 data blob + a plaintext MS meta, then assert the reader decodes it.
"""
from __future__ import annotations

import struct
import uuid

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
