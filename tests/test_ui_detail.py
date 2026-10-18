"""The detail pane (D6 U9): the save file's summary, the version card, the card of an empty
live slot, where every version is kept, and the notes read view."""
from __future__ import annotations

import tkinter as tk

import pytest

from nms_save_vault.core import safety
from nms_save_vault.ui.organizer import detail as D
from nms_save_vault.ui.organizer.live import NEVER_EMPTIES

from .organizer_world import PIDS, build_world


@pytest.fixture
def world(tmp_path, make_wgs_account, monkeypatch):
    monkeypatch.setattr(safety, "is_game_running", lambda: False)
    return build_world(tmp_path, make_wgs_account)


@pytest.fixture
def pane(gui_app):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    calls = []
    widget = D.DetailPane(top, themed=gui_app.themed,
                          on_link=lambda pid, head: calls.append(("link", pid, head)),
                          on_notes=lambda pid, head: calls.append(("notes", pid, head)),
                          on_pin=lambda pid, pinned: calls.append(("pin", pid, pinned)))
    widget.pack(fill=tk.BOTH, expand=True)
    widget.calls = calls
    yield widget
    top.destroy()


def _plain(segments) -> str:
    return "".join(text for text, _ in segments)


def _heads(world, key):
    return [r.head for r in world.model.versions(PIDS[key])]          # newest first


# --- the words (pure) ------------------------------------------------------------------


def test_a_save_files_summary(world):
    title, summary = D.line_title(world.model, PIDS["main"])
    assert title == "Main Save" and summary.startswith("Steam slot 9 - 2 versions")
    assert "pinned" in summary
    text = _plain(D.line_summary(world.model, PIDS["main"]))
    assert "Steam slot 9  #2" in text and "NO BACKUP YET" in text
    assert "Main run." in text and "never suggested" in text


def test_the_platform_state_in_words(world):
    text = _plain(D.line_summary(world.model, PIDS["cross"]))
    assert "the Xbox copy is behind (2h00 less play than the Steam copy)" in text
    assert "Steam slot 5" in text and "Xbox slot 1" in text and "2h00 behind" in text


def test_a_vault_only_save_file(world):
    text = _plain(D.line_summary(world.model, PIDS["old"]))
    assert "not in any live slot - kept in the vault" in text


def test_the_version_card_names_its_recorded_parent(world):
    newest, oldest = _heads(world, "main")
    card = D.version_card(world.model, PIDS["main"], newest)
    text = _plain(card)
    assert "Comes from\t#1 (seen in the live slot)" in text
    assert ("#1", ("link", oldest)) in card
    assert "Live: Steam slot 9" in text and "NO BACKUP YET" in text
    first = _plain(D.version_card(world.model, PIDS["main"], oldest))
    assert "Led to\t#2" in first and "Vault: backup" in first


def test_a_version_without_a_recorded_parent_says_so(world):
    (head,) = _heads(world, "other")
    text = _plain(D.version_card(world.model, PIDS["other"], head))
    assert D.NO_PARENT in text and "Led to" not in text


def test_the_empty_slot_card(world):
    source = world.model.live_board().sources[0]
    slot1 = source.slots[0]
    title, summary = D.slot_title(source, slot1)
    assert title == "Steam slot 1  -  empty"
    assert summary == "Steam live folder - 3 of 15 slots used"
    text = _plain(D.slot_card(world.model, source, slot1))
    assert "This slot is empty." in text
    assert "Steam Cloud may fill it again" in text and NEVER_EMPTIES in text
    assert "Copy into live slot..." in text


def test_where_every_version_is_kept(world):
    text = _plain(D.where_text(world.model, PIDS["main"]))
    assert text.index("#2") < text.index("#1")                       # newest first
    assert "Live: Steam slot 9" in text and "Vault: backup" in text


def test_notes_read_view(world):
    text = _plain(D.notes_text(world.model, PIDS["main"]))
    assert text.startswith("Main run.\nNext: the Atlas path.") and "Revision 1" in text
    assert "No note yet" in _plain(D.notes_text(world.model, PIDS["other"]))
    (head,) = _heads(world, "other")
    assert "No label" in _plain(D.notes_text(world.model, PIDS["other"], head))


# --- the widget -------------------------------------------------------------------------


def test_showing_a_line_a_version_and_a_slot(pane, world):
    pane.show_line(world.model, PIDS["main"])
    assert pane.title.cget("text") == "Main Save"
    assert "Main run." in pane.text_of("summary") and "Main run." in pane.text_of("notes")
    assert pane.pin_button.winfo_manager() and pane.pin_button.cget("text") == "Unpin"
    newest, oldest = _heads(world, "main")
    pane.show_version(world.model, PIDS["main"], newest)
    assert pane.title.cget("text") == "Main Save #2"
    assert "Comes from\t#1" in pane.text_of("summary")
    assert not pane.pin_button.winfo_manager() and pane.notes_button.winfo_manager()
    source = world.model.live_board().sources[0]
    pane.show_slot(world.model, source, source.slots[0])
    assert pane.title.cget("text") == "Steam slot 1  -  empty"
    assert "Steam Cloud may fill it again" in pane.text_of("summary")
    assert not pane.notes_button.winfo_manager()


def test_links_buttons_and_pin_reach_the_callbacks(pane, world):
    newest, oldest = _heads(world, "main")
    pane.show_version(world.model, PIDS["main"], newest)
    text = pane.summary_text
    links = [t for t in text.tag_names() if t.startswith("vlink")]
    assert [text.get(*text.tag_ranges(t)[:2]) for t in links] == ["#1"]
    assert text.tk.call(text._w, "tag", "bind", links[0], "<Button-1>")   # it is bound
    pane._follow(PIDS["main"], oldest)              # what a click on "#1" calls
    pane.notes_button.invoke()
    pane.show_line(world.model, PIDS["main"])
    pane.pin_button.invoke()
    assert pane.calls == [("link", PIDS["main"], oldest), ("notes", PIDS["main"], newest),
                          ("pin", PIDS["main"], False)]


def test_the_pages_are_read_only_and_follow_the_theme(pane, world, gui_app):
    from nms_save_vault import theme

    pane.show_line(world.model, PIDS["main"])
    assert pane.summary_text.cget("state") == "disabled"
    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.DARK
        gui_app._apply_theme()
        assert str(pane.summary_text.cget("background")) == theme.PALETTES[theme.DARK]["card"]
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()
