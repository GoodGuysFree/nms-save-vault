"""Tools > Free old backup folders...: the dry run is shown and a yes is asked before anything
is deleted; importing a vault that has a store goes through the merge (D3 4.5, 4.8)."""
from __future__ import annotations

import tkinter as tk

import pytest

from nms_save_vault import gui
from nms_save_vault.core import operations
from nms_save_vault.core.catalog import Vault

from .test_store_free import MANAGED, _folder, folded, isolated  # noqa: F401 - fixtures

pytest.importorskip("tkinter")


def _invoke_tools(app, label: str) -> None:
    bar = app.menubar
    for i in range(bar.index("end") + 1):
        if bar.type(i) == "cascade" and bar.entrycget(i, "label") == "Tools":
            menu = app.nametowidget(bar.entrycget(i, "menu"))
            for j in range(menu.index("end") + 1):
                if menu.type(j) != "separator" and menu.entrycget(j, "label") == label:
                    menu.invoke(j)
                    return
    raise AssertionError(label)


@pytest.fixture
def app(gui_app, folded, monkeypatch):
    monkeypatch.setattr(gui_app, "vault", Vault(folded.vault.root))
    monkeypatch.setattr(gui_app, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(gui_app.organizer_view, "vault_changed", lambda *a, **k: None)
    return gui_app


def test_the_dry_run_is_shown_and_declining_deletes_nothing(app, folded, dialogs, monkeypatch):
    dialogs.expect()
    asked = []
    monkeypatch.setattr(gui, "_askyesno", lambda title, message, **k: asked.append(message) or False)
    _invoke_tools(app, "Free old backup folders...")
    assert len(asked) == 1 and "4 old backup folders" in asked[0]
    assert "full check of the store" in asked[0] and "pre-upgrade copy" in asked[0]
    assert all(_folder(folded, k).is_dir() for k in MANAGED)
    assert dialogs.take() == []


def test_confirming_deletes_and_reports(app, folded, dialogs, monkeypatch):
    dialogs.expect()
    monkeypatch.setattr(gui, "_askyesno", lambda title, message, **k: True)
    _invoke_tools(app, "Free old backup folders...")
    assert not any(_folder(folded, k).exists() for k in MANAGED)
    shown = dialogs.take()
    assert shown[-1][0] == "showinfo" and "every backup is still listed" in shown[-1][2]


def test_nothing_to_free_just_says_so(app, folded, dialogs, monkeypatch):
    dialogs.expect()
    monkeypatch.setattr(gui, "_askyesno", lambda title, message, **k: True)
    _invoke_tools(app, "Free old backup folders...")
    dialogs.take()
    _invoke_tools(app, "Free old backup folders...")
    last = dialogs.take()[-1]
    assert last[0] == "showinfo" and "No old backup folder to free" in last[2]


def test_a_damaged_store_is_reported_and_nothing_is_deleted(app, folded, dialogs, monkeypatch):
    dialogs.expect()
    digest = next(f.sha256 for e in folded.store.entries() for f in e.files if f.inline is None)
    path = folded.store.objects.path_for(digest)
    path.write_bytes(path.read_bytes()[:-1])
    monkeypatch.setattr(gui, "_askyesno", lambda title, message, **k: True)
    _invoke_tools(app, "Free old backup folders...")
    last = dialogs.take()[-1]
    assert last[0] == "showerror" and "nothing was deleted" in last[2]
    assert all(_folder(folded, k).is_dir() for k in MANAGED)


def test_the_gui_import_of_a_store_vault_merges(app, tmp_path, dialogs, monkeypatch):
    from nms_save_vault.core import store as vstore

    from .store_helpers import steam_live

    dialogs.expect()
    source = Vault(tmp_path / "other")
    source.ensure()
    vstore.create_store(source.root)
    source.save()
    live = steam_live(tmp_path / "NMS-other")
    made = operations.create_full_backup(source, live, warnings=[])
    asked = []
    monkeypatch.setattr(gui, "_askyesno", lambda title, message, **k: asked.append(message) or True)
    app._import_vault_dir(source.root)
    assert "Merge the new ones" in asked[0] and "1 new" in asked[0]
    assert made.id in {e.id for e in vstore.open_store(app.vault.root).entries()}
    assert dialogs.take()[-1][0] == "showinfo"
