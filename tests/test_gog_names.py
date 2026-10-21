"""GOG's DefaultUser folder (Steam's format) is named GOG wherever a live account is named."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nms_save_vault import cli_live
from nms_save_vault.core.liveops.targets import slot_label
from nms_save_vault.core.organizer.model import LiveTarget, platform_letter, platform_name, sync_name
from nms_save_vault.core.state import Source
from nms_save_vault.ui.organizer.words import slot_words

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
