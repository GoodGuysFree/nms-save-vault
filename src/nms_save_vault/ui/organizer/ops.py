"""The windows around a live operation (D6 2.10, 2.9, 3.8; U16-U18): the Done bar and Undo, the
operations log, one confirmation dialog for every plan that is not "Move to live" (undo, swap,
promote, keep), the start-up recovery dialog, and the "Empty this slot" steps window.

Like the Move dialog these only render what the live-operations API returns: every plan and
every write runs in the UI worker, every text comes from the plan, the warnings, the recovery
items or the guide. The app never empties a slot (F8); the steps window only tells the owner how.
"""
from __future__ import annotations

import tkinter as tk
import weakref
from collections.abc import Callable
from types import SimpleNamespace
from tkinter import ttk

from ... import theme
from ...core.liveops import (
    EmptySlotGuide, OpContext, OpOutcome, OpSummary, PlacePlan, RecoveryItem, Warning, execute,
    op_history, resolve_recovery)
from ...core.organizer.model import LiveTarget
from ..dialogs import bind_escape, cap_minsize, keep_on_screen
from ..dpi import px, pxs
from ..fmt import play_time
from ..redact import rtitle, set_label, shown
from ..themeaware import ThemeRegistry
from ..worker import UiWorker, finisher, release_tk_variables, weak

SEVERITY_ORDER = {"block": 0, "confirm": 1, "warn": 2, "info": 3}
MARKS = {"info": "i", "warn": "!", "confirm": "?", "block": "X"}
STEPS = {"checking": "Checking", "preserving": "Saving what is in the slot to the vault",
         "snapshot": "Taking the restore point", "writing": "Writing", "verifying": "Verifying",
         "recording": "Recording"}
CHOICE_WORDS = {"forward": "Finish it", "back": "Put everything back", "leave": "Leave it as it is"}


def done_text(outcome: OpOutcome) -> str:
    """The Done bar's sentence (screen 09)."""
    text = f"Done. {outcome.outcome}"
    if not text.endswith("."):
        text += "."
    if outcome.verify == "pending":
        text += " Waiting for the next game launch to confirm."
    return text


class DoneBar(ttk.Frame):
    """Above the status bar: what was done, Undo (default), Show in history, and a close
    button. It stays until dismissed or replaced by the next operation; no modal."""

    def __init__(self, master, *, on_undo: Callable[[str], None],
                 on_history: Callable[[str | None], None]) -> None:
        super().__init__(master, style="Alert.TFrame", padding=pxs(8, 4))
        self.on_undo, self.on_history = on_undo, on_history
        self.outcome: OpOutcome | None = None
        self.text = ttk.Label(self, text="", style="Alert.TLabel", anchor="w", wraplength=px(700))
        self.close = ttk.Button(self, text="x", width=2, command=self.clear)
        self.close.pack(side=tk.RIGHT)
        self.history = ttk.Button(self, text="Show in history", command=self._history)
        self.history.pack(side=tk.RIGHT, padx=px(4))
        self.undo = ttk.Button(self, text="Undo", command=self._undo)
        self.text.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def show(self, outcome: OpOutcome) -> None:
        self.outcome = outcome
        set_label(self.text, done_text(outcome))
        if outcome.undo is not None and outcome.op_id:
            set_label(self.undo, "Undo")
            self.undo.pack(side=tk.RIGHT, padx=px(4), before=self.history)
        else:
            self.undo.pack_forget()
        if not self.winfo_ismapped():
            self.pack(side=tk.BOTTOM, fill=tk.X, padx=px(6), pady=pxs(2, 0))

    def clear(self) -> None:
        self.outcome = None
        self.pack_forget()

    def _undo(self) -> None:
        if self.outcome is not None and self.outcome.op_id:
            self.on_undo(self.outcome.op_id)

    def _history(self) -> None:
        self.on_history(self.outcome.op_id if self.outcome else None)


class _Dialog(tk.Toplevel):
    """The shared bones: a themed Toplevel that is modal and sizes from its content."""

    def __init__(self, master, themed: ThemeRegistry, title: str, *, modal: bool = True) -> None:
        super().__init__(master)
        self._pal = None
        rtitle(self, title)
        self.transient(master)
        self._themed = themed
        self._outer_grab = None
        if modal:
            try:
                self._outer_grab = master.grab_current()    # a dialog this one is opened from
                self.grab_set()
            except tk.TclError:
                pass
        bind_escape(self, self.cancel, lambda: getattr(self, "busy", False))
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def cancel(self) -> None:
        """Esc, the title-bar X and the Cancel / Close buttons: never while a write runs."""
        if not getattr(self, "busy", False):
            self.destroy()

    def destroy(self) -> None:
        release_tk_variables(self)
        outer, self._outer_grab = self._outer_grab, None
        super().destroy()
        try:
            if outer is not None and outer.winfo_exists():
                outer.grab_set()         # the grab went with this window: the opener has it back
        except tk.TclError:
            pass

    def _ready(self) -> None:
        """Last line of every constructor: the widgets exist, so the theme can reach them."""
        self._themed.register(self)

    def apply_palette(self, p) -> None:
        self._pal = p
        self.configure(background=p["bg"])
        self._recolour()

    def _recolour(self) -> None:
        pass


class PlanDialog(_Dialog):
    """Confirm and run a plan: undo, swap, promote, keep. The outcome names the button; the
    warnings are shown by severity, the confirmations as checkboxes; a plan with a block
    cannot be applied."""

    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry,
                 make_context: Callable[[], OpContext], plan: PlacePlan, title: str,
                 on_done: Callable[[OpOutcome], None] | None = None,
                 on_recover: Callable[[], None] | None = None) -> None:
        super().__init__(master, themed, title)
        self.worker, self.make_context, self.plan = worker, make_context, plan
        self.on_done, self.on_recover = on_done, on_recover
        self.outcome: OpOutcome | None = None
        self.busy = False
        self._box = SimpleNamespace(progress=None)       # worker -> Tk: plain data only
        self._gen = 0
        self.vars: dict[str, tk.BooleanVar] = {}
        pad = px(10)
        foot = ttk.Frame(self, padding=pad)
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        self.status = ttk.Label(foot, text="", style="Muted.TLabel")
        self.bar = ttk.Progressbar(foot, mode="determinate", length=px(160))
        self.cancel_button = ttk.Button(foot, text="Cancel", command=self.cancel)
        self.cancel_button.pack(side=tk.RIGHT)
        self.apply_button = ttk.Button(foot, text="", command=self.apply, default="active")
        self.apply_button.pack(side=tk.RIGHT, padx=pxs(0, 8))
        self.recover_button = ttk.Button(foot, text="Recover...", command=self._recover)
        ttk.Label(foot, text="Note (optional):").pack(side=tk.LEFT)
        self.note_var = tk.StringVar()
        ttk.Entry(foot, textvariable=self.note_var, width=26).pack(side=tk.LEFT, padx=px(6))
        self.error = tk.Label(self, text="", anchor="w", justify="left", wraplength=px(620))
        self.error.pack(side=tk.BOTTOM, fill=tk.X, padx=pad)
        body = ttk.Frame(self, padding=pad)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        ttk.Label(body, text=shown(self, title), style="Title.TLabel").pack(side=tk.TOP, anchor="w")
        self.summary = ttk.Label(body, text="", wraplength=px(620), justify="left")
        self.summary.pack(side=tk.TOP, anchor="w", pady=pxs(6, 4))
        self.warnings = ttk.Frame(body)
        self.warnings.pack(side=tk.TOP, fill=tk.X)
        cap_minsize(self, 520, 220)
        keep_on_screen(self)
        self._render()
        self._ready()

    # --- showing -----------------------------------------------------------------------------

    def describe(self) -> str:
        """What goes where, from the plan's targets alone."""
        if self.plan is None:
            return "Looking at what would change..."
        plan, lines = self.plan, [self.plan.outcome + "."]
        for t in plan.targets:
            gone = [f"'{e.name or 'a save'}' ({play_time(e.play_time) or 'no play time'})"
                    for e in t.evictions]
            if plan.kind == "keep" and gone:
                lines.append(f"{t.label}: " + ", ".join(gone) + " is copied to the vault; "
                             "nothing live is changed.")
            elif gone:
                lines.append(f"{t.label}: " + ", ".join(gone) + " leaves live and stays in the "
                             "vault.")
        return " ".join(lines)

    def _render(self) -> None:
        for w in self.warnings.winfo_children():
            w.destroy()
        self.vars.clear()
        set_label(self.summary, self.describe())
        if self.plan is None:
            return
        for w in sorted(self.plan.warnings + self.plan.blocked,
                        key=lambda w: SEVERITY_ORDER[w.severity]):
            self._row(w)
        set_label(self.apply_button, self.plan.outcome if self.plan else "Apply")
        self._recolour()
        self._update()

    def _row(self, w: Warning) -> None:
        row = tk.Frame(self.warnings)
        row.pack(side=tk.TOP, fill=tk.X, pady=px(1))
        row.severity = w.severity
        tk.Label(row, text=MARKS[w.severity], font=theme.BOLD_FONT, width=2).pack(
            side=tk.LEFT, anchor="n")
        if w.severity == "confirm" or w.code in self.plan.confirmations:
            var = tk.BooleanVar(value=False)
            self.vars[w.code] = var
            ttk.Checkbutton(row, text=shown(self, w.text), variable=var,
                            command=self._update).pack(side=tk.LEFT, anchor="w")
        else:
            tk.Label(row, text=shown(self, w.text), anchor="w", justify="left",
                     wraplength=px(580)).pack(side=tk.LEFT, anchor="w")

    def _recolour(self) -> None:
        p = self._pal
        if p is None:
            return
        colours = {"info": p["accent"], "warn": p["warn"], "confirm": p["warn"],
                   "block": p["danger"]}
        for row in self.warnings.winfo_children():
            row.configure(background=p["bg"])
            for w in row.winfo_children():
                if isinstance(w, tk.Label):
                    w.configure(background=p["bg"], foreground=p["fg"])
            row.winfo_children()[0].configure(foreground=colours[row.severity])
        self.error.configure(background=p["bg"], foreground=p["danger"])

    def _update(self) -> None:
        ok = (self.plan is not None and not self.plan.blocked and not self.busy and
              self.plan.confirmations <= {c for c, v in self.vars.items() if v.get()} and
              all(v.get() for v in self.vars.values()))
        self.apply_button.state(["!disabled"] if ok else ["disabled"])

    # --- executing -----------------------------------------------------------------------------

    def apply(self) -> None:
        if self.busy or self.apply_button.instate(["disabled"]):
            return
        confirmed = frozenset(c for c, v in self.vars.items() if v.get())
        note = self.note_var.get().strip()
        self._gen += 1
        gen = self._gen
        self.busy = True
        self.error.configure(text="")
        self.recover_button.pack_forget()
        self.apply_button.state(["disabled"])
        self.cancel_button.state(["disabled"])
        self.status.pack(side=tk.LEFT, padx=px(8))
        self.bar.pack(side=tk.LEFT)
        self.bar.configure(value=0, maximum=1)
        set_label(self.status, "Working...")
        self.after(100, self._poll)
        job = self._make_job(confirmed, note)
        self.worker.submit(
            job,
            on_done=finisher(weakref.ref(self), gen, self.on_done, self.on_recover),
            on_error=weak(self._failed, gen))

    def _make_job(self, confirmed, note):
        """The worker's job for Apply: a closure over plain data only, never the widget."""
        plan, make_context, box = self.plan, self.make_context, self._box

        def job():
            return execute(make_context(), plan, confirmed,
                           lambda p: setattr(box, "progress", p), note=note)
        return job

    def _poll(self) -> None:
        if not self.winfo_exists() or not self.busy:
            return
        p = self._box.progress
        if p is not None:
            self.bar.configure(maximum=max(1, p.total), value=p.done)
            set_label(self.status, STEPS.get(p.step, p.step) + "...")
        self.after(100, self._poll)

    def _idle(self) -> None:
        self.busy = False
        self.bar.pack_forget()
        self.status.pack_forget()
        self.cancel_button.state(["!disabled"])
        self._update()

    def _executed(self, gen: int, outcome: OpOutcome) -> None:
        if not self.winfo_exists():            # closed mid-write: the result still counts
            if outcome.status == "done" and self.on_done is not None:
                self.on_done(outcome)
            elif outcome.status in ("failed", "rolled_back") and self.on_recover is not None:
                self.on_recover()
            return
        if gen != self._gen:
            return
        self.outcome = outcome
        self._idle()
        if outcome.status == "done":
            if self.on_done is not None:
                self.on_done(outcome)
            self.destroy()
            return
        if outcome.status == "stale" and outcome.replan is not None:
            self.plan = outcome.replan
            self._render()
            self.error.configure(text=shown(self, "Something changed since you opened this. "
                                            "Please check again."))
            return
        why = outcome.error or " ".join(w.text for w in outcome.warnings) or outcome.outcome
        self.error.configure(text=shown(self, why))
        if outcome.status in ("failed", "rolled_back") and self.on_recover is not None:
            self.recover_button.pack(side=tk.LEFT, padx=px(6))

    def _failed(self, gen: int, error: BaseException) -> None:
        if gen != self._gen or not self.winfo_exists():
            return
        self._idle()
        self.error.configure(text=shown(self, f"This could not be done: {error}"))

    def _recover(self) -> None:
        if self.on_recover is not None:
            self.on_recover()


class RecoveryDialog(_Dialog):
    """An interrupted move (or a missing vault copy) the owner must decide on. One row per
    item: what happened, the recommended choice, and Apply; the game state that cannot be
    checked is confirmed first."""

    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry,
                 make_context: Callable[[], OpContext], items: list[RecoveryItem],
                 on_changed: Callable[[], None] | None = None) -> None:
        super().__init__(master, themed, "Finish an interrupted move")
        self.worker, self.make_context, self.on_changed = worker, make_context, on_changed
        self.items = list(items)
        self.choice: dict[str, tk.StringVar] = {}
        self.result: dict[str, ttk.Label] = {}
        self.apply_buttons: dict[str, ttk.Button] = {}
        self.confirm = tk.BooleanVar(value=False)
        self.busy = False
        pad = px(10)
        foot = ttk.Frame(self, padding=pad)
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(foot, text="Close", command=self.cancel).pack(side=tk.RIGHT)
        self.confirm_check = ttk.Checkbutton(
            foot, variable=self.confirm,
            text="I confirm the game is not running (the app could not check)")
        body = ttk.Frame(self, padding=pad)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        ttk.Label(body, text="An earlier move did not finish", style="Title.TLabel").pack(
            anchor="w")
        ttk.Label(body, text="Nothing was lost. Pick what to do with each one.",
                  style="Muted.TLabel").pack(anchor="w", pady=pxs(0, 6))
        self.rows = ttk.Frame(body)
        self.rows.pack(fill=tk.BOTH, expand=True)
        for item in self.items:
            self._item(item)
        cap_minsize(self, 560, 200)
        keep_on_screen(self)
        self._ready()

    def _item(self, item: RecoveryItem) -> None:
        box = ttk.LabelFrame(self.rows, text=f"{item.started}  {', '.join(item.labels)}",
                             padding=px(6))
        box.pack(fill=tk.X, pady=px(3))
        ttk.Label(box, text=shown(self, item.text), wraplength=px(520), justify="left").pack(
            anchor="w")
        if not item.choices:
            return
        var = tk.StringVar(value=item.recommended if item.recommended in item.choices
                           else item.choices[0])
        self.choice[item.op_id] = var
        line = ttk.Frame(box)
        line.pack(fill=tk.X, pady=pxs(4, 0))
        for c in item.choices:
            word = CHOICE_WORDS.get(c, c) + (" (recommended)" if c == item.recommended else "")
            ttk.Radiobutton(line, text=word, value=c, variable=var).pack(side=tk.LEFT, padx=px(4))
        button = ttk.Button(line, text="Apply", command=lambda i=item: self.apply(i))
        button.pack(side=tk.RIGHT)
        self.apply_buttons[item.op_id] = button
        self.result[item.op_id] = ttk.Label(box, text="", style="Muted.TLabel", wraplength=px(520))
        self.result[item.op_id].pack(anchor="w")

    def apply(self, item: RecoveryItem) -> None:
        if self.busy:
            return
        self.busy = True
        confirmed = frozenset({"GAME_STATE_UNKNOWN"}) if self.confirm.get() else frozenset()
        choice = self.choice[item.op_id].get()
        self.apply_buttons[item.op_id].state(["disabled"])
        set_label(self.result[item.op_id], "Working...")
        make_context, op_id = self.make_context, item.op_id
        self.worker.submit(
            lambda: resolve_recovery(make_context(), op_id, choice, confirmed),
            on_done=weak(self._applied, item), on_error=weak(self._apply_failed, item))

    def _apply_failed(self, item: RecoveryItem, error: BaseException) -> None:
        self._applied(item, None, error)

    def _applied(self, item: RecoveryItem, outcome: OpOutcome | None,
                 error: BaseException | None = None) -> None:
        if not self.winfo_exists():            # closed mid-write: the result still counts
            if outcome is not None and outcome.status == "done" and self.on_changed is not None:
                self.on_changed()
            return
        self.busy = False
        label = self.result[item.op_id]
        if outcome is None:
            set_label(label, f"This could not be done: {error}")
            self.apply_buttons[item.op_id].state(["!disabled"])
            return
        if outcome.status == "done":
            set_label(label, outcome.outcome or "Done.")
            if self.on_changed is not None:
                self.on_changed()
            return
        if outcome.code == "GAME_STATE_UNKNOWN":
            self.confirm_check.pack(side=tk.LEFT)
        set_label(label, outcome.error or " ".join(w.text for w in outcome.warnings)
                  or outcome.outcome)
        self.apply_buttons[item.op_id].state(["!disabled"])


class StepsWindow(_Dialog):
    """"Empty this slot": the app never does it (F8); these are the owner's own steps, with
    the warning that Steam Cloud or Xbox sync may fill the slot again."""

    def __init__(self, master, *, themed: ThemeRegistry, guide: EmptySlotGuide,
                 on_keep: Callable[[LiveTarget], None] | None = None) -> None:
        super().__init__(master, themed, f"Empty {guide.label}", modal=False)
        self.guide = guide
        pad = px(10)
        body = ttk.Frame(self, padding=pad)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=f"Empty {guide.label}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="The app never empties a slot. To do it yourself:",
                  style="Muted.TLabel").pack(anchor="w", pady=pxs(0, 6))
        if guide.occupied:
            text = ("What is in it is already in the vault." if guide.durable else
                    "What is in it is not in the vault yet. Keep a copy first.")
            ttk.Label(body, text=text, wraplength=px(480), justify="left").pack(anchor="w")
            if not guide.durable and on_keep is not None:
                ttk.Button(body, text="Keep a copy in the vault now",
                           command=lambda: on_keep(guide.target)).pack(anchor="w", pady=px(4))
        self.refill = None
        if guide.refill is not None:
            self.refill = tk.Label(body, text=shown(self, "! " + guide.refill.text), anchor="w",
                                   justify="left", wraplength=px(480))
            self.refill.pack(fill=tk.X, pady=pxs(6, 4))
        self.steps = ttk.Frame(body)
        self.steps.pack(fill=tk.X)
        for n, step in enumerate(guide.steps, 1):
            ttk.Label(self.steps, text=shown(self, f"{n}. {step}"), wraplength=px(480),
                      justify="left").pack(anchor="w", pady=px(1))
        ttk.Button(body, text="Close", command=self.destroy).pack(anchor="e", pady=pxs(8, 0))
        self._ready()

    def _recolour(self) -> None:
        if self.refill is not None and self._pal is not None:
            self.refill.configure(background=self._pal["callout_bg"],
                                  foreground=self._pal["fg"])


def _read_log(make_context, targets):
    """The worker's job for the log: a closure over plain data, never over the window."""
    def job() -> list[OpSummary]:
        ctx = make_context()
        merged: dict[str, OpSummary] = {}
        for t in targets:
            for s in op_history(ctx, t):
                merged.setdefault(s.op_id, s)
        return sorted(merged.values(), key=lambda s: (s.when, s.op_id), reverse=True)
    return job


class OpsLogWindow(_Dialog):
    """The operations log: every move touching the given slots, newest first, with Undo for
    the selected one."""

    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry,
                 make_context: Callable[[], OpContext], targets: list[LiveTarget],
                 on_undo: Callable[[str], None], select: str | None = None,
                 title: str = "Operations log") -> None:
        super().__init__(master, themed, title, modal=False)
        self.worker, self.make_context, self.targets = worker, make_context, targets
        self.on_undo, self.select = on_undo, select
        self.rows: dict[str, OpSummary] = {}
        pad = px(10)
        foot = ttk.Frame(self, padding=pad)
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(foot, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        self.undo_button = ttk.Button(foot, text="Undo this move", command=self._undo,
                                      state="disabled")
        self.undo_button.pack(side=tk.RIGHT, padx=px(6))
        self.status = ttk.Label(foot, text="Reading...", style="Muted.TLabel")
        self.status.pack(side=tk.LEFT)
        self.tree = ttk.Treeview(self, columns=("when", "what", "status", "verify"),
                                 show="headings", selectmode="browse", height=12)
        for key, title_, width in (("when", "When", 120), ("what", "What", 360),
                                   ("status", "Result", 80), ("verify", "Checked", 90)):
            self.tree.heading(key, text=title_)
            self.tree.column(key, width=px(width), stretch=key == "what")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=pad, pady=pad)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        cap_minsize(self, 640, 300)
        keep_on_screen(self)
        self._ready()
        worker.submit(_read_log(make_context, list(targets)), on_done=weak(self._loaded),
                      on_error=weak(self._failed))

    def _loaded(self, rows: list[OpSummary]) -> None:
        if not self.winfo_exists():
            return
        self.rows = {s.op_id: s for s in rows}
        for s in rows:
            self.tree.insert("", "end", iid=s.op_id, values=tuple(
                shown(self, v) for v in (s.when, s.text, s.status, s.verify)))
        set_label(self.status, f"{len(rows)} operations" if rows else "No operations yet.")
        if self.select in self.rows:
            self.tree.selection_set(self.select)

    def _failed(self, error: BaseException) -> None:
        if self.winfo_exists():
            set_label(self.status, f"The log could not be read: {error}")

    def _selected(self, _event=None) -> None:
        sel = self.tree.selection()
        row = self.rows.get(sel[0]) if sel else None
        ok = row is not None and row.status == "done" and row.kind not in ("keep", "undo")
        self.undo_button.state(["!disabled"] if ok else ["disabled"])

    def _undo(self) -> None:
        sel = self.tree.selection()
        if sel:
            self.on_undo(sel[0])
