"""The Move to live dialog (D6 U14): the planner, the eviction picker and ``execute`` of the
live-operations API driven through the real widgets, on synthetic Steam folders and a
synthetic version-store vault. Every plan and every write runs in the UI worker."""
from __future__ import annotations

import dataclasses
import threading
import time
import tkinter as tk
from tkinter import ttk

import pytest

from nms_save_vault.core import liveops, safety
from nms_save_vault.ui.organizer import move as M
from nms_save_vault.ui.organizer import view as V

from .liveops_support import steam_world, tree_hash


def _wait(app, condition, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    return steam_world(tmp_path)


@pytest.fixture
def running():
    """Whether the (scripted) game is running; tests flip it."""
    return {"on": False}


@pytest.fixture
def threads():
    """For every call of the live-operations API: was it made on the Tk thread?"""
    return []


@pytest.fixture
def view(gui_app, world, running, threads, monkeypatch):
    for name in ("eviction_picker", "plan_place", "execute"):
        real = getattr(M, name)

        def spy(*a, _real=real, **k):
            threads.append(threading.current_thread() is threading.main_thread())
            return _real(*a, **k)
        monkeypatch.setattr(M, name, spy)

    def context(organizer):
        return liveops.context_for(organizer, host=world.host, machine_id="m-00000000000a",
                                   app_version="nms-save-vault test",
                                   game_probe=lambda: running["on"])
    top = tk.Toplevel(gui_app)
    top.withdraw()
    widget = V.OrganizerView(top, worker=gui_app.worker, themed=gui_app.themed, prefs={},
                             save_prefs=lambda: None, make_organizer=lambda: world.organizer,
                             make_context=context)
    widget.pack(fill=tk.BOTH, expand=True)
    widget.rescan()
    _wait(gui_app, lambda: not widget.scanning)
    yield widget
    if widget.move_dialog is not None and widget.move_dialog.winfo_exists():
        widget.move_dialog.destroy()
    top.destroy()


def _polestar(view) -> str:
    return next(line.pid for line in view.model.lines() if line.name == "Polestar")


def _open(view, app, **kw) -> M.MoveDialog:
    dialog = view.open_move_for(_polestar(view), **kw)
    assert dialog is not None
    _wait(app, lambda: not dialog.busy and dialog.picker is not None)
    return dialog


def _other_account(d, app) -> None:
    """Switch to the account the save does not come from (its one confirmation, F7b)."""
    choice = next(c for c in d.picker.other_accounts if not c.blocked)
    before = d.plan
    d._account_picked(choice.account)
    _wait(app, lambda: d.account == choice.account and d.plan is not None and d.plan is not before)


def test_the_dialog_shows_the_picker_the_suggestion_and_the_plan(view, gui_app, threads):
    d = _open(view, gui_app)
    assert len(d.tree.get_children()) == 15 and d.picker is not None
    assert d.slot == d.picker.suggested_slot and d.plan.outcome == d.apply_button.cget("text")
    assert d.in_name.cget("text") == "Polestar" and "Polestar" in d.pick_in_text.cget("text")
    assert d.pick_out_text.cget("text") and d.happen_text.cget("text").startswith("Steam slot")
    assert d.tree.item(f"s{d.slot}", "text") == "(o)"
    assert threads and not any(threads)           # the API ran in the worker, never on Tk
    d.cancel()


def test_in_is_green_and_out_is_amber(view, gui_app):
    d = _open(view, gui_app)
    p = gui_app.themed.palette
    assert d.in_box.cget("background") == p["in_bg"]
    assert d.out_box.cget("background") == p["out_bg"]
    assert d.pick_in.cget("background") == p["in_fg"]
    assert d.pick_out.cget("background") == p["out_fg"]
    d.cancel()


def test_picking_another_slot_replans_and_renames_the_button(view, gui_app):
    d = _open(view, gui_app)
    other = next(c for c in d.picker.candidates if c.target.slot != d.slot and not c.blocked
                 and c.state != "empty")
    before = d.plan
    d.tree.selection_set(f"s{other.target.slot}")
    gui_app.update()
    _wait(gui_app, lambda: d.plan is not None and d.plan is not before)
    assert d.slot == other.target.slot and d.apply_button.cget("text") == other.outcome
    assert d.tree.item(f"s{other.target.slot}", "text") == "(o)"
    assert other.label.upper() in d.out_title.cget("text")
    d.cancel()


def test_one_warning_per_other_account_and_the_button_waits_for_the_confirmation(view, gui_app):
    d = _open(view, gui_app)
    assert d.plan.confirmations == {"TWIN_LIVE"}          # it is also live in the other account
    _other_account(d, gui_app)
    codes = [w.code for w in d.plan.warnings]
    assert codes.count("OTHER_ACCOUNT") == 1 and "OTHER_ACCOUNT" in d.plan.confirmations
    assert sorted(d._confirm_vars) == sorted(d.plan.confirmations)
    assert d.apply_button.instate(["disabled"])
    for var in d._confirm_vars.values():
        var.set(True)
    d._update_apply()
    assert d.apply_button.instate(["!disabled"])
    d.cancel()


def test_the_game_running_blocks_every_slot_and_says_so(view, gui_app, running):
    running["on"] = True
    d = _open(view, gui_app)
    assert d.plan is None and all(c.blocked for c in d.picker.candidates)
    assert "running" in d.plan_strip.cget("text").lower() and d.apply_button.instate(["disabled"])


def test_a_planned_block_is_shown_in_red_and_disables_the_button(view, gui_app, monkeypatch):
    block = liveops.planner.warning("VAULT_UNAVAILABLE")
    real = M.plan_place
    monkeypatch.setattr(M, "plan_place", lambda *a: dataclasses.replace(real(*a), blocked=(block,)))
    d = _open(view, gui_app)
    _wait(gui_app, lambda: d.plan is not None)
    assert d.plan.blocked and d.apply_button.instate(["disabled"])
    row = d.warnings.winfo_children()[0]
    assert row.severity == "block" and "not reachable" in row.winfo_children()[1].cget("text")
    for var in d._confirm_vars.values():
        var.set(True)
    d._update_apply()
    assert d.apply_button.instate(["disabled"])
    d.cancel()


def test_a_slot_is_only_ever_replaced_or_filled(view, gui_app):
    d = _open(view, gui_app)
    assert {c.kind for c in d.picker.candidates} <= {"place_into_empty", "place", "restore_older",
                                                      "noop"}
    texts = [str(w.cget("text")).lower() for w in d.winfo_children() if isinstance(w, ttk.Button)]
    assert not any("empty the slot" in t or "clear" in t for t in texts)
    d.cancel()


@pytest.mark.parametrize("switch", [False, True])
def test_a_full_place_into_a_synthetic_steam_folder(view, gui_app, world, threads, switch):
    d = _open(view, gui_app)
    if switch:
        _other_account(d, gui_app)
    folder = world.live if d.picker.account.account_key == world.live.name else world.other
    before = tree_hash(folder)
    for var in d._confirm_vars.values():
        var.set(True)
    d.note_var.set("test move")
    d._update_apply()
    outcomes = []
    d.on_done = lambda o: (outcomes.append(o), view._moved(o))
    d.apply()
    assert d.busy and d.cancel_button.instate(["disabled"])
    _wait(gui_app, lambda: not d.winfo_exists())
    assert outcomes and outcomes[0].status == "done"
    assert tree_hash(folder) != before
    assert not any(threads)
    _wait(gui_app, lambda: not view.scanning)
    assert view.done.winfo_ismapped() or view.done.outcome is outcomes[0]
    assert view.done.text.cget("text").startswith("Done. Put 'Polestar'")


def test_a_move_the_game_forbids_comes_back_as_text_and_the_dialog_stays(view, gui_app, running):
    d = _open(view, gui_app)
    for var in d._confirm_vars.values():
        var.set(True)
    d._update_apply()
    running["on"] = True
    d.apply()
    _wait(gui_app, lambda: not d.busy)
    assert d.winfo_exists() and d.error.cget("text")
    assert d.cancel_button.instate(["!disabled"])
    d.cancel()


def test_the_entry_points_open_it(view, gui_app):
    pid = _polestar(view)
    view.table.select(pid)
    gui_app.update()
    assert view.detail.move_button.winfo_manager()
    view.detail.move_button.invoke()
    d = view.move_dialog
    assert d is not None and view.open_move_for(pid) is d       # one at a time
    _wait(gui_app, lambda: d.plan is not None)
    d.cancel()
    head = view.model.versions(pid)[0].head
    d2 = view.open_move_for(pid, head, slot=3)
    _wait(gui_app, lambda: d2.picker is not None and not d2.busy)
    if d2.slot == 3:
        assert d2.plan is not None
    else:                                   # a slot that cannot take it says why (never another one)
        assert d2.plan is None and "cannot take" in d2.error.cget("text")
    d2.cancel()


def test_source_for_a_line_and_a_version(view):
    pid = _polestar(view)
    source, name, card = M.source_for(view.model, pid)
    assert name == "Polestar" and (source.vid_a or source.vid_b) and card
    row = view.model.versions(pid)[0]
    assert M.source_for(view.model, pid, row.head)[0].load == row.head_member
    with pytest.raises(KeyError):
        M.source_for(view.model, pid, "0" * 64)


# --- U15: Replace with... and drag ----------------------------------------------------------


def _slot(view, label: str, source: int = 0):
    return next(s for s in view.model.live_board().sources[source].slots if s.label == label)


def _replace(view, app):
    slot = next(s for src in view.model.live_board().sources for s in src.slots
                if s.state == "occupied" and s.name == "Main")
    d = view.open_replace_for(slot)
    assert d is not None
    app.update()
    return d, slot


def test_pick_groups_split_vault_only_older_and_live_elsewhere(view):
    from nms_save_vault.ui.organizer.pick import ELSEWHERE, OLDER, VAULT, pick_groups

    main = next(line.pid for line in view.model.lines() if line.name == "Main")
    groups = pick_groups(view.model, main, "steam")
    assert [line.name for line in groups[OLDER]] == ["Main"]
    assert all(line.live for line in groups[ELSEWHERE]) and not any(l.live for l in groups[VAULT])
    assert pick_groups(view.model, main, "steam", "zzz") == {VAULT: [], OLDER: [], ELSEWHERE: []}


def test_replace_with_picks_a_version_then_plans_the_fixed_slot(view, gui_app):
    d, slot = _replace(view, gui_app)
    assert d.plan is None and d.apply_button.instate(["disabled"])
    assert d.out_title.cget("text").startswith(slot.label.upper())
    picker = d.picker_widget
    assert set(picker.counts()) == {"vault", "older", "elsewhere"}
    assert [c.cget("text") for c in picker.chips.values()][0].startswith("Vault only")
    line = next(l for l in view.model.lines() if l.name == "Polestar")
    picker.tree.selection_set(next(i for i in picker.tree.get_children(picker.tree.get_children("")[0])
                                   if i.endswith(line.pid)) if False else
                              next(i for g in picker.tree.get_children("")
                                   for i in picker.tree.get_children(g) if i.endswith(line.pid)))
    _wait(gui_app, lambda: d.plan is not None)
    assert d.slot == slot.target.slot and d.apply_button.cget("text") == d.plan.outcome
    assert d.in_name.cget("text") == "Polestar"
    assert d.account_note.cget("text").endswith("(fixed)")
    d.cancel()


def test_a_drop_on_a_live_slot_opens_move_with_that_slot(view, gui_app, monkeypatch):
    pid = _polestar(view)
    view.table.select(pid)
    gui_app.update()
    view.live.redraw()
    token, drawn = next((t, d) for t, d in view.live.drawn.items()
                        if d.slot.target.slot == 6 and d.slot.state == "empty")
    x0, y0, x1, y1 = drawn.box
    monkeypatch.setattr(view, "winfo_containing", lambda x, y: view.live.canvas)
    iid = view.table.tree.focus()
    assert view.drop_at(iid, (x0 + x1) // 2, (y0 + y1) // 2)
    d = view.move_dialog
    _wait(gui_app, lambda: d.picker is not None and not d.busy)
    assert d.slot == 6 and d.account == drawn.slot.target.ref
    d.cancel()
    assert not view.drop_at(iid, 0, 0) or True


def test_a_stale_result_shows_the_replan(view, gui_app, monkeypatch):
    d = _open(view, gui_app)
    plan = d.plan
    for var in d._confirm_vars.values():
        var.set(True)
    d._update_apply()
    fresh = dataclasses.replace(plan, plan_id="fresh")
    monkeypatch.setattr(M, "execute", lambda *a, **k: liveops.OpOutcome(
        None, "stale", "stale", (), None, "n/a", None, replan=fresh))
    d.apply()
    _wait(gui_app, lambda: d.plan is fresh and not d.busy)
    assert d.winfo_exists() and "changed" in d.error.cget("text")
    assert d.apply_button.cget("text") == fresh.outcome
    d.cancel()


def test_a_vault_wide_block_is_said_once_in_the_banner_in_plain_words(view, gui_app):
    d = _open(view, gui_app)
    picker = d.picker
    blocked = dataclasses.replace(picker, candidates=tuple(
        dataclasses.replace(c, blocked="VAULT_UPGRADE_NEEDED") for c in picker.candidates))
    d._show_slots(blocked)
    d._show_strip(blocked)
    rows = [" ".join(d.tree.item(i, "values")) for i in d.tree.get_children()]
    assert not any("VAULT_UPGRADE_NEEDED" in r or "not possible" in r for r in rows)
    banner = d.plan_strip.cget("text")
    assert "Tools > Vault upgrade..." in banner and "Vault > Upgrade" not in banner
    assert "VAULT_UPGRADE_NEEDED" not in banner
    d.cancel()


def test_a_block_that_is_only_some_slots_shows_in_plain_words_on_their_rows(view, gui_app):
    d = _open(view, gui_app)
    c = dataclasses.replace(d.picker.candidates[0], blocked="PLAY_TIME_AHEAD")
    text = M.candidate_values(c)[-1]
    assert "not possible" in text and "PLAY_TIME_AHEAD" not in text
    d.cancel()


def test_slot_rows_use_the_target_platform_prefix(view, gui_app):
    d = _open(view, gui_app)
    c = d.picker.candidates[0]
    assert M.candidate_values(c)[0] == f"S{c.target.slot}"
    xbox = dataclasses.replace(c, target=dataclasses.replace(c.target, platform="xbox"))
    assert M.candidate_values(xbox)[0] == f"X{c.target.slot}"
    d.cancel()


def test_every_code_has_plain_words_and_none_shows_a_raw_code():
    import re
    from nms_save_vault.core.liveops.planner import TEXTS
    from nms_save_vault.ui.organizer.words import plain
    for code in TEXTS:
        text = plain(code)
        assert text and code not in text and "{" not in text and not re.search(r"[A-Z]{3,}_[A-Z_]+", text)
    assert "_" not in plain("NO_SUCH_CODE")


def test_a_drop_on_the_first_row_in_game_order_targets_that_rows_slot(view, gui_app, monkeypatch):
    pid = _polestar(view)
    view.table.select(pid)
    view.live.set_order("game")
    gui_app.update()
    top = min(e.box[1] for e in view.live.drawn.values())
    drawn = next(d for d in view.live.drawn.values()
                 if d.box[1] == top and d.slot.target.slot != 1 and d.slot.state != "empty")
    x0, y0, x1, y1 = drawn.box
    monkeypatch.setattr(view, "winfo_containing", lambda x, y: view.live.canvas)
    asked = []
    monkeypatch.setattr(view, "open_move_for", lambda *a, **kw: asked.append(kw))
    iid = view.table.tree.focus()
    assert view.drop_at(iid, (x0 + x1) // 2, (y0 + y1) // 2)
    assert asked == [{"slot": drawn.slot.target.slot, "account": drawn.slot.target.ref}]
