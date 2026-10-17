"""Directory fsync at the D5 2.7.1 points: a new ops.jsonl, and after the slot writers'
renames, before the next journal event (review item 7). ``safety.fsync_dir`` is a no-op on
Windows, so the calls are recorded, in order with the renames."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nms_save_vault.core import msstore, safety
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops import steamwrite as sw

from .liveops_support import make_ctx
from .test_liveops_opjournal import OP1, T7, preparing
from .test_liveops_steamwrite import TAG, live_folder, source_writes
from .test_msstore_pair import account, pair, payload


@pytest.fixture
def events(monkeypatch):
    seen: list[tuple[str, str]] = []
    real_replace = os.replace
    monkeypatch.setattr(safety, "fsync_dir", lambda p: seen.append(("fsync", Path(p).name)))
    monkeypatch.setattr(os, "replace", lambda s, d: (seen.append(("replace", Path(d).name)),
                                                     real_replace(s, d))[1])
    return seen


def test_a_new_journal_file_gets_its_directory_entry_synced_once(tmp_path, events):
    ctx = make_ctx(tmp_path)
    preparing(ctx, OP1, T7)
    assert ("fsync", "organizer") in events
    events.clear()
    jr.append(ctx, OP1, "abandoned", {"reason": "not_started"})
    assert not [e for e in events if e[0] == "fsync"]


def test_the_steam_renames_are_synced_before_the_writer_returns(tmp_path, events):
    folder = live_folder(tmp_path)
    writes = source_writes(tmp_path)
    plan = sw.prepare(folder, writes, 7, TAG)
    sw.stage(plan, sw.payload_of(writes, 7))
    events.clear()
    sw.swap(plan)
    assert [e[1] for e in events if e[0] == "replace"] == [f.name for f in plan.files]
    assert events[-1] == ("fsync", folder.name)


def test_the_xbox_blobs_index_and_mirror_are_synced_in_order(tmp_path, make_wgs_account, events):
    acct = account(tmp_path, make_wgs_account)
    writes = pair(2)                                     # Slot2Manual is new: a new record dir
    plan = msstore.prepare_pair(acct, writes)
    events.clear()
    msstore.apply_pair(plan, payload(writes))
    index_at = events.index(("replace", "containers.index"))
    before, after = events[:index_at], events[index_at + 1:]
    for b in plan.blobs:                                 # the blobs before the index names them
        assert ("fsync", b.dir_name) in before
    assert ("fsync", acct.name) in before                # the new record dir's entry
    assert after[0] == ("fsync", acct.name)              # the index rename
    assert ("fsync", "Slot2Manual") in after and ("fsync", "Slot2Auto") in after
