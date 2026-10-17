"""The game-version guard (D5): a save written by a newer game version than the app knows stays
readable, but no live write may read from it or replace it - Place, Swap, Promote, Undo, restore
are blocked with GAME_NEWER and the plain message; the Organizer says so. Synthetic metas with a
bumped BaseVersion / meta format."""
from __future__ import annotations

import dataclasses

import pytest

from nms_save_vault.core import gameversion, formats
from nms_save_vault.core import store as vstore
from nms_save_vault.core.liveops import PairSource, execute
from nms_save_vault.core.liveops.planner import (
    eviction_picker, plan_keep, plan_place, plan_promote, plan_swap)
from nms_save_vault.core.liveops.panic import plan_panic_restore
from nms_save_vault.core.organizer.locator import Location
from nms_save_vault.core.organizer.model import LiveRef, StoreRef

from .conftest import steam_save
from .liveops_support import STEAM_ACCOUNT, STEAM_ACCOUNT_2, T0, steam_world

LIVE = LiveRef("steam", STEAM_ACCOUNT)
OTHER = LiveRef("steam", STEAM_ACCOUNT_2)
NEWER_BV = gameversion.KNOWN["steam"].base_version + 1
NEWER_ID = 0x4E45574552000001
TEXT = ("This save was written by a newer game version than this app knows. "
        "Update the app before moving it.")


@pytest.fixture
def world(tmp_path):
    return steam_world(tmp_path)


def _newer_in_vault(world) -> PairSource:
    """A save written by a newer game, held by a vault backup of the other account (slot 7)."""
    steam_save(world.other, 7, 0, "FromTheFuture", 500, NEWER_ID, T0 + 900, extra="future",
               base_version=NEWER_BV)
    store = vstore.open_store(world.vault_root, live_roots=[world.live, world.other])
    entry = store.add_from_live(world.other, platform="steam", account_key=world.other.name,
                                kind="full", label="future backup", scope=vstore.CaptureScope())
    world.refresh()
    member = store.entry(entry.id).member(7, "A")
    return PairSource(member.vid, None, "A", Location(member.vid, "store", StoreRef(entry.id), 7,
                                                      "A", "steam", True))


def codes(items) -> set[str]:
    return {w.code for w in items}


def test_the_known_format_is_recorded_per_platform_with_the_container_kind():
    for platform in ("steam", "xbox"):
        known = gameversion.KNOWN[platform]
        assert known.meta_format == formats.META_FORMAT_4 and known.base_version >= 4228
        assert known.containers
    assert not gameversion.newer("steam", formats.META_FORMAT_4, 4228)
    assert gameversion.newer("steam", formats.META_FORMAT_4, 4229)
    assert gameversion.newer("steam", formats.META_FORMAT_4 + 1, 0)
    assert not gameversion.newer("unknown-platform", 9999, 9999)


def test_a_newer_source_blocks_place_and_is_stated_in_plain_words(world):
    source = _newer_in_vault(world)
    plan = plan_place(world.ctx, source, LIVE.target(4))
    assert "GAME_NEWER" in codes(plan.blocked)
    assert next(w for w in plan.blocked if w.code == "GAME_NEWER").text == TEXT
    outcome = execute(world.ctx, plan, plan.confirmations)
    assert outcome.status == "refused" and outcome.code == "GAME_NEWER"


def test_the_picker_blocks_every_slot_for_a_newer_source(world):
    picker = eviction_picker(world.ctx, _newer_in_vault(world))
    assert "GAME_NEWER" in codes(picker.warnings)
    assert all(c.blocked == "GAME_NEWER" for c in picker.candidates)


def test_a_newer_save_in_the_slot_cannot_be_replaced(world):
    steam_save(world.live, 1, 0, "Main", 7200, 0xDDDAD1BDD39F9C72, T0 + 100,
               extra="main-a" + world.live.name, base_version=NEWER_BV)
    world.refresh()
    plan = plan_place(world.ctx, world.polestar(), LIVE.target(1))
    assert "GAME_NEWER" in codes(plan.blocked)
    picker = eviction_picker(world.ctx, world.polestar(), LIVE)
    assert next(c for c in picker.candidates if c.target.slot == 1).blocked == "GAME_NEWER"
    assert next(c for c in picker.candidates if c.target.slot == 4).blocked != "GAME_NEWER"


def test_swap_and_promote_of_a_newer_save_are_blocked(world):
    steam_save(world.live, 2, 0, "Second", 600, 0x0123456789ABCDEF, T0 + 10,
               extra="second-a" + world.live.name, base_version=NEWER_BV)
    world.refresh()
    assert "GAME_NEWER" in codes(plan_swap(world.ctx, LIVE.target(1), LIVE.target(2)).blocked)
    steam_save(world.live, 1, 1, "Main", 7100, 0xDDDAD1BDD39F9C72, T0 + 50,
               extra="main-b" + world.live.name, base_version=NEWER_BV)
    world.refresh()
    assert "GAME_NEWER" in codes(plan_promote(world.ctx, LIVE.target(1), "B").blocked)


def test_a_newer_meta_format_counts_too(world):
    steam_save(world.live, 2, 0, "Second", 600, 0x0123456789ABCDEF, T0 + 10,
               extra="second-a" + world.live.name, meta_format=formats.META_FORMAT_4 + 1)
    world.refresh()
    assert "GAME_NEWER" in codes(plan_swap(world.ctx, LIVE.target(1), LIVE.target(2)).blocked)


def test_a_whole_folder_restore_that_would_replace_a_newer_save_is_blocked(world):
    store = vstore.open_store(world.vault_root, live_roots=[world.live, world.other])
    backup = store.add_from_live(world.live, platform="steam", account_key=world.live.name,
                                 kind="full", label="mine", scope=vstore.CaptureScope())
    steam_save(world.live, 1, 0, "Main", 7200, 0xDDDAD1BDD39F9C72, T0 + 100,
               extra="main-a" + world.live.name, base_version=NEWER_BV)
    world.refresh()
    plan = plan_panic_restore(world.ctx, LIVE, backup.id)
    assert "GAME_NEWER" in codes(plan.blocked)


def test_keeping_a_copy_of_a_newer_save_is_still_allowed(world):
    steam_save(world.live, 1, 0, "Main", 7200, 0xDDDAD1BDD39F9C72, T0 + 100,
               extra="main-a" + world.live.name, base_version=NEWER_BV)
    world.refresh()
    assert "GAME_NEWER" not in codes(plan_keep(world.ctx, LIVE.target(1)).blocked)


def test_an_older_or_equal_format_is_not_blocked(world):
    steam_save(world.live, 2, 0, "Second", 600, 0x0123456789ABCDEF, T0 + 10,
               extra="second-a" + world.live.name,
               base_version=gameversion.KNOWN["steam"].base_version)
    world.refresh()
    assert "GAME_NEWER" not in codes(plan_swap(world.ctx, LIVE.target(1), LIVE.target(2)).blocked)


def test_the_organizer_marks_a_newer_save(world):
    steam_save(world.live, 1, 0, "Main", 7200, 0xDDDAD1BDD39F9C72, T0 + 100,
               extra="main-a" + world.live.name, base_version=NEWER_BV)
    world.refresh()
    from nms_save_vault.ui.organizer import detail, table

    model = world.ctx.readmodel
    line = next(l for l in model.lines() if "newer" in l.flags)
    assert "NEWER GAME VERSION" in table.state_words(line)
    row = next(r for r in model.versions(line.pid) if "newer" in r.flags)
    assert "newer game version" in detail._version_state(row)


def test_an_xbox_meta_is_judged_by_its_own_fields():
    import struct

    from .conftest import xbox_meta

    meta = bytearray(xbox_meta("x", "", play=1, size=1, pid=1, ts=1))
    assert not gameversion.newer_plain("xbox", bytes(meta))
    struct.pack_into("<I", meta, 0, gameversion.KNOWN["xbox"].base_version + 1)
    assert gameversion.newer_plain("xbox", bytes(meta))
    assert not gameversion.newer_plain("xbox", b"garbage")          # unreadable is not "newer"
