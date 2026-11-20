"""organizer.notes and organizer.lineage: the owner's notes, pins and locks, and the shape
of lineage.json (D1 2.7.2, 2.9)."""
from __future__ import annotations

import json

import pytest

from nms_save_vault.core.organizer import notes as notes_mod
from nms_save_vault.core.organizer.journal import Journal
from nms_save_vault.core.organizer.lineage import ensure_skeleton, parse_lineage, read_lineage
from nms_save_vault.core.organizer.notes import NoteConflict, Notes, read_notes
from nms_save_vault.core.organizer.store import LINEAGE, NOTES, OrganizerStore

PID = "DDDAD1BDD39F9C72"
OTHER = "9E8385F0AB12CD34"


def V(n: int) -> str:
    return f"{n:064x}"


def _notes(tmp_path, start: int = 1000) -> Notes:
    now = [start]

    def clock() -> float:
        now[0] += 1
        return now[0]

    return Notes(OrganizerStore(tmp_path, create=True), clock)


# --- save-file notes ----------------------------------------------------------


def test_every_revision_of_a_save_file_note_is_kept(tmp_path):
    notes = _notes(tmp_path)
    first = notes.set_save_note(PID, "Main run.", base_rev=None)
    second = notes.set_save_note(PID, "Main run. Next: Atlas path.", base_rev=first.rev)
    third = notes.set_save_note(PID, "", base_rev=second.rev)           # cleared, not lost
    assert (first.rev, second.rev, third.rev) == (1, 2, 3)
    note = notes.save_note(PID)
    assert note.text == "" and [r.text for r in note.history] == [
        "Main run.", "Main run. Next: Atlas path."]
    assert notes.note_text_at(PID, 1) == "Main run."
    assert notes.note_text_at(PID, 3) == "" and notes.note_text_at(PID, 9) is None
    assert notes.set_save_note(PID, "", base_rev=3).rev == 3            # unchanged: no revision


def test_two_windows_editing_one_note_cannot_overwrite_each_other(tmp_path):
    gui = _notes(tmp_path)
    cli = Notes(OrganizerStore(tmp_path, create=False))
    started = gui.set_save_note(PID, "from the GUI", base_rev=None)
    cli.set_save_note(PID, "from the CLI", base_rev=started.rev)
    with pytest.raises(NoteConflict) as conflict:
        gui.set_save_note(PID, "GUI again", base_rev=started.rev)       # it read rev 1
    assert conflict.value.current.text == "from the CLI"
    assert gui.save_note(PID).text == "from the CLI"
    with pytest.raises(NoteConflict):
        Notes(OrganizerStore(tmp_path / "other", create=True)).set_save_note(
            PID, "x", base_rev=4)                                       # there is no note


def test_unicode_is_kept_and_control_characters_are_not(tmp_path):
    notes = _notes(tmp_path)
    text = "שלום\r\n旅行\r\U0001f680\x00\x07\ttab"
    saved = notes.set_save_note(PID, text, base_rev=None)
    assert saved.text == "שלום\n旅行\n\U0001f680\ttab"
    raw = (tmp_path / "organizer" / NOTES).read_bytes()
    assert raw.isascii() and b"\\u05e9" in raw                         # pure ASCII on disk
    assert read_notes(OrganizerStore(tmp_path, create=False)).save_files[PID].text == saved.text


def test_limits_are_checked_before_anything_is_written(tmp_path):
    notes = _notes(tmp_path)
    with pytest.raises(ValueError, match="at most"):
        notes.set_save_note(PID, "x" * (notes_mod.NOTE_MAX + 1), base_rev=None)
    with pytest.raises(ValueError, match="at most 80"):
        notes.set_version_note(V(1), pair=(V(1), None), pid=PID, label="y" * 81)
    with pytest.raises(ValueError, match="at most"):
        notes.set_version_note(V(1), pair=(V(1), None), pid=PID,
                               comment="z" * (notes_mod.COMMENT_MAX + 1))
    with pytest.raises(ValueError):
        notes.set_save_note("not a pid", "x", base_rev=None)
    assert read_notes(notes.store).save_files == {}


# --- pins, version notes and locks --------------------------------------------


def test_a_pin_follows_the_save_file(tmp_path):
    notes = _notes(tmp_path)
    notes.set_pinned(PID, True)
    assert notes.state().pinned_pids() == {PID}
    pinned = notes.save_note(PID)
    assert pinned.rev == 0 and pinned.text == ""                        # a pin, no note yet
    note = notes.set_save_note(PID, "keep me", base_rev=0)
    assert note.pinned and note.rev == 1
    notes.set_pinned(PID, False)
    assert notes.state().pinned_pids() == frozenset() and notes.save_note(PID).text == "keep me"
    notes.set_pinned(OTHER, False)                                      # nothing to unpin
    assert OTHER not in notes.state().save_files


def test_a_pinned_save_without_a_note_snapshots_no_revision(tmp_path):
    """New versions journal their save-file note's revision; a pin alone has none (rev 0)."""
    from nms_save_vault.core.catalog import Vault
    from nms_save_vault.core.organizer import scanner
    from nms_save_vault.core.organizer.cache import ScanCache
    from nms_save_vault.core.state import Source

    from .conftest import steam_save

    live = tmp_path / "NMS" / "st_76561198000000001"
    steam_save(live, 9, 0, "Main", 100, 0xDDDAD1BDD39F9C72, 1000)
    notes = Notes(OrganizerStore(tmp_path / "vault", create=True))
    notes.set_pinned(PID, True)
    source = Source(id="s", platform="steam", account=live.name, path=str(live))
    result = scanner.scan(Vault(tmp_path / "vault"), [source], ScanCache(None, None),
                          game_running=False)
    journal = Journal(notes.store, result.versions)
    journal.observe(result, src="scan", skip=frozenset())
    state = journal.replay()
    vid = result.captures[8].a.vid
    assert vid in state.metas and state.note_rev == {} and state.problems == ()


def test_version_notes_and_locks(tmp_path):
    notes = _notes(tmp_path)
    pair = (V(1), V(2))
    notes.set_version_note(V(1), pair=pair, pid=PID, label=" before\nthe\t10-05 session ")
    note = notes.set_version_note(V(1), pair=pair, pid=PID, comment="line one\nline two",
                                  locked=True)
    assert (note.label, note.comment, note.locked) == (
        "before the 10-05 session", "line one\nline two", True)      # label: one line
    assert notes.locked_vids() == {V(1), V(2)}                        # head and partner
    notes.set_version_note(V(1), pair=pair, pid=PID, locked=False)
    assert notes.locked_vids() == frozenset()
    assert notes.version_note(V(1)).label == "before the 10-05 session"   # kept
    with pytest.raises(ValueError):
        notes.set_version_note(V(3), pair=pair, pid=PID, label="not the head")


def test_an_entry_pin_is_kept_in_the_notes_and_cleared_by_unpinning(tmp_path):
    notes = _notes(tmp_path)
    notes.set_entry_pinned("full-steam-20261001-101500", True)
    notes.set_entry_pinned("snapshot-steam-20261002-090000", True)
    assert notes.state().pinned_entries() == {"full-steam-20261001-101500",
                                              "snapshot-steam-20261002-090000"}
    pin = notes.state().entries["full-steam-20261001-101500"]
    assert pin.pinned and pin.at > 1000
    other = Notes(OrganizerStore(tmp_path, create=False))              # another window
    other.set_entry_pinned("full-steam-20261001-101500", False)
    assert notes.state().pinned_entries() == {"snapshot-steam-20261002-090000"}
    assert "full-steam-20261001-101500" not in notes.state().entries   # no empty record left
    notes.set_entry_pinned("never-pinned", False)                      # nothing to unpin
    raw = json.loads((tmp_path / "organizer" / NOTES).read_text(encoding="ascii"))
    assert raw["entries"] == {"snapshot-steam-20261002-090000": {
        "pinned": True, "at": raw["entries"]["snapshot-steam-20261002-090000"]["at"]}}
    with pytest.raises(ValueError):
        notes.set_entry_pinned("../outside", True)


def test_a_damaged_entry_pin_does_not_hide_the_others(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    with store.transaction() as tx:
        tx.doc(NOTES)["entries"] = {"good-1": {"pinned": True, "at": 5},
                                    "bad-1": {"pinned": "yes", "at": 5},
                                    "bad-2": "pinned",
                                    "../x": {"pinned": True, "at": 5}}
    state = read_notes(store)
    assert state.pinned_entries() == {"good-1"}
    assert len(state.damaged) == 3 and all("was skipped" in d for d in state.damaged)  # named


def test_a_damaged_note_entry_does_not_hide_the_others(tmp_path):
    store = OrganizerStore(tmp_path, create=True)
    with store.transaction() as tx:
        tx.doc(NOTES)["save_files"] = {
            PID: {"text": "fine", "rev": 1, "at": 5, "pinned": False, "history": []},
            OTHER: {"text": 7, "rev": "x", "at": 1, "pinned": False, "history": []},
            "C" * 16: {"text": "no rev"},
        }
    state = read_notes(store)
    assert list(state.save_files) == [PID]


# --- lineage.json -------------------------------------------------------------


def test_the_lineage_skeleton_and_canonical_save_files():
    doc = {"schema": 1, "kind": "nmsvault.organizer.lineage"}
    ensure_skeleton(doc)
    assert (doc["corrections"], doc["aliases"], doc["splits"]) == ([], [], [])
    doc["aliases"] = [{"pid": "A" * 16, "into": "B" * 16, "at": 1},
                      {"pid": "B" * 16, "into": "C" * 16, "at": 2},
                      {"pid": "D" * 16, "into": "E" * 16, "at": 3},
                      {"pid": "E" * 16, "into": "D" * 16, "at": 4}]      # a cycle
    state = parse_lineage(doc)
    assert state.canonical("A" * 16) == "C" * 16                       # transitive
    assert state.canonical("D" * 16) in ("D" * 16, "E" * 16)           # stops, never loops
    assert state.canonical(PID) == PID and state.bootstrap is None


def test_the_bootstrap_writes_the_skeleton(tmp_path):
    from nms_save_vault.core.catalog import Vault
    from nms_save_vault.core.organizer.bootstrap import bootstrap
    from nms_save_vault.core.organizer.cache import ScanCache
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    store = OrganizerStore(vault.root, create=False)
    bootstrap(vault, store, [], ScanCache(None, None), game_running=lambda: False, clock=lambda: 7)
    raw = json.loads((vault.root / "organizer" / LINEAGE).read_text(encoding="ascii"))
    assert (raw["corrections"], raw["aliases"], raw["splits"]) == ([], [], [])
    info = read_lineage(store).bootstrap
    assert info.done_at == 7 and info.algo == 1 and info.app.startswith("nms-save-vault")
