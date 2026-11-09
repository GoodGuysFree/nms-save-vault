"""core.api CL9: an app started without NMS Save Vault (Savecraft) starts the vault where NMS
Save Vault looks for it, laid out as NMS Save Vault lays out a vault it creates, so that the
first edit goes through it and a later NMS Save Vault takes it as its own."""
from __future__ import annotations

from pathlib import Path

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


def test_an_unreachable_chosen_vault_is_waited_for_never_replaced(machine):
    """NMS Save Vault's settings choose a vault on a drive that is not connected: no write goes
    to another vault, and none is started in its place."""
    import json
    tmp_path, live = machine
    gone = Path(f"{_free_drive()}:/unplugged/vault")       # a drive that is not there
    (tmp_path / "own").mkdir(exist_ok=True)
    (tmp_path / "own" / "state.json").write_text(json.dumps({"vault": str(gone)}), encoding="utf-8")
    assert api.vault_dir() == gone
    with pytest.raises(api.StoreUnavailable, match="cannot be found"):
        api.create_vault()
    with pytest.raises(api.StoreUnavailable):
        api.open_live(live_dirs=[live])
    assert not gone.exists() and not (tmp_path / "default_vault").exists()


def test_a_vault_that_fails_half_way_is_cleared_and_can_be_started_again(machine, monkeypatch):
    tmp_path, _live = machine
    root = tmp_path / "v"
    real = versionstore.create_store

    def full_disk(vault_root, **kw):
        for name in ("objects", "entries"):
            (versionstore.store_root(vault_root) / name).mkdir(parents=True, exist_ok=True)
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(versionstore, "create_store", full_disk)
    with pytest.raises(api.StoreUnavailable, match="No space left"):
        api.create_vault(root)
    assert not root.exists()                                     # nothing half-made is left
    monkeypatch.setattr(versionstore, "create_store", real)
    assert versionstore.vault_mode(api.create_vault(root)) == "store"


def test_a_folder_that_is_not_empty_is_never_made_a_vault(machine):
    tmp_path, _live = machine
    odd = tmp_path / "odd"
    (odd / "organizer").mkdir(parents=True)
    before = fingerprint(odd)
    with pytest.raises(api.StoreNotUpgraded, match="not empty"):
        api.create_vault(odd)
    assert fingerprint(odd) == before and versionstore.vault_mode(odd) == "legacy"



def _free_drive() -> str:
    import os
    import string
    free = [d for d in reversed(string.ascii_uppercase) if not os.path.exists(f"{d}:\\")]
    if not free:
        pytest.skip("no free drive letter to stand for an unplugged drive")
    return free[0]


def _settings(tmp_path, **files):
    """NMS Save Vault's files that name its vault: {name: (path, content)}."""
    import json
    for path, content in files.values():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(content), encoding="utf-8")


def test_a_kit_on_an_unplugged_drive_keeps_its_vault_chosen(machine):
    """The settings and the vault are both on the drive that is gone: the pointer still says
    which vault it is, and no vault is started anywhere while it is gone."""
    from nms_save_vault.core import state
    tmp_path, _live = machine
    drive = _free_drive()
    vault = Path(f"{drive}:\\Kit\\vault")
    settings = Path(f"{drive}:\\Kit\\state.json")
    _settings(tmp_path, pointer=(state.pointer_path(), {"vault": str(vault), "state": str(settings)}))
    assert api.vault_dir() == vault
    for where in (None, api.default_vault_dir(), tmp_path / "elsewhere"):
        with pytest.raises(api.StoreUnavailable, match="cannot be found"):
            api.create_vault(where)
    assert not api.default_vault_dir().exists() and not (tmp_path / "elsewhere").exists()


def test_a_settings_file_naming_a_deleted_folder_on_a_present_drive_is_skipped(machine):
    """A deleted folder is not an unplugged drive: the next settings file decides, as before."""
    tmp_path, _live = machine
    real = tmp_path / "RealVault"
    real.mkdir()
    _settings(tmp_path, own=(tmp_path / "own" / "state.json", {"vault": str(tmp_path / "deleted")}))
    from nms_save_vault.core import state, platform as hostmod
    later = [p for p in state.state_candidates() if p != tmp_path / "own" / "state.json"]
    _settings(tmp_path, other=(later[-1], {"vault": str(real)}))
    assert api.vault_dir() == real


def test_a_vault_another_app_is_starting_is_never_cleared_away(machine, monkeypatch):
    """start_new_vault declines (another app holds the lock, or got there first): nothing that
    app made is removed."""
    tmp_path, _live = machine
    root = tmp_path / "v"

    def other_app_first(vault_root, **kw):
        (Path(vault_root) / "backups").mkdir(parents=True, exist_ok=True)
        (Path(vault_root) / "backups" / "theirs.txt").write_text("x", encoding="utf-8")
        return False
    monkeypatch.setattr(versionstore, "start_new_vault", other_app_first)
    with pytest.raises(api.StoreUnavailable, match="another app"):
        api.create_vault(root)
    assert (root / "backups" / "theirs.txt").is_file()


def test_dumps_named_refuses_what_cannot_be_a_field_name():
    for bad in ("", " ", "Amount\x00", "two words", "x" * 300):
        with pytest.raises(api.PatchRefused):
            api.dumps_named({bad: 1})
    assert api.dumps_named({"DiscoveryData-v1": 1})            # a real name with a hyphen
