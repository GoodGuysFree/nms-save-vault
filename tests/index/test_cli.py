"""``nmsvault index ...`` on a synthetic vault and live folder (no real data, temp cache)."""
from __future__ import annotations

import hashlib
import json
import struct
import time
from pathlib import Path

import pytest

from nms_save_vault import cli
from nms_save_vault.core import catalog, formats, meta, slotmap
from nms_save_vault.core.index import vaultlink

from .savebuilder import context_save, payload


def steam_pair(folder: Path, slot: int, readable: dict, *, name: str, play: int, ts: int) -> str:
    """One Steam save (data + encrypted meta) built from a readable save tree; returns its vid."""
    folder.mkdir(parents=True, exist_ok=True)
    fno = slotmap.slot_file_numbers(slot)[0]
    body = payload(readable)
    block = bytes([0xF0]) + _length(len(body) - 15) + body
    data = struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(body), 0) + block
    (folder / slotmap.data_filename(fno)).write_bytes(data)
    plain = bytearray(formats.META_LEN_WORLDS_II)
    struct.pack_into("<I", plain, formats.OFF_HEADER, formats.META_HEADER)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT, formats.META_FORMAT_4)
    struct.pack_into("<I", plain, formats.OFF_SIZE_DECOMPRESSED, len(body))
    struct.pack_into("<I", plain, formats.OFF_SIZE_DISK, len(data))
    struct.pack_into("<H", plain, formats.OFF_GAME_MODE, 1)
    struct.pack_into("<Q", plain, formats.OFF_TOTAL_PLAY_TIME, play)
    plain[formats.OFF_SAVE_NAME: formats.OFF_SAVE_NAME + len(name)] = name.encode("utf-8")
    struct.pack_into("<I", plain, formats.OFF_DIFFICULTY, 2)
    struct.pack_into("<Q", plain, formats.OFF_SLOT_IDENTIFIER, 0x5EED0000 + slot)
    struct.pack_into("<I", plain, formats.OFF_TIMESTAMP, ts)
    struct.pack_into("<I", plain, formats.OFF_META_FORMAT_TAIL, formats.META_FORMAT_4)
    (folder / slotmap.meta_filename(fno)).write_bytes(meta.encrypt(bytes(plain), slotmap.storage_ordinal(fno)))
    return hashlib.sha256(data).hexdigest()


def _length(rest: int) -> bytes:
    out = bytearray()
    while rest >= 255:
        out.append(255)
        rest -= 255
    out.append(rest)
    return bytes(out)


def save(units: int, glyphs: int, name: str, play: int) -> dict:
    return context_save(base={"Units": units, "KnownPortalRunes": glyphs, "SaveSummary": "Synth"},
                        common={"SaveName": name, "TotalPlayTime": play})


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A vault with one backup entry and a live folder; every per-machine cache under tmp."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    live = tmp_path / "live" / "st_76561190000000001"
    old = steam_pair(tmp_path / "vault" / "full-1", 1, save(1, 0b1, "Synth Main", 3600), name="Synth Main",
                     play=3600, ts=1_790_000_000)
    new = steam_pair(live, 1, save(5, 0b111, "Synth Main", 9000), name="Synth Main", play=9000, ts=1_790_100_000)
    vault_root = tmp_path / "vault"
    doc = {"version": 1, "entries": [{
        "id": "full-1", "kind": catalog.KIND_FULL, "label": "", "path": str(vault_root / "full-1"),
        "created": "2026-09-01T00:00:00", "managed": True, "note": "", "slots": []}]}
    (vault_root / "catalog.json").write_text(json.dumps(doc), encoding="utf-8")
    return {"vault": vault_root, "live": live, "old": old, "new": new, "tmp": tmp_path}


def run(world, *args: str) -> int:
    return cli.main(["--vault", str(world["vault"]), "--live", str(world["live"]), "index", *args])


def test_build_show_changes_and_status(world, capsys):
    assert run(world, "build", "--workers", "1") == 0
    out = capsys.readouterr().out
    assert "Indexing 2 of 2 versions" in out and "2 ok" in out

    assert run(world, "show", world["new"][:10]) == 0
    out = capsys.readouterr().out
    assert "Save name: Synth Main" in out and "Glyphs 3" in out

    assert run(world, "show", world["new"][:10], "--json") == 0
    view = json.loads(capsys.readouterr().out)
    assert view["vid"] == world["new"] and view["units"] == 5

    assert run(world, "changes", world["new"][:10]) == 0
    assert "no recorded parent" in capsys.readouterr().out          # no organizer journal here

    assert run(world, "changes", world["new"][:10], world["old"][:10]) == 0
    out = capsys.readouterr().out
    assert "your choice, not a recorded parent" in out
    assert "+1.5 h played" in out and "glyphs 1 -> 3 of 16" in out

    assert run(world, "status") == 0
    out = capsys.readouterr().out
    assert "Versions known: 2" in out and "ok" in out
    caches = list((world["tmp"] / "localappdata").rglob("index-v1.sqlite"))
    assert len(caches) == 1                                          # under the per-machine cache, not the vault
    assert not list(world["vault"].rglob("*.sqlite"))


def test_rebuild_live_only_and_off(world, capsys):
    assert run(world, "build", "--workers", "1", "--live-only") == 0
    assert "Indexing 1 of 1 versions" in capsys.readouterr().out
    assert run(world, "build", "--workers", "1", "--rebuild", "--live-only") == 0
    assert "Indexing 1 of 1 versions" in capsys.readouterr().out
    assert run(world, "build", "--workers", "0") == 0
    assert "off" in capsys.readouterr().out


def test_bad_references_are_errors(world):
    with pytest.raises(SystemExit, match="6 hex"):
        run(world, "show", "abc")
    with pytest.raises(SystemExit, match="no version matches"):
        run(world, "show", "ffffffffff")


def test_resolve_needs_a_unique_prefix():
    vids = ["abcdef01" + "0" * 56, "abcdef02" + "0" * 56]
    assert vaultlink.resolve("ABCDEF01", vids) == vids[0]
    with pytest.raises(LookupError, match="2 versions match"):
        vaultlink.resolve("abcdef0", vids)


def test_the_bench_tool_runs_on_copies(tmp_path, capsys):
    import importlib.util
    import sys

    copies = tmp_path / "copies"
    copies.mkdir()
    for i in range(3):
        (copies / f"v{i}.dat").write_bytes(
            _chunk(payload(save(i, i, "Synth", 60 * i))))
    spec = importlib.util.spec_from_file_location("bench_under_test", Path(__file__).resolve().parents[2] / "tools" / "bench_index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_under_test"] = module
    try:
        spec.loader.exec_module(module)
        t = time.perf_counter()
        assert module.main([str(copies), "--workers", "1"]) == 0
        assert time.perf_counter() - t < 120
    finally:
        sys.modules.pop("bench_under_test", None)
    out = capsys.readouterr().out
    assert "3 versions" in out and "{'ok': 3}" in out and "cache file after close" in out


def _chunk(body: bytes) -> bytes:
    block = bytes([0xF0]) + _length(len(body) - 15) + body
    return struct.pack("<4I", formats.SAVE_MAGIC, len(block), len(body), 0) + block
