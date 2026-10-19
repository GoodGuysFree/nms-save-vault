"""Notes (D1 2.9): the owner's words about his saves, kept in the vault (``notes.json``).

- a living **save-file note** per playthrough, with every earlier revision kept, and a
  **pin** (never suggest evicting this save from its slot);
- a **version note** per version (keyed by the head of its pair): a label, a comment and a
  lock (the owner's "keep" mark).

Each new version journals the save-file note's revision as it was (``note_revs``), so the
note can be read back as it stood at any version. Text is stored as Unicode; two windows
editing one note at once cannot overwrite each other (``base_rev``).
"""
from __future__ import annotations

import logging
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .model import EMPTY_PAIR, ModelError, PairKey, Pid, Vid, is_pid, is_vid, pair_from_json
from .store import NOTES, OrganizerStore

log = logging.getLogger(__name__)

LABEL_MAX = 80
COMMENT_MAX = 4000
NOTE_MAX = 20000


@dataclass(frozen=True)
class NoteRevision:
    rev: int
    at: int
    text: str


@dataclass(frozen=True)
class SaveNote:
    pid: Pid
    text: str
    rev: int                    # 0: no text yet (a pin alone)
    at: int
    pinned: bool
    history: tuple[NoteRevision, ...]   # every earlier revision, oldest first


@dataclass(frozen=True)
class VersionNote:
    head: Vid
    label: str
    comment: str
    locked: bool
    pair: PairKey
    pid: Pid
    at: int


@dataclass(frozen=True)
class NotesState:
    save_files: Mapping[Pid, SaveNote]
    versions: Mapping[Vid, VersionNote]

    def locked_vids(self) -> frozenset[Vid]:
        """Every member of a locked version: its head and its partner."""
        return frozenset(v for n in self.versions.values() if n.locked for v in n.pair if v)

    def pinned_pids(self) -> frozenset[Pid]:
        return frozenset(pid for pid, n in self.save_files.items() if n.pinned)


class NoteConflict(Exception):
    """The note changed since the editor read it (another window saved first)."""

    def __init__(self, current: SaveNote | None) -> None:
        super().__init__("The note was changed in another window; reload it before saving.")
        self.current = current


def read_notes(store: OrganizerStore) -> NotesState:
    return parse_notes(store.document(NOTES))


def parse_notes(doc: Mapping[str, Any]) -> NotesState:
    """The notes in ``doc``; an entry that is not in the expected form is skipped (and
    logged) rather than taking every other note down with it."""
    files: dict[Pid, SaveNote] = {}
    for pid, raw in (doc.get("save_files") or {}).items():
        try:
            files[pid] = _save_note(pid, raw)
        except (ModelError, KeyError, TypeError, ValueError) as exc:
            log.warning("notes.json: the note of %s was skipped: %s", pid, exc)
    versions: dict[Vid, VersionNote] = {}
    for head, raw in (doc.get("versions") or {}).items():
        try:
            versions[head] = _version_note(head, raw)
        except (ModelError, KeyError, TypeError, ValueError) as exc:
            log.warning("notes.json: the note of version %s was skipped: %s", head[:8], exc)
    return NotesState(files, versions)


class Notes:
    def __init__(self, store: OrganizerStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def state(self) -> NotesState:
        return read_notes(self.store)

    # --- save-file notes --------------------------------------------------------------

    def save_note(self, pid: Pid) -> SaveNote | None:
        return self.state().save_files.get(pid)

    def set_save_note(self, pid: Pid, text: str, *, base_rev: int | None) -> SaveNote:
        """Store a new revision of the save file's note. ``base_rev`` is the revision the
        editor started from (None or 0: there was no note); when the stored note is newer,
        nothing is written and :class:`NoteConflict` carries the current note."""
        _check_pid(pid)
        text = clean_text(text, NOTE_MAX, "a save-file note")
        with self.store.transaction() as tx:
            files = tx.doc(NOTES).setdefault("save_files", {})
            current = files.get(pid)
            current_rev = current["rev"] if current else 0
            if (base_rev or 0) != current_rev:
                raise NoteConflict(_save_note(pid, current) if current else None)
            if text == (current["text"] if current else ""):      # nothing new: no revision
                return _save_note(pid, current) if current else SaveNote(pid, "", 0, 0, False, ())
            history = list(current["history"]) if current else []
            if current_rev:
                history.append({"rev": current_rev, "at": current["at"], "text": current["text"]})
            files[pid] = {"text": text, "rev": current_rev + 1, "at": int(self.clock()),
                          "pinned": bool(current and current["pinned"]), "history": history}
            return _save_note(pid, files[pid])

    def note_text_at(self, pid: Pid, rev: int) -> str | None:
        """The save-file note as it read at revision ``rev``."""
        note = self.save_note(pid)
        if note is None:
            return None
        if rev == note.rev:
            return note.text
        return next((r.text for r in note.history if r.rev == rev), None)

    def set_pinned(self, pid: Pid, pinned: bool) -> None:
        _check_pid(pid)
        with self.store.transaction() as tx:
            files = tx.doc(NOTES).setdefault("save_files", {})
            if pid in files:
                files[pid]["pinned"] = bool(pinned)
            elif pinned:
                files[pid] = {"text": "", "rev": 0, "at": int(self.clock()), "pinned": True,
                              "history": []}

    # --- version notes ----------------------------------------------------------------

    def version_note(self, head: Vid) -> VersionNote | None:
        return self.state().versions.get(head)

    def set_version_note(self, head: Vid, *, pair: PairKey, pid: Pid, label: str | None = None,
                         comment: str | None = None, locked: bool | None = None) -> VersionNote:
        """Change what is given (None leaves a field as it is)."""
        if not is_vid(head) or head not in pair:
            raise ValueError("a version note belongs to the head of its pair")
        _check_pid(pid)
        with self.store.transaction() as tx:
            versions = tx.doc(NOTES).setdefault("versions", {})
            current = versions.get(head, {"label": "", "comment": "", "locked": False})
            entry = {
                "label": current["label"] if label is None else clean_label(label),
                "comment": (current["comment"] if comment is None
                            else clean_text(comment, COMMENT_MAX, "a comment")),
                "locked": current["locked"] if locked is None else bool(locked),
                "pair": list(pair), "pid": pid, "at": int(self.clock()),
            }
            versions[head] = entry
            return _version_note(head, entry)

    def locked_vids(self) -> frozenset[Vid]:
        return self.state().locked_vids()


# --- text ---------------------------------------------------------------------


def clean_text(text: str, limit: int, what: str) -> str:
    """Newlines as ``\\n``; control characters other than newline and tab dropped. Unicode
    is kept as it is (the UI draws what its font can)."""
    if not isinstance(text, str):
        raise ValueError(f"{what} must be text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")
    if len(text) > limit:
        raise ValueError(f"{what} can be at most {limit} characters (this one has {len(text)})")
    return text


def clean_label(text: str) -> str:
    """A label is one line."""
    cleaned = clean_text(text, 10 * LABEL_MAX, "a label").replace("\n", " ").replace("\t", " ")
    cleaned = cleaned.strip()
    if len(cleaned) > LABEL_MAX:
        raise ValueError(f"a label can be at most {LABEL_MAX} characters "
                         f"(this one has {len(cleaned)})")
    return cleaned


# --- parsing ------------------------------------------------------------------


def _save_note(pid: Pid, raw: Mapping[str, Any]) -> SaveNote:
    _check_pid(pid)
    history = tuple(NoteRevision(_int(h["rev"]), _int(h["at"]), _str(h["text"]))
                    for h in raw["history"])
    return SaveNote(pid, _str(raw["text"]), _int(raw["rev"]), _int(raw["at"]),
                    bool(raw["pinned"]), history)


def _version_note(head: Vid, raw: Mapping[str, Any]) -> VersionNote:
    if not is_vid(head):
        raise ModelError(f"not a version id: {head!r}")
    pid = _str(raw["pid"])
    _check_pid(pid)
    pair = pair_from_json(raw["pair"]) if raw.get("pair") is not None else EMPTY_PAIR
    return VersionNote(head, _str(raw["label"]), _str(raw["comment"]), bool(raw["locked"]),
                       pair, pid, _int(raw["at"]))


def _check_pid(pid: object) -> None:
    if not is_pid(pid):
        raise ValueError(f"not a playthrough id: {pid!r}")


def _int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"not a count or time: {value!r}")
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"not text: {value!r}")
    return value
