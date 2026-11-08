"""core.api: the public read API another app bundles (NMS Savecraft). Its names are pinned:
removing one or changing what it means is a new API_VERSION, never an accident."""
from __future__ import annotations

import json

import pytest

from nms_save_vault import __version__
from nms_save_vault.core import api, locations
from nms_save_vault.core.hashname import key

from .conftest import steam_save
from .store_helpers import fingerprint, new_vault, open_vault_store, steam_live

PINNED = {
    "API_VERSION", "CORE_VERSION",
    "VAULT", "AppIdentity", "app_of", "current", "is_stamp", "set_app",
    "default_vault_dir", "live_save_dirs",
    "MemberView", "SaveDirView", "SlotView", "platform_of", "scan_save_dir",
    "CtxInput", "DecodedSave", "SaveDecodeError", "SaveInput", "contexts_of", "decode_save",
    "PrivacyError", "get", "items", "newer_than_known", "read_member",
    "FileEntry", "StoreEntry", "StoreError", "StoreNotUpgraded", "StoreTooNew",
    "StoreUnavailable", "VersionStore", "open_vault",
}


def test_the_api_is_exactly_the_pinned_names_at_version_1():
    assert set(api.__all__) == PINNED and len(api.__all__) == len(PINNED)
    assert api.API_VERSION == 1
    assert api.CORE_VERSION == __version__
    assert all(hasattr(api, name) for name in api.__all__)


def _save(folder, *, units: int, base_version: int = 4233):
    payload = json.dumps({key("BaseVersion"): base_version,
                          key("PlayerStateData"): {key("Units"): units}}).encode()
    steam_save(folder, 1, 0, "Main", 60, 1, 1_790_000_000, payload=payload,
               base_version=base_version)


def test_a_save_is_found_decoded_and_read_by_readable_names(tmp_path):
    folder = tmp_path / "st_1"
    _save(folder, units=1234)
    before = fingerprint(folder)
    assert api.platform_of(folder) == "steam"
    member = api.scan_save_dir(folder).slots[1].a
    decoded = api.read_member(member)
    _save_input, contexts = api.contexts_of(decoded.root)
    assert api.get(contexts["base"].psd, "Units") == 1234
    assert api.items(contexts["base"].psd, "Units") == []
    assert not api.newer_than_known(member, platform="steam")
    assert fingerprint(folder) == before                        # reading wrote nothing


def test_a_save_from_a_newer_game_is_flagged(tmp_path):
    folder = tmp_path / "st_1"
    _save(folder, units=1, base_version=99_999)
    assert api.newer_than_known(api.scan_save_dir(folder).slots[1].a, platform="steam")


def test_ownership_fields_cannot_be_read_through_the_api():
    with pytest.raises(api.PrivacyError):
        api.get({}, "PlayerStateData", "Owner")


def test_live_save_dirs_are_steam_and_gog_then_microsoft(monkeypatch, tmp_path):
    monkeypatch.setattr(locations, "find_live_save_dirs", lambda: [tmp_path / "st_1"])
    monkeypatch.setattr(locations, "find_microsoft_save_dirs", lambda: [tmp_path / "wgs"])
    assert api.live_save_dirs() == [tmp_path / "st_1", tmp_path / "wgs"]


def test_the_vault_opens_to_read_and_nothing_in_it_is_written(tmp_path):
    vault = new_vault(tmp_path)
    added = open_vault_store(vault).add_from_folder(
        steam_live(tmp_path / "in"), kind="imported", label="x",
        source={"type": "folder", "name": "x"})
    before = fingerprint(vault)
    store = api.open_vault(vault, live_roots=())
    assert store.read_only
    assert [e.id for e in store.entries()] == [added.id]
    path = added.files[0].path
    assert store.read_file(added.id, path) == (tmp_path / "in" / "st_76561198000000001" / path).read_bytes()
    assert fingerprint(vault) == before


def test_no_vault_is_a_store_error(tmp_path):
    with pytest.raises(api.StoreError):
        api.open_vault(tmp_path / "none", live_roots=())
