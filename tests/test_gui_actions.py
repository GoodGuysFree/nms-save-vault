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
    # Only this folder is live: the machine's own save folders stay out of the test.
    from nms_save_vault.core import discover

    monkeypatch.setattr(discover, "discover_live_sources", lambda *a, **k: [])
    return folder


def _sync_worker(app, monkeypatch):
    """Run what would go to the worker thread inline: no wait window, no thread."""

    def run(fn, force, message):
        try:
            return {"result": fn(force)}
        except Exception as exc:  # noqa: BLE001 - handed back, as the real worker does
            return {"error": exc}

    monkeypatch.setattr(app, "_run_worker", run)


def _capture_run(app, monkeypatch):
    """Replace _run with something that records and executes the operation lambda (and
    run any worker-thread step, such as the clear plan, inline)."""
    calls = {}

    def fake_run(fn, *, success, message="", warnings=None):
        calls["result"] = fn(False)
        calls["success"] = success

    monkeypatch.setattr(app, "_run", fake_run)
    _sync_worker(app, monkeypatch)
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
    assert "only one save is refused" in asked["msg"], "the P6 rule, not 'both saves are copied'"
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
    monkeypatch.setattr(ops, "plan_clear_slot", lambda vault, directory, slot, **kw: plan)
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
    assert "2h30m less play time" in asked["msg"], "the user must see what clearing costs"
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


# --- step 11: undo names its target, asks over damage, offers the account data --------------


def _undo_plan(**kw):
    from nms_save_vault.core import operations as ops

    return ops.UndoPlan(op="repopulate_slot", ts="2026-10-17T10:00:00", snapshot_id="snapshot-1",
                        token="7:snapshot-1", **kw)


def test_undo_names_the_folder_and_passes_every_choice(app, live_target, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    monkeypatch.setattr(ops, "plan_undo", lambda vault, directory, **kw: _undo_plan())
    asked = {}

    def confirm(master, title, message, check=None):
        asked.update(message=message, check=check)
        return True, True

    monkeypatch.setattr(gui, "_confirm", confirm)
    recorded = {}
    monkeypatch.setattr(
        ops, "undo_last", lambda vault, directory, **kw: recorded.update(directory=Path(directory), **kw)
    )
    _capture_run(app, monkeypatch)

    app.on_undo()

    assert live_target.name in asked["message"] and "snapshot-1" in asked["message"]
    assert asked["check"] == gui.ACCOUNT_DATA_CHOICE
    assert recorded["directory"] == live_target
    assert recorded["restore_accountdata"] is True and recorded["acknowledge_damage"] is False
    assert any(Path(d).resolve() == live_target.resolve() for d in recorded["other_live_dirs"])
    assert recorded["expected"] == "7:snapshot-1", "the undo is held to the change confirmed"


@pytest.mark.parametrize("accept", [True, False], ids=["accepted", "declined"])
def test_undo_over_a_damaged_history_asks_first(app, live_target, monkeypatch, accept):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    monkeypatch.setattr(
        ops, "plan_undo", lambda *a, **k: _undo_plan(damaged_after=True, log_warning="1 damaged line")
    )
    questions = []
    monkeypatch.setattr(gui, "_askyesno", lambda title, msg: questions.append(msg) or accept)
    monkeypatch.setattr(gui, "_confirm", lambda *a, **k: (True, False))
    recorded = {}
    monkeypatch.setattr(ops, "undo_last", lambda vault, directory, **kw: recorded.update(kw))
    calls = _capture_run(app, monkeypatch)

    app.on_undo()

    assert questions and "1 damaged line" in questions[0]
    if accept:
        assert recorded["acknowledge_damage"] is True
    else:
        assert calls == {} and recorded == {}


def test_undo_from_a_folders_menu_acts_on_that_folder(app, live_target, tmp_path, monkeypatch):
    """Like Promote (P3): the menu of a folder that is not the active one must not undo the
    active one."""
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    other = tmp_path / "other-live"
    other.mkdir()
    seen = []

    def plan(vault, directory, **kw):
        seen.append(Path(directory))
        raise ops.OperationError("Nothing has been done yet, so there is nothing to undo.")

    monkeypatch.setattr(ops, "plan_undo", plan)
    shown = []
    monkeypatch.setattr(gui, "_showerror", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(gui, "_confirm", lambda *a, **k: pytest.fail("nothing to undo: must not ask"))

    meta = {"type": "live", "dir": str(other), "writable": True, "source_id": "other"}
    assert _invoke_menu_entry(app, app._menu_for_live_folder, meta, "Undo the last change")
    assert seen == [other] and shown


# --- step 11: messages an operation without an OpResult has to hand back ---------------------


@pytest.mark.parametrize("action", ["backup", "extract", "import"])
def test_a_history_that_could_not_be_written_is_shown(app, live_target, monkeypatch, action):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    message = "Done, but missing from the history: the undo log could not be written (disk full)."

    def op(*a, warnings=None, **k):
        warnings.append(message)

    name = {"backup": "create_full_backup", "extract": "extract_slot", "import": "import_backup"}[action]
    monkeypatch.setattr(ops, name, op)
    monkeypatch.setattr(gui.simpledialog, "askstring", lambda *a, **k: "label")
    monkeypatch.setattr(gui, "_askyesno", lambda *a, **k: False)
    shown = []
    monkeypatch.setattr(gui, "_showinfo", lambda title, msg: shown.append(msg))
    monkeypatch.setattr(app, "refresh", lambda: None)
    _sync_worker(app, monkeypatch)

    if action == "backup":
        app._backup_dir(live_target)
    elif action == "extract":
        app.on_extract({"type": "slot", "dir": str(live_target), "slot": 1, "live": True})
    else:
        app._import_dir(live_target)

    assert shown and message in shown[-1]


# --- step 11: the account data is a choice, unticked by default -------------------------------


@pytest.mark.parametrize("ticked", [False, True], ids=["kept", "restored"])
def test_a_full_restore_offers_the_account_data_and_passes_the_choice(app, live_target, monkeypatch, ticked):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    entry = next(e for e in app.vault.entries if e.kind == "full")
    offered = {}

    def confirm(master, title, message, check=None):
        offered["check"] = check
        return True, ticked

    monkeypatch.setattr(gui, "_confirm", confirm)
    recorded = {}
    monkeypatch.setattr(ops, "restore_entry", lambda vault, e, live, **kw: recorded.update(kw))
    _capture_run(app, monkeypatch)

    app.on_restore({"type": "entry", "entry": entry})

    assert offered["check"] == gui.ACCOUNT_DATA_CHOICE
    assert recorded["restore_accountdata"] is ticked


def test_the_confirm_dialog_starts_unticked(app, monkeypatch):
    from nms_save_vault import gui

    monkeypatch.setattr(gui.ConfirmDialog, "grab_set", lambda self: None)
    monkeypatch.setattr(gui.ConfirmDialog, "wait_window", lambda self, *a: None)
    dialog = gui.ConfirmDialog(app, "Restore", "Replace the live saves?", check=gui.ACCOUNT_DATA_CHOICE)
    try:
        assert dialog._check.get() is False
        dialog._yes()
        assert dialog.answer is True and dialog.ticked is False
    finally:
        if dialog.winfo_exists():
            dialog.destroy()


# --- step 11: refusals and half-done writes are told apart ------------------------------------


def test_a_partly_done_write_refreshes_and_says_so_with_its_warnings(app, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    result = ops.OpResult(False, "restore_full", "restored x", snapshot_id="snapshot-1",
                          warnings=["The live folder kept its own account data."])
    error = ops.WrittenButInvalidError(
        "The restore could not replace everything, so the folder is only partly restored. "
        "The live folder was changed: undo restores it from snapshot snapshot-1.", result,
    )
    monkeypatch.setattr(app, "_run_worker", lambda fn, force, message: {"error": error})
    refreshed, shown = [], []
    monkeypatch.setattr(app, "refresh", lambda: refreshed.append(True))
    monkeypatch.setattr(gui, "_showerror", lambda title, msg: shown.append((title, msg)))

    app._run(lambda force: None, success="Restored.")

    assert refreshed, "the live folder was changed: the view must show it"
    title, msg = shown[0]
    assert "Only partly done" in title and "only partly restored" in msg
    assert "kept its own account data" in msg


def test_another_accounts_backup_is_named_as_such(app, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    error = ops.ForeignAccountError("'full-x' was taken from another Xbox account. Nothing was changed.")
    monkeypatch.setattr(app, "_run_worker", lambda fn, force, message: {"error": error})
    shown = []
    monkeypatch.setattr(gui, "_showerror", lambda title, msg: shown.append((title, msg)))

    app._run(lambda force: None, success="Restored.")

    assert "Another account" in shown[0][0] and "another Xbox account" in shown[0][1]


# --- step 11: P3, promote acts on the row's own folder ----------------------------------------


def test_promote_acts_on_the_clicked_rows_folder_not_the_active_one(app, live_target, tmp_path, monkeypatch):
    from nms_save_vault.core import operations as ops
    from nms_save_vault.core import state as appstate

    other = tmp_path / "second-live"
    other.mkdir()
    app.state.sources.append(
        appstate.Source(id="steam-second", platform="steam", account="2", path=str(other), label="Second")
    )
    recorded = {}
    monkeypatch.setattr(
        ops, "promote_member",
        lambda vault, directory, slot, member, **kw: recorded.update(directory=Path(directory), slot=slot, member=member),
    )
    _capture_run(app, monkeypatch)
    meta = {"type": "member", "dir": str(other), "slot": 3, "live": True, "member": 1}

    assert _invoke_menu_entry(app, app._menu_for_slot, meta, "Make the Restore-Point")
    assert recorded == {"directory": other, "slot": 3, "member": 1}
    assert app.live_dir == live_target


# --- step 11: the clear plan off the Tk thread, with what it could not check ------------------


def test_the_clear_plan_runs_off_the_tk_thread_and_shows_what_it_could_not_check(app, live_target, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    plan = ops.ClearPlan(slot=4, occupied=True, live_name="Run", unverifiable=True,
                         notes=["entry-1 could not be checked (drive unplugged)."])
    planned = {}
    monkeypatch.setattr(ops, "plan_clear_slot", lambda vault, directory, slot, **kw: planned.update(kw) or plan)
    workers = []

    def worker(fn, force, message):
        workers.append(message)
        return {"result": fn(force)}

    monkeypatch.setattr(app, "_run_worker", worker)
    asked = {}
    monkeypatch.setattr(gui, "_askyesno", lambda title, msg: asked.setdefault("msg", msg) and False)

    app.on_clear({"type": "slot", "dir": str(live_target), "slot": 4, "live": True})

    assert workers, "the plan reads the whole vault: it must not run on the Tk thread"
    assert any(Path(d).resolve() == live_target.resolve() for d in planned["other_live_dirs"])
    assert "CANNOT VERIFY" in asked["msg"] and "drive unplugged" in asked["msg"]


# --- step 11: P10, nothing out of reach at a larger font ---------------------------------------


def test_the_toolbar_wraps_so_every_button_stays_reachable_when_zoomed(app):
    from nms_save_vault import gui

    before = app.state.font_scale
    try:
        app.state.font_scale = 1.2
        app._apply_theme()
        app.update_idletasks()
        assert len(app.toolbar.buttons) == 14
        assert "Undo" in [b.cget("text") for b in app.toolbar.buttons]
        for width in (1000, 400):
            app.toolbar.reflow(width)
            rows: dict[str, list[int]] = {}
            for button in app.toolbar.buttons:
                row = str(button.pack_info()["in"])
                rows.setdefault(row, []).append(button.winfo_reqwidth() + 2 * gui.FlowBar.PAD)
            assert all(sum(row) <= width or len(row) == 1 for row in rows.values())
        assert len(rows) > 1, "400 px cannot hold 14 buttons on one row"
    finally:
        app.state.font_scale = before
        app._apply_theme()
        app.toolbar.reflow(1000)


def test_the_copy_dialog_packs_its_buttons_before_the_slot_list(app, live_target, monkeypatch):
    """P10: packed last, Copy and Cancel were the first thing cut off when the window was
    too short for the zoomed fonts."""
    from tkinter import ttk

    from nms_save_vault import gui

    monkeypatch.setattr(gui.CopySlotDialog, "grab_set", lambda self: None)
    monkeypatch.setattr(gui.CopySlotDialog, "wait_window", lambda self, *a: None)
    dialog = gui.CopySlotDialog(app)
    try:
        slaves = dialog.pack_slaves()

        def holds_copy(widget):
            return any(isinstance(c, ttk.Button) and c.cget("text") == "Copy" for c in widget.winfo_children())

        buttons = next(w for w in slaves if holds_copy(w))
        slot_list = next(
            w for w in slaves if isinstance(w, ttk.LabelFrame) and "Into this live slot" in str(w.cget("text"))
        )
        assert slaves.index(buttons) < slaves.index(slot_list)
    finally:
        dialog.destroy()


# --- step 11: discover points out an entry that is a live folder ------------------------------


def test_discover_notes_an_entry_that_is_a_live_folder_and_leaves_it(app, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import discover

    entry = next(e for e in app.vault.entries if e.kind == "inplace")
    monkeypatch.setattr(discover, "all_live_paths", lambda state, *a, **k: [entry.path])
    monkeypatch.setattr(discover, "discover_inplace_backups", lambda *a, **k: [])
    shown = []
    monkeypatch.setattr(gui, "_showinfo", lambda title, msg: shown.append(msg))
    before = [e.id for e in app.vault.entries]

    app.on_discover()

    assert f"catalog entry '{entry.id}'" in shown[0]
    assert "is or lies inside a live save folder" in shown[0]
    assert [e.id for e in app.vault.entries] == before


# --- step 11 review ---------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [{"writable": False}, {"enabled": False}], ids=["read-only", "switched off"])
def test_promote_refuses_a_folder_that_takes_no_writes(app, live_target, tmp_path, monkeypatch, flags):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops
    from nms_save_vault.core import state as appstate

    folder = tmp_path / "no-writes"
    folder.mkdir()
    app.state.sources.append(
        appstate.Source(id="xbox-ro", platform="xbox", account="x", path=str(folder), label="X", **flags)
    )
    monkeypatch.setattr(ops, "promote_member", lambda *a, **k: pytest.fail("must not promote"))
    shown = []
    monkeypatch.setattr(gui, "_showinfo", lambda *a, **k: shown.append(a))
    calls = _capture_run(app, monkeypatch)

    app.on_promote({"type": "member", "dir": str(folder), "slot": 1, "live": True, "member": 0})

    assert shown and calls == {}


def test_undoing_a_restore_with_account_data_states_it_and_offers_no_checkbox(app, live_target, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    monkeypatch.setattr(ops, "plan_undo", lambda *a, **k: _undo_plan(with_accountdata=True))
    asked = {}

    def confirm(master, title, message, check=None):
        asked.update(message=message, check=check)
        return False, False

    monkeypatch.setattr(gui, "_confirm", confirm)
    _capture_run(app, monkeypatch)

    app.on_undo()

    assert asked["check"] is None and "account data" in asked["message"]


def test_discover_goes_on_past_a_folder_it_cannot_add_and_reports_everything(app, tmp_path, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import discover
    from nms_save_vault.core import operations as ops

    bad, good = tmp_path / "bad-backup", tmp_path / "good-backup"
    monkeypatch.setattr(discover, "all_live_paths", lambda state, *a, **k: [])
    monkeypatch.setattr(discover, "discover_inplace_backups", lambda *a, **k: [bad, good])

    def import_backup(vault, path, **kw):
        if path == bad:
            raise ops.OperationError("not an NMS save folder")
        kw["warnings"].append("missing from the history")

    monkeypatch.setattr(ops, "import_backup", import_backup)
    refreshed, shown = [], []
    monkeypatch.setattr(app, "refresh", lambda: refreshed.append(True))
    monkeypatch.setattr(gui, "_showinfo", lambda title, msg: shown.append(msg))

    app.on_discover()

    assert refreshed
    assert "Added 1 newly found backup" in shown[0]
    assert "Could not add" in shown[0] and "not an NMS save folder" in shown[0]
    assert "missing from the history" in shown[0]


def test_the_confirm_dialog_survives_a_refused_grab(app, monkeypatch):
    import tkinter as tk

    from nms_save_vault import gui

    def refuse(self):
        raise tk.TclError("grab failed: window not viewable")

    monkeypatch.setattr(gui.ConfirmDialog, "grab_set", refuse)
    monkeypatch.setattr(gui.ConfirmDialog, "wait_window", lambda self, *a: None)
    dialog = gui.ConfirmDialog(app, "Undo", "Undo the last change?")
    try:
        assert dialog.answer is False
    finally:
        dialog.destroy()


def test_the_copy_dialog_fits_the_screen_and_keeps_its_buttons(app, live_target, monkeypatch):
    from tkinter import ttk

    from nms_save_vault import gui

    monkeypatch.setattr(gui.CopySlotDialog, "grab_set", lambda self: None)
    monkeypatch.setattr(gui.CopySlotDialog, "wait_window", lambda self, *a: None)
    dialog = gui.CopySlotDialog(app)
    try:
        width = int(dialog.geometry().split("x")[0])
        assert width <= dialog.winfo_screenwidth()
        source_frame = next(
            w for w in dialog.pack_slaves() if isinstance(w, ttk.LabelFrame) and "Copy from" in str(w.cget("text"))
        )
        first = source_frame.pack_slaves()[0]
        assert isinstance(first, ttk.Button) and first.cget("text") == "Change..."
        rule = next(
            w for w in dialog.pack_slaves()
            if isinstance(w, ttk.Label) and "only one save is refused" in str(w.cget("text"))
        )
        assert int(str(rule.cget("wraplength")) or 0) > 0, "the caption wraps instead of widening the window"
    finally:
        dialog.destroy()


# --- step 11 final round ------------------------------------------------------------------------


def test_promote_refuses_a_switched_off_folder_the_app_fell_back_to(app, live_target, monkeypatch):
    """With every source switched off, the LIVE pane falls back to the folder the app was
    started on; that is still a switched-off source, so nothing may be written into it."""
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    app.state.sources[0].enabled = False
    try:
        monkeypatch.setattr(ops, "promote_member", lambda *a, **k: pytest.fail("must not promote"))
        shown = []
        monkeypatch.setattr(gui, "_showinfo", lambda *a, **k: shown.append(a))
        calls = _capture_run(app, monkeypatch)

        app.on_promote({"type": "member", "dir": str(live_target), "slot": 1, "live": True, "member": 0})

        assert shown and calls == {}
    finally:
        app.state.sources[0].enabled = True


def test_a_partly_done_write_names_its_snapshot_for_undo(app, monkeypatch):
    from nms_save_vault import gui
    from nms_save_vault.core import operations as ops

    result = ops.OpResult(False, "repopulate_slot", "slot 1 -> 1", snapshot_id="snapshot-9")
    error = ops.WrittenButInvalidError("Slot 1A is still not the newest after promoting it.", result)
    monkeypatch.setattr(app, "_run_worker", lambda fn, force, message: {"error": error})
    monkeypatch.setattr(app, "refresh", lambda: None)
    shown = []
    monkeypatch.setattr(gui, "_showerror", lambda title, msg: shown.append(msg))

    app._run(lambda force: None, success="Copied.")

    assert "Undo available (snapshot snapshot-9)." in shown[0]


def test_the_confirm_dialog_starts_on_yes_and_answers_to_the_keys(app, monkeypatch):
    from tkinter import ttk

    from nms_save_vault import gui

    monkeypatch.setattr(gui.ConfirmDialog, "grab_set", lambda self: None)
    monkeypatch.setattr(gui.ConfirmDialog, "wait_window", lambda self, *a: None)
    # The session's app is withdrawn, so the dialog is never mapped and Tk records no
    # focus for it: watch which button is given the focus instead.
    focused = []
    monkeypatch.setattr(ttk.Button, "focus_set", lambda self: focused.append(self.cget("text")))
    dialog = gui.ConfirmDialog(app, "Undo", "Undo the last change?")
    try:
        assert focused == ["Yes"]
        assert dialog.bind("<Escape>") and dialog.bind("<Return>")
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
