"""The correction dialog (D6 2.5, D1 2.7; U27): the owner's word over the recorded history.

For one version of one save file: **Comes from** (draw a link the app could not see, or say the
version has no known parent), **Split here** (this version and everything after it become a
save file of their own) and **Combine** (this save file becomes part of another). Below, every
correction that touches the save file, each with Undo. Nothing here touches a save file - the
corrections are the owner's notes in the vault (``organizer/lineage.json``) - so every one is
undone by removing it (``remove_correction``, ``uncombine``, ``unsplit``).

Writes run in the UI worker; a finished one reloads the view and the list below follows.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk
from typing import Any

from ...core.organizer.lineage import Corrections
from ...core.organizer.readmodel import ReadModel
from ..dpi import fit_to_content, px, pxs
from ..fmt import local_time, play_time
from ..redact import RedactingTreeview, rtitle, set_label
from ..themeaware import ThemeRegistry
from ..worker import UiWorker
from .words import line_name, version_numbers

NO_PARENT = "(nothing - it has no known parent)"
CURRENT_LINK = "(the link it has now)"


def candidate_parents(model: ReadModel, pid: str, head: str) -> list[tuple[str, str]]:
    """(label, version id) of what ``head`` could come from: every other version of the save
    file that does not come after it (newest first), and the fork points that are no head -
    restore points the branches start from."""
    numbers = version_numbers(model, pid)
    out = []
    points = [n for n in model.history(pid, "all").nodes if n.kind == "fork-member"]

    def label(row, extra: str = "") -> str:
        return (f"#{numbers[row.head]}  {local_time(row.saved_at)}  "
                f"{play_time(row.play_time) or '0h00'}{extra}")

    for row in model.versions(pid):
        if row.head == head or model.graph.descends_from(row.head, head):
            continue
        out.append((label(row), row.head))
        for node in points:
            if node.version.head == row.head                     and not model.graph.descends_from(node.vid, head):
                out.append((label(row, "  (restore point)"), node.vid))
    return out


def into_labels(model: ReadModel, pid: str) -> dict[str, str]:
    """Label -> effective save file for the Combine box. Names that collide get the
    platform, the last played date and the start of the id, so every entry is one save
    file."""
    others = [line for line in model.lines() if line.pid != pid]
    names = [line_name(line) for line in others]
    out = {}
    for line in others:
        name = line_name(line)
        if names.count(name) > 1:
            plat = "/".join("Steam" if p == "steam" else "Xbox" for p in line.platforms)
            name = (f"{name} ({plat}, last played {local_time(line.last_played)}, "
                    f"{line.pid[-6:]})")
        out[name] = line.pid
    return dict(sorted(out.items()))


def branch_of(model: ReadModel, pid: str, head: str) -> list[str]:
    """The version and everything that came after it (what a Split takes along)."""
    return sorted({v for r in model.versions(pid) for v in r.pair
                   if v and (v == head or model.graph.descends_from(v, head))})


def correction_rows(model: ReadModel, pid: str, state) -> list[tuple[tuple, str]]:
    """((kind, key), words) of every correction that touches the save file, newest last."""
    line = model.line(pid)
    raw = set(line.raw_pids) if line is not None else {pid}
    numbers = version_numbers(model, pid)

    def ver(vid: str | None) -> str:
        if vid is None:
            return "nothing"
        mine = model.line_of(vid)
        if mine == pid and vid in numbers:
            return f"#{numbers[vid]}"
        other = model.line(mine) if mine else None
        return f"a version of {line_name(other)}" if other is not None else "a missing version"

    dropped = dict(model.graph.dropped)
    out: list[tuple[tuple, str]] = []
    for c in state.corrections:
        cid = c.get("id")
        if not isinstance(cid, str):
            continue
        vids = [v for v in (c.get("child"), c.get("parent"), c.get("a"), c.get("b")) if v]
        if not any(model.line_of(v) == pid for v in vids):
            continue
        off = "" if cid not in dropped else " - not in effect (" + dropped[cid] + ")"
        if c.get("kind") == "set_parent":
            words = (f"{ver(c.get('child'))} comes from {ver(c.get('parent'))}"
                     if c.get("parent") else f"{ver(c.get('child'))} has no known parent")
        else:
            words = f"{ver(c.get('a'))} and {ver(c.get('b'))} are not related"
        out.append((("cor", cid), words + off))
    for a in state.aliases:
        if a.get("pid") in raw or a.get("into") in raw:
            out.append((("alias", a["pid"]),
                        f"save file {str(a['pid'])[:8]} was combined into this one"
                        if a.get("into") in raw else "this save file was combined into another"))
    for sp in state.splits:
        if sp.get("pid") == pid:
            out.append((("split", pid), f"split off from {str(sp.get('from'))[:8]}"))
        elif sp.get("from") in raw:
            out.append((("split", sp["pid"]), f"{len(sp.get('vids', ()))} version(s) were split "
                                              f"off as {sp.get('name') or str(sp['pid'])[-6:]}"))
    return out


class CorrectDialog(tk.Toplevel):
    """Non-modal, one per app. ``corrections`` is the organizer's ``Corrections``."""

    def __init__(self, master, *, model: ReadModel, corrections: Corrections, worker: UiWorker,
                 themed: ThemeRegistry, pid: str, head: str,
                 on_done: Callable[[], None] | None = None) -> None:
        super().__init__(master)
        self.model, self.corrections, self.worker = model, corrections, worker
        self.pid, self.head = pid, head
        self.on_done = on_done
        self.ask = messagebox.askyesno           # tests replace it
        self._busy = False
        self._undo_keys: dict[str, tuple] = {}
        self._parents: dict[str, str | None] = {}
        self._current_parent: str | None = None
        self._into: dict[str, str] = {}
        self._build()
        themed.register(self)
        self.show(pid, head)
        fit_to_content(self, 620, 520)

    # --- building ----------------------------------------------------------------------

    def _build(self) -> None:
        pad = pxs(12, 8, 12, 4)
        self.heading = ttk.Label(self, text="", style="Title.TLabel", padding=pad)
        self.heading.pack(fill=tk.X)
        ttk.Label(self, text="Your word over the record. Nothing here changes a save file; "
                             "every correction can be undone below.", style="Muted.TLabel",
                  padding=pxs(12, 0, 12, 6), wraplength=px(560)).pack(fill=tk.X)

        box = ttk.Labelframe(self, text="Comes from", padding=px(8))
        box.pack(fill=tk.X, padx=px(12), pady=pxs(0, 6))
        self.parent_box = ttk.Combobox(box, state="readonly", width=46)
        self.parent_box.pack(side=tk.LEFT)
        self.parent_button = ttk.Button(box, text="Set", command=self.apply_parent)
        self.parent_button.pack(side=tk.LEFT, padx=pxs(8, 0))

        box = ttk.Labelframe(self, text="Split here", padding=px(8))
        box.pack(fill=tk.X, padx=px(12), pady=pxs(0, 6))
        self.split_info = ttk.Label(box, text="", style="Muted.TLabel", wraplength=px(540))
        self.split_info.pack(fill=tk.X)
        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=pxs(4, 0))
        ttk.Label(row, text="Name").pack(side=tk.LEFT)
        self.split_name = tk.StringVar()
        ttk.Entry(row, textvariable=self.split_name, width=30).pack(side=tk.LEFT, padx=px(8))
        self.split_button = ttk.Button(row, text="Split...", command=self.apply_split)
        self.split_button.pack(side=tk.LEFT)

        box = ttk.Labelframe(self, text="Combine this save file into another", padding=px(8))
        box.pack(fill=tk.X, padx=px(12), pady=pxs(0, 6))
        self.combine_box = ttk.Combobox(box, state="readonly", width=46)
        self.combine_box.pack(side=tk.LEFT)
        self.combine_button = ttk.Button(box, text="Combine...", command=self.apply_combine)
        self.combine_button.pack(side=tk.LEFT, padx=pxs(8, 0))

        box = ttk.Labelframe(self, text="What you told the app about this save file",
                             padding=px(8))
        box.pack(fill=tk.BOTH, expand=True, padx=px(12), pady=pxs(0, 6))
        self.existing = RedactingTreeview(box, columns=(), show="tree", height=4,
                                          selectmode="browse")
        self.existing.pack(fill=tk.BOTH, expand=True)
        self.undo_button = ttk.Button(box, text="Undo the selected", command=self.undo)
        self.undo_button.pack(anchor="e", pady=pxs(4, 0))
        bottom = ttk.Frame(self, padding=pxs(12, 0, 12, 8))
        bottom.pack(fill=tk.X)
        self.status = ttk.Label(bottom, text="", style="Muted.TLabel", wraplength=px(440))
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side=tk.RIGHT)

    def apply_palette(self, _p) -> None:
        pass

    # --- what is shown ------------------------------------------------------------------

    def show(self, pid: str, head: str) -> None:
        self.pid, self.head = pid, head
        self._fill()

    def set_model(self, model: ReadModel) -> None:
        self.model = model
        if model.line(self.pid) is None:
            self.destroy()
            return
        self._fill()

    def _fill(self) -> None:
        model, pid, head = self.model, self.pid, self.head
        line = model.line(pid)
        numbers = version_numbers(model, pid)
        name = line_name(line) if line is not None else "(gone)"
        rtitle(self, f"Correct the history - {name}")
        set_label(self.heading, f"{name}, version #{numbers.get(head, '?')}")
        current = next((r for r in model.versions(pid) if r.head == head), None)
        options = candidate_parents(model, pid, head)
        self._parents = {NO_PARENT: None, **{label: h for label, h in options}}
        self.parent_box.configure(values=list(self._parents))
        shown = NO_PARENT
        self._current_parent = current.parent if current is not None else None
        if self._current_parent is not None:
            shown = next((lbl for lbl, h in options if h == self._current_parent), None)
            if shown is None:           # a real link that cannot be listed: never erase it
                shown = CURRENT_LINK
                self._parents[CURRENT_LINK] = self._current_parent
                self.parent_box.configure(values=list(self._parents))
        self.parent_box.set(shown)
        branch = branch_of(model, pid, head)
        row_count = sum(1 for r in model.versions(pid) if any(v in branch for v in r.pair if v))
        set_label(self.split_info, f"This version and the {max(row_count - 1, 0)} after it become "
                                   "a save file of their own.")
        self._into = into_labels(model, pid)
        self.combine_box.configure(values=list(self._into))
        self.combine_box.set("")
        self._fill_existing()
        self._enable()

    def _fill_existing(self) -> None:
        tree = self.existing
        tree.delete(*tree.get_children(""))
        self._undo_keys.clear()
        for n, (key, words) in enumerate(correction_rows(self.model, self.pid,
                                                         self.corrections.state())):
            iid = f"C{n}"
            tree.insert("", "end", iid, text=words)
            self._undo_keys[iid] = key
        self.undo_button.state(["!disabled" if self._undo_keys and not self._busy
                                else "disabled"])

    def _enable(self) -> None:
        state = ["disabled"] if self._busy else ["!disabled"]
        for b in (self.parent_button, self.split_button, self.combine_button):
            b.state(state)

    # --- writing ------------------------------------------------------------------------

    def _run(self, fn: Callable[[], Any], done: str) -> None:
        if self._busy:
            return
        self._busy = True
        self._enable()
        set_label(self.status, "Saving...")

        def finished(_result) -> None:
            self._busy = False
            if self.winfo_exists():
                set_label(self.status, done)
                self._enable()
                self._fill_existing()
            if self.on_done is not None:        # written: the view reloads, closed or not
                self.on_done()

        def failed(error: BaseException) -> None:
            self._busy = False
            if self.winfo_exists():
                set_label(self.status, f"Not done: {error}")
                self._enable()
                self._fill_existing()

        self.worker.submit(fn, on_done=finished, on_error=failed)

    def apply_parent(self) -> None:
        parent = self._parents.get(self.parent_box.get())
        model, head = self.model, self.head
        if parent == self._current_parent:
            set_label(self.status, "It already comes from that.")
            return
        self._run(lambda: self.corrections.set_parent(head, parent, graph=model.graph,
                                                      line_of=model.line_of),
                  "Saved: this version comes from "
                  + ("nothing." if parent is None else f"{self.parent_box.get().split()[0]}."))

    def apply_split(self) -> None:
        model, pid, head = self.model, self.pid, self.head
        raw = model.raw_pid_of(head)
        branch = branch_of(model, pid, head)
        if raw is None or not branch:
            set_label(self.status, "That version is not known to this vault.")
            return
        name = self.split_name.get().strip()
        if not self.ask("Split", "Split this version and everything after it into a save file "
                                 "of its own?\n\nNothing in the save files changes; you can "
                                 "undo it below.", parent=self):
            return
        self._run(lambda: self.corrections.split(raw, branch, name), "Split. It is a save "
                                                                   "file of its own now.")

    def apply_combine(self) -> None:
        into = self._into.get(self.combine_box.get())
        if into is None:
            set_label(self.status, "Pick the save file to combine this one into.")
            return
        pid = self.pid
        line = self.model.line(pid)
        if not self.ask("Combine", f"Combine this save file into {self.combine_box.get()}?\n\n"
                                   "Their notes are merged into one; you can undo it below.",
                        parent=self):
            return
        names = {pid: line_name(line) if line is not None else pid[:8]}
        self._run(lambda: self.corrections.combine([pid], into, names=names),
                  f"Combined into {self.combine_box.get()}.")

    def undo(self) -> None:
        selected = self.existing.selection()
        key = self._undo_keys.get(selected[0]) if selected else None
        if key is None:
            set_label(self.status, "Select a correction to undo.")
            return
        kind, value = key
        action = {"cor": self.corrections.remove_correction, "alias": self.corrections.uncombine,
                  "split": self.corrections.unsplit}[kind]
        self._run(lambda: action(value), "Undone.")
