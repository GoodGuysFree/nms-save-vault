"""GOG's DefaultUser folder (Steam's format) is named GOG wherever a live account is named."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nms_save_vault import cli_live
from nms_save_vault.core.liveops.targets import slot_label
from nms_save_vault.core.organizer.model import LiveTarget, platform_letter, platform_name, sync_name
from nms_save_vault.core.state import Source
from nms_save_vault.ui.organizer.detail import _platform_sentence
from nms_save_vault.ui.organizer.live import slot_tip
from nms_save_vault.ui.organizer.table import state_words
from nms_save_vault.ui.organizer.words import slot_words

from .conftest import steam_save
from .liveops_support import steam_world
from .test_organizer_readmodel import MAIN, PID, T0, World

STEAM = "st_76561198000000001"


def test_a_steam_format_folder_named_default_user_is_gog():
    assert [platform_name(*a) for a in (("steam", STEAM), ("steam", "DefaultUser"), ("xbox", "x"))] \
        == ["Steam", "GOG", "Xbox"]
    assert platform_letter("steam", "DefaultUser") == "G"
    assert platform_name("xbox", "DefaultUser") == "Xbox"     # the name only counts on Steam's format
    assert [sync_name("steam", STEAM), sync_name("steam", "DefaultUser"), sync_name("xbox")] == \
        ["Steam Cloud", "Cloud sync", "Xbox sync"]
    assert slot_label("steam", 7, account_key="DefaultUser") == "GOG slot 7"
    assert [slot_words("G7"), slot_words("S2.7")] == ["GOG slot 7", "Steam 2 slot 7"]


def test_the_cli_reads_gog_slots_as_g7_and_gog_7(tmp_path):
    ctx = SimpleNamespace(sources=[
        Source(id="s", platform="steam", account="", path=str(tmp_path / STEAM)),
        Source(id="g", platform="steam", account="", path=str(tmp_path / "DefaultUser"))])
    gog4 = LiveTarget("steam", "DefaultUser", 4)
    assert cli_live.parse_target(ctx, "G4") == gog4
    assert cli_live.parse_target(ctx, "gog:4") == gog4
    assert cli_live.parse_target(ctx, "S4") == LiveTarget("steam", STEAM, 4)
    assert cli_live.badge_ref("gog:7") == "G7"
    with pytest.raises(SystemExit):
        cli_live.parse_target(ctx, "X4")                     # no Xbox folder here


def test_the_cli_names_a_gog_slot_by_its_badge(tmp_path):
    second = "st_76561198000000002"
    ctx = SimpleNamespace(sources=[
        Source(id=n, platform="steam", account="", path=str(tmp_path / n))
        for n in (STEAM, "DefaultUser", second)])
    for target, text in ((LiveTarget("steam", "DefaultUser", 5), "G5"),
                         (LiveTarget("steam", second, 7), "S2.7"),
                         (LiveTarget("steam", STEAM, 7), "S1.7")):
        assert cli_live.target_text(ctx, target) == text
        assert cli_live.parse_target(ctx, text) == target


def test_emptying_a_gog_slot_keeps_that_slot_first_and_warns_of_its_cloud(tmp_path, monkeypatch,
                                                                         capsys):
    world = steam_world(tmp_path, cloud=False, other_name="DefaultUser")
    steam_save(world.other, 5, 0, "GogOnly", 900, 0x1111222233334444, T0 + 500, extra="gog5")
    steam_save(world.live, 5, 0, "SteamFive", 800, 0x5555666677778888, T0 + 400, extra="st5")
    ctx = world.refresh()
    monkeypatch.setattr(cli_live, "_context", lambda args: ctx)

    cli_live.cmd_clear(SimpleNamespace(target="5", live=str(world.other)))
    out = capsys.readouterr().out
    assert "To empty GOG slot 5" in out and "nmsvault-cli keep G5" in out
    assert cli_live.parse_target(ctx, "G5") == LiveTarget("steam", "DefaultUser", 5)
    assert "cloud sync may put a save back" in out and "GOG Galaxy" in out

    cli_live.cmd_clear(SimpleNamespace(target="5", live=str(world.live)))
    out = capsys.readouterr().out                    # the Steam account has no cloud here
    assert "nmsvault-cli keep S5" in out and "may put a save back" not in out


def test_a_slot_live_in_steam_and_gog_names_the_other_folder(tmp_path):
    w = World(tmp_path)
    gog = tmp_path / "NMS" / "DefaultUser"
    w.sources.append(Source(id="gog", platform="steam", account="", path=str(gog)))
    steam_save(w.steam, 9, 0, "Main Save", 5000, MAIN, T0, extra="a")
    steam_save(gog, 4, 0, "Main Save", 4000, MAIN, T0 - 900, extra="b")
    board = w.refresh().live_board()
    tip = slot_tip(board.sources[1].slots[3], board.sources[1], {})
    assert "Also live in Steam slot 9." in tip and "another slot of this folder" not in tip


def test_gog_against_xbox_is_named_gog_not_steam(tmp_path, make_wgs_account):
    w = World(tmp_path)
    gog = tmp_path / "NMS" / "DefaultUser"
    w.sources = [Source(id="gog", platform="steam", account="", path=str(gog), label="GOG")]
    steam_save(gog, 2, 0, "Main Save", 7000, MAIN, T0)
    w.add_xbox(make_wgs_account, [("Slot4Auto", '{"x":"x1"}', "Main Save", "", 8000, T0, MAIN)])
    line = w.refresh().line(PID)
    assert line.cross_platform == "steam-behind"
    assert "GOG behind" in state_words(line) and "Steam" not in state_words(line)
    assert _platform_sentence(line).startswith("the GOG copy is behind")
