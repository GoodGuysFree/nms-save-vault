"""The notes window (D6 2.6, 3.5) and the pin (C20): one per app, non-modal.

Two levels (C19): the save file's note - one living text with every earlier revision kept -
and, per version, a label (<= 80 characters), a comment (<= 4,000) and a lock. Writes go
through the UI worker (they take the organizer store's lock) and are checked against the
revision the editor started from: when another window or the CLI saved meanwhile, both
texts are shown and the owner keeps theirs or takes the other (``NoteConflict``).

Notes are kept in the vault (``organizer/notes.json``), never in the save files. The note
snapshot diff and the automatic "what changed" arrive with phases 4 and 3.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ... import theme
from ...core.organizer.notes import COMMENT_MAX, LABEL_MAX, NoteConflict, Notes
from ...core.organizer.readmodel import ReadModel, VersionRow
from ..dpi import fit_to_content, px, pxs
from ..fmt import local_time, play_time
from ..redact import RedactingTreeview, needs_latin1, rtitle, set_label, shown
from ..themeaware import ThemeRegistry
from ..worker import UiWorker
from .words import line_name, slot_words, version_numbers

WHERE_KEPT = ("In the vault itself (organizer/notes.json), so notes travel with the vault "
              "when you copy or import it. The save-file note keeps all its revisions; labels, "
              "comments and locks are kept per version. Notes never go into the save files, "
              "so the game never sees them.")
CANNOT_SHOW = ("This note contains characters this build cannot show. Edit it on Windows or "
               "with `nmsvault note`.")


def cannot_show(widget: tk.Misc, text: str) -> bool:
    """The Linux kit draws Latin-1 only: a note it cannot show is not edited there, so
    saving never destroys text the owner could not see (D6 3.14)."""
    return needs_latin1(widget) and any(ord(ch) > 255 for ch in text)


def toggle_pin(worker: UiWorker, notes: Notes, pid: str, pinned: bool,
               on_done: Callable[[], None],
               on_error: Callable[[BaseException], None]) -> None:
    """Pin or unpin a save file (never suggested when a slot is needed), in the worker."""
    worker.submit(notes.set_pinned, pid, pinned, on_done=lambda _r: on_done(),
                  on_error=on_error)


class NotesWindow(tk.Toplevel):
    def __init__(self, master, *, notes: Notes, model: ReadModel, worker: UiWorker,
                 themed: ThemeRegistry, pid: str, head: str | None = None,
                 on_saved: Callable[[], None] | None = None) -> None:
        super().__init__(master)
        self.notes = notes
        self.model = model
        self.worker = worker
        self.on_saved = on_saved
        self.pid = pid
        self.head: str | None = None
        self.base_rev: int | None = None
        self.loaded_text = ""
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        themed.register(self)
        self.show(pid, head)
        fit_to_content(self, 980, 640)

    # --- building ----------------------------------------------------------------------

    def _build(self) -> None:
        top = ttk.Frame(self, padding=pxs(12, 10, 12, 6))
        top.pack(side=tk.TOP, fill=tk.X)
        self.heading = ttk.Label(top, text="", style="Title.TLabel")
        self.heading.pack(side=tk.LEFT)
        ttk.Label(top, text="one living note for the save file + a label and comment per "
                            "version", style="Muted.TLabel").pack(side=tk.LEFT, padx=pxs(10, 0))
        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=px(10), pady=pxs(0, 10))
        left, right = ttk.Frame(body), ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=1)

        note = ttk.Labelframe(left, text="Save-file note (one per save file)",
                              padding=px(8))
        note.pack(fill=tk.BOTH, expand=True)
        self.editor = tk.Text(note, wrap="word", undo=True, maxundo=-1, height=7,
                              relief="flat", padx=px(8), pady=px(6))
        self.editor.pack(fill=tk.BOTH, expand=True)
        self.editor.bind("<<Modified>>", self._modified)
        row = ttk.Frame(note)
        row.pack(fill=tk.X, pady=pxs(6, 0))
        self.status = ttk.Label(row, text="", style="Muted.TLabel")
        self.status.pack(side=tk.LEFT)
        self.revert_button = ttk.Button(row, text="Revert", command=self.revert)
        self.revert_button.pack(side=tk.RIGHT)
        self.save_button = ttk.Button(row, text="Save note", command=self.save_note)
        self.save_button.pack(side=tk.RIGHT, padx=pxs(0, 6))
        self.conflict = ttk.Frame(note, style="Alert.TFrame", padding=px(6))
        ttk.Label(self.conflict, text="Saved meanwhile in another window:",
                  style="Alert.TLabel").pack(anchor="w")
        self.theirs = tk.Text(self.conflict, wrap="word", height=4, relief="flat",
                              state="disabled")
        self.theirs.pack(fill=tk.X, pady=pxs(4, 4))
        buttons = ttk.Frame(self.conflict, style="Alert.TFrame")
        buttons.pack(anchor="e")
        ttk.Button(buttons, text="Take theirs", command=self.take_theirs).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Keep mine", command=self.keep_mine).pack(
            side=tk.RIGHT, padx=pxs(0, 6))
        self._theirs_rev: int | None = None

        revisions = ttk.Labelframe(left, text="Earlier revisions of the save-file note",
                                   padding=px(8))
        revisions.pack(fill=tk.X, pady=pxs(8, 0))
        self.revisions = RedactingTreeview(revisions, columns=("text",), show="tree headings",
                                           height=4, selectmode="browse")
        self.revisions.heading("#0", text="Saved")
        self.revisions.heading("text", text="Text")
        self.revisions.column("#0", width=px(170), stretch=False)
        self.revisions.column("text", width=px(360))
        self.revisions.pack(fill=tk.X)

        listing = ttk.Labelframe(left, text="Labels and comments of every version (click one "
                                            "to edit it on the right)", padding=px(8))
        listing.pack(fill=tk.BOTH, expand=True, pady=pxs(8, 0))
        self.versions = RedactingTreeview(listing, columns=("saved", "play", "label"),
                                          show="headings", height=8, selectmode="browse")
        for key, title, width in (("saved", "Saved", 130), ("play", "Play time", 80),
                                  ("label", "Label / comment", 320)):
            self.versions.heading(key, text=title)
            self.versions.column(key, width=px(width), stretch=key == "label")
        self.versions.configure(displaycolumns=("saved", "play", "label"))
        self.versions.pack(fill=tk.BOTH, expand=True)
        self.versions.bind("<<TreeviewSelect>>", self._version_picked)

        this = ttk.Labelframe(right, text="This version", padding=px(8))
        this.pack(fill=tk.X)
        nav = ttk.Frame(this)
        nav.pack(fill=tk.X)
        self.older = ttk.Button(nav, text="< Older", command=lambda: self.step(1))
        self.older.pack(side=tk.LEFT)
        self.version_title = ttk.Label(nav, text="", style="Head.TLabel")
        self.version_title.pack(side=tk.LEFT, padx=px(10))
        self.newer = ttk.Button(nav, text="Newer >", command=lambda: self.step(-1))
        self.newer.pack(side=tk.LEFT)
        form = ttk.Frame(this)
        form.pack(fill=tk.X, pady=pxs(8, 0))
        ttk.Label(form, text="Label").grid(row=0, column=0, sticky="w")
        self.label_var = tk.StringVar()
        check = (self.register(lambda proposed: len(proposed) <= LABEL_MAX), "%P")
        self.label_entry = ttk.Entry(form, textvariable=self.label_var, validate="key",
                                     validatecommand=check)
        self.label_entry.grid(row=0, column=1, sticky="we", padx=pxs(8, 8))
        self.counter = ttk.Label(form, text=f"0 / {LABEL_MAX}", style="Muted.TLabel")
        self.counter.grid(row=0, column=2, sticky="e")
        self.label_var.trace_add("write", lambda *_: self._count())
        ttk.Label(form, text="Comment").grid(row=1, column=0, sticky="nw", pady=pxs(6, 0))
        self.comment = tk.Text(form, wrap="word", height=5, relief="flat", padx=px(6),
                               pady=px(4))
        self.comment.grid(row=1, column=1, columnspan=2, sticky="we", padx=pxs(8, 0),
                          pady=pxs(6, 0))
        form.columnconfigure(1, weight=1)
        bottom = ttk.Frame(this)
        bottom.pack(fill=tk.X, pady=pxs(8, 0))
        self.lock_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Lock this version (kept: never deleted)",
                        variable=self.lock_var).pack(side=tk.LEFT)
        self.version_save = ttk.Button(bottom, text="Save", command=self.save_version)
        self.version_save.pack(side=tk.RIGHT)
        self.version_status = ttk.Label(this, text="", style="Muted.TLabel")
        self.version_status.pack(anchor="w", pady=pxs(4, 0))

        kept = ttk.Labelframe(right, text="Where notes are kept", padding=px(8))
        kept.pack(fill=tk.BOTH, expand=True, pady=pxs(8, 0))
        ttk.Label(kept, text=WHERE_KEPT, wraplength=px(420), justify="left").pack(anchor="nw")

    def apply_palette(self, p) -> None:
        for text in (self.editor, self.comment, self.theirs):
            text.configure(background=p["field"], foreground=p["fg"], insertbackground=p["fg"],
                           selectbackground=p["sel_bg"], selectforeground=p["sel_fg"])
        self.versions.tag_configure("plain", font=theme.ITALIC_FONT, foreground=p["muted"])
        self._count()

    # --- what is shown -----------------------------------------------------------------------

    def show(self, pid: str, head: str | None = None) -> bool:
        """Edit the notes of ``pid`` (and version ``head``). Refused - False - while the
        note being edited has unsaved changes."""
        if pid != self.pid and self.dirty():
            set_label(self.status, "Save or revert the note first.")
            return False
        self.pid = pid
        line = self.model.line(pid)
        name = line_name(line) if line is not None else "(gone)"
        rtitle(self, f"Notes - {name}")
        set_label(self.heading, f"Notes - {name}")
        self._load_note()
        self._fill_versions()
        rows = self.model.versions(pid)
        self.pick_version(head if head is not None else (rows[0].head if rows else None))
        self.lift()
        return True

    def set_model(self, model: ReadModel) -> None:
        """A newer read model: the lists follow; text being edited stays."""
        self.model = model
        if not self.dirty():
            self._load_note()
        self._fill_versions()
        if self.head is not None:
            self._show_version_heading()

    def dirty(self) -> bool:
        return self.editor.get("1.0", "end-1c") != self.loaded_text

    def _load_note(self) -> None:
        note = self.notes.save_note(self.pid)
        self.loaded_text = note.text if note is not None else ""
        self.base_rev = note.rev if note is not None else None
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", self.loaded_text)
        self.editor.edit_reset()
        self.editor.edit_modified(False)
        self.conflict.pack_forget()
        if cannot_show(self, self.loaded_text):
            self.editor.configure(state="disabled")
            self.save_button.state(["disabled"])
            set_label(self.status, CANNOT_SHOW)
        else:
            self.save_button.state(["!disabled"])
            self._status_saved(note)
        for child in self.revisions.get_children():
            self.revisions.delete(child)
        for rev in reversed(note.history if note is not None else ()):
            first = next((ln for ln in rev.text.splitlines() if ln.strip()), "")
            self.revisions.insert("", "end", text=f"rev {rev.rev}  {local_time(rev.at)}",
                                  values=(first,))

    def _status_saved(self, note) -> None:
        if note is None or not note.rev:
            set_label(self.status, "No note yet.")
        else:
            set_label(self.status, f"Saved (revision {note.rev}, {local_time(note.at)})")

    def _modified(self, _event=None) -> None:
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            if self.dirty():
                current = f"current: revision {self.base_rev}" if self.base_rev else "no note yet"
                set_label(self.status, f"Editing - not saved yet ({current})")

    def _fill_versions(self) -> None:
        numbers = version_numbers(self.model, self.pid)
        self._rows: dict[str, VersionRow] = {}
        for child in self.versions.get_children():
            self.versions.delete(child)
        for row in self.model.versions(self.pid):
            text = row.label or next(iter(row.comment.splitlines()), "")
            if row.locked:
                text = f"{text}  [locked]".strip()
            self.versions.insert("", "end", row.head, text=f"#{numbers[row.head]}",
                                 values=(f"#{numbers[row.head]}  {local_time(row.saved_at)}",
                                         play_time(row.play_time), text),
                                 tags=() if (row.label or row.comment) else ("plain",))
            self._rows[row.head] = row

    # --- the save-file note -------------------------------------------------------------------

    def save_note(self) -> None:
        text = self.editor.get("1.0", "end-1c")
        self._submit_note(text, self.base_rev)

    def _submit_note(self, text: str, base_rev: int | None) -> None:
        self.save_button.state(["disabled"])
        set_label(self.status, "Saving...")
        self.worker.submit(self.notes.set_save_note, self.pid, text, base_rev=base_rev,
                           on_done=self._note_saved, on_error=self._note_failed)

    def _note_saved(self, note) -> None:
        self.save_button.state(["!disabled"])
        self.conflict.pack_forget()
        self.loaded_text, self.base_rev = note.text, note.rev
        self._status_saved(note)
        self._load_note()
        if self.on_saved is not None:
            self.on_saved()

    def _note_failed(self, error: BaseException) -> None:
        self.save_button.state(["!disabled"])
        if isinstance(error, NoteConflict):
            current = error.current
            self._theirs_rev = current.rev if current is not None else None
            self.theirs.configure(state="normal")
            self.theirs.delete("1.0", "end")
            self.theirs.insert("1.0", shown(self, current.text if current else ""))
            self.theirs.configure(state="disabled")
            self.conflict.pack(fill=tk.X, pady=pxs(6, 0))
            set_label(self.status, "Not saved: the note was changed in another window.")
        else:
            set_label(self.status, f"Not saved: {error}")

    def keep_mine(self) -> None:
        """Save this text over the other window's revision (that one stays in the history)."""
        self._submit_note(self.editor.get("1.0", "end-1c"), self._theirs_rev)

    def take_theirs(self) -> None:
        self._load_note()

    def revert(self) -> None:
        self._load_note()

    # --- the version's label, comment and lock ---------------------------------------------

    def _version_picked(self, _event=None) -> None:
        selected = self.versions.selection()
        if selected and selected[0] != self.head:
            self.pick_version(selected[0])

    def pick_version(self, head: str | None) -> None:
        self.head = head
        row = self._rows.get(head) if head is not None else None
        self.label_var.set(row.label if row else "")
        self.comment.delete("1.0", "end")
        self.comment.insert("1.0", row.comment if row else "")
        self.lock_var.set(bool(row and row.locked))
        set_label(self.version_status, "")
        state = ["!disabled"] if row is not None else ["disabled"]
        for widget in (self.label_entry, self.version_save):
            widget.state(state)
        if head is not None and self.versions.exists(head):
            self.versions.selection_set(head)
            self.versions.see(head)
        self._show_version_heading()

    def _show_version_heading(self) -> None:
        row = self._rows.get(self.head) if self.head is not None else None
        if row is None:
            set_label(self.version_title, "(no version)")
            return
        number = version_numbers(self.model, self.pid)[row.head]
        bits = [f"#{number}", f"saved {local_time(row.saved_at)}",
                play_time(row.play_time) or "0h00"]
        line = self.model.line(self.pid)
        live = [b.label for b in line.live if b.head == row.head] if line else []
        if live:
            bits.append("live in " + ", ".join(slot_words(label) for label in live))
        set_label(self.version_title, "  -  ".join(bits))
        heads = [r.head for r in self.model.versions(self.pid)]
        index = heads.index(row.head)
        self.older.state(["!disabled"] if index < len(heads) - 1 else ["disabled"])
        self.newer.state(["!disabled"] if index > 0 else ["disabled"])

    def step(self, direction: int) -> None:
        """Older (+1) or newer (-1) version."""
        heads = [r.head for r in self.model.versions(self.pid)]
        if self.head in heads:
            index = heads.index(self.head) + direction
            if 0 <= index < len(heads):
                self.pick_version(heads[index])

    def _count(self) -> None:
        n = len(self.label_var.get())
        self.counter.configure(text=f"{n} / {LABEL_MAX}",
                               style="Warn.TLabel" if n >= LABEL_MAX else "Muted.TLabel")

    def save_version(self) -> None:
        row = self._rows.get(self.head) if self.head is not None else None
        if row is None:
            return
        comment = self.comment.get("1.0", "end-1c")
        if len(comment) > COMMENT_MAX:
            set_label(self.version_status, f"A comment can be at most {COMMENT_MAX} "
                                           f"characters (this one has {len(comment)}).")
            return
        self.version_save.state(["disabled"])
        set_label(self.version_status, "Saving...")
        self.worker.submit(self.notes.set_version_note, row.head, pair=row.pair, pid=row.pid,
                           label=self.label_var.get(), comment=comment,
                           locked=self.lock_var.get(),
                           on_done=self._version_saved, on_error=self._version_failed)

    def _version_saved(self, _note) -> None:
        self.version_save.state(["!disabled"])
        set_label(self.version_status, "Saved.")
        if self.on_saved is not None:
            self.on_saved()

    def _version_failed(self, error: BaseException) -> None:
        self.version_save.state(["!disabled"])
        set_label(self.version_status, f"Not saved: {error}")

    def close(self) -> None:
        if self.dirty():
            set_label(self.status, "Unsaved changes: Save note or Revert first, then close.")
            return
        self.destroy()
