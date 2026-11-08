"""lz4_block.compress_block / compress: the pure-Python LZ4 encoder an editing app needs (CL3).
Every block keeps the format's end rules, decodes back exactly with the module's own decoder,
and a container is framed as the game frames it."""
from __future__ import annotations

import random
import struct

import pytest

from nms_save_vault.core import formats, lz4_block


def sequences(block: bytes) -> list[tuple[int, int, int]]:
    """(literal count, match offset, match length) of each sequence; the last has no match."""
    out, s, n = [], 0, len(block)
    while s < n:
        token = block[s]
        s += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = block[s]
                s += 1
                lit += b
                if b != 255:
                    break
        s += lit
        if s >= n:
            out.append((lit, 0, 0))
            break
        offset = block[s] | block[s + 1] << 8
        s += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = block[s]
                s += 1
                ml += b
                if b != 255:
                    break
        out.append((lit, offset, ml + lz4_block.MIN_MATCH))
    return out


def check(src: bytes) -> bytes:
    block = lz4_block.compress_block(src)
    assert lz4_block.decompress_block(block, len(src), exact=True) == src
    seqs = sequences(block)
    assert seqs[-1][1:] == (0, 0)                              # ends with literals only
    if len(seqs) > 1:
        assert seqs[-1][0] >= lz4_block.LAST_LITERALS          # at least 5 of them
        consumed = sum(lit + ml for lit, _, ml in seqs[:-1])
        last_start = consumed - seqs[-2][2]
        assert last_start <= len(src) - lz4_block.MF_LIMIT     # last match 12 bytes from the end
    assert all(0 < off <= lz4_block.MAX_OFFSET for _, off, ml in seqs if ml)
    return block


@pytest.mark.parametrize("src", [
    b"", b"a", b"abcdefghijk", b"abcdefghijkl", b"abcdefghijklm", b"x" * 13, b"x" * 300,
    b"ab" * 40, bytes(range(256)) * 2, b'{"Units":12,"Nanites":7}' * 400,
], ids=lambda s: f"{len(s)}b")
def test_small_and_edge_inputs_round_trip_within_the_format_rules(src):
    check(src)


def test_long_literal_runs_and_long_matches_use_length_extensions():
    rng = random.Random(1)
    noise = bytes(rng.randrange(256) for _ in range(5000))           # literals >= 15 + 255s
    block = check(noise + b"z" * 20_000 + noise)                     # a match of 20 kB
    assert any(lit > 270 for lit, _, _ in sequences(block))
    assert any(ml > 270 for _, _, ml in sequences(block))
    assert len(block) < len(noise) * 2 + 200


def test_a_repeat_further_back_than_64_kib_is_never_referenced():
    rng = random.Random(2)
    first = bytes(rng.randrange(256) for _ in range(1000))
    check(first + bytes(rng.randrange(256) for _ in range(70_000)) + first)


@pytest.mark.parametrize("seed", range(40))
def test_random_structured_inputs_round_trip(seed):
    rng = random.Random(seed)
    words = [bytes(rng.randrange(97, 123) for _ in range(rng.randrange(1, 12)))
             for _ in range(rng.randrange(1, 40))]
    src = b"".join(rng.choice(words) + bytes([rng.randrange(256)]) * rng.randrange(0, 3)
                   for _ in range(rng.randrange(0, 3000)))
    check(src)


def test_the_same_input_gives_the_same_bytes():
    src = b'{"a":[1,2,3],"b":"text"}' * 1000
    assert lz4_block.compress_block(src) == lz4_block.compress_block(src)


def test_a_container_is_framed_in_512_kib_chunks_as_the_game_frames_it():
    size = formats.SAVE_CHUNK_DECOMPRESSED_MAX
    raw = (b'{"PlayerStateData":{"Units":1}}' * 40_000)[: size * 2 + 7] + b"\x00"
    packed = lz4_block.compress(raw)
    chunks, consumed = lz4_block.walk_chunks(packed)
    assert consumed == len(packed)
    assert [c.decompressed_size for c in chunks] == [size, size, 8]
    for c in chunks:
        header = packed[c.payload_offset - formats.SAVE_CHUNK_HEADER_LENGTH:c.payload_offset]
        assert struct.unpack("<4I", header) == (formats.SAVE_MAGIC, c.compressed_size,
                                                c.decompressed_size, 0)
    assert lz4_block.is_chunked(packed) and lz4_block.decompress(packed) == raw


def test_exactly_one_chunk_and_an_empty_save_is_refused():
    one = b"y" * formats.SAVE_CHUNK_DECOMPRESSED_MAX
    assert len(lz4_block.walk_chunks(lz4_block.compress(one))[0]) == 1
    with pytest.raises(lz4_block.LZ4FormatError, match="never empty"):
        lz4_block.compress(b"")


def test_more_than_a_save_can_hold_is_refused(monkeypatch):
    monkeypatch.setattr(lz4_block, "MAX_SAVE_DECOMPRESSED", 100)
    with pytest.raises(lz4_block.LZ4FormatError, match="more than a save holds"):
        lz4_block.compress(b"x" * 101)


def test_a_container_that_would_not_decode_back_is_refused(monkeypatch):
    monkeypatch.setattr(lz4_block, "compress_block", lambda src: b"\x10" + src[:1])
    with pytest.raises(lz4_block.LZ4FormatError):
        lz4_block.compress(b"hello world, hello world")



def test_a_block_ending_with_a_match_or_short_literals_is_refused_by_the_end_check():
    src = b"a" * 20
    ends_on_match = bytes([0x1F]) + b"a" + bytes([1, 0, 0])      # 1 literal, a 19-byte match
    assert lz4_block.decompress_block(ends_on_match, 20, exact=True) == src   # lenient
    with pytest.raises(lz4_block.LZ4FormatError, match="ends with a match"):
        lz4_block.check_block_ends(ends_on_match, 20)
    short_tail = bytes([0x1F]) + b"a" + bytes([1, 0, 0]) + bytes([0x10]) + b"a"
    with pytest.raises(lz4_block.LZ4FormatError, match="end rules"):
        lz4_block.check_block_ends(short_tail, 21)
    lz4_block.check_block_ends(lz4_block.compress_block(src), 20)


def test_compress_refuses_a_block_the_reference_library_would_reject(monkeypatch):
    ends_on_match = bytes([0x1F]) + b"a" + bytes([1, 0, 0])
    monkeypatch.setattr(lz4_block, "compress_block", lambda src: ends_on_match)
    with pytest.raises(lz4_block.LZ4FormatError, match="ends with a match"):
        lz4_block.compress(b"a" * 20)


@pytest.mark.slow
def test_real_saves_round_trip(live_save_dir):
    """Read-only: every real save here decodes, re-encodes and decodes back to the same JSON."""
    for path in sorted(live_save_dir.glob("save*.hg"))[:4]:
        raw = lz4_block.decompress(path.read_bytes())
        assert lz4_block.decompress(lz4_block.compress(raw)) == raw, path.name
