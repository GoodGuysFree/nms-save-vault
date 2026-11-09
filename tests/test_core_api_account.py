"""api.read_account: a Steam account's account data (rewards unlocked for the account), read
only, as the game writes it: plain JSON text ending in one NUL."""
from __future__ import annotations

from nms_save_vault.core import api
from nms_save_vault.core.testing import obfuscated

import json


def test_a_steam_account_s_data_is_read_by_readable_names(tmp_path):
    folder = tmp_path / "st_76561198000000001"
    folder.mkdir()
    data = obfuscated({"UserSettingsData": {"UnlockedSpecials": ["^BANNER_AF"]}})
    (folder / "accountdata.hg").write_bytes(json.dumps(data, separators=(",", ":")).encode() + b"\x00")
    root = api.read_account(folder)
    assert api.items(root, "UserSettingsData", "UnlockedSpecials") == ["^BANNER_AF"]


def test_no_account_data_reads_as_none(tmp_path):
    folder = tmp_path / "st_76561198000000001"
    folder.mkdir()
    assert api.read_account(folder) is None
    (folder / "accountdata.hg").write_bytes(b"not json")
    assert api.read_account(folder) is None
