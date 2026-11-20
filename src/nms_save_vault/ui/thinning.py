"""Thin the vault... (docs/thinning.md "The apps"; the owner's Q4: "we must show what we're going
to delete", Q5): the list window. It plans in the UI worker, shows one ticked row per backup the
schedule lets go (date, kind, its saves, the space, why) and, below, what stays although the
schedule alone would drop it. Nothing is deleted until the owner presses Delete; the run goes in
the worker with a progress bar, then the window says what went and what was left, and why.

Every job is a closure over plain data (the vault folder, the plan, the ticked ids), never over
a widget; progress is polled, not pushed.
"""
from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

from ..core.store import thinning
from .dialogs import cap_minsize, focus_first, place_over, tab_last
from .dpi import fit_to_content, px, pxs
from .organizer.ops import _Dialog
from .redact import RedactingTreeview, set_label
from .themeaware import ThemeRegistry
from .worker import UiWorker, weak

TITLE = "Thin the vault"
SCHEDULE_TEXT = ("Old backups go by your schedule: every save of the last 7 days, then one a day "
                 "until 30 days, one a week until 6 months, and one a month after that. The "
                 "newest save of each save file always stays, and so does what you pinned, named "
                 "or wrote a note on. Untick a row to keep that backup. Nothing is deleted until "
                 "you press Delete.")
NOTHING_TEXT = "Nothing to thin: the schedule keeps every backup."
TICKED, UNTICKED, DELETED, LEFT = "[x]", "[ ]", "deleted", "kept"
#: How an entry's kind reads (the owner's words: full backup, snapshot, preserve).
KINDS = {"full": "full backup"}
GO_COLUMNS = (("when", "Date", 130, "w"), ("kind", "Kind", 90, "w"),
              ("saves", "Saves", 260, "w"), ("frees", "Frees", 80, "e"),
              ("why", "Why it goes", 320, "w"))
KEPT_COLUMNS = (("when", "Date", 130, "w"), ("kind", "Kind", 90, "w"),
                ("saves", "Saves", 260, "w"), ("why", "Why it stays", 400, "w"))
_SHOWN_LEFT = 8          # the result names this many entries that stayed, then counts the rest


# --- the words (pure) ----------------------------------------------------------------------------


def size_text(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{max(n, 0) / 1024:.0f} KB"


def kind_text(kind: str) -> str:
    return KINDS.get(kind, kind)


def when_text(created: str) -> str:
    """``2026-03-03 14:01``, as the Backups list shows a backup's date."""
    return created.replace("T", " ")[:16]


def saves_text(saves) -> str:
    return ", ".join(f"slot {s.slot} {s.name}" for s in saves) or "no saves"


def delete_text(n: int, nbytes: int) -> str:
    """The Delete button, as the owner worded it: ``Delete N entries (frees X GB)``."""
    return f"Delete {n} {'entry' if n == 1 else 'entries'} (frees {size_text(nbytes)})"


def result_text(result: thinning.ThinResult, plan: thinning.ThinPlan) -> str:
    """What the run did: how many went and the space, then each entry left and why."""
    n = len(result.thinned)
    noun = "backup" if n == 1 else "backups"
    freed = size_text(result.freed_bytes)
    lines = [f"Stopped: {n} {noun} deleted so far, {freed} freed." if result.cancelled
             else f"{n} {noun} deleted, {freed} freed."]
    rows = {c.id: c for c in plan.candidates}
    for entry_id, why in result.left[:_SHOWN_LEFT]:
        c = rows.get(entry_id)
        what = (f"{when_text(c.created)} {kind_text(c.kind)} ({saves_text(c.saves)})"
                if c is not None else entry_id)
        lines.append(f"Kept: {what} - {why}")
    if len(result.left) > _SHOWN_LEFT:
        lines.append(f"and {len(result.left) - _SHOWN_LEFT} more kept.")
    return "\n".join(lines)


# --- the jobs (worker; plain data in, plain data out) ---------------------------------------------


def plan_job(vault_root: Path) -> thinning.ThinPlan:
    return thinning.plan_thin(vault_root)


def run_job(vault_root: Path, plan: thinning.ThinPlan, chosen: list[str], box: SimpleNamespace,
            cancel: threading.Event) -> thinning.ThinResult:
    return thinning.run_thin(vault_root, plan, chosen,
                             progress=lambda p: setattr(box, "progress", p), cancel=cancel)


# --- the window ----------------------------------------------------------------------------------


class ThinWindow(_Dialog):
    """Modal. ``on_changed()`` runs after a run (done or refused part-way): the Backups list and
    the status bar read the vault again. ``busy``: a run is deleting (Esc and Cancel wait for
    it); ``planning``: the plan is being made (only reads, so Esc still closes)."""

    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry, vault_root: Path,
                 on_changed: Callable[[], None] | None = None) -> None:
        super().__init__(master, themed, TITLE)
        self.worker, self.vault_root, self.on_changed = worker, Path(vault_root), on_changed
        self.busy = False
        self.planning = True
        self.plan: thinning.ThinPlan | None = None
        self.ticked: set[str] = set()
        self.finished = False
        self._box = SimpleNamespace(progress=None)
        self._cancel = threading.Event()
        pad = px(10)

        foot = ttk.Frame(self, padding=pad)
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        self.cancel_button = ttk.Button(foot, text="Cancel", command=self.cancel)
        self.cancel_button.pack(side=tk.RIGHT)
        self.delete_button = ttk.Button(foot, text=delete_text(0, 0), command=self.delete)
        self.status = ttk.Label(foot, text="Reading the vault...", style="Muted.TLabel")
        self.status.pack(side=tk.LEFT)
        self.bar = ttk.Progressbar(foot, mode="determinate", length=px(200))
        self.stop_button = ttk.Button(foot, text="Stop", command=self._stop)

        self.result = ttk.Label(self, text="", justify="left", wraplength=px(860))
        self.result.pack(side=tk.BOTTOM, fill=tk.X, padx=pad)
        kept = ttk.Frame(self, padding=pxs(10, 0, 10, 4))
        kept.pack(side=tk.BOTTOM, fill=tk.X)
        self.kept_title = ttk.Label(kept, text="", style="Head.TLabel")
        self.kept_title.pack(side=tk.TOP, anchor="w")
        self.kept_tree = self._table(kept, KEPT_COLUMNS, height=5)

        head = ttk.Frame(self, padding=pxs(10, 10, 10, 4))
        head.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(head, text=TITLE, style="Title.TLabel").pack(anchor="w")
        self.intro = ttk.Label(head, text=SCHEDULE_TEXT, justify="left", wraplength=px(860))
        self.intro.pack(anchor="w", pady=pxs(4, 0))
        self.message = ttk.Label(head, text="", style="Warn.TLabel", justify="left",
                                 wraplength=px(860))
        self.message.pack(anchor="w", pady=pxs(4, 0))
        body = ttk.Frame(self, padding=pxs(10, 0, 10, 4))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.tree = self._table(body, GO_COLUMNS, height=12, tick=True)
        self.tree.bind("<Button-1>", self._clicked, add="+")
        self.tree.bind("<space>", lambda _e: (self._space(), "break")[1])
        tab_last(kept, foot)                       # built first to keep their room

        cap_minsize(self, 760, 420)
        fit_to_content(self, 940, 600)
        place_over(self, master)
        focus_first(self, self.tree)
        self._ready()
        root = self.vault_root
        worker.submit(lambda: plan_job(root), on_done=weak(self._planned),
                      on_error=weak(self._plan_failed))

    def _table(self, parent, columns, *, height: int, tick: bool = False) -> RedactingTreeview:
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        tree = RedactingTreeview(wrap, columns=[c[0] for c in columns],
                                 show="tree headings" if tick else "headings",
                                 selectmode="extended", height=height)
        if tick:
            tree.heading("#0", text="Delete")
            tree.column("#0", width=px(64), stretch=False, anchor="center")
        for key, title, width, anchor in columns:
            tree.heading(key, text=title)
            tree.column(key, width=px(width), anchor=anchor, stretch=key == "why")
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return tree

    def _recolour(self) -> None:
        if self._pal is not None:
            self.tree.tag_configure("off", foreground=self._pal["muted"])

    @property
    def ready(self) -> bool:
        """Neither planning nor deleting: what the owner sees is final until the next click."""
        return not self.planning and not self.busy

    # --- the plan ------------------------------------------------------------------------------

    def _planned(self, plan: thinning.ThinPlan) -> None:
        if not self.winfo_exists():
            return
        self.planning = False
        self.plan = plan
        set_label(self.status, "")
        if plan.blocked:
            self._end(" ".join(plan.blocked))
            self._fill_kept(())
            return
        for c in plan.candidates:
            self.tree.insert("", "end", iid=c.id, text=TICKED, values=(
                when_text(c.created), kind_text(c.kind), saves_text(c.saves), size_text(c.bytes),
                c.reason))
        self.ticked = {c.id for c in plan.candidates}
        self._fill_kept(plan.kept)
        if not plan.candidates:
            self._end(NOTHING_TEXT, warn=False)
            return
        self.delete_button.pack(side=tk.RIGHT, padx=px(6))
        self._update_button()
        first = self.tree.get_children("")[0]
        self.tree.focus(first)
        self.tree.selection_set(first)

    def _fill_kept(self, kept) -> None:
        set_label(self.kept_title, f"Kept although the schedule would let them go "
                                   f"({len(kept)})")
        for k in kept:
            self.kept_tree.insert("", "end", iid=k.id, values=(
                when_text(k.created), kind_text(k.kind), saves_text(k.saves),
                "; ".join(k.reasons)))

    def _plan_failed(self, error: BaseException) -> None:
        if not self.winfo_exists():
            return
        self.planning = False
        set_label(self.status, "")
        self._end(str(error) or type(error).__name__)

    def _end(self, text: str, *, warn: bool = True) -> None:
        """No Delete from here on: the window only says ``text`` and closes."""
        self.finished = True
        self.message.configure(style="Warn.TLabel" if warn else "TLabel")
        set_label(self.message, text)
        self.delete_button.pack_forget()
        set_label(self.cancel_button, "Close")

    # --- the ticks -----------------------------------------------------------------------------

    def toggle(self, iid: str) -> None:
        if self.busy or self.finished or not self.tree.exists(iid):
            return
        if iid in self.ticked:
            self.ticked.discard(iid)
            self.tree.item(iid, text=UNTICKED, tags=("off",))
        else:
            self.ticked.add(iid)
            self.tree.item(iid, text=TICKED, tags=())
        self._update_button()

    def _clicked(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and self.tree.identify_region(event.x, event.y) in ("tree", "cell"):
            self.toggle(iid)

    def _space(self) -> None:
        for iid in self.tree.selection():
            self.toggle(iid)

    def _chosen(self) -> list[str]:
        """The ticked rows, in the list's order (oldest first)."""
        return [i for i in self.tree.get_children("") if i in self.ticked]

    def _update_button(self) -> None:
        if self.plan is None:
            return
        chosen = self._chosen()
        set_label(self.delete_button, delete_text(len(chosen), self.plan.freed_bytes(chosen)))
        ok = bool(chosen) and not self.busy and not self.finished
        self.delete_button.state(["!disabled"] if ok else ["disabled"])

    # --- the run -------------------------------------------------------------------------------

    def delete(self) -> None:
        chosen = self._chosen()
        if self.busy or self.finished or self.plan is None or not chosen:
            return
        self.busy = True
        self._update_button()
        self.cancel_button.state(["disabled"])
        self._cancel = threading.Event()
        self._box.progress = None
        set_label(self.status, "Deleting...")
        self.bar.configure(value=0, maximum=len(chosen))
        self.bar.pack(side=tk.LEFT, padx=px(6))
        self.stop_button.pack(side=tk.LEFT, padx=px(6))
        root, plan, box, cancel = self.vault_root, self.plan, self._box, self._cancel
        self.after(150, self._poll)
        self.worker.submit(lambda: run_job(root, plan, chosen, box, cancel),
                           on_done=weak(self._ran), on_error=weak(self._run_failed))

    def _stop(self) -> None:
        self._cancel.set()
        set_label(self.status, "Stopping after this backup...")

    def _poll(self) -> None:
        if not self.winfo_exists() or not self.busy:
            return
        p = self._box.progress
        if p is not None and not self._cancel.is_set():
            self.bar.configure(maximum=max(1, p.total), value=p.done)
            if p.current:
                set_label(self.status, f"Deleting {p.done + 1} of {p.total}...")
        self.after(150, self._poll)

    def _idle(self) -> None:
        self.busy = False
        self.bar.pack_forget()
        self.stop_button.pack_forget()
        set_label(self.status, "")
        self.cancel_button.state(["!disabled"])

    def _ran(self, result: thinning.ThinResult) -> None:
        if not self.winfo_exists():
            self._changed()
            return
        self._idle()
        left = dict(result.left)
        for iid in result.thinned:
            if self.tree.exists(iid):
                self.tree.item(iid, text=DELETED, tags=("off",))
        for iid, why in left.items():
            if self.tree.exists(iid):
                self.tree.item(iid, text=LEFT, tags=())
                self.tree.set(iid, "why", why)
        set_label(self.result, result_text(result, self.plan))
        self._end("")
        self._changed()

    def _run_failed(self, error: BaseException) -> None:
        if self.winfo_exists():
            self._idle()
            self._end(str(error) or type(error).__name__)
        self._changed()

    def _changed(self) -> None:
        if self.on_changed is not None:
            self.on_changed()
