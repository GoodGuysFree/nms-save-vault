"""The LZ4 C library (lz4 4.4.5, BSD, shipped inside the kits on the lz4-accel branch,
2026-11-23) does the block work when it is there; the pure-Python code is the fallback and is
chosen with NMSVAULT_PURE_LZ4=1. Either way the checks are the same: a block that decodes to
more than it declares, or runs past its end, is refused, every block keeps the format's end
rules, and a save compressed is decoded back and compared before it is used."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("lz4.block")

from nms_save_vault.core import lz4_block  # noqa: E402


def _raw() -> bytes:
    rows = [{"Id": f"^ITEM_{n % 97}", "Amount": n * 7 % 9999, "MaxAmount": 9999,
             "Index": {"X": n % 10, "Y": n // 10 % 6}, "DamageFactor": 0.0, "FullyInstalled": True}
            for n in range(9000)]
    return json.dumps({"Version": 4233, "Slots": rows}, separators=(",", ":")).encode() + b"\x00"


def test_the_c_library_does_the_work_when_it_is_there():
    assert lz4_block.FAST


def test_both_ways_decode_alike(monkeypatch):
    raw = _raw()
    packed = lz4_block.compress(raw)                    # with the C library
    fast = lz4_block.decompress(packed)
    monkeypatch.setattr(lz4_block, "FAST", False)
    assert lz4_block.decompress(packed) == fast == raw


def test_what_the_pure_code_packs_the_c_library_reads(monkeypatch):
    raw = _raw()
    monkeypatch.setattr(lz4_block, "FAST", False)
    packed = lz4_block.compress(raw)
    monkeypatch.setattr(lz4_block, "FAST", True)
    assert lz4_block.decompress(packed) == raw


@pytest.mark.parametrize("fast", [True, False])
def test_a_broken_block_is_refused_either_way(monkeypatch, fast):
    piece = _raw()[:4000]
    block = lz4_block.compress_block(piece)
    monkeypatch.setattr(lz4_block, "FAST", fast)
    with pytest.raises(lz4_block.LZ4FormatError):
        lz4_block.decompress_block(block[:-9], len(piece), exact=True)
    with pytest.raises(lz4_block.LZ4FormatError):
        lz4_block.decompress_block(block, len(piece) - 100)     # more than it declares
    assert lz4_block.decompress_block(block, len(piece), exact=True) == piece


@pytest.mark.parametrize("size", [13, 64, 4096, 524_288])
def test_the_c_library_s_blocks_keep_the_format_s_end_rules(size):
    piece = (_raw() * 3)[:size]
    block = lz4_block.compress_block(piece)
    lz4_block.check_block_ends(block, len(piece))
    assert lz4_block.decompress_block(block, len(piece), exact=True) == piece


@pytest.mark.parametrize("pure, fast", [("1", "False"), ("", "True")])
def test_the_pure_code_is_chosen_with_the_environment(pure, fast):
    """In a process of its own: reloading the module here would make its error class anew
    under the other tests' feet."""
    import os
    import subprocess
    import sys
    env = dict(os.environ, NMSVAULT_PURE_LZ4=pure)
    out = subprocess.run([sys.executable, "-c", "from nms_save_vault.core import lz4_block; "
                          "print(lz4_block.FAST)"], env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.stdout.strip() == fast, out.stderr
