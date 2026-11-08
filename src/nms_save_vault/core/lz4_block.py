"""Pure-Python reader for the NMS save *data* container (no third-party deps).

A ``saveN.hg`` is a sequence of chunks:
    [magic 0xFEEDA1E5 u32][compressed_size u32][decompressed_size u32][0 u32] + LZ4-block

This module walks that container and (for deep verification) LZ4-block-decodes it. It also
*encodes* (:func:`compress_block`, :func:`compress`) for an app that edits a save's content
(NMS Savecraft): NMS Save Vault itself never recompresses, it copies data verbatim. The
encoder is a plain greedy LZ4 ("fast" style); any valid block decodes the same in the game,
so its ratio only has to be reasonable, and every container it makes is decoded again and
compared before it is returned.

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


# --- encoding -------------------------------------------------------------------------------

MIN_MATCH = 4
LAST_LITERALS = 5           # the format: a block ends with at least 5 literal bytes
MF_LIMIT = 12               # ... and its last match starts at least 12 bytes before the end
MAX_OFFSET = 0xFFFF
SKIP_STRENGTH = 6           # after 64 positions without a match, look less often (as LZ4 fast)


def _length(out: bytearray, extra: int) -> None:
    """The bytes after a nibble of 15: 255s, then the rest."""
    while extra >= 255:
        out.append(255)
        extra -= 255
    out.append(extra)


def _sequence(out: bytearray, literals: memoryview, offset: int, match: int) -> None:
    """One sequence: ``literals``, then ``match`` bytes from ``offset`` back (``match`` 0: the
    last sequence, literals only)."""
    lit = len(literals)
    ml = match - MIN_MATCH if match else 0
    out.append((min(lit, 15) << 4) | (min(ml, 15) if match else 0))
    if lit >= 15:
        _length(out, lit - 15)
    out += literals
    if match:
        out += offset.to_bytes(2, "little")
        if ml >= 15:
            _length(out, ml - 15)


def _common(src: bytes, a: int, b: int, limit: int) -> int:
    """How many bytes from ``a`` equal those from ``b`` (``b`` + the result <= ``limit``):
    doubling, then halving, so a long match costs about its length in compared bytes."""
    most = limit - b
    if most <= 0 or src[a] != src[b]:
        return 0
    lo, step = 1, 8                         # src[a:a+lo] == src[b:b+lo] holds
    while lo < most:
        hi = min(lo + step, most)
        if src[a + lo:a + hi] == src[b + lo:b + hi]:
            lo, step = hi, step * 2
        elif step == 1:
            break
        else:
            step //= 2
    return lo


def compress_block(src: bytes) -> bytes:
    """One raw LZ4 block holding ``src`` (no size header): what each save chunk carries.
    :func:`decompress_block` gives ``src`` back."""
    n = len(src)
    out = bytearray()
    view = memoryview(src)
    anchor = 0
    if n > MF_LIMIT:
        table: dict[bytes, int] = {}
        start_limit = n - MF_LIMIT          # a match starts before this
        end_limit = n - LAST_LITERALS       # ... and ends at or before this
        i, misses = 0, 0
        while i < start_limit:
            key = src[i:i + MIN_MATCH]
            cand = table.get(key)
            table[key] = i
            if cand is None or i - cand > MAX_OFFSET:
                misses += 1
                i += 1 + (misses >> SKIP_STRENGTH)
                continue
            misses = 0
            while i > anchor and cand > 0 and src[i - 1] == src[cand - 1]:
                i, cand = i - 1, cand - 1   # the match starts earlier than the 4 bytes seen
            length = MIN_MATCH + _common(src, cand + MIN_MATCH, i + MIN_MATCH, end_limit)
            _sequence(out, view[anchor:i], i - cand, length)
            i += length
            anchor = i
            if i - 2 < start_limit:
                table[src[i - 2:i + 2]] = i - 2
    _sequence(out, view[anchor:n], 0, 0)
    return bytes(out)


def check_block_ends(block: bytes, size: int) -> None:
    """The format's end rules, which this module's own decoder does not insist on but the
    reference library does: a block of more than one sequence ends with at least 5 literals,
    and its last match starts at least 12 bytes before the end. :class:`LZ4FormatError`."""
    s, n, out, last_match_start, last_lit = 0, len(block), 0, None, 0

    def ext() -> int:
        nonlocal s
        total = 0
        while True:
            b = block[s]
            s += 1
            total += b
            if b != 255:
                return total

    while s < n:
        token = block[s]
        s += 1
        lit = token >> 4
        if lit == 15:
            lit += ext()
        s += lit
        out += lit
        last_lit = lit
        if s >= n:
            break
        s += 2
        ml = token & 15
        if ml == 15:
            ml += ext()
        last_match_start = out
        out += ml + MIN_MATCH
        last_lit = -1                       # the block may not end on a match
    if last_lit < 0:
        raise LZ4FormatError("an LZ4 block ends with a match")
    if last_match_start is not None and (last_lit < LAST_LITERALS
                                         or last_match_start > size - MF_LIMIT):
        raise LZ4FormatError("an LZ4 block breaks the format's end rules")


def compress(raw: bytes) -> bytes:
    """The game's save container for ``raw`` (the save's JSON, with its NUL): chunks of at
    most 512 KiB, each a 16-byte header and one LZ4 block. Decoded again before it is
    returned; :class:`LZ4FormatError` if it does not give ``raw`` back."""
    if not raw:
        raise LZ4FormatError("a save is never empty")
    if len(raw) > MAX_SAVE_DECOMPRESSED:
        raise LZ4FormatError(f"{len(raw)} bytes is more than a save holds ({MAX_SAVE_DECOMPRESSED})")
    out = bytearray()
    for off in range(0, len(raw), formats.SAVE_CHUNK_DECOMPRESSED_MAX):
        piece = raw[off:off + formats.SAVE_CHUNK_DECOMPRESSED_MAX]
        block = compress_block(piece)
        check_block_ends(block, len(piece))
        out += struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(piece), 0) + block
    packed = bytes(out)
    if decompress(packed) != raw:
        raise LZ4FormatError("the compressed save does not decode back to its content")
    return packed
