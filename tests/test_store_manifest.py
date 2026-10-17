"""Version store B-3: entry manifests - canonical JSON, checksum, strict loader (D3 T5, T7)."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from nms_save_vault.core import store as vstore
from nms_save_vault.core.store.manifest import FileEntry, MemberRecord, StoreEntry, canonical

from .store_helpers import new_vault, open_vault_store


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


DATA = b"D" * 9000
META = b"M" * 432
VDF = b'"UserLocalConfigStore"\n'


def _member(**kw) -> MemberRecord:
    fields = dict(slot=7, member="A", data="save13.hg", meta="mf_save13.hg", vid=_sha(DATA),
                  ordinal=14, meta_plain_sha256=_sha(b"plain"), pid="0123456789ABCDEF",
                  play_time=1916280, meta_ts=1791199000, name="Main Save (S2)", summary="On a moon",
                  difficulty=3, game_mode=1, season=0, base_version=4220, meta_format=2004,
                  size_decompressed=18800000, valid=True, file_time_ns=1791200000000000000)
    fields.update(kw)
    return MemberRecord(**fields)


def _entry(**kw) -> StoreEntry:
    fields = dict(
        id="snapshot-steam-20261016-141500", kind="snapshot", label="auto: pre-place slot 7",
        reason="pre-place", op_id="op-20261016T111500Z-ab12", created_utc="2026-10-16T11:15:00Z",
        created_local="2026-10-16T14:15:00", app_version="0.3.0", platform="steam",
        account_key="st_76561198000000001",
        source={"type": "live", "platform": "steam", "account_key": "st_76561198000000001"},
        scope={"slots": "all", "account_files": True, "cache": "none"}, origin=None,
        files=[FileEntry("steam_autocloud.vdf", len(VDF), _sha(VDF), 5, inline=VDF),
               FileEntry("save13.hg", len(DATA), _sha(DATA), 1791200000000000000),
               FileEntry("mf_save13.hg", len(META), _sha(META), 1791200000000000000, inline=META)],
        members=[_member()], account={"autocloud": "steam_autocloud.vdf"}, xbox=None)
    fields.update(kw)
    return StoreEntry.new(**fields)


def _reseal(raw: bytes, edit) -> bytes:
    """Apply ``edit`` to the parsed manifest and write it back with a fresh checksum, as a newer
    app would."""
    d = json.loads(raw)
    d.pop("body_sha256")
    edit(d)
    return canonical(dict(d, body_sha256=_sha(canonical(d))))


def test_round_trip_is_byte_exact_and_canonical():
    entry = _entry()
    raw = entry.to_bytes()
    assert raw == canonical(json.loads(raw)) and raw.isascii()
    back = StoreEntry.from_bytes(raw)
    assert back == entry and back.to_bytes() == raw
    assert [f.path for f in back.files] == sorted(f.path for f in back.files)
    assert back.file("mf_save13.hg").inline == META and back.file("save13.hg").inline is None
    assert back.member(7, "A") == entry.members[0] and back.member(7, "B") is None
    assert back.object_digests() == frozenset({_sha(DATA)})
    d = json.loads(raw)
    assert d["slots"]["7"]["A"]["vid"] == _sha(DATA)
    assert base64.b64decode(next(f for f in d["files"] if f["path"] == "mf_save13.hg")["inline"]) == META


def test_manifest_checksum():
    raw = _entry().to_bytes()
    for i in range(0, len(raw), 7):
        mutated = bytearray(raw)
        mutated[i] = ord("0") if raw[i] != ord("0") else ord("1")
        with pytest.raises(vstore.EntryCorrupt):
            StoreEntry.from_bytes(bytes(mutated))


def test_unknown_keys_round_trip():
    raw = _entry().to_bytes()

    def newer(d):
        d["schema"] = 2
        d["future"] = {"x": [1, 2]}
        d["files"][0]["attrs"] = "hidden"
        d["slots"]["7"]["A"]["facts"] = {"ships": 3}

    newer_raw = _reseal(raw, newer)
    entry = StoreEntry.from_bytes(newer_raw)
    assert entry.schema == 2 and entry.extra == {"future": {"x": [1, 2]}}
    assert entry.to_bytes() == newer_raw


@pytest.mark.parametrize("edit", [
    lambda d: d["files"][0].__setitem__("path", "../escape"),
    lambda d: d["files"][0].__setitem__("path", "/abs"),
    lambda d: d["files"][0].__setitem__("path", "a\\b"),
    lambda d: d["files"][0].__setitem__("path", "C:x"),
    lambda d: d["files"].reverse(),
    lambda d: d["files"][0].__setitem__("inline", base64.b64encode(b"other").decode()),
    lambda d: d["slots"]["7"]["A"].__setitem__("vid", "0" * 64),
    lambda d: d["slots"]["7"]["A"].__setitem__("data", "save99.hg"),
    lambda d: d["slots"]["7"]["A"].__setitem__("pid", "not-a-pid"),
    lambda d: d["slots"].__setitem__("16", d["slots"]["7"]),
    lambda d: d["slots"]["7"].__setitem__("C", d["slots"]["7"]["A"]),
    lambda d: d.__setitem__("kind", "trash"),
    lambda d: d.__setitem__("id", "../x"),
    lambda d: d.__setitem__("account", {"data": "accountdata.hg"}),
    lambda d: d.__setitem__("format", "something-else"),
    lambda d: d.__setitem__("schema", 0),
    lambda d: d.pop("label"),
])
def test_the_loader_is_strict(edit):
    with pytest.raises(vstore.EntryCorrupt):
        StoreEntry.from_bytes(_reseal(_entry().to_bytes(), edit))


def test_floats_and_duplicate_keys_are_refused():
    raw = _entry().to_bytes()
    with pytest.raises(vstore.EntryCorrupt):
        StoreEntry.from_bytes(_reseal(raw, lambda d: d.__setitem__("future", 1.5)))
    dup = raw.replace(b'{"account":', b'{"account":{},"account":', 1)
    with pytest.raises(vstore.EntryCorrupt):
        StoreEntry.from_bytes(dup)


def test_new_refuses_inconsistent_entries():
    with pytest.raises(vstore.EntryCorrupt):
        _entry(members=[_member(vid=_sha(b"other"))])
    with pytest.raises(vstore.EntryCorrupt):
        _entry(xbox={"index": "containers.index", "index_kind": "verbatim"})


def test_inline_threshold(tmp_path):
    store = open_vault_store(new_vault(tmp_path))
    small, big = b"s" * 8192, b"b" * 8193
    fe_small = store.store_file("cache/a.bin", small, 1)
    fe_big = store.store_file("cache/b.bin", big, 2)
    assert fe_small.inline == small and not store.objects.has(_sha(small))
    assert fe_big.inline is None and store.objects.get(_sha(big)) == big
    assert store.file_bytes(fe_small) == small and store.file_bytes(fe_big) == big
