"""liveops.steamwrite: stage beside the live files, swap with the loading meta last, roll
back exactly (D5 2.6.1; T-W1, T-W2, crash points staged:n / swapped:n)."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from nms_save_vault.core import meta, savedir, slotmap
from nms_save_vault.core.liveops import FaultInjector, ResolvedTarget, RollbackError, SimulatedCrash
from nms_save_vault.core.liveops import LiveChanged, WriteError
from nms_save_vault.core.liveops import steamwrite as sw
from nms_save_vault.core.liveops.pairmeta import SourceMember, SourcePair, compute
from nms_save_vault.core.organizer.model import LiveTarget

from .conftest import steam_save
from .liveops_support import STEAM_ACCOUNT, tree_hash

NOW = 1_792_500_903
TAG = "3fa2"
MAIN = 0xDDDAD1BDD39F9C72


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(sw, "RETRY_DELAY", 0.0)


def live_folder(tmp_path: Path) -> Path:
    """Slot 7 holds 'Polestar'; slot 1 another save; accountdata, vdf and cache beside."""
    folder = tmp_path / "live" / STEAM_ACCOUNT
    steam_save(folder, 7, 0, "Polestar", 100, 0x1111, 1_791_900_000, extra="pa")
    steam_save(folder, 7, 1, "Polestar", 90, 0x1111, 1_791_800_000, extra="pb")
    steam_save(folder, 1, 0, "Other", 5, 0x2222, 1_790_000_000)
    (folder / "accountdata.hg").write_bytes(b"account")
    (folder / "mf_accountdata.hg").write_bytes(b"account meta")
    (folder / "steam_autocloud.vdf").write_text("cloud", "utf-8")
    (folder / "cache").mkdir()
    (folder / "cache" / "x.bin").write_bytes(b"cache")
    return folder


def source_writes(tmp_path: Path, slot: int = 7):
    """'Main Save' captured from another folder's slot 3, planned for ``slot`` (both
    positions, A loading), as (member write, data, meta file bytes)."""
    src = tmp_path / "src"
    steam_save(src, 3, 0, "Main Save", 3700, MAIN, 1_791_500_000, extra="ma")
    steam_save(src, 3, 1, "Main Save", 3600, MAIN, 1_791_400_000, extra="mb")
    members, data = {}, {}
    for position in ("A", "B"):
        data_name, meta_name = sw.file_names(3, position)
        raw = (src / data_name).read_bytes()
        vid = hashlib.sha256(raw).hexdigest()
        ordinal = slotmap.storage_ordinal(slotmap.file_no(3, slotmap.member_index(position)))
        plain = meta.decrypt((src / meta_name).read_bytes(), ordinal)
        members[position] = SourceMember(position, vid, "DDDAD1BDD39F9C72", plain)
        data[vid] = raw
    rt = ResolvedTarget(LiveTarget("steam", STEAM_ACCOUNT, slot), "s", Path("x"), "steam-cloud",
                        None, f"Steam slot {slot}")
    planned = compute(SourcePair("steam", "A", members["A"], members["B"]), rt, NOW)
    return [(pm.write, data[pm.write.vid], pm.meta_disk) for pm in planned]


def pre_bytes(folder: Path, plan: sw.SteamWritePlan) -> dict[str, bytes]:
    """What the op's store snapshot would hold for the slot (read before the write)."""
    return {n: (folder / n).read_bytes() for n, st in plan.pre.items() if st is not None}


def minus_slot(tree: dict, slot: int) -> dict:
    names = {n for p in ("A", "B") for n in sw.file_names(slot, p)}
    return {k: v for k, v in tree.items() if k not in names}


def test_stage_then_swap_puts_the_pair_in_with_the_loading_meta_last(tmp_path, monkeypatch):
    """T-W1: temp files fsynced and verified; final names change only in the swap, in the
    order O data, O meta, L data, L meta; no temp file left; nothing else touched."""
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    before = tree_hash(folder)
    plan = sw.prepare(folder, writes, 7, TAG)
    assert tree_hash(folder) == before                   # prepare reads only
    assert [f.name for f in plan.files] == ["save14.hg", "mf_save14.hg", "save13.hg", "mf_save13.hg"]
    assert all(plan.pre[n] is not None for n in plan.pre)

    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(sw.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    sw.stage(plan, sw.payload_of(writes, 7))
    assert len(synced) == 4
    staged = {k: v for k, v in tree_hash(folder).items() if k.startswith(".tmp_")}
    assert sorted(staged) == sorted(f.temp_name for f in plan.files)
    assert {k: v for k, v in tree_hash(folder).items() if k not in staged} == before

    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(sw.os, "replace", lambda s, d: (replaced.append(Path(d).name),
                                                        real_replace(s, d))[1])
    sw.swap(plan)
    assert replaced == [f.name for f in plan.files]
    after = tree_hash(folder)
    assert not [k for k in after if k.startswith(".tmp_")]
    for mw, data, meta_disk in writes:
        data_name, meta_name = sw.file_names(7, mw.position)
        assert (folder / data_name).read_bytes() == data
        assert (folder / meta_name).read_bytes() == meta_disk
        assert (folder / data_name).stat().st_mtime == (folder / meta_name).stat().st_mtime == mw.disk_time
    view = savedir.scan_slot(folder, 7)
    assert view.newest.label == "A" and view.a.save_name == "Main Save"
    assert minus_slot(after, 7) == minus_slot(before, 7)


def test_a_rename_held_up_briefly_is_retried(tmp_path, monkeypatch):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    plan = sw.prepare(folder, writes, 7, TAG)
    sw.stage(plan, sw.payload_of(writes, 7))
    real_replace = os.replace
    refusals = {"save13.hg": 2}

    def flaky(src, dst):
        name = Path(dst).name
        if refusals.get(name):
            refusals[name] -= 1
            raise PermissionError(13, "held open")
        real_replace(src, dst)

    monkeypatch.setattr(sw.os, "replace", flaky)
    sw.swap(plan)
    assert refusals["save13.hg"] == 0
    assert (folder / "save13.hg").read_bytes() == writes[0][1]


def test_a_rename_that_keeps_failing_is_rolled_back_exactly(tmp_path, monkeypatch):
    """T-W2: the files already swapped go back with their bytes and mtime_ns."""
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    before = tree_hash(folder)
    plan = sw.prepare(folder, writes, 7, TAG)
    saved = pre_bytes(folder, plan)
    sw.stage(plan, sw.payload_of(writes, 7))
    real_replace = os.replace
    tries = []

    def stuck(src, dst):
        if Path(dst).name == "save13.hg":
            tries.append(1)
            raise PermissionError(13, "held open")
        real_replace(src, dst)

    monkeypatch.setattr(sw.os, "replace", stuck)
    with pytest.raises(WriteError) as info:
        sw.swap(plan)
    assert info.value.swapped == ("save14.hg", "mf_save14.hg")
    assert len(tries) == sw.RETRIES + 1
    monkeypatch.setattr(sw.os, "replace", real_replace)
    sw.rollback(plan, saved)
    assert tree_hash(folder) == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to replace an open file")
def test_a_file_held_open_on_windows_stops_the_swap(tmp_path):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    before = tree_hash(folder)
    plan = sw.prepare(folder, writes, 7, TAG)
    saved = pre_bytes(folder, plan)
    sw.stage(plan, sw.payload_of(writes, 7))
    with open(folder / "save13.hg", "rb"):
        with pytest.raises(WriteError) as info:
            sw.swap(plan)
    assert info.value.swapped == ("save14.hg", "mf_save14.hg")
    sw.rollback(plan, saved)
    assert tree_hash(folder) == before


@pytest.mark.parametrize("crash", ["swapped:1", "swapped:2", "swapped:3", "swapped:4"])
def test_rollback_after_a_crash_mid_swap_restores_the_pre_image(tmp_path, crash):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    before = tree_hash(folder)
    plan = sw.prepare(folder, writes, 7, TAG)
    saved = pre_bytes(folder, plan)
    sw.stage(plan, sw.payload_of(writes, 7))
    with pytest.raises(SimulatedCrash):
        sw.swap(plan, FaultInjector({crash}))
    assert tree_hash(folder) != before
    sw.rollback(plan, saved)
    assert tree_hash(folder) == before


def test_filling_an_empty_slot_rolls_back_to_empty(tmp_path):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path, slot=9)
    before = tree_hash(folder)
    plan = sw.prepare(folder, writes, 9, TAG)
    assert set(plan.pre.values()) == {None}
    sw.stage(plan, sw.payload_of(writes, 9))
    sw.swap(plan)
    assert (folder / "save17.hg").is_file()
    sw.rollback(plan, {})                               # nothing to restore: only removals
    assert tree_hash(folder) == before


def test_a_crash_while_staging_leaves_only_temp_files_which_sweep_removes(tmp_path):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    before = tree_hash(folder)
    (folder / ".tmp_9999_save13.hg.part").write_bytes(b"another op's leftover")
    plan = sw.prepare(folder, writes, 7, TAG)
    with pytest.raises(SimulatedCrash):
        sw.stage(plan, sw.payload_of(writes, 7), FaultInjector({"staged:2"}))
    assert sorted(sw.sweep(folder, TAG)) == [".tmp_3fa2_mf_save14.hg.part", ".tmp_3fa2_save14.hg.part"]
    assert (folder / ".tmp_9999_save13.hg.part").is_file()      # not this op's
    (folder / ".tmp_9999_save13.hg.part").unlink()
    assert tree_hash(folder) == before


def test_wrong_bytes_are_refused_before_anything_is_written(tmp_path):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    before = tree_hash(folder)
    mw, data, meta_disk = writes[0]
    with pytest.raises(ValueError):
        sw.prepare(folder, [(mw, data + b"x", meta_disk), writes[1]], 7, TAG)
    plan = sw.prepare(folder, writes, 7, TAG)
    payload = sw.payload_of(writes, 7)
    payload["save13.hg"] = b"not the planned bytes"
    with pytest.raises(WriteError):
        sw.stage(plan, payload)
    assert tree_hash(folder) == before                   # what was staged is swept


def test_a_slot_written_after_the_plan_is_never_swapped(tmp_path):
    """Review item 5: the game saved between plan and swap; nothing is renamed, the staged
    files go, and the error is not a WriteError (a rollback would overwrite the game's
    save with the pre-image)."""
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    plan = sw.prepare(folder, writes, 7, TAG)
    sw.stage(plan, sw.payload_of(writes, 7))
    steam_save(folder, 7, 1, "Polestar", 95, 0x1111, 1_791_950_000, extra="game saved")
    game_state = {k: v for k, v in tree_hash(folder).items() if not k.startswith(".tmp_")}
    with pytest.raises(LiveChanged) as info:
        sw.swap(plan)
    assert not isinstance(info.value, WriteError)
    assert tree_hash(folder) == game_state


def test_rollback_without_every_pre_image_changes_nothing(tmp_path):
    """Review item 4: one name's pre-op bytes are missing, so no name is restored and the
    post state stays whole (not half put back)."""
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    plan = sw.prepare(folder, writes, 7, TAG)
    saved = pre_bytes(folder, plan)
    sw.stage(plan, sw.payload_of(writes, 7))
    sw.swap(plan)
    after = tree_hash(folder)
    del saved["mf_save13.hg"]
    with pytest.raises(RollbackError, match="nothing was changed"):
        sw.rollback(plan, saved)
    assert tree_hash(folder) == after


def test_rollback_refuses_bytes_that_are_not_the_recorded_ones(tmp_path):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    plan = sw.prepare(folder, writes, 7, TAG)
    sw.stage(plan, sw.payload_of(writes, 7))
    sw.swap(plan)
    wrong = pre_bytes(folder, plan)                      # read AFTER the write: the new bytes
    with pytest.raises(RollbackError):
        sw.rollback(plan, wrong)
    assert (folder / "save13.hg").read_bytes() == writes[0][1]   # nothing overwritten wrongly
