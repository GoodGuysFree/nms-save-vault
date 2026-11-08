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


def test_the_version_store_is_opened_with_the_current_app(tmp_path, monkeypatch, savecraft):
    seen = {}

    def fake_open(root, **kw):
        seen.update(kw)
        return "store"

    monkeypatch.setattr(versionstore.VersionStore, "open", staticmethod(fake_open))
    assert versionstore.open_store(tmp_path) == "store"
    assert seen["app_version"] == "nms-savecraft 0.1.0"
