"""The hardened LZ4 block decoder (P8): faster, and safe on damaged input.

The decoder before this change is kept verbatim below as the reference: on every valid
block the new one must produce exactly the same bytes. On damaged input it must raise
``LZ4FormatError`` (a ``ValueError``) rather than allocate what a header claims, read
outside the output, return the wrong length or raise anything else.
"""
from __future__ import annotations

import random
import shutil
import struct
import tracemalloc

import pytest

from nms_save_vault.core import formats, lz4_block, playtime, savedir, slotmap
from nms_save_vault.core import operations as ops
from nms_save_vault.core.lz4_block import LZ4FormatError, UnsupportedContainer

from .conftest import chunked, find_live_save_dir, steam_save


def reference_decompress_block(src: bytes, expected_size: int) -> bytes:
    """The decoder as it was (per-byte match copy), verbatim, as the reference."""
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


# --- a tiny LZ4 block writer for the tests ---------------------------------------------------


def _length(rest: int) -> bytes:
    out = bytearray()
    while rest >= 255:
        out.append(255)
        rest -= 255
    out.append(rest)
    return bytes(out)


def sequence(literals: bytes, offset: int | None = None, match: int = 0) -> bytes:
    """One LZ4 sequence: literals, then (unless last) a match of ``match`` bytes."""
    lit = len(literals)
    lit_nib = min(lit, 15)
    if offset is None:
        token = bytes([lit_nib << 4])
        return token + (_length(lit - 15) if lit >= 15 else b"") + literals
    ml = match - 4
    token = bytes([(lit_nib << 4) | min(ml, 15)])
    out = token + (_length(lit - 15) if lit >= 15 else b"") + literals + struct.pack("<H", offset)
    return out + (_length(ml - 15) if ml >= 15 else b"")


def random_block(rng: random.Random) -> tuple[bytes, int]:
    """A valid block built sequence by sequence; returns (block, decoded size)."""
    block = bytearray()
    size = 0
    for _ in range(rng.randint(1, 12)):
        lits = bytes(rng.randrange(256) for _ in range(rng.choice([0, 1, 3, 15, 40, 300])))
        if size + len(lits) == 0:
            lits = b"x"
        size += len(lits)
        offset = rng.randint(1, min(size, 65535))
        match = rng.choice([4, 5, 7, 18, 19, 20, 300, 1000])
        block += sequence(lits, offset, match)
        size += match
    block += sequence(b"end")
    return bytes(block), size + 3


# --- the same bytes as before ------------------------------------------------------------------


@pytest.mark.parametrize(("block", "expected"), [
    (sequence(b"a", 1, 20) + sequence(b"!"), b"a" * 21 + b"!"),
    (sequence(b"abc", 3, 10) + sequence(b"!"), b"abcabcabcabca" + b"!"),
    (sequence(b"abcd", 4, 4) + sequence(b"!"), b"abcdabcd!"),
    (sequence(b"abcdefgh", 8, 5) + sequence(b"!"), b"abcdefghabcde!"),
    (sequence(b"xy" * 20, 2, 300) + sequence(b"."), b"xy" * 20 + (b"xy" * 150) + b"."),
], ids=["period 1", "period 3", "offset equals length", "no overlap", "long lengths"])
def test_overlapping_and_long_matches_decode_as_before(block, expected):
    assert lz4_block.decompress_block(block, len(expected)) == expected
    assert reference_decompress_block(block, len(expected)) == expected


def test_a_far_match_reaches_back_64_kib():
    first = bytes(range(256)) * 256
    block = sequence(first, 65535, 10) + sequence(b"z")
    expected = first + first[1:11] + b"z"
    assert lz4_block.decompress_block(block, len(expected)) == expected


def test_random_valid_blocks_decode_exactly_as_the_old_decoder():
    rng = random.Random(20261017)
    for _ in range(300):
        block, size = random_block(rng)
        assert lz4_block.decompress_block(block, size, exact=True) == reference_decompress_block(block, size)


def test_real_saves_decode_exactly_as_before(tmp_path):
    """Read-only: two of the real Steam saves, copied out first, through both decoders."""
    live = find_live_save_dir()
    if live is None:
        pytest.skip("no live NMS save directory found")
    members = [m for sv in savedir.scan(live).occupied_slots for m in sv.present_members][:2]
    for m in members:
        copy = shutil.copyfile(m.data_path, tmp_path / m.data_path.name)
        data = copy.read_bytes()
        chunks, _ = lz4_block.walk_chunks(data)
        old = b"".join(
            reference_decompress_block(data[c.payload_offset : c.payload_offset + c.compressed_size], c.decompressed_size)
            for c in chunks
        )
        assert lz4_block.decompress(data) == old


# --- damaged input: a clear error, never a huge allocation or a wrong answer --------------------


def _frame(block: bytes, claimed: int) -> bytes:
    return struct.pack("<4I", formats.SAVE_MAGIC, len(block), claimed, 0) + block


def _steam_pair(tmp_path) -> tuple[bytes, int, bytes]:
    """(meta bytes, ordinal, data bytes) of one valid synthetic Steam save."""
    steam_save(tmp_path, 1, 0, "Voyagers", 100, 0x1234, 1000)
    fno = slotmap.slot_file_numbers(1)[0]
    return (
        (tmp_path / slotmap.meta_filename(fno)).read_bytes(),
        slotmap.storage_ordinal(fno),
        (tmp_path / slotmap.data_filename(fno)).read_bytes(),
    )


@pytest.mark.parametrize("claimed", [formats.SAVE_CHUNK_DECOMPRESSED_MAX + 1, 100_000],
                         ids=["over the format's maximum", "more than its payload can hold"])
def test_an_impossible_chunk_header_ends_the_run_like_any_trailing_bytes(tmp_path, claimed):
    """Valid chunks followed by a header with the magic but a size no chunk can have: the
    valid chunks still decode and validate, the rest is trailing data. Only the meta's
    decompressed size decides whether the run is complete."""
    meta_bytes, ordinal, good = _steam_pair(tmp_path)
    junk = _frame(sequence(b"a"), claimed)
    raw = good + junk

    chunks, consumed = lz4_block.walk_chunks(raw)
    assert len(chunks) == 1 and consumed == len(good)
    assert lz4_block.stats(raw).trailing == len(junk)
    assert lz4_block.decompress(raw) == lz4_block.decompress(good)
    assert playtime._id_from_bytes(raw, 0) == 0x1234
    ops._validate_pair(meta_bytes, ordinal, raw)

    with pytest.raises(ops.ValidationError, match="no valid chunks"):
        ops._validate_pair(meta_bytes, ordinal, junk + good)


def test_a_size_its_input_cannot_produce_is_refused_before_allocating():
    """A damaged 20-byte blob claiming 60 MB (under the bare-block cap) must not cost 60 MB."""
    blob = sequence(b"x" * 18)
    assert len(blob) == 20
    tracemalloc.start()
    try:
        with pytest.raises(LZ4FormatError, match="its 20 bytes can hold"):
            lz4_block.decompress(blob, 60 * 1024 * 1024)
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024


@pytest.mark.parametrize(("size", "message"), [
    (-1, "claims a negative size"),
    (lz4_block.MAX_SAVE_DECOMPRESSED + 1, f"more than {lz4_block.MAX_SAVE_DECOMPRESSED}"),
    (2**32 - 1, f"more than {lz4_block.MAX_SAVE_DECOMPRESSED}"),
], ids=["negative", "over the cap", "corrupt u32"])
def test_negative_and_too_large_sizes_are_told_apart(size, message):
    with pytest.raises(LZ4FormatError, match=message):
        lz4_block.decompress_block(sequence(b"a"), size)


def test_a_container_claiming_too_much_in_all_is_refused(monkeypatch):
    monkeypatch.setattr(lz4_block, "MAX_SAVE_DECOMPRESSED", 150)
    raw = chunked(b"a" * 100) + chunked(b"b" * 100)
    with pytest.raises(LZ4FormatError, match="in all"):
        lz4_block.decompress(raw)


def test_a_bare_block_with_an_absurd_size_hint_is_refused():
    with pytest.raises(LZ4FormatError, match=f"more than {lz4_block.MAX_BARE_BLOCK}"):
        lz4_block.decompress(sequence(b"a"), lz4_block.MAX_BARE_BLOCK + 1)


def test_a_bare_block_must_decode_to_exactly_its_meta_size():
    assert lz4_block.decompress(sequence(b"abc"), 3) == b"abc"
    with pytest.raises(LZ4FormatError, match="decoded to 3 bytes, not the 10 declared"):
        lz4_block.decompress(sequence(b"abc"), 10)


@pytest.mark.parametrize(("block", "size", "message"), [
    (sequence(b"ab", 0, 4) + sequence(b"!"), 7, "points outside"),
    (sequence(b"ab", 5, 4) + sequence(b"!"), 7, "points outside"),
    (sequence(b"abcdef"), 3, "more bytes than it declares"),
    (sequence(b"ab", 2, 10) + sequence(b"!"), 5, "more bytes than it declares"),
    (sequence(b"abcdef")[:-2], 6, "past the end"),
    (sequence(b"ab", 2, 4)[:-1], 6, "cut off"),
], ids=["offset 0", "before the start", "literals overflow", "match overflow", "literals cut", "offset cut"])
def test_inconsistent_blocks_raise_a_format_error(block, size, message):
    with pytest.raises(LZ4FormatError, match=message):
        lz4_block.decompress_block(block, size)


def test_a_chunk_that_decodes_short_of_its_header_is_refused():
    with pytest.raises(LZ4FormatError, match="not the 10 declared"):
        lz4_block.decompress(_frame(sequence(b"abc"), 10))


def test_hgsavev2_is_recognised_and_refused():
    raw = lz4_block.HGSAVEV2_HEADER + b"\x00" * 64
    with pytest.raises(UnsupportedContainer, match="4.52-4.99"):
        lz4_block.decompress(raw, 1000)


def test_damaged_containers_never_raise_anything_but_a_value_error():
    rng = random.Random(7)
    good = b"".join(chunked(bytes(rng.randrange(256) for _ in range(rng.randint(1, 200)))) for _ in range(3))
    for _ in range(500):
        bad = bytearray(good)
        for _ in range(rng.randint(1, 4)):
            bad[rng.randrange(len(bad))] = rng.randrange(256)
        cut = bytes(bad[: rng.randint(0, len(bad))]) if rng.random() < 0.3 else bytes(bad)
        try:
            lz4_block.decompress(cut, 500)
        except ValueError:
            pass


def test_invalid_utf8_in_the_save_text_does_not_stop_the_id_search():
    raw = chunked(b'{"F2P":4737,"<h0":{"WmU":"0x1234ABCD","X":"\xff\xfe\xc3"}}')
    assert playtime._id_from_bytes(raw, 0) == 0x1234ABCD
