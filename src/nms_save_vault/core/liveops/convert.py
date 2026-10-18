"""Steam <-> Xbox meta conversion (D5 3.5.2, F6): pure functions, no I/O.

The data blob is never touched: both platforms hold the same ``0xFEEDA1E5`` chunk stream, only
the meta differs. A meta is converted field by field; anything that cannot be mapped with
certainty is refused with :class:`ConvertUnsupported` (code ``CONVERT_UNSUPPORTED``) instead
of being guessed: another meta length or format, a data blob that is not a clean current
chunk stream (HGSAVEV2, single LZ4, plain JSON), a meta written by a newer game than the app
knows, or a Steam meta whose dropped fields are not the zeros the map expects.
"""
from __future__ import annotations

import struct

from .. import formats, gameversion, lz4_block, meta, msstore

CONVERT_UNSUPPORTED = "CONVERT_UNSUPPORTED"

STEAM_LEN = formats.META_LEN_WORLDS_II          # 432
XBOX_LEN = msstore.MS_META_LEN_WORLDS_II        # 360

# Steam plaintext offsets that have no Xbox counterpart (D5 3.5.2 table).
_OFF_HASH_AREA, _HASH_AREA_END = 0x08, 0x38
_OFF_PROFILE_HASH = 0x40
_OFF_UNNAMED = 0x54                              # == SizeDecompressed in game-written metas
_OFF_TAIL = 0x1AC
_OFF_TAG_STEAM, _OFF_TAG_XBOX, _TAG_LEN = 0x16C, 0x128, 64


class ConvertUnsupported(ValueError):
    """A meta that cannot be converted with certainty. ``reason`` is a short machine word."""

    code = CONVERT_UNSUPPORTED

    def __init__(self, reason: str, text: str) -> None:
        super().__init__(text)
        self.reason = reason


def chunk_total(data: bytes) -> int:
    """The decompressed size of a clean current chunk stream; refuses anything else."""
    if data[:4] != formats.SAVE_STREAMING_HEADER:
        raise ConvertUnsupported("data_format", "the save data is not a current chunk stream "
                                 "(an older format: load and save it once in the game first)")
    try:
        stats = lz4_block.stats(data)
    except Exception as exc:  # noqa: BLE001 - any walk failure means "not a clean stream"
        raise ConvertUnsupported("data_walk", f"the save data cannot be read ({exc})") from exc
    if stats.chunk_count < 1 or stats.trailing != 0:
        raise ConvertUnsupported("data_walk", "the save data is not a clean chunk stream")
    return stats.total_decompressed


def steam_to_xbox(plain: bytes, data: bytes, ts: int) -> bytes:
    """A decrypted Steam meta (432 B, format 2004) as an Xbox plaintext meta (360 B)."""
    if len(plain) != STEAM_LEN:
        raise ConvertUnsupported("meta_length", f"a Steam meta of {len(plain)} bytes")
    if struct.unpack_from("<I", plain, formats.OFF_HEADER)[0] != formats.META_HEADER:
        raise ConvertUnsupported("meta_header", "the Steam meta is not decrypted")
    fmt = _steam_format(plain)
    total = chunk_total(data)
    info = meta.parse(plain, 0)
    _refuse_newer("steam", info.meta_format, info.base_version)
    if info.size_decompressed != total:
        raise ConvertUnsupported("size_mismatch", "the Steam meta's size does not match its data")
    if any(plain[_OFF_HASH_AREA:_HASH_AREA_END]) or any(plain[_OFF_PROFILE_HASH:_OFF_PROFILE_HASH + 4]) \
            or any(plain[_OFF_TAIL:STEAM_LEN]):
        raise ConvertUnsupported("unmapped_field", "the Steam meta has data in a field with no "
                                 "Xbox counterpart")
    out = bytearray(XBOX_LEN)
    struct.pack_into("<I", out, msstore.MS_OFF_BASE_VERSION, info.base_version)
    struct.pack_into("<H", out, msstore.MS_OFF_GAME_MODE, info.game_mode)
    struct.pack_into("<H", out, msstore.MS_OFF_SEASON, info.season)
    struct.pack_into("<Q", out, msstore.MS_OFF_TOTAL_PLAY_TIME, info.total_play_time)
    struct.pack_into("<I", out, msstore.MS_OFF_SIZE, total)
    _copy(plain, formats.OFF_SAVE_NAME, out, msstore.MS_OFF_NAME, formats.NAME_FIELD_LENGTH)
    _copy(plain, formats.OFF_SAVE_SUMMARY, out, msstore.MS_OFF_SUMMARY,
          formats.SUMMARY_FIELD_LENGTH)
    struct.pack_into("<I", out, msstore.MS_OFF_DIFFICULTY, info.difficulty)
    _copy(plain, formats.OFF_SLOT_IDENTIFIER, out, msstore.MS_OFF_SLOT_ID, 8)
    struct.pack_into("<I", out, msstore.MS_OFF_TIMESTAMP, _u32(ts))
    struct.pack_into("<I", out, msstore.MS_OFF_META_FORMAT, fmt)
    _copy(plain, _OFF_TAG_STEAM, out, _OFF_TAG_XBOX, _TAG_LEN)
    result = bytes(out)
    fields = msstore.parse_ms_meta(result)
    if (fields["name"], fields["summary"], fields["slot_id"], fields["total_play_time"],
            fields["difficulty"], fields["timestamp"], fields["size_field"]) != (
            info.save_name, info.save_summary, info.slot_identifier, info.total_play_time,
            info.difficulty, _u32(ts), total):
        raise ConvertUnsupported("verify", "the converted Xbox meta does not read back the same")
    return result


def xbox_to_steam_plain(xmeta: bytes, data: bytes, ts: int) -> bytes:
    """An Xbox plaintext meta (360 B, format 2004) as a decrypted Steam meta (432 B)."""
    if len(xmeta) != XBOX_LEN:
        raise ConvertUnsupported("meta_length", f"an Xbox meta of {len(xmeta)} bytes")
    fields = msstore.parse_ms_meta(xmeta)
    fmt = fields["meta_format"]
    if fmt != formats.META_FORMAT_4:
        raise ConvertUnsupported("meta_format", f"an Xbox meta of format {fmt}")
    _refuse_newer("xbox", fmt, fields["base_version"])
    total = chunk_total(data)
    out = bytearray(STEAM_LEN)
    struct.pack_into("<I", out, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", out, formats.OFF_META_FORMAT, fmt)
    struct.pack_into("<I", out, formats.OFF_SIZE_DECOMPRESSED, total)
    struct.pack_into("<I", out, formats.OFF_SIZE_DISK, len(data))
    struct.pack_into("<I", out, formats.OFF_BASE_VERSION, fields["base_version"])
    struct.pack_into("<H", out, formats.OFF_GAME_MODE, fields["game_mode"])
    struct.pack_into("<H", out, formats.OFF_SEASON, struct.unpack_from(
        "<H", xmeta, msstore.MS_OFF_SEASON)[0])
    struct.pack_into("<Q", out, formats.OFF_TOTAL_PLAY_TIME, fields["total_play_time"])
    struct.pack_into("<I", out, _OFF_UNNAMED, total)
    _copy(xmeta, msstore.MS_OFF_NAME, out, formats.OFF_SAVE_NAME, formats.NAME_FIELD_LENGTH)
    _copy(xmeta, msstore.MS_OFF_SUMMARY, out, formats.OFF_SAVE_SUMMARY,
          formats.SUMMARY_FIELD_LENGTH)
    struct.pack_into("<I", out, formats.OFF_DIFFICULTY, fields["difficulty"])
    _copy(xmeta, msstore.MS_OFF_SLOT_ID, out, formats.OFF_SLOT_IDENTIFIER, 8)
    struct.pack_into("<I", out, formats.OFF_TIMESTAMP, _u32(ts))
    struct.pack_into("<I", out, formats.OFF_META_FORMAT_TAIL, fmt)
    _copy(xmeta, _OFF_TAG_XBOX, out, _OFF_TAG_STEAM, _TAG_LEN)
    result = bytes(out)
    info = meta.parse(result, 0)
    if (info.save_name, info.save_summary, info.slot_identifier, info.total_play_time,
            info.difficulty, info.timestamp, info.size_decompressed) != (
            fields["name"], fields["summary"], fields["slot_id"], fields["total_play_time"],
            fields["difficulty"], _u32(ts), total):
        raise ConvertUnsupported("verify", "the converted Steam meta does not read back the same")
    return result


def xbox_to_steam(xmeta: bytes, data: bytes, ts: int, ordinal: int) -> bytes:
    """The Steam meta file bytes (432 B, encrypted for ``ordinal``) of an Xbox meta."""
    disk = meta.encrypt(xbox_to_steam_plain(xmeta, data, ts), ordinal)
    if not meta.is_valid_for(disk, ordinal):
        raise ConvertUnsupported("verify", "the converted Steam meta does not decrypt for its slot")
    return disk


def _steam_format(plain: bytes) -> int:
    head = struct.unpack_from("<I", plain, formats.OFF_META_FORMAT)[0]
    tail = struct.unpack_from("<I", plain, formats.OFF_META_FORMAT_TAIL)[0]
    if head != formats.META_FORMAT_4 or tail != head:
        raise ConvertUnsupported("meta_format", f"a Steam meta of format {head}/{tail}")
    return head


def _refuse_newer(platform: str, meta_format: int, base_version: int) -> None:
    if gameversion.newer(platform, meta_format, base_version):
        raise ConvertUnsupported("GAME_NEWER", gameversion.GAME_NEWER_TEXT)


def _copy(src: bytes, s_off: int, dst: bytearray, d_off: int, length: int) -> None:
    dst[d_off:d_off + length] = src[s_off:s_off + length]


def _u32(value: int) -> int:
    return int(value) & 0xFFFFFFFF
