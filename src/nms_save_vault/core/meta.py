"""Meta (``mf_*.hg``) crypto and field access for Steam/GOG NMS saves.

The meta is a fixed-size record encrypted with an XXTEA variant whose first key word is
derived from the file's storage ordinal -- so the meta is *slot-bound*. This module ports
the exact algorithm from libNOM.io (verified to round-trip on the user's real files) and
exposes helpers to read fields, edit the timestamp, and re-key a meta to another slot.

Attribution: the decrypt/encrypt routines here are a port of ``DecryptMetaStorageEntry`` /
``EncryptMeta`` from libNOM.io by Christian Engelhardt (zencq),
https://github.com/zencq/libNOM.io, licensed GPL-3.0. Because of that this whole project
is distributed under GPL-3.0 (see LICENSE and the credits in README.md).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import formats

_MASK = 0xFFFFFFFF


def _rotl(x: int, n: int) -> int:
    x &= _MASK
    return ((x << n) | (x >> (32 - n))) & _MASK


def slot_key(ordinal: int) -> tuple[int, int, int, int]:
    """XXTEA key for a given storage ordinal (key[0] derived, key[1:] constant)."""
    k0 = (_rotl((ordinal ^ formats.KEY_CONST_XOR) & _MASK, 13) * 5 + formats.KEY_CONST_ADD) & _MASK
    k = formats.META_ENCRYPTION_KEY
    return (k0, k[1], k[2], k[3])


def _iterations(byte_len: int) -> int:
    # Vanilla meta uses 8 rounds; Waypoint/Worlds use 6.
    return 8 if byte_len == formats.META_LEN_VANILLA else 6


def _to_words(data: bytes) -> list[int]:
    n = len(data) // 4
    return list(struct.unpack(f"<{n}I", data[: n * 4]))


def _to_bytes(words: list[int]) -> bytes:
    return struct.pack(f"<{len(words)}I", *words)


def decrypt(meta: bytes, ordinal: int) -> bytes:
    """Decrypt a meta blob with the key for ``ordinal`` (inverse of :func:`encrypt`)."""
    key = slot_key(ordinal)
    r = _to_words(meta)
    last = len(r) - 1
    iterations = _iterations(len(meta))

    hsh = 0
    for _ in range(iterations):
        hsh = (hsh + formats.XXTEA_DELTA) & _MASK

    for _ in range(iterations):
        current = r[0]
        key_index = (hsh >> 2) & 3
        vi = last
        for j in range(last, 0, -1):
            prev = r[vi - 1]
            a = (((current >> 3) ^ ((prev << 4) & _MASK)) + (((current * 4) & _MASK) ^ (prev >> 5))) & _MASK
            b = ((prev ^ key[(j & 3) ^ key_index]) + (current ^ hsh)) & _MASK
            r[vi] = (r[vi] - (a ^ b)) & _MASK
            current = r[vi]
            vi -= 1
        last_word = r[last]
        a = (((current >> 3) ^ ((last_word << 4) & _MASK)) + (((current * 4) & _MASK) ^ (last_word >> 5))) & _MASK
        b = ((last_word ^ key[key_index]) + (current ^ hsh)) & _MASK
        r[0] = (r[0] - (a ^ b)) & _MASK
        hsh = (hsh + formats.XXTEA_DELTA_NEG) & _MASK

    return _to_bytes(r)


def encrypt(plain: bytes, ordinal: int) -> bytes:
    """Encrypt a decrypted meta blob with the key for ``ordinal``."""
    key = slot_key(ordinal)
    r = _to_words(plain)
    last = len(r) - 1
    iterations = _iterations(len(plain))

    current = 0
    hsh = 0
    for _ in range(iterations):
        hsh = (hsh + formats.XXTEA_DELTA) & _MASK
        key_index = (hsh >> 2) & 3
        vi = 0
        for j in range(0, last):
            nxt = r[vi + 1]
            a = (((nxt >> 3) ^ ((current << 4) & _MASK)) + (((nxt * 4) & _MASK) ^ (current >> 5))) & _MASK
            b = ((current ^ key[(j & 3) ^ key_index]) + (nxt ^ hsh)) & _MASK
            r[vi] = (r[vi] + (a ^ b)) & _MASK
            current = r[vi]
            vi += 1
        first = r[0]
        a = (((first >> 3) ^ ((current << 4) & _MASK)) + (((first * 4) & _MASK) ^ (current >> 5))) & _MASK
        b = ((current ^ key[(last & 3) ^ key_index]) + (first ^ hsh)) & _MASK
        r[last] = (r[last] + (a ^ b)) & _MASK
        current = r[last]

    return _to_bytes(r)


def is_valid_for(meta: bytes, ordinal: int) -> bool:
    """True if decrypting ``meta`` with ``ordinal`` yields the expected header."""
    if len(meta) not in formats.META_LENGTHS_KNOWN:
        return False
    plain = decrypt(meta, ordinal)
    return struct.unpack_from("<I", plain, formats.OFF_HEADER)[0] == formats.META_HEADER


def decrypt_autodetect(meta: bytes, preferred_ordinal: int | None = None) -> tuple[int, bytes]:
    """Decrypt, trying ``preferred_ordinal`` first then every ordinal (handles files that
    were manually moved between slots). Returns ``(ordinal_used, plaintext)``.

    Raises ValueError if no ordinal produces the header.
    """
    if len(meta) not in formats.META_LENGTHS_KNOWN:
        raise ValueError(f"unexpected meta length {len(meta)}")
    candidates: list[int] = []
    if preferred_ordinal is not None:
        candidates.append(preferred_ordinal)
    # AccountData(1) .. PlayerState30(31)
    candidates += [o for o in range(1, formats.MAX_SAVE_FILES + 2) if o != preferred_ordinal]
    for ordinal in candidates:
        plain = decrypt(meta, ordinal)
        if struct.unpack_from("<I", plain, formats.OFF_HEADER)[0] == formats.META_HEADER:
            return ordinal, plain
    raise ValueError("no storage ordinal produced a valid meta header")


def re_key(meta: bytes, src_ordinal: int, dst_ordinal: int) -> bytes:
    """Return ``meta`` re-encrypted for ``dst_ordinal`` (decrypt with src, encrypt with dst).

    Validates the round-trip. The decrypted plaintext (incl. all fields) is preserved
    exactly; only the encryption key changes.
    """
    plain = decrypt(meta, src_ordinal)
    if struct.unpack_from("<I", plain, formats.OFF_HEADER)[0] != formats.META_HEADER:
        raise ValueError(f"source meta does not decrypt with ordinal {src_ordinal}")
    out = encrypt(plain, dst_ordinal)
    if not is_valid_for(out, dst_ordinal):
        raise ValueError("re-key round-trip validation failed")
    return out


def set_timestamp(plain: bytes, unix_ts: int) -> bytes:
    """Return decrypted meta plaintext with the load-screen timestamp replaced."""
    buf = bytearray(plain)
    struct.pack_into("<I", buf, formats.OFF_TIMESTAMP, unix_ts & _MASK)
    return bytes(buf)


def _read_string(plain: bytes, offset: int, length: int) -> str:
    raw = plain[offset : offset + length]
    nul = raw.find(b"\x00")
    if nul >= 0:
        raw = raw[:nul]
    return raw.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class MetaInfo:
    """Human-meaningful fields parsed from a decrypted meta."""

    ordinal: int
    header: int
    meta_format: int
    size_decompressed: int
    size_disk: int
    base_version: int
    game_mode: int
    season: int
    total_play_time: int
    save_name: str
    save_summary: str
    difficulty: int
    slot_identifier: int
    timestamp: int

    @property
    def valid(self) -> bool:
        return self.header == formats.META_HEADER


def parse(plain: bytes, ordinal: int) -> MetaInfo:
    """Parse a *decrypted* meta into a :class:`MetaInfo` (tolerant of shorter formats)."""

    def u32(off: int) -> int:
        return struct.unpack_from("<I", plain, off)[0] if off + 4 <= len(plain) else 0

    def i32(off: int) -> int:
        return struct.unpack_from("<i", plain, off)[0] if off + 4 <= len(plain) else 0

    def u16(off: int) -> int:
        return struct.unpack_from("<H", plain, off)[0] if off + 2 <= len(plain) else 0

    def u64(off: int) -> int:
        return struct.unpack_from("<Q", plain, off)[0] if off + 8 <= len(plain) else 0

    return MetaInfo(
        ordinal=ordinal,
        header=u32(formats.OFF_HEADER),
        meta_format=u32(formats.OFF_META_FORMAT),
        size_decompressed=u32(formats.OFF_SIZE_DECOMPRESSED),
        size_disk=u32(formats.OFF_SIZE_DISK),
        base_version=i32(formats.OFF_BASE_VERSION),
        game_mode=u16(formats.OFF_GAME_MODE),
        season=u16(formats.OFF_SEASON),
        total_play_time=u64(formats.OFF_TOTAL_PLAY_TIME),
        save_name=_read_string(plain, formats.OFF_SAVE_NAME, formats.NAME_FIELD_LENGTH),
        save_summary=_read_string(plain, formats.OFF_SAVE_SUMMARY, formats.SUMMARY_FIELD_LENGTH),
        difficulty=u32(formats.OFF_DIFFICULTY),
        slot_identifier=u64(formats.OFF_SLOT_IDENTIFIER),
        timestamp=u32(formats.OFF_TIMESTAMP),
    )
