"""liveops.planner: the eviction picker, plan_place and plan_promote over real D1 and D3
pieces in a sandbox (D5 2.3; T-P0, T-B1, T-B2, T-B3)."""
from __future__ import annotations

import dataclasses
import os
import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from nms_save_vault.core.liveops import PairSource, SlotCandidate
from nms_save_vault.core.liveops.errors import SourceUnreadable
from nms_save_vault.core.liveops import opjournal as jr
from nms_save_vault.core.liveops import planner
from nms_save_vault.core.liveops.planner import eviction_picker, plan_place, plan_promote, suggest
from nms_save_vault.core.organizer.model import LiveRef, LiveTarget

from .conftest import steam_save
from .liveops_support import (
    MAIN,
    POLESTAR,
    STEAM_ACCOUNT,
    STEAM_ACCOUNT_2,
    T0,
    XBOX_ACCOUNT,
    live_source,
    steam_world,
    tree_hash,
)

LIVE = LiveRef("steam", STEAM_ACCOUNT)
OTHER = LiveRef("steam", STEAM_ACCOUNT_2)
OP = "op-20261016T101502Z-3fa2"


@pytest.fixture
def world(tmp_path):
    return steam_world(tmp_path)


def codes(warnings) -> set[str]:
    return {w.code for w in warnings}


def test_planning_writes_nothing_anywhere(world):
    """T-P0: the picker and every plan read only - no live file, no vault file, no journal,
    no scan cache is written."""
    before = tree_hash(world.tmp)
    picker = eviction_picker(world.ctx, world.polestar())
    plan_place(world.ctx, world.polestar(), LIVE.target(1))
    plan_promote(world.ctx, LIVE.target(1), "B")
    assert tree_hash(world.tmp) == before
    assert not jr.ops_path(world.ctx).exists()
    assert len(picker.candidates) == 15


def test_the_picker_shows_each_slot_with_what_writing_there_does(world):
    """T-B1 and the suggestion: the save's own account would be the default, here the
    caller names the desktop; slot 3 is the lowest empty one."""
    picker = eviction_picker(world.ctx, world.polestar(), LIVE)
    assert picker.account == LIVE and picker.source.name == "Polestar"
    assert picker.source.where_label == "vault: backup of laptop"
    one, two, three = picker.candidates[:3]
    assert (one.state, one.kind, one.line_name, one.newer) == ("occupied", "place", "Main", "A")
    assert one.outcome == "Swap into Steam slot 1 (Desktop)"
    assert codes(one.warnings) == {"NOT_IN_VAULT"} and not one.durable
    assert one.vault_copy == "none" and one.a.play_time == 7200 and one.b.play_time == 7100
    assert two.state == "occupied" and two.b is None
    assert (three.state, three.kind) == ("empty", "place_into_empty")
    assert codes(three.warnings) == {"EMPTY_SLOT"}
    assert (picker.suggested_slot, three.suggested, three.suggest_reason) == (3, True, "empty")
    assert codes(picker.warnings) == {"OTHER_ACCOUNT", "LIVE_ELSEWHERE",
                                      "STEAM_CLOUD_OTHER_DEVICE"}
    (laptop,) = picker.other_accounts
    assert (laptop.account, laptop.label, laptop.blocked, laptop.warnings) == (
        OTHER, "Laptop", "", ())                       # its own account: no warning
    assert eviction_picker(world.ctx, world.polestar()).account == OTHER     # where it is live


def test_slot_states_partial_unreadable_and_mixed(world):
    steam_save(world.live, 4, 0, "Main", 10, MAIN, T0, extra="x")            # mixed slot
    steam_save(world.live, 4, 1, "Other", 10, 0x1234, T0, extra="y")
    (world.live / "save11.hg").write_bytes(b"half a pair")                   # slot 6 partial
    steam_save(world.live, 7, 0, "Bad", 10, 0x5555, T0, extra="z")           # slot 7 unreadable
    (world.live / "mf_save13.hg").write_bytes(b"\x00" * 432)
    ctx = world.refresh()
    cands = {c.target.slot: c for c in eviction_picker(ctx, world.polestar(), LIVE).candidates}
    assert (cands[4].state, codes(cands[4].warnings)) == ("mixed", {"MIXED_SLOT", "NOT_IN_VAULT"})
    assert cands[6].state == "partial" and "PARTIAL_SLOT" in codes(cands[6].warnings)
    assert cands[7].state == "unreadable" and "UNREADABLE_MEMBER" in codes(cands[7].warnings)


Cand = namedtuple("Cand", "slot kind pinned durable last blocked")


def _cand(c: Cand) -> SlotCandidate:
    return SlotCandidate(LiveTarget("steam", STEAM_ACCOUNT, c.slot), "", "occupied", None, None,
                         None, None, "", 0, c.last, c.durable, "none", c.pinned, False, False, "",
                         c.kind, "", (), c.blocked)


@pytest.mark.parametrize("cands,expected", [
    ([Cand(2, "restore_older", True, False, 5, ""), Cand(4, "restore_older", False, False, 9, ""),
      Cand(1, "place_into_empty", False, False, 0, "")], (4, "same_line")),
    ([Cand(9, "place_into_empty", False, False, 0, ""), Cand(3, "place_into_empty", False, False, 0, ""),
      Cand(1, "place", False, True, 1, "")], (3, "empty")),
    ([Cand(1, "place", False, False, 1, ""), Cand(2, "place", False, True, 9, ""),
      Cand(3, "place", False, True, 5, "")], (3, "in_vault_least_recent")),
    ([Cand(1, "place", True, True, 1, ""), Cand(2, "place", False, False, 9, ""),
      Cand(3, "place", False, False, 5, "")], (3, "least_recent")),
    ([Cand(1, "place", True, True, 1, ""), Cand(2, "place_into_empty", False, False, 0, "X")],
     (None, "")),
])
def test_the_suggestion_order(cands, expected):
    """T-B2: own line > lowest empty > in the vault, least recent > least recent > none."""
    assert suggest([_cand(c) for c in cands]) == expected


def test_place_plans_both_writes_and_what_it_evicts(world):
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    assert plan.kind == "place" and not plan.blocked
    assert plan.confirmations == {"OTHER_ACCOUNT"}
    (t,) = plan.targets
    entry = world.ctx.versions.entry(world.backup_id)
    assert [(w.position, w.vid) for w in t.writes] == [("A", entry.member(3, "A").vid),
                                                       ("B", entry.member(3, "B").vid)]
    assert t.writes[0].meta_ts == t.writes[1].meta_ts + 1
    assert t.writes[0].source_meta_ts == T0 + 300
    assert [(e.position, e.name, e.durable_before) for e in t.evictions] == [
        ("A", "Main", False), ("B", "Main", False)]
    assert t.source == world.polestar()
    assert plan.fingerprint == plan_place(world.ctx, world.polestar(), LIVE.target(1)).fingerprint
    os.utime(world.live / "save.hg", (T0, T0))
    assert plan.fingerprint != plan_place(world.ctx, world.polestar(), LIVE.target(1)).fingerprint
    assert plan.estimate.write_bytes > 0 and plan.estimate.snapshot_bytes >= 0


def test_restore_older_twins_and_the_version_already_there(world):
    """The save's own slot: an older version needs the play-time confirmation; the same
    version is blocked; a second slot of the same save needs the twin confirmation."""
    steam_save(world.other, 3, 0, "Polestar", 5000, POLESTAR, T0 + 900, extra="pole-played")
    ctx = world.refresh()
    plan = plan_place(ctx, world.polestar(), OTHER.target(3))
    assert plan.kind == "restore_older" and "PLAY_TIME_AHEAD" in plan.confirmations
    played = next(w for w in plan.warnings if w.code == "PLAY_TIME_AHEAD")
    assert played.data["seconds"] == 1000 and "16m" in played.text
    assert plan.outcome.startswith("Go back to this version in Steam slot 3")
    twin = plan_place(ctx, world.polestar(), OTHER.target(5))
    assert "TWIN_LIVE" in twin.confirmations and "OTHER_ACCOUNT" not in twin.confirmations
    world2 = steam_world(world.tmp / "again")
    same = plan_place(world2.ctx, world2.polestar(), OTHER.target(3))
    assert codes(same.blocked) == {"ALREADY_LIVE"}


def test_promote_plans_the_slots_own_pair(world):
    plan = plan_promote(world.ctx, LIVE.target(1), "B")
    assert plan.kind == "promote" and not plan.blocked and not plan.confirmations
    lw, ow = plan.targets[0].writes
    assert (lw.position, ow.position) == ("B", "A") and lw.meta_ts > ow.meta_ts
    assert codes(plan_promote(world.ctx, LIVE.target(1), "A").blocked) == {"ALREADY_LOADS"}
    assert codes(plan_promote(world.ctx, LIVE.target(2), "B").blocked) == {"NOTHING_TO_PROMOTE"}


@pytest.mark.parametrize("probe,code,severity", [(True, "GAME_RUNNING", "block"),
                                                 (None, "GAME_STATE_UNKNOWN", "confirm")])
def test_the_game_blocks_or_asks(world, probe, code, severity):
    world.ctx.game_probe = lambda: probe
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(3))
    found = [w for w in plan.blocked + plan.warnings if w.code == code]
    assert found and found[0].severity == severity


def test_an_open_operation_blocks_its_account_only(world):
    jr.append(world.ctx, OP, "preparing", {"kind": "place", "targets": [jr.target_json(
        LIVE.target(9))]})
    assert "RECOVERY_PENDING" in codes(plan_place(world.ctx, world.polestar(),
                                                 LIVE.target(3)).blocked)
    assert not plan_place(world.ctx, world.polestar(), OTHER.target(5)).blocked


def test_a_vault_without_the_store_or_space_blocks(world, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(planner.shutil, "disk_usage", lambda p: usage(1, 1, 1024))
    assert "VAULT_SPACE" in codes(plan_place(world.ctx, world.polestar(), LIVE.target(3)).blocked)
    monkeypatch.setattr(planner.shutil, "disk_usage", shutil.disk_usage)
    (world.vault_root / "store" / "FORMAT").rename(world.vault_root / "FORMAT.aside")
    assert "VAULT_UPGRADE_NEEDED" in codes(
        plan_place(world.ctx, world.polestar(), LIVE.target(3)).blocked)


def test_a_store_that_cannot_be_opened_is_unavailable_not_upgrade_needed(world):
    ctx = dataclasses.replace(world.ctx, versions=None)
    found = codes(plan_place(ctx, world.polestar(), LIVE.target(3)).blocked)
    assert "VAULT_UNAVAILABLE" in found and "VAULT_UPGRADE_NEEDED" not in found


def test_a_source_that_cannot_be_read_is_blocked(world):
    plan = plan_place(world.ctx, PairSource("ab" * 32, None, "A"), LIVE.target(3))
    assert codes(plan.blocked) == {"SOURCE_UNREADABLE"} and not plan.targets[0].writes


def test_other_platforms_and_xgs_only_folders_are_listed_with_why(world, make_wgs_account,
                                                                  monkeypatch):
    acct = make_wgs_account(world.tmp / "SystemAppData",
                            [("Slot1Auto", '{"a":1}', "Xmain", "", 60, T0 + 5.5)])
    old = T0 - 3600
    os.utime(acct / "containers.index", (old, old))
    xgs_only = world.tmp / "Other" / "xgs" / "0000000000000002_ABC"
    (xgs_only / "Slot1Auto").mkdir(parents=True)
    (xgs_only / "Slot1Auto" / "data").write_bytes(b"x")
    world.sources += [live_source(acct, platform_name="xbox"),
                      live_source(xgs_only, platform_name="xbox", writable=False)]
    ctx = world.refresh()
    picker = eviction_picker(ctx, world.polestar(), LIVE)
    blocked = {c.account.account_key: c.blocked for c in picker.other_accounts}
    assert blocked[XBOX_ACCOUNT] == ""
    assert blocked[xgs_only.name] == "XGS_READ_ONLY"
    with pytest.raises(SourceUnreadable):
        eviction_picker(ctx, PairSource(None, None))


def test_xbox_sync_activity_pending_uploads_and_quota(world, make_wgs_account, monkeypatch):
    acct = make_wgs_account(world.tmp / "SystemAppData", [
        ("Slot1Auto", '{"a":1}', "Xmain", "", 60, T0 + 5.5),
        ("Slot1Manual", '{"b":1}', "Xmain", "", 50, T0 + 2.5)])
    world.sources.append(live_source(acct, platform_name="xbox"))
    ctx = world.refresh()
    xbox = LiveRef("xbox", XBOX_ACCOUNT)
    promote = plan_promote(ctx, xbox.target(1), "B")
    assert "XBOX_SYNC_BUSY" in codes(promote.blocked)            # the index was just written
    old = T0 - 3600
    os.utime(acct / "containers.index", (old, old))
    promote = plan_promote(ctx, xbox.target(1), "B")
    assert not promote.blocked and "PENDING_UPLOAD" in codes(promote.warnings)
    monkeypatch.setattr(planner, "XBOX_QUOTA_MAX", 10)
    assert "XBOX_QUOTA" in codes(plan_promote(ctx, xbox.target(1), "B").blocked)


def test_warning_texts_are_ascii_and_never_name_an_account(world):
    plans = [plan_place(world.ctx, world.polestar(), LIVE.target(s)) for s in (1, 3)]
    picker = eviction_picker(world.ctx, world.polestar(), LIVE)
    texts = [w.text for p in plans for w in p.warnings + p.blocked] + [p.outcome for p in plans]
    texts += [c.outcome for c in picker.candidates] + [w.text for w in picker.warnings]
    for text in texts:
        assert text.isascii() and "76561198" not in text and str(Path(world.tmp)) not in text
