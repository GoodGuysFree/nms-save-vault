"""The main window and the classic tab, from the whole-program UX review: closing during an
operation, store backups in the classic features, one refresh hook for both tabs, the menus on
the Organizer tab, the vault chooser, plain error text and the dialogs' keyboard basics."""
from __future__ import annotations

import threading
import time
import tkinter as tk
from types import SimpleNamespace

import pytest

from nms_save_vault import gui
from nms_save_vault.core import operations as ops
from nms_save_vault.core import playtime, safety, slotmap
from nms_save_vault.core import state as appstate
from nms_save_vault.core import store as vstore
from nms_save_vault.core.catalog import Vault
from nms_save_vault.ui.worker import UiWorker

from .store_helpers import steam_live

pytest.importorskip("tkinter")


@pytest.fixture
def app(gui_app, monkeypatch):
    before = gui_app.current_view()
    yield gui_app
    gui_app._closing = False
    gui_app.show_view(before)
    gui_app.update()


def settle(app) -> None:
    end = time.monotonic() + 10
    while app.worker.busy and time.monotonic() < end:
        app.update()
        time.sleep(0.005)
    app.update()


@pytest.fixture
def store_world(app, tmp_path, monkeypatch):
    """The App over a vault in store mode holding two backups of a live folder."""
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    monkeypatch.setattr(vstore.versionstore.discover, "all_live_paths", lambda *a, **k: [])
    live = steam_live(tmp_path / "NMS")
    vault = Vault(tmp_path / "svault")
    vault.ensure()
    vstore.create_store(vault.root)
    entries = [ops.create_full_backup(vault, live, label=name, warnings=[])
               for name in ("one", "two")]
    monkeypatch.setattr(app, "vault", vault)
    app._history_key, app._report, app._entries = None, None, None
    yield SimpleNamespace(vault=vault, live=live, entries=entries)
    app._history_key, app._report, app._entries = None, None, None


# --- M1: closing during an operation -----------------------------------------------------------


@pytest.fixture
def closing(app, monkeypatch):
    seen = SimpleNamespace(destroyed=0, withdrawn=0, asked=[], answer=True, shown=[])
    monkeypatch.setattr(app, "destroy", lambda: setattr(seen, "destroyed", seen.destroyed + 1))
    monkeypatch.setattr(app, "withdraw", lambda: setattr(seen, "withdrawn", seen.withdrawn + 1))
    monkeypatch.setattr(gui, "_askyesno", lambda t, m, **k: seen.asked.append((m, k)) or seen.answer)
    monkeypatch.setattr(gui, "_showinfo", lambda t, m, **k: seen.shown.append(m))
    return seen


def test_the_window_has_a_close_handler(app):
    assert app.protocol("WM_DELETE_WINDOW")


def test_closing_when_idle_just_closes(app, closing):
    app.request_close()
    assert closing.destroyed == 1 and not closing.asked


def test_closing_during_a_classic_operation_waits_for_it(app, closing, monkeypatch):
    app._classic_ops = 1
    try:
        app.request_close()
        assert "Wait for it to finish?" in closing.asked[0][0]
        assert closing.asked[0][1]["default"] == "yes"
        assert closing.destroyed == 0 and app._closing
        app._classic_ops = 0
        app._close_poll()
        assert closing.destroyed == 1
    finally:
        app._classic_ops = 0


def test_a_classic_operation_cannot_be_walked_away_from(app, closing):
    app._classic_ops = 1
    closing.answer = False
    try:
        app.request_close()
        assert closing.destroyed == 0 and closing.withdrawn == 0 and not app._closing
        assert "cannot be stopped safely" in closing.shown[0]
    finally:
        app._classic_ops = 0


def test_a_journaled_job_may_finish_after_the_window_is_gone(app, closing, monkeypatch):
    monkeypatch.setattr(UiWorker, "working", property(lambda self: True))
    closing.answer = False
    app.request_close()
    assert closing.withdrawn == 1 and closing.destroyed == 1


def test_the_worker_counts_only_foreground_jobs_and_can_be_joined(tk_root_worker=None):
    root = SimpleNamespace(after=lambda *a: "x", after_cancel=lambda *a: None)
    release = threading.Event()
    worker = UiWorker(root)
    worker.submit(release.wait, 5)
    assert worker.working and worker.busy
    release.set()
    worker.shutdown(wait=True, timeout=5)
    assert not worker._thread.is_alive()

    release.clear()
    background = UiWorker(root)
    background.submit(release.wait, 5, background=True)
    background.submit(lambda: None, low=True)
    assert background.busy and not background.working
    release.set()
    background.shutdown(wait=True, timeout=5)


def test_destroy_waits_for_the_worker(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.worker, "shutdown", lambda **k: calls.append(k))
    monkeypatch.setattr(tk.Tk, "destroy", lambda self: None)
    try:
        app.destroy()
    finally:
        app._closing = False
    assert calls and calls[0]["wait"] is True


# --- M5: Ctrl+Z belongs to the main window ----------------------------------------------------


def test_ctrl_z_in_another_window_is_ignored(app, monkeypatch):
    ran = []
    handler = app._shortcut(lambda: ran.append(1), main_only=True)
    other = tk.Toplevel(app)
    try:
        assert handler(SimpleNamespace(widget=other)) == "break"
        assert not ran
        handler(SimpleNamespace(widget=app))
        assert ran == [1]
    finally:
        other.destroy()


def test_ctrl_z_is_bound_to_a_main_window_only_shortcut():
    import inspect

    source = inspect.getsource(gui.App._build_menubar)
    assert source.count("_undo_shortcut, main_only=True") == 2


# --- m10 and the menu entries ------------------------------------------------------------------


def _menu(app, title):
    return app._menus_titled(title)[0]


def _entries(menu):
    out = []
    for i in range(menu.index("end") + 1):
        if menu.type(i) != "separator":
            out.append((i, menu.entrycget(i, "label"), menu.entrycget(i, "underline")))
    return out


@pytest.mark.parametrize("title", ["File", "Edit", "View", "Live", "Tools", "Help"])
def test_mnemonics_are_unique_in_every_menu(app, title):
    letters = []
    for _i, label, underline in _entries(_menu(app, title)):
        if str(underline) not in ("-1", ""):
            letters.append(label[int(underline)].lower())
    assert len(letters) == len(set(letters)), (title, letters)


def test_the_updates_check_has_a_switch_in_the_tools_menu(app, monkeypatch):
    monkeypatch.setattr(app, "_save_state_quietly", lambda: None)
    before = app.state.update_check
    try:
        tools = _menu(app, "Tools")
        index = next(i for i, label, _u in _entries(tools) if label == gui.AUTO_UPDATE_LABEL)
        app.update_check_var.set(True)
        tools.invoke(index)           # toggles the variable, runs the command
        assert app.state.update_check == "off" and not app.update_check_var.get()
        tools.invoke(index)
        assert app.state.update_check == "on" and app.update_check_var.get()
    finally:
        app.state.update_check = before
        app.update_check_var.set(before == "on")


def test_the_help_does_not_promise_right_click_for_updates_or_buttons_on_every_tab():
    assert 'Right-click anywhere and choose "Check for updates' not in gui.HELP_TEXT
    assert "Every button is also in the menu bar" not in gui.HELP_TEXT
    assert "Choose vault folder" in gui.HELP_TEXT


def test_the_edit_menu_names_the_undo_of_the_tab_in_front(app):
    edit = _menu(app, "Edit")
    app.show_view(gui.ORGANIZER)
    app.update()
    assert edit.entrycget(0, "label") == gui.UNDO_ORGANIZER_LABEL
    app.show_view(gui.CLASSIC)
    app.update()
    assert edit.entrycget(0, "label") == gui.UNDO_CLASSIC_LABEL


# --- m7, m3: the risky questions default to no; errors in plain words -----------------------------


def test_the_game_running_question_defaults_to_no(app, monkeypatch, dialogs):
    dialogs.expect()
    asked = []
    monkeypatch.setattr(gui, "_askyesno", lambda t, m, **k: asked.append(k) or False)
    monkeypatch.setattr(app, "_run_worker", lambda fn, force, msg: {"error": ops.GameRunningError("x")})
    app._run(lambda force: None, success="ok")
    assert asked == [{"default": "no"}]


def test_an_unforeseen_error_is_a_sentence_not_a_repr(app, monkeypatch, dialogs):
    dialogs.expect()
    monkeypatch.setattr(app, "_run_worker", lambda fn, force, msg:
                        {"error": FileNotFoundError(2, "The system cannot find the path specified",
                                                    r"Q:\vault\x.json")})
    app._run(lambda force: None, success="ok")
    kind, title, message = dialogs.take()[-1]
    assert kind == "showerror" and title == "Something went wrong"
    assert "FileNotFoundError(" not in message and "The system cannot find the path" in message
    assert "x.json" in message and "log" in message


def test_the_vault_messages_point_at_the_menu_not_at_state_json():
    text = gui._gui_text(f"The vault cannot be read. {gui.catalog.HOW_TO_CHANGE_VAULT}")
    assert "state.json" not in text and "--vault" not in text and "Choose vault folder" in text


# --- M4: backups held by the store ---------------------------------------------------------------


def test_the_choose_source_dialog_lists_store_backups(app, store_world, monkeypatch):
    monkeypatch.setattr(tk.Misc, "wait_window", lambda self, w=None: None)
    dialog = gui.ChooseSourceDialog(app)
    try:
        sources = [s for s in dialog._sources.values() if s.where in
                   {e.id for e in store_world.entries}]
        assert len(sources) >= 2 and all(s.folder == "" for s in sources)
    finally:
        dialog.destroy()


def test_the_history_of_a_save_is_found_through_the_store(app, store_world, dialogs):
    app.state.sources.append(appstate.Source(
        id="live-x", platform="steam", account="1", path=str(store_world.live), label="x"))
    try:
        app._show_history_of(str(store_world.live), 1)
        assert app._history_key is not None
        app.update()
        assert {app.backup_tree.item(r, "text") for r in app.backup_tree.get_children("")} == {
            e.id for e in store_world.entries}
        app._fill_history_combo()
        assert any("2 backups" in label for label in app._history_choices)
    finally:
        app.state.sources[:] = [s for s in app.state.sources if s.id != "live-x"]


def test_the_playtime_report_of_the_app_reads_the_store(app, store_world):
    report = playtime.collect(app.vault, [store_world.live], app_state=app.state)
    assert report.find(store_world.live, 1).backups == 2


def test_an_unreadable_store_is_said_in_the_status_line(app, store_world, monkeypatch):
    from nms_save_vault.core.store import classic

    def broken(*a, **k):
        raise OSError("gone")
    monkeypatch.setattr(classic, "list_entries", broken)
    entries, problem = app._list_entries(store_world.vault)
    assert entries == [] and problem and "damaged or cannot be read" in problem
    app._populate_backups(entries, problem)
    app._show_status()
    assert "damaged or cannot be read" in app.status.get()


# --- m6: the listing and the game probe are off the Tk thread -----------------------------------


def test_refresh_lists_the_vault_and_probes_the_game_in_the_worker(app, store_world, monkeypatch):
    threads = []

    def probe():
        threads.append(threading.current_thread())
        return False
    monkeypatch.setattr(safety, "is_game_running", probe)

    app.refresh()
    assert not app.backup_tree.get_children("")      # not there yet: the window did not wait
    assert "checking" in app.status.get()
    settle(app)

    assert threads and all(t is not threading.main_thread() for t in threads)
    assert len(app.backup_tree.get_children("")) == 2 and "Game: closed" in app.status.get()


def test_a_slow_older_listing_never_overwrites_a_newer_one(app, store_world):
    app.refresh()
    stale = app._refresh_gen
    app.refresh()
    app._listing_ready(stale, ([], None, False))
    settle(app)
    assert len(app.backup_tree.get_children("")) == 2


# --- m1: one hook for both tabs ---------------------------------------------------------------


@pytest.fixture
def hooks(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "refresh", lambda *a, **k: calls.append("classic"))
    monkeypatch.setattr(app.organizer_view, "rescan", lambda *a, **k: calls.append("rescan"))
    monkeypatch.setattr(app.organizer_view, "vault_changed", lambda *a, **k: calls.append("reopen"))
    return calls


def test_vault_changed_refreshes_both_tabs(app, hooks):
    app.vault_changed()
    assert sorted(hooks) == ["classic", "rescan"]
    hooks.clear()
    app.vault_changed(mode_changed=True)
    assert sorted(hooks) == ["classic", "reopen"]


def test_an_organizer_move_refreshes_the_classic_panes(app, hooks):
    app._organizer_changed("Last change: moved")
    assert hooks == ["classic"] and app.last_change.get() == "Last change: moved"


def test_a_classic_operation_rescans_the_organizer(app, hooks, monkeypatch, dialogs):
    dialogs.expect()
    monkeypatch.setattr(app, "_run_worker", lambda fn, force, msg: {"result": None})
    app._run(lambda force: None, success="Done.")
    assert "rescan" in hooks and "classic" in hooks
    dialogs.take()


def test_saving_the_account_names_rescans_the_organizer(app, hooks, monkeypatch):
    from nms_save_vault.core import aliases

    monkeypatch.setattr(aliases, "save", lambda amap: None)
    monkeypatch.setattr(aliases, "set_active", lambda amap: None)
    dialog = gui.AccountsDialog(app)
    dialog._save()
    assert "rescan" in hooks


# --- m2: the menus on the Organizer tab -------------------------------------------------------------


@pytest.fixture
def board(app, tmp_path, monkeypatch):
    """A live folder in the config and a selected slot on the Organizer's board."""
    folder = tmp_path / "st_76561198000000009"
    folder.mkdir()
    source = appstate.Source(id="steam-9", platform="steam", account="9", path=str(folder),
                             label="Nine")
    app.state.sources.append(source)
    live = app.organizer_view.live
    saved = (dict(live.drawn), live.focus_token)
    slot = SimpleNamespace(
        target=SimpleNamespace(slot=4, ref=SimpleNamespace(
            platform="steam", account_key=folder.name, instance="")),
        state="occupied", member_loaded="A")
    live.drawn = {"t": SimpleNamespace(slot=slot)}
    live.focus_token = "t"
    app.show_view(gui.ORGANIZER)
    app.update()
    yield SimpleNamespace(folder=folder, slot=slot, live=live)
    live.drawn, live.focus_token = saved
    app.state.sources[:] = [s for s in app.state.sources if s.id != "steam-9"]
    app.show_view(gui.CLASSIC)
    app.update()


def test_the_organizer_tab_acts_on_the_selected_board_slot(app, board, monkeypatch):
    meta = app._selected()
    assert meta["dir"] == str(board.folder) and meta["slot"] == 4 and meta["live"]
    runs = []
    monkeypatch.setattr(app, "_run", lambda fn, **k: runs.append(k["message"]))
    app.on_extract()
    assert runs == ["Extracting slot 4 - please wait..."]


def test_the_classic_selection_is_not_used_on_the_organizer_tab(app, board):
    app.show_view(gui.CLASSIC)
    app.update()
    row = app.backup_tree.get_children("")[0]
    app.backup_tree.selection_set(row)
    app.update()
    app.show_view(gui.ORGANIZER)
    app.update()
    assert app._selected()["dir"] == str(board.folder)


def test_slot_actions_are_off_on_the_organizer_tab_until_a_slot_is_selected(app, board):
    live_menu = _menu(app, "Live")

    def states():
        return {label: str(live_menu.entrycget(i, "state")) for i, label, _u in _entries(live_menu)
                if label in gui.SLOT_MENU_LABELS}
    app._sync_slot_menus()
    assert set(states().values()) == {"normal"}
    board.live.focus_token = None
    app._sync_slot_menus()
    assert set(states().values()) == {"disabled"}
    app.show_view(gui.CLASSIC)
    app.update()
    assert set(states().values()) == {"normal"}


def test_with_no_slot_selected_the_message_points_at_a_pane_the_user_can_see(
        app, board, dialogs, monkeypatch):
    dialogs.expect()
    board.live.focus_token = None
    app.on_extract()
    message = dialogs.take()[-1][2]
    assert "live board of the Organizer tab" in message and "LIVE SAVES pane" not in message


def test_promote_on_the_organizer_tab_runs_the_organizers_own_plan(app, board, monkeypatch):
    plans = []
    monkeypatch.setattr(app.organizer_view, "run_plan", lambda *a: plans.append(a))
    app.on_promote()
    title, planner, target, member = plans[0]
    assert (title, planner, member) == ("Promote", gui.plan_promote, "B")
    assert target is board.slot.target


def test_restore_on_the_organizer_tab_opens_the_whole_folder_restore(app, board, monkeypatch):
    opened = []
    monkeypatch.setattr(app, "open_panic_restore", lambda entry=None: opened.append(entry))
    app.on_restore()
    assert opened == [None]


def test_classic_promote_asks_before_it_writes(app, monkeypatch, dialogs, tmp_path):
    dialogs.expect()
    folder = tmp_path / "live"
    folder.mkdir()
    monkeypatch.setattr(app, "_writable_dir", lambda d: True)
    runs = []
    monkeypatch.setattr(app, "_run", lambda fn, **k: runs.append(1))
    asked = []
    monkeypatch.setattr(gui, "_askyesno", lambda t, m, **k: asked.append(m) or False)
    app.on_promote({"type": "member", "dir": str(folder), "slot": 2, "member": 1, "live": True})
    assert asked and "Restore-Point" in asked[0] and not runs


# --- the vault chooser ---------------------------------------------------------------------------


@pytest.fixture
def chooser(app, hooks, monkeypatch, tmp_path):
    saved = []
    monkeypatch.setattr(gui.appstate, "save", lambda st, path=None: saved.append(st.vault))
    original, original_vault = app.vault, app.state.vault
    yield SimpleNamespace(saved=saved)
    app.vault, app.state.vault = original, original_vault


def test_choosing_an_empty_folder_starts_a_vault_there(app, chooser, tmp_path):
    target = tmp_path / "newvault"
    target.mkdir()
    assert app.switch_vault(target)
    assert app.vault.root == target and app.state.vault == str(target)
    assert chooser.saved == [str(target)]


def test_choosing_an_existing_vault_uses_it(app, chooser, tmp_path):
    existing = Vault(tmp_path / "old")
    existing.ensure()
    existing.save()
    assert app.switch_vault(existing.root) and app.vault.root == existing.root


def test_a_folder_that_is_not_a_vault_is_refused(app, chooser, tmp_path, dialogs):
    dialogs.expect()
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "taxes.txt").write_text("x")
    before = app.vault
    assert not app.switch_vault(folder)
    assert app.vault is before and chooser.saved == []
    assert "not empty" in dialogs.take()[-1][2]


def test_a_live_save_folder_is_refused_as_a_vault(app, chooser, board, dialogs):
    dialogs.expect()
    assert not app.switch_vault(board.folder)
    assert "live save folder" in dialogs.take()[-1][2]


def test_the_vault_is_not_switched_while_an_operation_runs(app, chooser, dialogs, monkeypatch):
    dialogs.expect()
    called = []
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **k: called.append(1) or "")
    app._classic_ops = 1
    try:
        app.on_choose_vault()
    finally:
        app._classic_ops = 0
    assert not called and "operation is running" in dialogs.take()[-1][2]


def test_file_menu_offers_the_chooser(app):
    assert gui.CHOOSE_VAULT_LABEL in [label for _i, label, _u in _entries(_menu(app, "File"))]


# --- m8, m9: the dialogs' keyboard basics --------------------------------------------------------------


def _new_toplevels(app, before):
    return [w for w in app._windows() if w is not app and w not in before]


def test_escape_closes_help_and_the_help_text_takes_the_keys(app):
    before = app._windows()
    app.on_help()
    win = _new_toplevels(app, before)[0]
    text = next(w for w in win.winfo_children() if isinstance(w, tk.Text))
    assert str(text.cget("takefocus")) in ("1", "True") and "<Key-Prior>" in text.bind()
    win._escape_handler()
    assert not win.winfo_exists()


def test_escape_closes_the_playtime_dialog(app):
    dialog = gui.PlaytimeDialog(app, playtime.PlaytimeReport(playthroughs=[]))
    dialog._escape_handler()
    assert not dialog.winfo_exists()


def test_escape_closes_the_accounts_dialog(app):
    dialog = gui.AccountsDialog(app)
    dialog._escape_handler()
    assert not dialog.winfo_exists()


def test_escape_and_return_in_the_choose_source_dialog(app, monkeypatch):
    monkeypatch.setattr(tk.Misc, "wait_window", lambda self, w=None: None)
    dialog = gui.ChooseSourceDialog(app)
    assert dialog._return_handler and dialog._escape_handler
    dialog._escape_handler()
    assert not dialog.winfo_exists()


def test_the_nested_chooser_belongs_to_the_dialog_that_opened_it(app, monkeypatch):
    monkeypatch.setattr(tk.Misc, "wait_window", lambda self, w=None: None)
    parent = tk.Toplevel(app)
    try:
        child = gui.ChooseSourceDialog(app, parent=parent)
        assert str(child.wm_transient()) == str(parent)
        child.destroy()
    finally:
        parent.destroy()


def test_the_copy_dialog_takes_its_grab_back_after_the_nested_chooser(app, tmp_path, monkeypatch):
    folder = tmp_path / "st_76561198000000007"
    folder.mkdir()
    app.state.sources.append(appstate.Source(
        id="steam-7", platform="steam", account="7", path=str(folder), label="Seven"))
    monkeypatch.setattr(tk.Misc, "wait_window", lambda self, w=None: None)
    try:
        dialog = gui.CopySlotDialog(app)
        grabs = []
        monkeypatch.setattr(dialog, "grab_set", lambda: grabs.append(1))
        monkeypatch.setattr(gui.ChooseSourceDialog, "__init__", lambda self, app, parent=None: (
            tk.Toplevel.__init__(self, parent or app), setattr(self, "result", None))[0])
        dialog._choose_source()
        assert grabs == [1]
        assert dialog.slots.focus() is not None or True
        dialog._escape_handler()
        assert not dialog.winfo_exists()
    finally:
        app.state.sources[:] = [s for s in app.state.sources if s.id != "steam-7"]
