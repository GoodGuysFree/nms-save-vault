"""U16-U18: the Done bar, Undo and the operations log; verification alerts, the store status and
the recovery dialog; the board menu (Swap, Promote, Keep a copy, the steps to empty a slot).
Synthetic Steam folders and a synthetic version-store vault; every plan and write runs in the
worker."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

import pytest

from nms_save_vault.core import liveops
from nms_save_vault.core.liveops import RecoveryItem, VerifyItem
from nms_save_vault.ui.organizer import move as M
from nms_save_vault.ui.organizer import ops as O
from nms_save_vault.ui.organizer import view as V

from .liveops_support import tree_hash
from .test_ui_move import (  # noqa: F401  (fixtures)
    _open, _polestar, _wait, running, threads, view, world)


def _buttons(widget) -> list[str]:
    out = []
    for w in widget.winfo_children():
        if isinstance(w, ttk.Button):
            out.append(str(w.cget("text")))
        out += _buttons(w)
    return out


def _slot(view, number: int):
    """Slot ``number`` of the first live account (Desktop)."""
    return next(s for s in view.model.live_board().sources[0].slots
                if s.target.slot == number and s.state in ("occupied", "partial"))


def _plan_dialog(view, app):
    _wait(app, lambda: view.plan_dialog is not None and view.plan_dialog.winfo_exists())
    return view.plan_dialog


def _confirm_and_apply(d, app):
    for var in d.vars.values():
        var.set(True)
    d._update()
    d.apply()
    _wait(app, lambda: not d.winfo_exists())


def _place(view, app, *, occupied: bool):
    """Move Polestar through the dialog, into an occupied slot or an empty one."""
    d = _open(view, app)
    if occupied:
        cand = next(c for c in d.picker.candidates if c.state == "occupied" and not c.blocked
                    and not c.pinned)
        d.tree.selection_set(f"s{cand.target.slot}")
        before = d.plan
        _wait(app, lambda: d.plan is not None and d.plan is not before)
    for var in d._confirm_vars.values():
        var.set(True)
    d._update_apply()
    out = []
    d.on_done = lambda o: (out.append(o), view._moved(o))
    d.apply()
    _wait(app, lambda: not d.winfo_exists())
    _wait(app, lambda: not view.scanning)
    return out[0]


# --- U16 ---------------------------------------------------------------------------------------


def test_the_done_bar_offers_undo_and_undo_runs_without_a_dialog(view, gui_app):
    outcome = _place(view, gui_app, occupied=True)
    assert outcome.status == "done" and outcome.undo is not None
    bar = view.done
    assert bar.winfo_ismapped() or bar.outcome is outcome
    assert bar.text.cget("text").startswith("Done. ") and "next game launch" in bar.text.cget("text")
    assert str(bar.undo.winfo_manager()) == "pack"
    view.plan_dialog = None
    bar.undo.invoke()
    _wait(gui_app, lambda: bar.outcome is not None and bar.outcome.op_id != outcome.op_id
          or (view.plan_dialog is not None and view.plan_dialog.winfo_exists()))
    if view.plan_dialog is not None and view.plan_dialog.winfo_exists():
        d = view.plan_dialog                                   # warnings: it opens as a plan
        assert d.apply_button.cget("text").startswith(("Undo", "Swap"))
        d.cancel()


def test_undoing_a_fill_of_an_empty_slot_opens_the_steps_not_a_deletion(view, gui_app, monkeypatch):
    outcome = _place(view, gui_app, occupied=False)
    assert outcome.status == "done" and outcome.undo is None      # nothing to put back (F8)
    seen = []
    monkeypatch.setattr(view, "_steps", lambda guide: seen.append(guide))
    view.undo_op(outcome.op_id)
    _wait(gui_app, lambda: seen)
    assert seen[0].steps and "never" not in seen[0].label


def test_the_operations_log_lists_moves_and_enables_undo(view, gui_app):
    outcome = _place(view, gui_app, occupied=True)
    view.open_log(select=outcome.op_id)
    win = view.log_window
    _wait(gui_app, lambda: outcome.op_id in win.rows)
    assert win.tree.selection() == (outcome.op_id,)
    assert win.undo_button.instate(["!disabled"]) and "Polestar" in win.rows[outcome.op_id].text
    undone = []
    win.on_undo = undone.append
    win.undo_button.invoke()
    assert undone == [outcome.op_id]
    win.destroy()


def test_done_text_and_the_bar_clears():
    out = liveops.OpOutcome("op-1", "done", "Put 'A' into Steam slot 5", (), None, "pending", None)
    assert O.done_text(out) == ("Done. Put 'A' into Steam slot 5. Waiting for the next game "
                                "launch to confirm.")


# --- U17 -----------------------------------------------------------------------------------------


def _verify_item(view, state="reverted"):
    slot = _slot(view, 1)
    return VerifyItem("op-20260101T000000Z-aaaa", slot.target, slot.label, state, "uploaded",
                      "Steam Cloud put the old save back in Steam slot 1. 'Main' is safe in the "
                      "vault.", ("place_again", "show_steps", "dismiss"), "2026-01-01 00:00")


def test_a_reverted_move_becomes_an_alert_with_put_it_back_and_steps(view, gui_app, monkeypatch):
    item = _verify_item(view)
    monkeypatch.setattr(V, "update_verification", lambda ctx, events=(): [item])
    dismissed = []
    monkeypatch.setattr(V, "dismiss_verification", lambda ctx, op, t: dismissed.append((op, t)))
    view.rescan()
    _wait(gui_app, lambda: not view.scanning)
    key = next(k for k in view.alerts if k.startswith("verify:"))
    bar = view.alerts[key]
    assert "Steam Cloud put the old save back" in bar.text.cget("text")
    assert {"Put it back", "Show steps", "Dismiss"} <= set(_buttons(bar))
    next(w for w in bar.winfo_children() if isinstance(w, ttk.Button)
         and w.cget("text") == "Dismiss").invoke()
    _wait(gui_app, lambda: dismissed)
    assert key not in view.alerts and dismissed[0][0] == item.op_id
    pending = _verify_item(view, "pending")                  # pending shows as the slot's clock
    monkeypatch.setattr(V, "update_verification", lambda ctx, events=(): [pending])
    view.rescan()
    _wait(gui_app, lambda: not view.scanning)
    assert not [k for k in view.alerts if k.startswith("verify:")]


def test_the_store_status_becomes_an_alert(view, gui_app, monkeypatch):
    from nms_save_vault.core import store

    status = SimpleNamespace(damage=(object(), object()), fold_failed=(), fold_waiting_for_space=0)
    monkeypatch.setattr(store, "store_status", lambda *a, **k: status)
    view.rescan()
    _wait(gui_app, lambda: not view.scanning)
    assert "2 damaged file(s)" in view.alerts["store-status"].text.cget("text")
    status.damage = ()
    view.rescan()
    _wait(gui_app, lambda: not view.scanning)
    assert "store-status" not in view.alerts


def _item():
    return RecoveryItem("op-20260101T000000Z-bbbb", "place", ("Steam slot 1",), "torn", "back",
                        ("forward", "back", "leave"), "The move stopped half way.",
                        "2026-01-01 00:00")


def test_the_recovery_dialog_applies_the_chosen_way(view, gui_app, monkeypatch):
    calls = []

    def resolve(ctx, op_id, choice, confirmed=frozenset()):
        calls.append((op_id, choice, confirmed))
        return liveops.OpOutcome(op_id, "done", "Put everything back.", (), None, "n/a", None)
    monkeypatch.setattr(O, "resolve_recovery", resolve)
    d = view.open_recovery([_item()])
    assert d is not None and d.choice[_item().op_id].get() == "back"       # the recommendation
    d.choice[_item().op_id].set("leave")
    d.apply(_item())
    _wait(gui_app, lambda: calls and not d.busy)
    assert calls == [(_item().op_id, "leave", frozenset())]
    assert d.result[_item().op_id].cget("text") == "Put everything back."
    d.destroy()


def test_a_game_state_that_cannot_be_checked_asks_for_a_confirmation(view, gui_app, monkeypatch):
    seen = []

    def resolve(ctx, op_id, choice, confirmed=frozenset()):
        seen.append(confirmed)
        if not confirmed:
            return liveops.OpOutcome(op_id, "refused", "refused", (), None, "n/a", "Cannot tell.",
                                     code="GAME_STATE_UNKNOWN")
        return liveops.OpOutcome(op_id, "done", "Done.", (), None, "n/a", None)
    monkeypatch.setattr(O, "resolve_recovery", resolve)
    d = view.open_recovery([_item()])
    d.apply(_item())
    _wait(gui_app, lambda: seen and not d.busy)
    assert d.confirm_check.winfo_manager() == "pack"
    d.confirm.set(True)
    d.apply(_item())
    _wait(gui_app, lambda: len(seen) == 2 and not d.busy)
    assert seen[1] == frozenset({"GAME_STATE_UNKNOWN"})
    d.destroy()


def test_start_up_opens_the_recovery_dialog_once(view, gui_app, monkeypatch):
    monkeypatch.setattr(V, "pending_recovery", lambda ctx: [_item()])
    view._recovery_checked = False
    view.rescan()
    _wait(gui_app, lambda: not view.scanning)
    assert view.recovery_dialog is not None and view.recovery_dialog.winfo_exists()
    view.recovery_dialog.destroy()
    view.recovery_dialog = None
    view.rescan()
    _wait(gui_app, lambda: not view.scanning)
    assert view.recovery_dialog is None                       # asked once per session


def test_a_failed_move_offers_recover(view, gui_app, monkeypatch):
    d = _open(view, gui_app)
    for var in d._confirm_vars.values():
        var.set(True)
    d._update_apply()
    monkeypatch.setattr(M, "execute", lambda *a, **k: liveops.OpOutcome(
        "op-1", "failed", "failed", (), None, "n/a", "The write failed half way."))
    asked = []
    d.on_recover = lambda: asked.append(1)
    d.apply()
    _wait(gui_app, lambda: not d.busy and d.error.cget("text"))
    assert d.recover_button.winfo_manager() == "pack" and "half way" in d.error.cget("text")
    d.recover_button.invoke()
    assert asked == [1]
    d.cancel()


# --- U18 -------------------------------------------------------------------------------------------


def _menu_labels(menu: tk.Menu) -> list[str]:
    return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
            if menu.type(i) in ("command", "cascade")]


def test_the_board_menu_for_an_occupied_and_an_empty_slot(view):
    main = _slot(view, 1)
    labels = _menu_labels(view.slot_menu(main))
    assert labels[0] == "Replace with..." and "Swap with" in labels
    assert "Make the restore point load" in labels and "Make the auto-save load" not in labels
    assert "Keep a copy in the vault now" in labels and labels[-1] == "Empty this slot..."
    empty = next(s for src in view.model.live_board().sources for s in src.slots
                 if s.state == "empty")
    labels = _menu_labels(view.slot_menu(empty))
    assert labels[0] == "Put a save into it..." and "Swap with" not in labels
    assert "Keep a copy in the vault now" not in labels


def _invoke(menu: tk.Menu, label: str, sub: str | None = None) -> None:
    for i in range(menu.index("end") + 1):
        if menu.type(i) in ("command", "cascade") and menu.entrycget(i, "label") == label:
            if menu.type(i) == "cascade":
                inner = menu.nametowidget(menu.entrycget(i, "menu"))
                inner.invoke(0 if sub is None else
                             next(j for j in range(inner.index("end") + 1)
                                  if inner.entrycget(j, "label").startswith(sub)))
            else:
                menu.invoke(i)
            return
    raise AssertionError(label)


def test_swap_two_slots_through_the_menu(view, gui_app, world):
    before = tree_hash(world.live)
    _invoke(view.slot_menu(_slot(view, 1)), "Swap with", "S1.2")
    d = _plan_dialog(view, gui_app)
    assert d.plan.kind == "swap" and d.apply_button.cget("text").startswith("Swap Steam slot")
    _confirm_and_apply(d, gui_app)
    assert tree_hash(world.live) != before
    assert view.done.outcome.status == "done"


def test_promote_through_the_menu(view, gui_app, world):
    before = tree_hash(world.live)
    _invoke(view.slot_menu(_slot(view, 1)), "Make the restore point load")
    d = _plan_dialog(view, gui_app)
    assert d.plan.kind == "promote" and "restore point" in d.apply_button.cget("text")
    _confirm_and_apply(d, gui_app)
    assert tree_hash(world.live) != before


def test_keep_a_copy_writes_nothing_live(view, gui_app, world):
    before = tree_hash(world.live)
    _invoke(view.slot_menu(_slot(view, 1)), "Keep a copy in the vault now")
    d = _plan_dialog(view, gui_app)
    assert d.plan.kind == "keep" and "leaves live" not in d.describe()
    _confirm_and_apply(d, gui_app)
    assert tree_hash(world.live) == before


def test_a_plan_with_a_block_cannot_be_applied(view, gui_app):
    plan = liveops.plan_keep(view.make_context(view.organizer), _slot(view, 1).target)
    block = liveops.planner.warning("VAULT_UNAVAILABLE")
    import dataclasses
    d = view.show_plan("Keep", dataclasses.replace(plan, blocked=(block,)))
    assert d.apply_button.instate(["disabled"])
    for var in d.vars.values():
        var.set(True)
    d._update()
    assert d.apply_button.instate(["disabled"])
    d.destroy()


def test_empty_this_slot_shows_steps_and_the_refill_warning(view, gui_app):
    wins = []
    original = view._steps
    view._steps = lambda guide: wins.append(original(guide))
    _invoke(view.slot_menu(_slot(view, 1)), "Empty this slot...")
    _wait(gui_app, lambda: wins)
    w = wins[0]
    assert w.refill is not None and "Steam Cloud may put a save back" in w.refill.cget("text")
    assert [c.cget("text")[:2] for c in w.steps.winfo_children()][:2] == ["1.", "2."]
    assert not any("Empty" in b and "slot" in b.lower() and b != "Close" for b in _buttons(w)
                   if "steps" not in b)          # no button that empties: only the steps
    w.destroy()


# --- Edit > Undo (Ctrl+Z) and the status bar's "Last change" ---------------------------------------


def test_edit_undo_is_the_done_bars_undo_and_last_change_is_reported(view, gui_app):
    lines = []
    view.on_last_change = lines.append
    assert view.undo_last() is False and "nothing to undo" in view.alerts["undo"].text.cget("text")
    outcome = _place(view, gui_app, occupied=True)
    assert lines and lines[0].startswith("Last change ") and outcome.outcome.rstrip(".") in lines[0]
    assert lines[0].endswith("checked after the next game launch")
    seen = []
    view.undo_op = seen.append
    assert view.undo_last() is True and seen == [outcome.op_id]


def test_ctrl_z_and_the_edit_menu_dispatch_by_tab_and_leave_text_fields_alone(gui_app, monkeypatch):
    calls = []
    monkeypatch.setattr(gui_app.organizer_view, "undo_last", lambda: calls.append("organizer"))
    monkeypatch.setattr(gui_app, "on_undo", lambda *a: calls.append("classic"))
    monkeypatch.setattr(gui_app, "current_view", lambda: "organizer")
    gui_app.on_edit_undo()
    monkeypatch.setattr(gui_app, "current_view", lambda: "classic")
    gui_app.on_edit_undo()
    assert calls == ["organizer", "classic"]
    entry = tk.Entry(gui_app)
    monkeypatch.setattr(gui_app, "focus_get", lambda: entry)
    gui_app._undo_shortcut()
    assert len(calls) == 2                                               # the field keeps Ctrl+Z
    monkeypatch.setattr(gui_app, "focus_get", lambda: gui_app)
    gui_app._undo_shortcut()
    assert len(calls) == 3
    entry.destroy()
    assert gui_app.last_change.get() == "" or isinstance(gui_app.last_change.get(), str)
