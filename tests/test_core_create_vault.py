"""core.api CL9: an app started without NMS Save Vault (Savecraft) starts the vault where NMS
Save Vault looks for it, laid out as NMS Save Vault lays out a vault it creates, so that the
first edit goes through it and a later NMS Save Vault takes it as its own."""
from __future__ import annotations

import pytest

from nms_save_vault.core import api, appinfo, locations, platform, safety, state, testing
from nms_save_vault.core.store import versionstore

from .liveops_support import host_in
from .store_helpers import fingerprint


@pytest.fixture
def machine(tmp_path, monkeypatch, new_vault_store):
    """A machine with one live Steam save, no vault, no NMS Save Vault settings, the game shut."""
    host = host_in(tmp_path)
    monkeypatch.setattr(platform.Host, "current", classmethod(lambda cls: host))
    monkeypatch.setattr(state, "default_state_path", lambda: tmp_path / "own" / "state.json")
    monkeypatch.setattr(safety, "is_game_running", lambda *a, **k: False)
    monkeypatch.setattr(locations, "default_vault_dir", lambda: tmp_path / "default_vault")
    live = tmp_path / "NMS" / "st_76561198000000001"
    testing.write_steam_save(live, 1, {"PlayerStateData": {"Units": 5}}, name="Main")
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    yield tmp_path, live
    appinfo.set_app(previous.name, previous.version)


def test_a_new_vault_goes_where_nms_save_vault_looks_and_takes_an_edit(machine):
    tmp_path, live = machine
    with pytest.raises(api.StoreUnavailable):
        api.open_live(live_dirs=[live])                          # no vault yet
    root = api.create_vault()
    assert root == api.vault_dir() == tmp_path / "default_vault"
    assert versionstore.vault_mode(root) == "store"              # as NMS Save Vault creates one
    session = api.open_live(live_dirs=[live])
    plan = session.plan_edit(api.slot_of(live, 1), [api.Replace(("PlayerStateData", "Units"),
                                                                api.dumps(9))], what="units")
    assert not plan.blocked, plan.blocked
    assert session.write(plan, plan.confirmations).status == "done"
    assert api.read_member(api.scan_save_dir(live).slots[1].newest).root  # still a good save
    assert api.open_vault(root).entries()                         # the before-copy is kept
    from nms_save_vault.core.catalog import Vault
    Vault(root).ensure()                                          # NMS Save Vault opens it as is
    assert versionstore.vault_mode(root) == "store"


def test_a_vault_already_there_is_left_as_it_is(machine):
    tmp_path, _live = machine
    root = api.create_vault(tmp_path / "chosen")
    before = fingerprint(root)
    assert api.create_vault(tmp_path / "chosen") == root and fingerprint(root) == before


def test_an_empty_folder_becomes_the_vault_but_an_old_vault_is_never_converted(machine):
    tmp_path, _live = machine
    empty = tmp_path / "empty"
    empty.mkdir()
    assert versionstore.vault_mode(api.create_vault(empty)) == "store"
    old = tmp_path / "old"
    old.mkdir()
    (old / "catalog.json").write_text('{"entries": []}', encoding="utf-8")
    before = fingerprint(old)
    with pytest.raises(api.StoreNotUpgraded, match="NMS Save Vault"):
        api.create_vault(old)
    assert fingerprint(old) == before


def test_a_vault_that_cannot_be_made_says_so(machine):
    tmp_path, _live = machine
    blocker = tmp_path / "a file"
    blocker.write_text("not a folder", encoding="utf-8")
    with pytest.raises(api.StoreUnavailable):
        api.create_vault(blocker)
