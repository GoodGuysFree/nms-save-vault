"""What each kind of row offers, and what it resolves to.

Two things are covered here:

1. Row metadata must be keyed per tree. Tk numbers items per widget, so both panes emit
   "I001", "I002", ... A single row-keyed dict let the backups pane overwrite the live
   pane's metadata, and a right-click on a live folder then acted on a backup entry.

2. Copying is reachable from either end. The user can start from "put this backup
   somewhere" or from "fill this live slot", and both must work.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest

pytest.importorskip("tkinter")


@pytest.fixture
def app(gui_app):
    return gui_app


def _menu_labels(app, builder, meta):
    menu = tk.Menu(app, tearoff=0)
    builder(menu, meta)
    last = menu.index("end")
    out = []
    for i in range(0 if last is None else last + 1):
        try:
            out.append(menu.entrycget(i, "label"))
        except tk.TclError:
            out.append("---")
    return out


def _invoke_menu_entry(app, builder, meta, label_starts_with):
    """Build the context menu for a row and INVOKE the matching entry, the way a click on
    it does. Reading the labels only proves the entry is offered -- it is what the entry
    passes to the handler that decides whether clicking it does anything."""
    menu = tk.Menu(app, tearoff=0)
    builder(menu, meta)
    last = menu.index("end")
    for i in range(0 if last is None else last + 1):
        try:
            if menu.entrycget(i, "label").startswith(label_starts_with):
                menu.invoke(i)
                return True
        except tk.TclError:
            continue
    return False


def _find(app, tree, kind, predicate=None):
    def walk(node=""):
        for child in tree.get_children(node):
            meta = app._meta_for(tree, child)
            if meta and meta.get("type") == kind and (predicate is None or predicate(meta)):
                return meta
            found = walk(child)
            if found:
                return found
        return None

    return walk()


# --- the metadata collision --------------------------------------------------


def test_the_two_panes_do_not_share_row_metadata(app):
    """Regression: both trees number items per widget, so both emit "I001", "I002", ...
    With a row-keyed dict the backups pane clobbered the live pane, and right-clicking a
    live folder acted on a backup entry instead."""
    backup_row = app.backup_tree.get_children("")[0]
    # Tk lets each widget reuse the id, which is exactly how the collision arose.
    live_row = app.live_tree.insert("", "end", iid=backup_row, text="a live folder")
    try:
        app._remember(app.live_tree, live_row, {"type": "live", "dir": "L", "writable": True})
        assert app._meta_for(app.live_tree, live_row)["type"] == "live"
        assert app._meta_for(app.backup_tree, backup_row)["type"] == "entry"
    finally:
        app.live_tree.delete(live_row)
        app._meta.pop((str(app.live_tree), live_row), None)


def test_a_row_id_resolves_differently_in_each_tree(app):
    """The same id in the other tree must not return this tree's meta."""
    row = app.backup_tree.get_children("")[0]
    assert app._meta_for(app.backup_tree, row) is not None
    assert app._meta_for(app.live_tree, row) is None


def test_tooltips_are_looked_up_per_tree(app):
    row = app.backup_tree.get_children("")[0]
    assert app._tip_for_row(app.backup_tree, row)
    assert app._tip_for_row(app.live_tree, row) == ""


# --- what a row can be copied from -------------------------------------------


def test_a_backup_slot_is_a_copy_source(app):
    meta = _find(app, app.backup_tree, "slot")
    source = app._source_from(meta)
    assert source is not None and source.slot == meta["slot"]


def test_a_single_save_resolves_to_its_slot(app):
    """Copying is slot-granular, so selecting either save must still work -- pressing the
    toolbar button on one used to answer "Select a source slot"."""
    meta = _find(app, app.backup_tree, "member")
    source = app._source_from(meta)
    assert source is not None
    assert source.slot == meta["slot"]


def test_a_single_slot_extract_is_a_copy_source(app):
    meta = _find(app, app.backup_tree, "entry", lambda m: m["entry"].kind == "extract")
    source = app._source_from(meta)
    assert source is not None and source.slot == 1


def test_a_full_backup_is_not_a_single_slot_source(app):
    meta = _find(app, app.backup_tree, "entry", lambda m: m["entry"].kind == "full")
    assert app._source_from(meta) is None


# --- the menus ---------------------------------------------------------------


def test_an_extract_offers_both_its_own_slot_and_a_choice(app):
    """The bug: after the restore fix, an extract could ONLY go back to its own slot."""
    meta = _find(app, app.backup_tree, "entry", lambda m: m["entry"].kind == "extract")
    labels = _menu_labels(app, app._menu_for_backup, meta)
    assert any("Put slot 1 back into live slot 1" == x for x in labels)
    assert any("different live slot" in x for x in labels)


def test_a_full_backup_offers_only_a_whole_restore(app):
    meta = _find(app, app.backup_tree, "entry", lambda m: m["entry"].kind == "full")
    labels = _menu_labels(app, app._menu_for_backup, meta)
    assert any("Restore all" in x for x in labels)
    assert not any("Put slot" in x for x in labels)


def test_a_backup_slot_offers_a_direct_copy_and_a_chosen_one(app):
    meta = _find(app, app.backup_tree, "slot")
    labels = _menu_labels(app, app._menu_for_slot, meta)
    slot = meta["slot"]
    assert f"Copy slot {slot} into live slot {slot}" in labels
    assert f"Copy slot {slot} into a live slot..." in labels
    assert not any("promote" in x.lower() for x in labels), "a backup save is not live"


def test_a_save_inside_a_backup_offers_the_same_as_its_slot(app):
    slot_labels = _menu_labels(app, app._menu_for_slot, _find(app, app.backup_tree, "slot"))
    member_labels = _menu_labels(app, app._menu_for_slot, _find(app, app.backup_tree, "member"))
    assert slot_labels == member_labels


def test_a_live_slot_can_be_a_destination_as_well_as_a_source(app):
    """The complaint: selecting a live slot only let you copy it somewhere else, with no
    way to say "fill THIS slot"."""
    meta = {"type": "slot", "dir": "X", "slot": 4, "live": True}
    labels = _menu_labels(app, app._menu_for_slot, meta)
    assert "Replace live slot 4 with a save from anywhere..." in labels
    assert "Copy live slot 4 into another live slot..." in labels
    assert "Extract live slot 4 to the vault" in labels


def test_a_live_slot_can_be_cleared_but_a_backup_slot_cannot(app):
    """Clear deletes live saves, so it belongs only on a live row -- a slot inside a
    backup has nothing live to empty."""
    live = _menu_labels(app, app._menu_for_slot, {"type": "slot", "dir": "X", "slot": 4, "live": True})
    backup = _menu_labels(app, app._menu_for_slot, {"type": "slot", "dir": "X", "slot": 4, "live": False})
    assert "Clear live slot 4 (delete its saves)..." in live
    assert not [x for x in backup if x.startswith("Clear")]


def test_a_live_save_adds_promote_on_top_of_its_slot_actions(app):
    slot_labels = _menu_labels(
        app, app._menu_for_slot, {"type": "slot", "dir": "X", "slot": 4, "live": True}
    )
    member_labels = _menu_labels(
        app, app._menu_for_slot,
        {"type": "member", "dir": "X", "slot": 4, "live": True, "member": 1},
    )
    assert "Make the Restore-Point the one the game loads (promote)" in member_labels
    for label in slot_labels:
        assert label in member_labels


def test_a_live_folder_offers_backup_copy_in_and_undo(app):
    meta = {"type": "live", "dir": "X", "writable": True, "source_id": "s1"}
    labels = _menu_labels(app, app._menu_for_live_folder, meta)
    assert "Back up this folder now..." in labels
    assert "Copy a save into one of its slots..." in labels
    assert "Undo the last change to live saves" in labels


def test_a_read_only_folder_offers_import_not_writes(app):
    meta = {"type": "live", "dir": "X", "writable": False, "source_id": "s1"}
    labels = _menu_labels(app, app._menu_for_live_folder, meta)
    assert any("Import" in x for x in labels)
    assert not any("Back up" in x for x in labels)


def test_the_active_folder_is_not_offered_as_a_target_again(app):
    inactive = {"type": "live", "dir": "X", "writable": True, "source_id": "other"}
    app.active_source_id = "other"
    assert "Set as active live target" not in _menu_labels(app, app._menu_for_live_folder, inactive)
    app.active_source_id = "someone-else"
    assert "Set as active live target" in _menu_labels(app, app._menu_for_live_folder, inactive)


# --- the copy actually runs, with the right ends -----------------------------


@pytest.fixture
def live_target(app, tmp_path, monkeypatch):
    """Give the shared App one writable live folder so copy actions can proceed."""
    from nms_save_vault.core import state as appstate

    folder = tmp_path / "live"
    folder.mkdir()
    source = appstate.Source(
        id="steam-test", platform="steam", account="1", path=str(folder), label="Test live"
    )
    monkeypatch.setattr(app.state, "sources", [source])
    monkeypatch.setattr(app, "live_dir", folder)
    return folder


def _capture_run(app, monkeypatch):
    """Replace _run with something that records and executes the operation lambda."""
    calls = {}

    def fake_run(fn, *, success, message=""):
        calls["result"] = fn(False)
        calls["success"] = success

    monkeypatch.setattr(app, "_run", fake_run)
    return calls


def test_direct_copy_confirms_then_writes_the_named_slot(app, live_target, tmp_path, monkeypatch):
    tmp_backup = tmp_path / "backup"
    tmp_backup.mkdir()
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    asked = {}
    monkeypatch.setattr(gui, "_askyesno", lambda title, msg: asked.setdefault("msg", msg) or True)
    recorded = {}
    monkeypatch.setattr(
        ops, "repopulate_slot",
        lambda vault, src, src_slot, live, dst_slot, **kw: recorded.update(
            src=str(src), src_slot=src_slot, live=str(live), dst_slot=dst_slot
        ),
    )
    calls = _capture_run(app, monkeypatch)

    source = gui.SlotSource(folder=str(tmp_backup), slot=7, where="some-backup")
    app.on_copy_slot(source=source, dest_slot=7, ask=False)

    assert Path(recorded["src"]) == tmp_backup
    assert recorded["src_slot"] == 7
    assert recorded["dst_slot"] == 7
    assert str(live_target) == recorded["live"]
    assert "slot 7 of some-backup" in asked["msg"]
    assert "currently holds" in asked["msg"], "the user must see what is being replaced"
    assert "slot 7" in calls["success"]


def test_declining_the_confirmation_writes_nothing(app, live_target, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: False)
    monkeypatch.setattr(ops, "repopulate_slot", lambda *a, **k: pytest.fail("must not run"))
    calls = _capture_run(app, monkeypatch)
    app.on_copy_slot(source=gui.SlotSource("B:/x", 3, "b"), dest_slot=3, ask=False)
    assert calls == {}


def test_clearing_shows_the_play_time_gap_and_then_clears(app, live_target, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    plan = ops.ClearPlan(
        slot=4, occupied=True, live_name="Expedition", live_play_time=12600,
        entry_id="full-steam-20260101-000000", entry_kind="full",
        entry_created="2026-01-01T00:00:00", backup_name="Expedition", backup_play_time=3600,
    )
    monkeypatch.setattr(ops, "plan_clear_slot", lambda vault, directory, slot: plan)
    asked = {}
    monkeypatch.setattr(gui, "_askyesno", lambda title, msg: asked.setdefault("msg", msg) or True)
    recorded = {}
    monkeypatch.setattr(
        ops, "clear_slot",
        lambda vault, directory, slot, **kw: recorded.update(directory=str(directory), slot=slot),
    )
    calls = _capture_run(app, monkeypatch)

    app.on_clear({"type": "slot", "dir": str(live_target), "slot": 4, "live": True})

    assert recorded == {"directory": str(live_target), "slot": 4}
    assert "2h30m more play time" in asked["msg"], "the user must see what clearing costs"
    assert "full-steam-20260101-000000" in asked["msg"]
    assert "Undo" in asked["msg"]
    assert "slot 4" in calls["success"]


def test_declining_the_clear_deletes_nothing(app, live_target, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    monkeypatch.setattr(
        ops, "plan_clear_slot", lambda *a, **k: ops.ClearPlan(slot=4, occupied=True, live_name="X")
    )
    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: False)
    monkeypatch.setattr(ops, "clear_slot", lambda *a, **k: pytest.fail("must not run"))
    calls = _capture_run(app, monkeypatch)
    app.on_clear({"type": "slot", "dir": str(live_target), "slot": 4, "live": True})
    assert calls == {}


def test_clearing_an_already_empty_slot_says_so_and_stops(app, live_target, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    monkeypatch.setattr(ops, "plan_clear_slot", lambda *a, **k: ops.ClearPlan(slot=4, occupied=False))
    shown = []
    monkeypatch.setattr(gui, "_showinfo", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: pytest.fail("must not ask"))
    calls = _capture_run(app, monkeypatch)
    app.on_clear({"type": "slot", "dir": str(live_target), "slot": 4, "live": True})
    assert shown and calls == {}


@pytest.mark.parametrize(
    "meta",
    [
        {"type": "slot", "dir": "X", "slot": 4, "live": True},
        {"type": "member", "dir": "X", "slot": 4, "live": True, "member": 1},
    ],
    ids=["slot", "save-inside-it"],
)
def test_clear_from_the_right_click_menu_actually_clears(app, live_target, monkeypatch, meta):
    """Regression: the menu narrowed a member row to its slot with a hand-built dict that
    dropped the "live" flag, so Clear -- which needs a LIVE slot -- refused every row the
    menu offered it to, on a slot and on either save inside it alike. Reported as "no
    matter what I try, I get this error"."""
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    meta = dict(meta, dir=str(live_target))
    monkeypatch.setattr(
        ops, "plan_clear_slot", lambda *a, **k: ops.ClearPlan(slot=4, occupied=True, live_name="X")
    )
    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: True)
    monkeypatch.setattr(gui, "_showinfo", lambda *a, **k: pytest.fail("must not report a bad row"))
    recorded = {}
    monkeypatch.setattr(ops, "clear_slot", lambda vault, d, slot, **k: recorded.update(slot=slot))
    _capture_run(app, monkeypatch)

    assert _invoke_menu_entry(app, app._menu_for_slot, meta, "Clear live slot 4")
    assert recorded == {"slot": 4}


def test_clearing_needs_a_live_row(app, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    shown = []
    monkeypatch.setattr(gui, "_showinfo", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(ops, "plan_clear_slot", lambda *a, **k: pytest.fail("must not plan"))
    calls = _capture_run(app, monkeypatch)
    app.on_clear({"type": "slot", "dir": "X", "slot": 4, "live": False})
    assert shown and calls == {}


def test_copy_refuses_when_there_is_nowhere_writable(app, monkeypatch):
    from nms_save_vault import gui

    monkeypatch.setattr(app.state, "sources", [])
    shown = []
    monkeypatch.setattr(gui, "_showerror", lambda *a, **k: shown.append(a))
    calls = _capture_run(app, monkeypatch)
    app.on_copy_slot(source=gui.SlotSource("B:/x", 3, "b"), dest_slot=3, ask=False)
    assert shown and calls == {}
