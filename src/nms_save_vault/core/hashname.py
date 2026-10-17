"""The obfuscated save-file key of a readable field name.

The game writes save JSON with three-character keys (``"6f="`` for ``PlayerStateData``).
Each key is a hash of the field's name: MBINCompiler's ``HashName``
(``SaveFileMapping/Program.cs``, credited to AndASM) runs SpookyHash V2 over the UTF-8
name with both seeds set to :data:`SEED` and takes three characters from the first 64-bit
result ``h``: ``h % 68``, ``(h >> 21) % 68`` and ``(h >> 42) % 68``, each shifted value
taken whole (not masked to 21 bits). Computing keys this way means no mapping file ships,
and a field keeps its key for as long as the game keeps its name.

SpookyHash V2 is Bob Jenkins' public-domain hash; only its short path (messages under 192
bytes) is ported, which covers every field name (the longest real one is 60 bytes).
"""
from __future__ import annotations

import functools
import struct

SEED = 8268756125562466087
_SC_CONST = 0xDEADBEEFDEADBEEF
_MASK = (1 << 64) - 1
SHORT_LIMIT = 192


def _rot(x: int, k: int) -> int:
    return ((x << k) | (x >> (64 - k))) & _MASK


def _short_mix(h0: int, h1: int, h2: int, h3: int) -> tuple[int, int, int, int]:
    h2 = _rot(h2, 50); h2 = (h2 + h3) & _MASK; h0 ^= h2  # noqa: E702 - one ShortMix line each
    h3 = _rot(h3, 52); h3 = (h3 + h0) & _MASK; h1 ^= h3  # noqa: E702
    h0 = _rot(h0, 30); h0 = (h0 + h1) & _MASK; h2 ^= h0  # noqa: E702
    h1 = _rot(h1, 41); h1 = (h1 + h2) & _MASK; h3 ^= h1  # noqa: E702
    h2 = _rot(h2, 54); h2 = (h2 + h3) & _MASK; h0 ^= h2  # noqa: E702
    h3 = _rot(h3, 48); h3 = (h3 + h0) & _MASK; h1 ^= h3  # noqa: E702
    h0 = _rot(h0, 38); h0 = (h0 + h1) & _MASK; h2 ^= h0  # noqa: E702
    h1 = _rot(h1, 37); h1 = (h1 + h2) & _MASK; h3 ^= h1  # noqa: E702
    h2 = _rot(h2, 62); h2 = (h2 + h3) & _MASK; h0 ^= h2  # noqa: E702
    h3 = _rot(h3, 34); h3 = (h3 + h0) & _MASK; h1 ^= h3  # noqa: E702
    h0 = _rot(h0, 5); h0 = (h0 + h1) & _MASK; h2 ^= h0  # noqa: E702
    h1 = _rot(h1, 36); h1 = (h1 + h2) & _MASK; h3 ^= h1  # noqa: E702
    return h0, h1, h2, h3


def _short_end(h0: int, h1: int, h2: int, h3: int) -> tuple[int, int, int, int]:
    h3 ^= h2; h2 = _rot(h2, 15); h3 = (h3 + h2) & _MASK  # noqa: E702 - one ShortEnd line each
    h0 ^= h3; h3 = _rot(h3, 52); h0 = (h0 + h3) & _MASK  # noqa: E702
    h1 ^= h0; h0 = _rot(h0, 26); h1 = (h1 + h0) & _MASK  # noqa: E702
    h2 ^= h1; h1 = _rot(h1, 51); h2 = (h2 + h1) & _MASK  # noqa: E702
    h3 ^= h2; h2 = _rot(h2, 28); h3 = (h3 + h2) & _MASK  # noqa: E702
    h0 ^= h3; h3 = _rot(h3, 9); h0 = (h0 + h3) & _MASK  # noqa: E702
    h1 ^= h0; h0 = _rot(h0, 47); h1 = (h1 + h0) & _MASK  # noqa: E702
    h2 ^= h1; h1 = _rot(h1, 54); h2 = (h2 + h1) & _MASK  # noqa: E702
    h3 ^= h2; h2 = _rot(h2, 32); h3 = (h3 + h2) & _MASK  # noqa: E702
    h0 ^= h3; h3 = _rot(h3, 25); h0 = (h0 + h3) & _MASK  # noqa: E702
    h1 ^= h0; h0 = _rot(h0, 63); h1 = (h1 + h0) & _MASK  # noqa: E702
    return h0, h1, h2, h3


def spooky_short(message: bytes, seed1: int, seed2: int) -> tuple[int, int]:
    """SpookyHash V2 ``Short`` (messages under 192 bytes): the two 64-bit result words."""
    length = len(message)
    if length >= SHORT_LIMIT:
        raise ValueError(f"spooky_short takes under {SHORT_LIMIT} bytes, got {length}")
    a, b, c, d = seed1 & _MASK, seed2 & _MASK, _SC_CONST, _SC_CONST
    p = 0
    rem = length % 32
    if length > 15:
        for _ in range(length // 32):
            w0, w1, w2, w3 = struct.unpack_from("<4Q", message, p)
            c = (c + w0) & _MASK
            d = (d + w1) & _MASK
            a, b, c, d = _short_mix(a, b, c, d)
            a = (a + w2) & _MASK
            b = (b + w3) & _MASK
            p += 32
        if rem >= 16:
            w0, w1 = struct.unpack_from("<2Q", message, p)
            c = (c + w0) & _MASK
            d = (d + w1) & _MASK
            a, b, c, d = _short_mix(a, b, c, d)
            p += 16
            rem -= 16
    d = (d + (length << 56)) & _MASK
    tail = message[p : p + rem]
    if rem == 0:
        c = (c + _SC_CONST) & _MASK
        d = (d + _SC_CONST) & _MASK
    elif rem >= 8:
        c = (c + int.from_bytes(tail[:8], "little")) & _MASK
        d = (d + int.from_bytes(tail[8:], "little")) & _MASK
    else:
        c = (c + int.from_bytes(tail, "little")) & _MASK
    a, b, _c, _d = _short_end(a, b, c, d)
    return a, b


def hash_name(name: str) -> str:
    """The 3-character obfuscated save key of a readable field name.

    Characters are ``'0' + (h % 68)``, moved up 6 past ``'Z'``: ``0-9 : ; < = > ? @ A-Z a-y``.
    Raises ``ValueError`` for a name of 192 or more UTF-8 bytes.
    """
    h1, _h2 = spooky_short(name.encode("utf-8"), SEED, SEED)
    out = []
    for shift in (0, 21, 42):
        # The whole shifted value modulo 68, as Program.cs does. Do NOT mask it to 21 bits
        # first: (h >> 21) & 0x1FFFFF gives different keys for most names.
        ch = (h1 >> shift) % 68 + ord("0")
        if ch > ord("Z"):
            ch += 6
        out.append(chr(ch))
    return "".join(out)


@functools.lru_cache(maxsize=None)
def key(name: str) -> str:
    """:func:`hash_name`, remembered: extractors call it with the same few hundred names."""
    return hash_name(name)
