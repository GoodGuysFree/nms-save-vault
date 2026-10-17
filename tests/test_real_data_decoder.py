"""Opt-in, read-only: the hardened decoder against the old one on every live save.

Runs only with ``NMSVAULT_REAL_DATA=1``. Every present member of every live Steam and Xbox
account is read (``rb``, nothing is written anywhere) and decoded by the old per-byte
decoder and by the current one; the sha256 of both outputs must match, and the JSON must
parse. HGSAVEV2 members, which the old decoder misread, are only counted.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from nms_save_vault.core import locations, lz4_block, msstore, savedir, savejson

from .test_lz4_hardening import reference_decompress_block

REAL_DATA = os.environ.get("NMSVAULT_REAL_DATA") == "1"

pytestmark = pytest.mark.skipif(not REAL_DATA, reason="set NMSVAULT_REAL_DATA=1 to read the live saves")


def _live_members():
    folders = list(locations.find_live_save_dirs())
    folders += [d for d in msstore.find_microsoft_save_dirs() if msstore.is_microsoft_save_dir(d)]
    for folder in folders:
        view = savedir.scan_any(folder)
        for slot in view.occupied_slots:
            for m in slot.present_members:
                if m.info is not None:
                    yield m


def _old_decode(data: bytes, size_hint: int) -> bytes:
    chunks, _consumed = lz4_block.walk_chunks(data)
    if chunks:
        return b"".join(
            reference_decompress_block(data[c.payload_offset : c.payload_offset + c.compressed_size],
                                       c.decompressed_size)
            for c in chunks
        )
    return reference_decompress_block(data, size_hint)


def _read(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def test_every_live_save_decodes_to_the_same_bytes_as_before():
    compared = unsupported = 0
    for m in _live_members():
        data = _read(Path(m.data_path))
        hint = m.info.size_decompressed
        if data.startswith(lz4_block.HGSAVEV2_HEADER):
            with pytest.raises(lz4_block.UnsupportedContainer):
                lz4_block.decompress(data, hint)
            unsupported += 1
            continue
        new = lz4_block.decompress(data, hint)
        assert hashlib.sha256(new).hexdigest() == hashlib.sha256(_old_decode(data, hint)).hexdigest()
        decoded = savejson.decode_save(data, bare_size_hint=hint)
        assert decoded.decompressed_sha256 == hashlib.sha256(new).hexdigest()
        compared += 1
    if compared + unsupported == 0:
        pytest.skip("no live saves on this machine")
    print(f"\nreal data: {compared} live saves decoded identically, {unsupported} HGSAVEV2")
