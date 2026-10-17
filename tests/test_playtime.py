"""Total play time: one row per playthrough, however many copies of it exist.

The whole value of the report is the deduplication, so that is what these pin: the two
members of a slot, the same save in a dozen dated backups, and the same run carried to
another platform by the NMS cloud all have to collapse onto one row carrying the highest
play time seen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nms_save_vault.core import playtime, slotmap
from nms_save_vault.core.catalog import Vault

from .conftest import chunked, lz4_literals, steam_save


def _vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    return v


# --- identity ----------------------------------------------------------------


def test_the_playthrough_id_is_read_out_of_the_data_blob():
    """Xbox metas leave the id field 0, so the id has to come from the save data."""
    raw = chunked(b'{"F2P":4737,"<h0":{"WmU":"0xDDDAD1BDD39F9C72"}}')
    assert playtime._id_from_bytes(raw, 0) == 0xDDDAD1BDD39F9C72


def test_a_save_without_the_id_field_reports_none():
    """Pre-Waypoint saves predate it; they key the player block '6f=' and have no id."""
    raw = chunked(b'{"F2P":4146,"6f=":{"Pk4":"Voyagers"}}')
    assert playtime._id_from_bytes(raw, 0) is None


def test_an_unframed_old_save_is_read_with_the_size_from_its_meta():
    payload = b'{"F2P":4146,"<h0":{"WmU":"0x1234ABCD"}}' + b" " * 40
    assert playtime._id_from_bytes(lz4_literals(payload), len(payload)) == 0x1234ABCD


def test_the_id_cache_decodes_a_file_once(tmp_path, monkeypatch):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Cached", 3600, 0xABCDEF, 1000)
    calls = []
    real = playtime._id_from_data

    def counting(path, expected):
        calls.append(path)
        return real(path, expected)

    monkeypatch.setattr(playtime, "_id_from_data", counting)
    cache = playtime._IdCache(tmp_path / "ids.json")
    data = live / slotmap.data_filename(slotmap.slot_file_numbers(1)[0])
    st = data.stat()
    for _ in range(3):
        assert cache.get_or_read(data, st.st_size, st.st_mtime, 0) == 0xABCDEF
    assert len(calls) == 1

    cache.flush()
    assert playtime._IdCache(tmp_path / "ids.json").get_or_read(data, st.st_size, st.st_mtime, 0) == 0xABCDEF
    assert len(calls) == 1          # a second run does not decode it again either


# --- the report ---------------------------------------------------------------


def test_both_members_and_every_backup_count_once(tmp_path):
    """One playthrough: two members, plus an older copy of it in the vault."""
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Main", 7200, 0xAAAA, 2000)
    steam_save(live, 1, 1, "Main", 7100, 0xAAAA, 1900)
    backup = tmp_path / "bk"
    steam_save(backup, 1, 0, "Main", 3600, 0xAAAA, 1000)

    vault = _vault(tmp_path)
    from nms_save_vault.core import operations as ops

    ops.import_backup(vault, backup, label="yesterday")
    report = playtime.collect(vault, [live])

    assert len(report.playthroughs) == 1
    p = report.playthroughs[0]
    assert p.play_time == 7200 and p.copies == 3        # highest wins, not the sum
    assert report.total_play_time == 7200
    assert report.saves == 3 and p.identified


def test_a_renamed_save_is_still_one_playthrough_under_its_current_name(tmp_path):
    live = tmp_path / "live"
    steam_save(live, 2, 0, "New Name", 9000, 0xBBBB, 5000)
    old = tmp_path / "old"
    steam_save(old, 7, 0, "Old Name", 4000, 0xBBBB, 1000)   # different slot, too

    vault = _vault(tmp_path)
    from nms_save_vault.core import operations as ops

    ops.import_backup(vault, old, label="before the rename")
    report = playtime.collect(vault, [live])

    assert len(report.playthroughs) == 1
    assert report.playthroughs[0].name == "New Name"
    assert report.playthroughs[0].play_time == 9000


def test_a_renamed_save_is_found_in_every_backup_under_every_name(tmp_path):
    """Save-file history: from any slot holding the run, reach every backup of it."""
    from nms_save_vault.core import operations as ops

    vault = _vault(tmp_path)
    for name, slot, ts in (("First", 1, 1000), ("Second", 4, 2000), ("First", 1, 1500)):
        bk = tmp_path / f"bk-{ts}"
        steam_save(bk, slot, 0, name, ts, 0xBBBB, ts)
        steam_save(bk, 2, 0, "Other run", ts, 0xCCCC, ts)
        ops.import_backup(vault, bk, label=name)
    live = tmp_path / "live"
    steam_save(live, 9, 0, "Third", 3000, 0xBBBB, 3000)

    report = playtime.collect(vault, [live])
    p = report.find(live, 9)

    assert p is not None and p.name == "Third"
    assert p.former_names == ["Second", "First"]
    assert p.backups == 3
    entries = {Path(e.path): e for e in vault.entries}
    assert {(Path(f), s) for f, s in p.locations} == (
        {(path, 4 if e.label == "Second" else 1) for path, e in entries.items()} | {(live, 9)}
    )
    assert report.find(next(iter(entries)), 2).name == "Other run"
    assert report.find(live, 5) is None


def test_the_same_run_on_two_platforms_is_one_playthrough(tmp_path, make_wgs_account, monkeypatch):
    """An NMS cloud save carried to another platform keeps its id, so it must not double."""
    ident = 0xDECA9E5054EFD19E
    live = tmp_path / "steam"
    steam_save(live, 3, 0, "The Cartographers", 30000, ident, 4000)
    xbox = make_wgs_account(tmp_path / "xb", [("Slot3Auto", '{"a":1}', "The Cartographers", "", 29000, 3000)])
    # The wgs meta carries no id; the real one lives in the blob, which the fixture cannot
    # build at this size -- so stand in for the decode with the id that save would hold.
    monkeypatch.setattr(playtime, "_id_from_data", lambda path, expected: ident)

    report = playtime.collect(_vault(tmp_path), [live, xbox], use_cache=False)

    assert len(report.playthroughs) == 1
    p = report.playthroughs[0]
    assert sorted(p.platforms) == ["steam", "xbox"] and p.cross_platform
    assert p.play_time == 30000


def test_saves_without_an_id_fall_back_to_name_and_are_flagged(tmp_path, make_wgs_account, monkeypatch):
    xbox = make_wgs_account(tmp_path / "xb", [
        ("Slot1Auto", '{"a":1}', "Voyagers", "", 30865, 1000),
        ("Slot2Auto", '{"b":2}', "Singularity", "", 21623, 1000),
    ])
    monkeypatch.setattr(playtime, "_id_from_data", lambda path, expected: None)

    report = playtime.collect(_vault(tmp_path), [xbox], use_cache=False)

    assert len(report.playthroughs) == 2
    assert report.unidentified == 2
    assert report.total_play_time == 30865 + 21623


def test_an_unreadable_folder_does_not_sink_the_report(tmp_path):
    live = tmp_path / "live"
    steam_save(live, 1, 0, "Main", 1000, 0xCCCC, 1000)
    report = playtime.collect(_vault(tmp_path), [live, tmp_path / "does-not-exist"])
    assert len(report.playthroughs) == 1 and report.folders == 1
