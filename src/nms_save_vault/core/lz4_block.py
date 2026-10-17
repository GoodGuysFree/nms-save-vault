"""Pure-Python reader for the NMS save *data* container (no third-party deps).

A ``saveN.hg`` is a sequence of chunks:
    [magic 0xFEEDA1E5 u32][compressed_size u32][decompressed_size u32][0 u32] + LZ4-block

This module walks that container and (for deep verification) LZ4-block-decodes it. Only
*decoding* is implemented -- the app never recompresses, it copies data verbatim.

Pre-Waypoint Microsoft saves predate that framing: the whole save is ONE bare LZ4 block
with no chunk headers, so nothing on the wire says how big it decompresses to. The size
lives in the wgs meta instead, and the caller passes it in (see :func:`decompress`).
Xbox saves of game versions 4.52-4.99 use a third container, ``HGSAVEV2``; it is
recognised and refused (:class:`UnsupportedContainer`), not decoded.

The decoder is hardened against damaged input: every size a header claims is capped before
anything is allocated, every match must stay inside the output produced so far, and any
inconsistency raises :class:`LZ4FormatError` (a ``ValueError``) instead of decoding to
wrong bytes.

Note: the game does not always truncate ``saveN.hg`` when a later save is smaller, so the
file may have trailing stale bytes after the last valid chunk. ``walk_chunks`` reports the
number of *valid* bytes consumed (which equals the meta's ``size_disk``).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import formats


class LZ4FormatError(ValueError):
    """A block or container that cannot be LZ4-decoded: corrupt, truncated or inconsistent."""


class UnsupportedContainer(ValueError):
    """A recognised save container this version cannot decode (Xbox HGSAVEV2, game 4.52-4.99)."""


HGSAVEV2_HEADER = b"HGSAVEV2\x00"
HGSAVEV2_UNSUPPORTED = (
    "this save uses the HGSAVEV2 container (Xbox, game versions 4.52-4.99), which this "
    "version cannot read"
)
MAX_SAVE_DECOMPRESSED = 256 * 1024 * 1024     # 14x the largest real save (18.8 MB of JSON)
MAX_BARE_BLOCK = 64 * 1024 * 1024             # pre-4.52 Xbox saves are 6-7 MB
# LZ4 cannot expand an input byte into more than 255 output bytes (each 255 in a length
# extension adds 255); a claim beyond that is a damaged header, refused before allocating.
MAX_EXPANSION = 255


def _max_output(compressed: int) -> int:
    return compressed * MAX_EXPANSION + 64


def _check_claim(claimed: int, compressed: int, limit: int, what: str) -> None:
    if claimed < 0:
        raise LZ4FormatError(f"{what} claims a negative size ({claimed})")
    if claimed > limit:
        raise LZ4FormatError(f"{what} claims {claimed} bytes, more than {limit}")
    if claimed > _max_output(compressed):
        raise LZ4FormatError(f"{what} claims {claimed} bytes, more than its {compressed} bytes can hold")


@dataclass(frozen=True)
class Chunk:
    payload_offset: int
    compressed_size: int
    decompressed_size: int


def walk_chunks(data: bytes) -> tuple[list[Chunk], int]:
    """Return (chunks, bytes_consumed) for the leading run of valid chunks.

    The run ends at the first header that cannot start a chunk: wrong magic, a payload past
    the end, or a size no chunk can have (beyond the format's per-chunk maximum, or more
    than its payload can expand to). What follows is trailing data, not an error; callers
    catch a short run by comparing the total with the meta's decompressed size.
    """
    chunks: list[Chunk] = []
    off = 0
    n = len(data)
    while off + formats.SAVE_CHUNK_HEADER_LENGTH <= n:
        magic, comp, dec, _zero = struct.unpack_from("<4I", data, off)
        if magic != formats.SAVE_MAGIC:
            break
        payload = off + formats.SAVE_CHUNK_HEADER_LENGTH
        if payload + comp > n:
            break  # truncated/corrupt trailing chunk
        if dec > formats.SAVE_CHUNK_DECOMPRESSED_MAX or dec > _max_output(comp):
            break
        chunks.append(Chunk(payload, comp, dec))
        off = payload + comp
    return chunks, off


@dataclass(frozen=True)
class ContainerStats:
    chunk_count: int
    consumed: int            # valid compressed bytes (== meta.size_disk)
    total_decompressed: int  # == meta.size_decompressed
    file_size: int
    trailing: int            # stale bytes after the last valid chunk

    @property
    def looks_valid(self) -> bool:
        return self.chunk_count > 0 and self.consumed <= self.file_size


def stats(data: bytes) -> ContainerStats:
    """Cheap structural summary without decompressing payloads."""
    chunks, consumed = walk_chunks(data)
    total = sum(c.decompressed_size for c in chunks)
    return ContainerStats(
        chunk_count=len(chunks),
        consumed=consumed,
        total_decompressed=total,
        file_size=len(data),
        trailing=len(data) - consumed,
    )


def decompress_block(
    src: bytes, expected_size: int, *, exact: bool = False, limit: int = MAX_SAVE_DECOMPRESSED
) -> bytes:
    """Decode a single raw LZ4 block into at most ``expected_size`` bytes.

    With ``exact`` the block must produce exactly ``expected_size`` bytes; without it a
    shorter output is returned as it is. Raises :class:`LZ4FormatError` for anything
    inconsistent, including a size over ``limit`` or one the block could not possibly
    produce (both checked before anything is allocated).
    """
    _check_claim(expected_size, len(src), limit, "an LZ4 block")
    out = bytearray(expected_size)
    s = 0
    d = 0
    n = len(src)

    def length_extension() -> int:
        nonlocal s
        total = 0
        while True:
            if s >= n:
                raise LZ4FormatError("an LZ4 length runs past the end of the block")
            b = src[s]
            s += 1
            total += b
            if b != 255:
                return total

    while s < n:
        token = src[s]
        s += 1
        lit = token >> 4
        if lit == 15:
            lit += length_extension()
        if lit:
            end = s + lit
            if end > n:
                raise LZ4FormatError("LZ4 literals run past the end of the block")
            if d + lit > expected_size:
                raise LZ4FormatError("an LZ4 block decodes to more bytes than it declares")
            out[d : d + lit] = src[s:end]
            s = end
            d += lit
        if s >= n:
            break  # last sequence: literals only
        if s + 2 > n:
            raise LZ4FormatError("an LZ4 match offset is cut off")
        offset = src[s] | (src[s + 1] << 8)
        s += 2
        if offset == 0 or offset > d:
            raise LZ4FormatError(f"an LZ4 match offset ({offset}) points outside the output")
        mlen = token & 0x0F
        if mlen == 15:
            mlen += length_extension()
        mlen += 4  # minmatch
        if d + mlen > expected_size:
            raise LZ4FormatError("an LZ4 block decodes to more bytes than it declares")
        mpos = d - offset
        if offset >= mlen:
            out[d : d + mlen] = out[mpos : mpos + mlen]
        else:
            # An overlapping match repeats the last ``offset`` bytes: copy that period.
            period = out[mpos:d]
            whole, part = divmod(mlen, offset)
            out[d : d + mlen] = period * whole + period[:part]
        d += mlen
    if exact and d != expected_size:
        raise LZ4FormatError(f"an LZ4 block decoded to {d} bytes, not the {expected_size} declared")
    return bytes(out) if d == expected_size else bytes(out[:d])


def is_chunked(data: bytes) -> bool:
    """True when the container uses the 0xFEEDA1E5 chunk framing (everything post-Waypoint,
    and every Steam save). False means a bare LZ4 block -- an old Microsoft save."""
    return data[: len(formats.SAVE_STREAMING_HEADER)] == formats.SAVE_STREAMING_HEADER


def decode_chunk(data: bytes, chunk: Chunk) -> bytes:
    """One chunk (from :func:`walk_chunks`, which vetted its size), exactly as large as its
    header says."""
    end = chunk.payload_offset + chunk.compressed_size
    return decompress_block(data[chunk.payload_offset : end], chunk.decompressed_size, exact=True)


def decompress(data: bytes, expected_size: int | None = None) -> bytes:
    """Fully decompress the save container into the concatenated (obfuscated) JSON bytes.

    ``expected_size`` is used only for a bare, unframed block (an old Microsoft save),
    which carries no decompressed size of its own -- pass the wgs meta's size field; the
    block must decode to exactly that (as every real one does). A chunked container
    carries its own sizes and ignores it; their total is capped at
    ``MAX_SAVE_DECOMPRESSED`` before anything is decoded. An HGSAVEV2 container raises
    :class:`UnsupportedContainer`.
    """
    if data.startswith(HGSAVEV2_HEADER):
        raise UnsupportedContainer(HGSAVEV2_UNSUPPORTED)
    chunks, _consumed = walk_chunks(data)
    if chunks:
        total = sum(c.decompressed_size for c in chunks)
        if total > MAX_SAVE_DECOMPRESSED:
            raise LZ4FormatError(f"the save claims {total} bytes in all, more than {MAX_SAVE_DECOMPRESSED}")
        return b"".join(decode_chunk(data, c) for c in chunks)
    if expected_size:
        return decompress_block(data, expected_size, exact=True, limit=MAX_BARE_BLOCK)
    raise ValueError("no valid save chunks found, and no expected size for a bare LZ4 block")
