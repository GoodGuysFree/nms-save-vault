"""The xgs mirror (SystemAppData\\xgs\\<account>\\<identifier>\\{data,meta}) that the game keeps
beside wgs: every vault write to wgs must land in xgs too, and an xgs-only account must
still be readable. All in a sandbox built from ``make_wgs_account``."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nms_save_vault.core import msstore, savedir
from nms_save_vault.core import operations as ops
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault


def _vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    vstore.create_store(v.root)          # live operations need the version store (D3)
    return v


@pytest.fixture(autouse=True)
def _xbox_app_is_quiet(monkeypatch):
    from nms_save_vault.core.liveops import planner

    monkeypatch.setattr(planner, "XBOX_SYNC_SECONDS", 0)


def _add_xgs_twin(wgs_account: Path) -> Path:
    """Build the xgs mirror the game keeps: same bytes, plain <identifier>/{data,meta}."""
    twin = wgs_account.parent.parent / "xgs" / wgs_account.name
    _, containers = msstore.parse_containers_index(wgs_account)
    for c in containers:
        (twin / c.identifier).mkdir(parents=True)
        (twin / c.identifier / "data").write_bytes(c.data_file.read_bytes())
        (twin / c.identifier / "meta").write_bytes(c.meta_file.read_bytes())
    return twin


def _xgs_state(twin: Path) -> dict[str, tuple[bytes, bytes]]:
    return {
        d.name: ((d / "data").read_bytes(), (d / "meta").read_bytes())
        for d in twin.iterdir()
        if d.is_dir()
    }


def _wgs_state(wgs_account: Path) -> dict[str, tuple[bytes, bytes]]:
    _, containers = msstore.parse_containers_index(wgs_account)
    return {
        c.identifier: (c.data_file.read_bytes(), c.meta_file.read_bytes())
        for c in containers
        if c.sync_state != msstore.SYNC_STATE_DELETED and c.data_file and c.data_file.is_file()
    }


SAVES = [
    ("AccountData", '{"acct":1}', "", "", 0, 50),
    ("Slot1Auto", '{"a":1}', "One A", "", 100, 1000),
    ("Slot1Manual", '{"b":1}', "One B", "", 90, 2000),
    ("Slot2Auto", '{"a":2}', "Two A", "", 80, 800),
]


def test_promote_mirrors_into_xgs_and_leaves_others(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, SAVES)
    twin = _add_xgs_twin(live)
    before = _xgs_state(twin)

    ops.promote_member(_vault(tmp_path), live, 1, 0, allow_game_running=True)

    after = _xgs_state(twin)
    assert after == _wgs_state(live)
    assert after["Slot1Auto"] != before["Slot1Auto"]          # the promoted meta was re-stamped
    assert after["Slot1Auto"][0] == before["Slot1Auto"][0]    # data verbatim
    assert after["Slot1Manual"][0] == before["Slot1Manual"][0]   # the pair is written whole
    for ident in ("AccountData", "Slot2Auto"):
        assert after[ident] == before[ident]




def test_repopulate_creates_the_slot_in_xgs(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, SAVES)
    twin = _add_xgs_twin(live)

    ops.repopulate_slot(_vault(tmp_path), live, 1, live, 5, allow_game_running=True)

    state = _xgs_state(twin)
    assert state["Slot5Auto"][0] == state["Slot1Auto"][0]        # data verbatim
    assert state == _wgs_state(live)


def test_restore_and_undo_resync_xgs(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path / "live", SAVES)
    twin = _add_xgs_twin(live)
    original = _xgs_state(twin)
    backup = make_wgs_account(tmp_path / "bk", [
        ("AccountData", '{"acct":2}', "", "", 0, 60),
        ("Slot3Manual", '{"z":1}', "Backup", "", 60, 600),
    ])
    vault = _vault(tmp_path)
    entry = ops.import_backup(vault, backup, label="bk")

    ops.restore_full(vault, entry, live, allow_game_running=True)
    assert _xgs_state(twin) == _wgs_state(live)
    # the slots the backup lacks are left alone (F8); the account data stays the live one
    assert sorted(_xgs_state(twin)) == ["AccountData", "Slot1Auto", "Slot1Manual", "Slot2Auto",
                                        "Slot3Auto", "Slot3Manual"]
    assert _xgs_state(twin)["AccountData"] == original["AccountData"]

    ops.undo_last(vault, live, allow_game_running=True)
    assert _xgs_state(twin) == _wgs_state(live)
    for ident in ("Slot1Auto", "Slot1Manual", "Slot2Auto"):
        assert _xgs_state(twin)[ident] == original[ident]


def test_no_xgs_folder_is_not_created(make_wgs_account, tmp_path):
    live = make_wgs_account(tmp_path, SAVES)
    ops.promote_member(_vault(tmp_path), live, 1, 0, allow_game_running=True)
    assert not (tmp_path / "xgs").exists()


def test_xgs_only_account_is_readable_but_not_writable(make_wgs_account, tmp_path):
    wgs = make_wgs_account(tmp_path, SAVES)
    twin = _add_xgs_twin(wgs)
    shutil.rmtree(wgs.parent)                                  # the game dropped wgs

    assert savedir.platform_of(twin) == "xbox"
    view = savedir.scan_any(twin)
    assert view.account_present
    assert view.slots[1].a.save_name == "One A" and view.slots[1].a.info is not None
    assert view.slots[2].a.save_name == "Two A"
    assert not view.slots[3].occupied

    with pytest.raises(ops.OperationError):
        ops.promote_member(_vault(tmp_path), twin, 1, 0, allow_game_running=True)
    with pytest.raises(ops.OperationError):
        ops.repopulate_slot(_vault(tmp_path), twin, 1, twin, 5, allow_game_running=True)


def test_discovery_adds_xgs_only_accounts(make_wgs_account, tmp_path, monkeypatch):
    paired = make_wgs_account(tmp_path, SAVES, name="0000000000000001_A")
    _add_xgs_twin(paired)
    lone = make_wgs_account(tmp_path, SAVES, name="0000000000000002_B")
    lone_twin = _add_xgs_twin(lone)
    shutil.rmtree(lone)
    monkeypatch.setattr(msstore, "microsoft_root", lambda: tmp_path / "wgs")

    assert msstore.find_microsoft_save_dirs() == [paired, lone_twin]


def test_identifier_is_never_a_path(tmp_path):
    with pytest.raises(ValueError):
        msstore._xgs_write(tmp_path, "..\\Slot1Auto", b"d", b"m")
