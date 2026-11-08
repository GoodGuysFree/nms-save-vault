"""core.appinfo: every vault record names the app that caused it (NMS Save Vault or NMS
Savecraft), from one source, so the last version before an editor change can be found."""
from __future__ import annotations

import dataclasses
import json

import pytest

from nms_save_vault import __version__
from nms_save_vault.core import appinfo
from nms_save_vault.core.organizer.recorder import OpLineage
from nms_save_vault.core.organizer.store import HEADER, OrganizerStore
from nms_save_vault.core.store import versionstore

from .store_helpers import new_vault, steam_live


@pytest.fixture
def savecraft():
    """Run the test as NMS Savecraft would, and give the core back to NMS Save Vault after."""
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        yield
    finally:
        appinfo.set_app(previous.name, previous.version)


def test_without_set_app_the_core_is_nms_save_vault():
    assert appinfo.current() == appinfo.AppIdentity("nms-save-vault", __version__)
    assert appinfo.stamp() == f"nms-save-vault {__version__}"


def test_set_app_names_the_app_and_returns_the_previous_one():
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        assert previous.name == "nms-save-vault"
        assert appinfo.stamp() == "nms-savecraft 0.1.0"
    finally:
        appinfo.set_app(previous.name, previous.version)
    assert appinfo.current() == previous


@pytest.mark.parametrize("name, version", [
    ("NMS Savecraft", "0.1.0"), ("nms savecraft", "0.1.0"), ("", "0.1.0"), ("x", "0.1.0"),
    ("nms-savecraft", ""), ("nms-savecraft", "0.1 beta"), ("nms-savecraft", "-1"),
])
def test_an_ambiguous_stamp_is_refused(name, version):
    before = appinfo.current()
    with pytest.raises(ValueError):
        appinfo.set_app(name, version)
    assert appinfo.current() == before


@pytest.mark.parametrize("recorded, app", [
    ("0.3.6", "nms-save-vault"),                    # the store's records before the stamp named its app
    ("nms-save-vault 0.3.6", "nms-save-vault"),
    ("nms-save-vault test", "nms-save-vault"),
    ("nms-savecraft 0.1.0", "nms-savecraft"),
    ("", "unknown"), ("   ", "unknown"), (None, "unknown"), (36, "unknown"),
    ("test", "unknown"), ("Weird Thing 1", "unknown"),
])
def test_a_recorded_stamp_reads_back_as_its_app_and_never_guesses_another(recorded, app):
    assert appinfo.app_of(recorded) == app


def test_a_new_organizer_header_names_the_app(tmp_path, savecraft):
    OrganizerStore(tmp_path, create=True, clock=lambda: 1_792_137_600)
    header = json.loads((tmp_path / "organizer" / HEADER).read_text(encoding="ascii"))
    assert header["app"] == "nms-savecraft 0.1.0"


def test_an_op_record_takes_the_app_when_it_is_made_not_when_the_module_loaded(savecraft):
    default = next(f for f in dataclasses.fields(OpLineage) if f.name == "app")
    assert default.default_factory() == "nms-savecraft 0.1.0"


def test_an_app_named_after_the_store_opened_is_the_one_its_entries_name(tmp_path):
    vault = new_vault(tmp_path)
    store = versionstore.open_store(vault, live_roots=())
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        entry = store.add_from_folder(steam_live(tmp_path / "in"), kind="imported", label="x",
                                      source={"type": "folder", "name": "x"})
    finally:
        appinfo.set_app(previous.name, previous.version)
    assert entry.app_version == "nms-savecraft 0.1.0"


def test_a_live_operation_context_names_the_app_current_when_it_writes():
    from nms_save_vault.core.liveops.context import OpContext

    ctx = OpContext(vault=None, versions=None, store=None, locator=None, lineage=None,
                    readmodel=None, sources=[], host=None, machine_id="m")
    previous = appinfo.set_app("nms-savecraft", "0.1.0")
    try:
        assert ctx.app() == "nms-savecraft 0.1.0"
    finally:
        appinfo.set_app(previous.name, previous.version)
    assert ctx.app() == appinfo.stamp()


def test_an_old_backup_folded_into_the_store_reads_as_nms_save_vault_legacy(tmp_path, savecraft):
    vault = new_vault(tmp_path)
    entry = versionstore.open_store(vault, live_roots=()).add_from_folder(
        steam_live(tmp_path / "in"), kind="full", label="old", source={"type": "folder", "name": "x"},
        app_version=appinfo.LEGACY)
    assert entry.app_version == "nms-save-vault legacy"
    assert appinfo.app_of(entry.app_version) == appinfo.VAULT


def test_an_entry_added_after_set_app_names_that_app_in_its_manifest(tmp_path, savecraft):
    vault = new_vault(tmp_path)
    store = versionstore.open_store(vault, live_roots=())
    entry = store.add_from_folder(steam_live(tmp_path / "in"), kind="imported", label="x",
                                  source={"type": "folder", "name": "x"})
    assert entry.app_version == "nms-savecraft 0.1.0"
    assert versionstore.open_store(vault, live_roots=()).entry(entry.id).app_version == (
        "nms-savecraft 0.1.0")


@pytest.mark.parametrize("bare", ["0.1.0", "test", "", "nms-savecraft", "NMS Savecraft 0.1.0"])
def test_a_store_refuses_a_stamp_that_would_read_back_as_another_app(tmp_path, bare):
    vault = new_vault(tmp_path)
    with pytest.raises(ValueError, match="<name> <version>"):
        versionstore.VersionStore.open(vault, lock=versionstore.vault_lock(vault),
                                       app_version=bare, live_roots=())


def test_the_index_cache_records_the_current_app_by_default(savecraft):
    from nms_save_vault.core.index.cache import IndexCache

    cache = IndexCache(None, vault_id="v")
    try:
        row = cache._write_con().execute("SELECT v FROM meta WHERE k = 'app_version'").fetchone()
    finally:
        cache.close()
    assert row[0] == "nms-savecraft 0.1.0"


def test_a_classic_op_log_line_names_the_app(savecraft):
    from nms_save_vault.core import operations

    assert operations._oplog("clear_slot")["app"] == "nms-savecraft 0.1.0"


def test_the_vault_window_freezes_no_stamp(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nms_save_vault import gui

    seen = {}
    monkeypatch.setattr(gui, "VaultWindow", lambda *a, env, **kw: seen.setdefault("env", env))
    app = SimpleNamespace(vault=SimpleNamespace(root=tmp_path),
                          state=SimpleNamespace(live_sources=[]), worker=None, themed=None,
                          vault_changed=lambda **kw: None)
    gui.App.open_vault_window(app)
    assert seen["env"].app_version is None          # each write names the app current then
