"""The save-data container reader, including the unframed form.

Pre-Waypoint Microsoft saves are a single bare LZ4 block: no 0xFEEDA1E5 chunk headers and
therefore no decompressed size on the wire, which is why the size has to be handed in from
the wgs meta. Everything else -- every Steam save, every current Xbox save -- is chunked.
"""
from __future__ import annotations

import struct

import pytest

from nms_save_vault.core import formats, lz4_block


def lz4_literals(payload: bytes) -> bytes:
    """Encode ``payload`` as one literals-only LZ4 block (a legal last sequence)."""
    lit = len(payload)
    if lit < 15:
        return bytes([lit << 4]) + payload
    rest, token = lit - 15, bytes([0xF0])
    cont = bytearray()
    while rest >= 255:
        cont.append(255)
        rest -= 255
    cont.append(rest)
    return token + bytes(cont) + payload


def chunked(payload: bytes) -> bytes:
    block = lz4_literals(payload)
    return struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(payload), 0) + block


def test_bare_block_decompresses_with_the_size_from_the_meta():
    payload = b'{"F2P":4146,"6f=":{"Pk4":"Voyagers"}}' + b" " * 600
    raw = lz4_literals(payload)
    assert not lz4_block.is_chunked(raw)
    assert lz4_block.decompress(raw, len(payload)) == payload


def test_bare_block_without_a_size_says_what_is_missing():
    raw = lz4_literals(b'{"a":1}')
    with pytest.raises(ValueError, match="bare LZ4 block"):
        lz4_block.decompress(raw)


def test_a_chunked_container_ignores_the_expected_size():
    payload = b'{"F2P":4737}'
    raw = chunked(payload)
    assert lz4_block.is_chunked(raw)
    assert lz4_block.decompress(raw, 999999) == payload
    assert lz4_block.decompress(raw) == payload


def test_multi_chunk_container_concatenates_in_order():
    a, b = b'{"first":1,', b'"second":2}'
    raw = chunked(a) + chunked(b)
    assert lz4_block.decompress(raw) == a + b
    assert lz4_block.stats(raw).chunk_count == 2


def test_trailing_stale_bytes_after_the_last_chunk_are_ignored():
    """The game does not truncate saveN.hg when a later save is smaller."""
    payload = b'{"F2P":4737}'
    raw = chunked(payload) + b"\xde\xad\xbe\xef" * 8
    st = lz4_block.stats(raw)
    assert st.chunk_count == 1 and st.trailing == 32
    assert lz4_block.decompress(raw) == payload


def test_garbage_is_still_refused():
    with pytest.raises(ValueError, match="no valid save chunks"):
        lz4_block.decompress(b"not a save at all")
