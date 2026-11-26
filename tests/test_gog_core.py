"""GOG in the core (GGF 2026-11-26): GOG's DefaultUser folder is a Steam-format account
("steam", "DefaultUser"). It has a version guard of its own, KNOWN["gog"], so a save from a newer
Steam game is not put where an older GOG game would load it. It gets the Galaxy note where Steam
gets the Steam Cloud one, and an edit and an unlock go through the api there as on Steam."""
from __future__ import annotations

import dataclasses

import pytest

from nms_save_vault.core import api, gameversion, savejson
from nms_save_vault.core.hashname import key as K
from nms_save_vault.core.liveops.edit import EditRefused, plan_edit
from nms_save_vault.core.liveops.planner import plan_place
from nms_save_vault.core.organizer.model import LiveRef
from nms_save_vault.core.savepatch import AddMember

from .conftest import steam_save
from .liveops_support import MAIN, POLESTAR, STEAM_ACCOUNT, T0, steam_world
from .test_core_api_account import give_account, unlocked

GOG = LiveRef("steam", "DefaultUser")
STEAM = LiveRef("steam", STEAM_ACCOUNT)
NEWEST = gameversion.KNOWN["steam"].base_version


def codes(warnings) -> set[str]:
    return {w.code for w in warnings}


@pytest.fixture
def gog_behind(monkeypatch):
    """GOG's game one version behind Steam's."""
    behind = dataclasses.replace(gameversion.KNOWN["gog"], base_version=NEWEST - 1)
    monkeypatch.setitem(gameversion.KNOWN, "gog", behind)


def _gog_world(tmp_path, *, cloud=False):
    """Steam account 1 and GOG's folder; slot 1 of each written as the game writes it."""
    world = steam_world(tmp_path, cloud=cloud, other_name="DefaultUser")
    for folder in (world.live, world.other):
        for member, play, ts in ((0, 7200, T0 + 100), (1, 7100, T0 + 50)):
            body = b'{"' + K("SaveName").encode() + b'":"Main","m":%d}' % member
            steam_save(folder, 1, member, "Main", play, MAIN, ts, payload=body + b"\x00",
                       extra=folder.name)
    world.refresh()
    return world


def test_the_guard_of_a_folder_is_gog_only_for_gog_s_own_folder():
    assert gameversion.guard_for("steam", "DefaultUser") == "gog"
    assert gameversion.guard_for("steam", STEAM_ACCOUNT) == "steam"
    assert gameversion.guard_for("xbox", "DefaultUser") == "xbox"
    assert gameversion.KNOWN["gog"].meta_format == gameversion.KNOWN["steam"].meta_format


def test_a_save_newer_than_gog_s_game_is_not_put_into_gog(tmp_path, gog_behind):
    """Steam's game writes NEWEST; GOG's knows NEWEST - 1. Into GOG: GAME_NEWER, with
    the same code and text as every other newer save; into Steam: no such block."""
    world = steam_world(tmp_path, other_name="DefaultUser")
    steam_save(world.live, 4, 0, "Fresh", 100, POLESTAR + 1, T0 + 500, base_version=NEWEST)
    ctx = world.refresh()
    fresh = live_steam_source(world, 4)
    into_gog = plan_place(ctx, fresh, GOG.target(5))
    assert "GAME_NEWER" in codes(into_gog.blocked)
    (block,) = [w for w in into_gog.blocked if w.code == "GAME_NEWER"]
    assert block.text == gameversion.GAME_NEWER_TEXT
    assert "GAME_NEWER" not in codes(plan_place(ctx, fresh, STEAM.target(5)).blocked)


def test_a_gog_save_newer_than_gog_s_game_is_not_edited(tmp_path, gog_behind):
    world = _gog_world(tmp_path)
    for member in (0, 1):
        body = b'{"' + K("SaveName").encode() + b'":"Main","m":%d}' % member
        steam_save(world.other, 1, member, "Main", 7200 - member, MAIN, T0 + 100 - member,
                   payload=body + b"\x00", base_version=NEWEST)
    ctx = world.refresh()
    with pytest.raises(EditRefused, match="newer game version"):
        plan_edit(ctx, GOG.target(1), [AddMember((), "Units", b"1")], what="x")
    plan_edit(ctx, STEAM.target(1), [AddMember((), "Units", b"1")], what="x")   # Steam: fine


def test_known_game_version_judges_a_gog_save_by_gog_s_game(tmp_path, gog_behind):
    """Savecraft asks this up front, before the user builds a change (SC review 2026-11-26)."""
    from nms_save_vault.core import savedir

    world = steam_world(tmp_path, other_name="DefaultUser")
    for folder in (world.live, world.other):
        steam_save(folder, 6, 0, "Fresh", 100, POLESTAR + 2, T0 + 500, base_version=NEWEST,
                   extra=folder.name)
    gog_member = savedir.scan_slot(world.other, 6).a
    steam_member = savedir.scan_slot(world.live, 6).a
    assert api.known_game_version(gog_member, platform="steam") is False
    assert api.known_game_version(steam_member, platform="steam") is True


def test_a_write_into_gog_carries_the_galaxy_note_and_not_steam_s(tmp_path):
    world = steam_world(tmp_path, other_name="DefaultUser")     # Steam Cloud on in st_
    into_gog = plan_place(world.ctx, world.polestar(), GOG.target(5))
    assert "GOG_CLOUD_OTHER_DEVICE" in codes(into_gog.warnings)
    assert "STEAM_CLOUD_OTHER_DEVICE" not in codes(into_gog.warnings)
    (note,) = [w for w in into_gog.warnings if w.code == "GOG_CLOUD_OTHER_DEVICE"]
    assert note.severity == "info" and "GOG Galaxy" in note.text
    assert "GOG_CLOUD_OTHER_DEVICE" not in into_gog.confirmations
    into_steam = plan_place(world.ctx, world.polestar(), STEAM.target(5))
    assert "STEAM_CLOUD_OTHER_DEVICE" in codes(into_steam.warnings)
    assert "GOG_CLOUD_OTHER_DEVICE" not in codes(into_steam.warnings)


def test_an_edit_and_an_unlock_go_through_the_api_in_gog_s_folder(tmp_path, monkeypatch):
    """End to end through api.open_live: an edit of GOG slot 1, its undo, and an unlock of
    GOG's account data, each kept and named apart from Steam slot 1 of the same number."""
    from nms_save_vault.core import appinfo, platform, safety, state

    from .liveops_support import host_in

    world = _gog_world(tmp_path)
    host = host_in(tmp_path)
    monkeypatch.setattr(platform.Host, "current", classmethod(lambda cls: host))
    monkeypatch.setattr(state, "default_state_path", lambda: tmp_path / "own" / "state.json")
    monkeypatch.setattr(safety, "is_game_running", lambda *a, **k: False)
    (tmp_path / "own").mkdir()
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        steam_before = (world.live / "save.hg").read_bytes()
        gog_before = (world.other / "save.hg").read_bytes()
        session = api.open_live(world.vault_root, live_dirs=[world.live, world.other])
        slot = api.slot_of(world.other, 1)
        assert (slot.platform, slot.account_key, slot.slot) == ("steam", "DefaultUser", 1)
        plan = session.plan_edit(slot, [api.AddMember((), "Units", api.dumps(4242))],
                                 what="add units")
        assert "GOG slot 1" in plan.outcome
        done = session.write(plan, plan.confirmations)
        assert done.status == "done", done.text
        assert savejson.decode_save((world.other / "save.hg").read_bytes()).root[K("Units")] == 4242
        assert (world.live / "save.hg").read_bytes() == steam_before    # Steam slot 1 untouched
        history = api.open_history(world.vault_root)
        last = history.last_before("nms-savecraft", slot=slot)
        assert last is not None and last.change.op_id == done.op_id
        assert history.last_before("nms-savecraft", slot=api.slot_of(world.live, 1)) is None
        undo = session.plan_undo(done.op_id)
        assert session.write(undo, undo.confirmations).status == "done"
        assert (world.other / "save.hg").read_bytes() == gog_before

        give_account(world.other, UnlockedSeasonRewards=["S1"])
        give_account(world.live, UnlockedSeasonRewards=["S1"])
        unlock = session.plan_unlock(world.other, {"UnlockedSeasonRewards": ["^S2"]},
                                     what="unlock 1 reward")
        assert not unlock.blocked
        wrote = session.write_account(unlock, unlock.confirmations)
        assert wrote.status == "done", wrote.text
        assert unlocked(world.other, "UnlockedSeasonRewards") == ["^S1", "^S2"]
        assert unlocked(world.live, "UnlockedSeasonRewards") == ["^S1"]  # Steam's untouched
    finally:
        appinfo.set_app(previous.name, previous.version)


def live_steam_source(world, slot: int):
    """A live Steam slot of account 1 as a place source."""
    import hashlib
    from pathlib import Path

    from nms_save_vault.core import savedir
    from nms_save_vault.core.liveops import PairSource
    from nms_save_vault.core.organizer.locator import Location

    a = savedir.scan_slot(world.live, slot).a
    vid = hashlib.sha256(Path(a.data_path).read_bytes()).hexdigest()
    return PairSource(vid, None, "A", Location(vid, "live", STEAM, slot, "A", "steam", False))
