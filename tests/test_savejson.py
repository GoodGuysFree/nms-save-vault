"""Save data -> JSON root (``core/savejson.py``): lossless text, escape repair, error kinds."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nms_save_vault.core import formats, lz4_block, savejson
from nms_save_vault.core.savejson import SaveDecodeError, decode_save

from .conftest import chunked, lz4_literals

TOOL = Path(__file__).resolve().parent.parent / "tools" / "nms_visit_history.py"


def two_chunks(payload: bytes) -> bytes:
    half = len(payload) // 2
    out = b""
    for part in (payload[:half], payload[half:]):
        block = lz4_literals(part)
        out += struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(part), 0) + block
    return out


def test_a_chunked_save_decodes_to_its_root_with_container_facts():
    payload = b'{"F2P":4737,"<h0":{"Pk4":"Main"}}\x00'
    data = chunked(payload) + b"stale bytes the game left behind"
    got = decode_save(data)
    assert got.root == {"F2P": 4737, "<h0": {"Pk4": "Main"}}
    assert (got.container, got.chunks) == ("chunked", 1)
    assert got.decompressed_bytes == len(payload)
    assert got.decompressed_sha256 == hashlib.sha256(payload[:-1]).hexdigest()   # the JSON, no NUL
    assert got.json_bytes == len(payload) - 1
    assert got.repaired_escapes == 0


def test_text_after_the_first_nul_is_ignored_and_chunks_are_joined():
    payload = b'{"a":[1,2,3],"b":"' + b"x" * 300 + b'"}\x00{"not":"json'
    got = decode_save(two_chunks(payload))
    assert got.chunks == 2
    assert got.root["a"] == [1, 2, 3]
    assert got.json_bytes == payload.index(b"\x00")


def test_an_unframed_save_needs_its_size_hint():
    payload = b'{"F2P":4135}\x00'
    block = lz4_literals(payload)
    got = decode_save(block, bare_size_hint=len(payload))
    assert (got.container, got.chunks, got.root) == ("unframed", 0, {"F2P": 4135})
    with pytest.raises(SaveDecodeError) as err:
        decode_save(block)
    assert err.value.kind == "corrupt"


def test_invalid_utf8_inside_a_string_survives_losslessly():
    raw_id = b"^SEED\xff\xfeX"
    got = decode_save(chunked(b'{"id":"' + raw_id + b'"}'))
    value = got.root["id"]
    assert value.encode("utf-8", errors="surrogateescape") == raw_id
    assert "�" not in value


def test_the_content_hash_covers_the_json_only_not_what_follows_its_nul():
    body = b'{"a":1}'
    one = decode_save(chunked(body + b"\x00" + b"old bytes"))
    two = decode_save(chunked(body + b"\x00" + b"other old bytes, longer"))
    assert one.decompressed_bytes != two.decompressed_bytes
    assert one.decompressed_sha256 == two.decompressed_sha256 == hashlib.sha256(body).hexdigest()


def test_an_integer_past_the_digit_limit_is_corrupt_not_a_crash():
    with pytest.raises(SaveDecodeError) as err:
        decode_save(chunked(b'{"a":' + b"9" * 5000 + b"}"))
    assert err.value.kind == "corrupt"
    with pytest.raises(SaveDecodeError) as err:      # the same after an escape repair
        decode_save(chunked(b'{"n":"a\\\tb","a":' + b"9" * 5000 + b"}"))
    assert err.value.kind == "corrupt"


def test_absurd_nesting_after_an_escape_repair_is_corrupt():
    body = b'{"n":"a\\\tb","deep":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    with pytest.raises(SaveDecodeError) as err:
        decode_save(chunked(body))
    assert err.value.kind == "corrupt"


def test_a_backslash_before_a_raw_tab_is_repaired_like_libnom():
    got = decode_save(chunked(b'{"n":"a\\\tb","m":"c\\\nd"}'))
    assert got.root == {"n": "a\tb", "m": "c\nd"}
    assert got.repaired_escapes == 2


def test_raw_control_characters_inside_strings_are_accepted():
    assert decode_save(chunked(b'{"n":"a\tb"}')).root == {"n": "a\tb"}


def test_hgsavev2_is_unsupported():
    with pytest.raises(SaveDecodeError) as err:
        decode_save(lz4_block.HGSAVEV2_HEADER + b"\x00" * 64)
    assert err.value.kind == "unsupported"


def test_sizes_over_the_cap_are_too_large_before_decoding(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("decoded although the size is over the cap")

    monkeypatch.setattr(lz4_block, "decompress", boom)
    with pytest.raises(SaveDecodeError) as err:
        decode_save(chunked(b'{"a":"' + b"x" * 100 + b'"}'), max_decompressed=50)
    assert err.value.kind == "too-large"
    with pytest.raises(SaveDecodeError) as err:
        decode_save(lz4_literals(b"{}"), bare_size_hint=lz4_block.MAX_BARE_BLOCK + 1)
    assert err.value.kind == "too-large"


@pytest.mark.parametrize("data", [
    chunked(b'{"a":'),                                    # truncated JSON
    chunked(b'[1,2,3]'),                                  # not an object
    chunked(b'{"a":"\\q"}'),                              # an escape no repair fixes
    chunked(b"[" * 100_000 + b"]" * 100_000),             # absurd nesting
    struct.pack("<4I", formats.SAVE_MAGIC, 3, 9, 0) + b"\x10ab",   # LZ4 that cannot fill its chunk
    b"",
], ids=["truncated", "array root", "bad escape", "nesting", "short chunk", "empty"])
def test_damaged_data_is_corrupt(data):
    with pytest.raises(SaveDecodeError) as err:
        decode_save(data)
    assert err.value.kind == "corrupt"
    assert isinstance(err.value, ValueError)


def _load_tool(monkeypatch):
    name = "nms_visit_history_under_test"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)   # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def test_the_visit_history_tool_decodes_through_savejson(tmp_path, monkeypatch):
    tool = _load_tool(monkeypatch)
    assert tool.savejson is savejson
    raw_id = b"\xc3("
    payload = b'{"6f=":{"nwS":["0x10"]},"id":"' + raw_id + b'"}\x00'
    path = tmp_path / "save.hg"
    path.write_bytes(lz4_literals(payload))
    member = SimpleNamespace(data_path=path, info=SimpleNamespace(size_decompressed=len(payload)))
    root = tool.decode_save(member)
    assert root["id"].encode("utf-8", errors="surrogateescape") == raw_id
    assert json.dumps(root["6f="]) == '{"nwS": ["0x10"]}'
