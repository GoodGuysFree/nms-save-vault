"""Pure-Python reader for the NMS save *data* container (no third-party deps).

A ``saveN.hg`` is a sequence of chunks:
    [magic 0xFEEDA1E5 u32][compressed_size u32][decompressed_size u32][0 u32] + LZ4-block

This module walks that container and (for deep verification) LZ4-block-decodes it. Only
*decoding* is implemented -- the app never recompresses, it copies data verbatim.

Note: the game does not always truncate ``saveN.hg`` when a later save is smaller, so the
file may have trailing stale bytes after the last valid chunk. ``walk_chunks`` reports the
number of *valid* bytes consumed (which equals the meta's ``size_disk``).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import formats


@dataclass(frozen=True)
class Chunk:
    payload_offset: int
    compressed_size: int
    decompressed_size: int


def walk_chunks(data: bytes) -> tuple[list[Chunk], int]:
    """Return (chunks, bytes_consumed) for the leading run of valid chunks."""
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


def decompress_block(src: bytes, expected_size: int) -> bytes:
    """Decode a single raw LZ4 block into at most ``expected_size`` bytes."""
    out = bytearray(expected_size)
    s = 0
    d = 0
    n = len(src)
    while s < n:
        token = src[s]
        s += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[s]
                s += 1
                lit += b
                if b != 255:
                    break
        out[d : d + lit] = src[s : s + lit]
        s += lit
        d += lit
        if s >= n:
            break  # last sequence: literals only
        offset = src[s] | (src[s + 1] << 8)
        s += 2
        mlen = token & 0x0F
        if mlen == 15:
            while True:
                b = src[s]
                s += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4  # minmatch
        mpos = d - offset
        if offset == 0:
            raise ValueError("invalid LZ4 match offset 0")
        for _ in range(mlen):
            out[d] = out[mpos]
            d += 1
            mpos += 1
    return bytes(out[:d])


def decompress(data: bytes) -> bytes:
    """Fully decompress the save container into the concatenated (obfuscated) JSON bytes."""
    chunks, _consumed = walk_chunks(data)
    if not chunks:
        raise ValueError("no valid save chunks found")
    parts = [decompress_block(data[c.payload_offset : c.payload_offset + c.compressed_size], c.decompressed_size) for c in chunks]
    return b"".join(parts)
