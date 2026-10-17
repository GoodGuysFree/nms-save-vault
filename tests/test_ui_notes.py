"""The notes window and the pin (D6 U11): the save-file note with its revisions, a
version's label, comment and lock, writes in the worker, and the conflict with a note saved
meanwhile elsewhere."""
from __future__ import annotations

import time

import pytest

from nms_save_vault.core import safety
from nms_save_vault.core.organizer.notes import LABEL_MAX
from nms_save_vault.ui.organizer import notes as N

from .organizer_world import PIDS, build_world


@pytest.fixture
def world(tmp_path, make_wgs_account, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    return build_world(tmp_path, make_wgs_account)


@pytest.fixture
def window(gui_app, world):
    saved = []
    win = N.NotesWindow(gui_app, notes=world.organizer.notes, model=world.model,
                        worker=gui_app.worker, themed=gui_app.themed, pid=PIDS["main"],
                        on_saved=lambda: saved.append(1))
    win.withdraw()
    win.saved = saved
    yield win
    if win.winfo_exists():
        win.destroy()


def _wait(app, condition, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


def _type(win, text: str) -> None:
    win.editor.delete("1.0", "end")
    win.editor.insert("1.0", text)


def test_the_window_shows_the_note_and_every_version(window, world):
    assert window.title() == "Notes - Main Save"
    assert window.editor.get("1.0", "end-1c") == "Main run.\nNext: the Atlas path."
    assert window.status.cget("text").startswith("Saved (revision 1")
    assert window.revisions.get_children() == ()               # no earlier revision yet
    assert len(window.versions.get_children()) == 2
    newest = world.model.versions(PIDS["main"])[0].head
    assert window.head == newest and window.versions.selection() == (newest,)
    assert "live in Steam slot 9" in window.version_title.cget("text")


def test_saving_the_note_keeps_the_old_revision(window, world, gui_app):
    _type(window, "Main run.\nNext: base computer archive.")
    window.update()                                     # <<Modified>> arrives as an event
    assert window.dirty() and "not saved yet" in window.status.cget("text")
    window.save_note()
    _wait(gui_app, lambda: window.saved)
    note = world.organizer.notes.save_note(PIDS["main"])
    assert note.rev == 2 and note.text.endswith("base computer archive.")
    assert not window.dirty() and "revision 2" in window.status.cget("text")
    (rev,) = window.revisions.get_children()
    assert window.revisions.item(rev, "text").startswith("rev 1")


def test_a_note_saved_meanwhile_elsewhere_is_shown_and_mine_can_win(window, world, gui_app):
    world.organizer.notes.set_save_note(PIDS["main"], "From the CLI.", base_rev=1)
    _type(window, "Mine.")
    window.save_note()
    _wait(gui_app, lambda: window.conflict.winfo_manager())
    assert window.theirs.get("1.0", "end-1c") == "From the CLI."
    assert world.organizer.notes.save_note(PIDS["main"]).text == "From the CLI."
    window.keep_mine()
    _wait(gui_app, lambda: window.saved)
    note = world.organizer.notes.save_note(PIDS["main"])
    assert note.text == "Mine." and note.rev == 3
    assert [r.text for r in note.history][-1] == "From the CLI."   # theirs is kept


def test_taking_theirs_loads_it(window, world, gui_app):
    world.organizer.notes.set_save_note(PIDS["main"], "Theirs.", base_rev=1)
    _type(window, "Mine.")
    window.save_note()
    _wait(gui_app, lambda: window.conflict.winfo_manager())
    window.take_theirs()
    assert window.editor.get("1.0", "end-1c") == "Theirs." and not window.dirty()
    assert not window.conflict.winfo_manager()


def test_a_versions_label_comment_and_lock(window, world, gui_app):
    oldest = world.model.versions(PIDS["main"])[-1].head
    window.step(1)
    assert window.head == oldest
    window.label_entry.insert(0, "before the Atlas")
    window.comment.insert("1.0", "Good state.")
    window.lock_var.set(True)
    window.save_version()
    _wait(gui_app, lambda: window.saved)
    note = world.organizer.notes.version_note(oldest)
    assert (note.label, note.comment, note.locked) == ("before the Atlas", "Good state.", True)
    window.set_model(world.refresh())
    assert "before the Atlas  [locked]" in window.versions.set(oldest, "label")


def test_the_label_stops_at_80_characters_and_the_counter_warns(window):
    window.label_entry.insert(0, "x" * (LABEL_MAX + 10))
    assert len(window.label_var.get()) == 0                 # refused as a whole
    window.label_entry.insert(0, "x" * LABEL_MAX)
    assert window.counter.cget("text") == f"{LABEL_MAX} / {LABEL_MAX}"
    assert str(window.counter.cget("style")) == "Warn.TLabel"


def test_unsaved_text_is_never_dropped(window):
    window.ask = lambda *a, **k: None                 # Cancel
    _type(window, "half a thought")
    assert window.show(PIDS["other"]) is False and window.pid == PIDS["main"]
    window.close()
    assert window.winfo_exists() and "Unsaved" in window.status.cget("text")
    window.revert()
    assert window.show(PIDS["other"]) is True and window.pid == PIDS["other"]


def test_a_note_this_build_cannot_draw_is_not_edited(window, world, monkeypatch):
    world.organizer.notes.set_save_note(PIDS["main"], "Base Δω", base_rev=1)
    monkeypatch.setattr(N, "needs_latin1", lambda widget: True)
    window._load_note()
    assert window.editor.cget("state") == "disabled"
    assert window.save_button.instate(["disabled"])
    assert window.status.cget("text") == N.CANNOT_SHOW


def test_pin_and_unpin_in_the_worker(world, gui_app):
    done, errors = [], []
    N.toggle_pin(gui_app.worker, world.organizer.notes, PIDS["main"], False,
                 lambda: done.append(1), errors.append)
    _wait(gui_app, lambda: done or errors)
    assert not errors and not world.organizer.notes.save_note(PIDS["main"]).pinned


def test_text_typed_while_a_save_is_in_flight_is_kept(window, world, gui_app):
    _type(window, "first draft")
    window.save_note()
    _type(window, "first draft, and more")             # typed after Save, before it returned
    _wait(gui_app, lambda: window._sent is None)
    assert window.editor.get("1.0", "end-1c") == "first draft, and more"
    assert window.dirty() and window.loaded_text == "first draft"
    assert world.organizer.notes.save_note(PIDS["main"]).text == "first draft"
    assert str(window.save_button.state()).find("disabled") < 0


def test_closing_with_an_unsaved_note_asks_save_discard_or_cancel(window, world, gui_app):
    answers = []
    window.ask = lambda *a, **k: answers.pop(0)
    _type(window, "keep me")
    answers.append(None)                                  # Cancel
    window.close()
    assert window.winfo_exists() and window.dirty()
    answers.append(True)                                  # Save
    window.close()
    assert not window.winfo_exists()
    assert world.organizer.notes.save_note(PIDS["main"]).text == "keep me"


def test_discarding_closes_without_saving(window, world):
    window.ask = lambda *a, **k: False
    _type(window, "throw away")
    window.close()
    assert not window.winfo_exists()
    assert world.organizer.notes.save_note(PIDS["main"]).text != "throw away"


def test_closing_the_main_window_asks_about_an_unsaved_note(gui_app, world, window):
    from nms_save_vault.ui.organizer import view as V

    answers = [None]
    window.ask = lambda *a, **k: answers.pop(0)
    _type(window, "unsaved")
    holder = V.OrganizerView.__new__(V.OrganizerView)
    holder.notes_window = window
    gui_app.__dict__["organizer_view"], real = holder, gui_app.organizer_view
    try:
        gui_app.destroy()                                 # Cancel: the app stays
        assert gui_app.winfo_exists() and not answers
        answers.append(False)
        assert holder.confirm_close() is True             # Discard: the close goes on
    finally:
        gui_app.__dict__["organizer_view"] = real
