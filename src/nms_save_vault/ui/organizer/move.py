"""The Move to live dialog (D6 2.4 and 3.6, U14): put a version of a save file into a live slot.

The web mockup's layout, built in Tk: two boxes (green: what comes IN, amber: what goes OUT
of the slot, with the same two colours on the picked rows and the ``IN`` / ``OUT`` tags of
"Your pick"), the account switch, the fifteen slots, "What you are putting in" on the
right, "What will happen" (the plan's paragraph and its warnings by severity, the
confirmations as checkboxes), "Your pick", and a button named after the plan's outcome.

Nothing here decides anything: every text, every suggestion, every warning and block comes
from the live-operations API (``eviction_picker``, ``plan_place``, ``execute``), and all of
those run in the UI worker, never on the Tk thread. The only mode is replace-or-fill: the
app never offers to empty a slot (F8), and a save that comes from another account carries
that account's one confirmation (F7b), exactly as the plan states it.
"""
from __future__ import annotations

import tkinter as tk
import weakref
from types import SimpleNamespace
from collections.abc import Callable
from tkinter import ttk

from ... import theme
from ...core.liveops import (
    EvictionPicker, OpContext, OpOutcome, PairSource, PlacePlan, Progress,
    SlotCandidate, Warning, eviction_picker, execute, plan_place)
from ...core.organizer.locator import Location
from ...core.organizer.model import LiveRef, StoreRef, platform_letter, platform_name
from ...core.organizer.readmodel import BoardSlot, ReadModel
from ..dialogs import bind_escape, cap_minsize, focus_first, keep_on_screen, tab_last
from ..dpi import px, pxs
from ..fmt import local_time, play_time
from ..redact import rtext, rtitle, set_label, shown
from ..themeaware import ThemeRegistry
from ..worker import UiWorker, finisher, release_tk_variables, weak
from .detail import Segment
from .pick import VersionPicker
from .words import find_row, line_name, plain

SEVERITY_ORDER = {"block": 0, "confirm": 1, "warn": 2, "info": 3}
MARKS = {"info": "i", "warn": "!", "confirm": "?", "block": "X"}
STEPS = {"checking": "Checking", "preserving": "Saving what is in the slot to the vault",
         "snapshot": "Taking the restore point", "writing": "Writing", "verifying": "Verifying",
         "recording": "Recording"}
VAULT_WORDS = {"durable": "yes", "partial": "NO", "outside-vault": "NO", "none": "-"}


def source_for(model: ReadModel, pid: str, head: str | None = None) -> tuple[PairSource, str, list[Segment]]:
    """The :class:`PairSource` of a line (its newest version) or of one version row, the name
    to show, and the version's card segments for the "putting in" panel."""
    from .detail import version_card

    line = model.line(pid)
    if line is None:
        raise KeyError(pid)
    head = head or line.representative
    row = find_row(model, pid, head)
    if row is None:
        raise KeyError(head)
    return (PairSource(row.pair[0], row.pair[1], row.head_member, _prefer(row)),
            line_name(line), version_card(model, pid, head))


def _prefer(row) -> Location | None:
    """Where the version was captured, for the planner (which account it comes from, F7b): a
    kept vault copy first, else a live one; a hand-made folder is not a capture."""
    options = [s for s in row.locations if s.kind != "external"]
    options.sort(key=lambda s: s.kind != "vault")
    if not options:
        return None
    raw = options[0].location
    kind = ("live" if isinstance(raw.where, LiveRef) else
            "store" if isinstance(raw.where, StoreRef) else "entry")
    return Location(row.head, kind, raw.where, raw.slot, raw.member, row.platform,
                    raw.managed)


def candidate_values(c: SlotCandidate, *, in_banner: bool = False) -> tuple[str, ...]:
    """The columns of a slot row: Slot, In it now, Play time, Last played, In the vault, note."""
    now = "empty (emptied outside the app)" if c.state == "empty" else (
        c.line_name or ("(unreadable)" if c.state == "unreadable" else "(unnamed)"))
    if c.pinned:
        now += "  (pinned)"
    notes = []
    if c.suggested:
        notes.append("Suggested - checked after the next launch" if c.state == "empty"
                     else ("Suggested: same save file" if c.suggest_reason == "same_line"
                           else "Suggested"))
    if c.state != "empty" and c.vault_copy != "durable" and c.state != "unreadable":
        notes.append("copied to the vault first")
    if c.pinned:
        notes.append("pinned - not suggested")
    if c.blocked and not in_banner:
        notes.append(f"not possible: {plain(c.blocked)}")
    return (f"{platform_letter(c.target.platform, c.target.account_key)}{c.target.slot}", now, play_time(c.play_time), local_time(c.last_played),
            "-" if c.state == "empty" else VAULT_WORDS[c.vault_copy], "; ".join(notes))


def _common_block(picker: EvictionPicker) -> str:
    """The block that every slot has, said once in the banner instead of on every row."""
    codes = {c.blocked for c in picker.candidates}
    return codes.pop() if len(codes) == 1 and picker.candidates else ""


def outcome_paragraph(plan: PlacePlan) -> str:
    """One paragraph from the plan: what goes into the slot, what leaves it (F4a, F5b)."""
    if not plan.targets or plan.source is None:
        return plan.outcome
    target, src = plan.targets[0], plan.source
    text = (f"{target.label} will hold {src.name} ({play_time(src.play_time) or 'no play time'})"
            f" with its own restore point. The game will list it as saved now; its original "
            f"date ({local_time(src.saved_at)}) stays in the vault record.")
    gone = [e for e in target.evictions]
    if gone:
        e = max(gone, key=lambda e: e.saved_at)
        text += (f" {e.name or 'The save in it'} ({play_time(e.play_time)}, last played "
                 f"{local_time(e.saved_at)}) leaves live: it is already in the vault and can "
                 f"come back any time." if e.durable_before else
                 f" {e.name or 'The save in it'} ({play_time(e.play_time)}, last played "
                 f"{local_time(e.saved_at)}) leaves live: it is copied to the vault first.")
    return text


class MoveDialog(tk.Toplevel):
    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry,
                 make_context: Callable[[], OpContext], source: PairSource | None, name: str,
                 card: list[Segment] | None = None, labels: dict | None = None,
                 model: ReadModel | None = None, fixed: BoardSlot | None = None,
                 prefer_slot: int | None = None, account: LiveRef | None = None, on_done: Callable[[OpOutcome], None] | None = None,
                 on_recover: Callable[[], None] | None = None) -> None:
        super().__init__(master)
        self.worker, self.make_context, self.source, self.name = worker, make_context, source, name
        self.card = card or []
        self.labels = labels or {}
        self.prefer_slot = prefer_slot
        self.model = model
        self.fixed_slot = fixed        # Replace with...: the slot is fixed, the version is picked
        self.fixed = fixed.target if fixed is not None else None
        self._rows_stale = False                # the slot rows belong to a picker being replaced
        self._keep_gen: int | None = None      # the reload after a stale result keeps its replan
        self.start_account = account
        self.want_account: LiveRef | None = account     # what the owner asked for, not yet shown
        self.executing = False
        self._picked_pair: tuple[str, str] | None = None
        self._box = SimpleNamespace(progress=None)       # worker -> Tk: plain data, no widgets
        self.on_done = on_done
        self.on_recover = on_recover
        self.picker: EvictionPicker | None = None
        self.accounts: list[LiveRef] = []
        self.account: LiveRef | None = None
        self.slot: int | None = None
        self.plan: PlacePlan | None = None
        self.outcome: OpOutcome | None = None
        self.busy = False
        self._gen = 0
        self._progress: Progress | None = None
        self._confirm_vars: dict[str, tk.BooleanVar] = {}
        self._rows: dict[str, SlotCandidate] = {}
        self._pal = None
        self._build()
        themed.register(self)
        rtitle(self, "Replace with" if fixed else "Move to live")
        self.transient(master)
        cap_minsize(self, 860, 640)
        keep_on_screen(self)
        tab_last(self.foot)
        focus_first(self, self.tree)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        bind_escape(self, self.cancel, lambda: self.executing)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        if fixed is not None:
            self._start_replace(fixed)
        else:
            self._load(self.start_account)

    # --- building (top to bottom; the footer first so it always stays visible) -----------

    def _build(self) -> None:
        pad = px(10)
        foot = ttk.Frame(self, padding=(pad, pxs(4, 8)[0], pad, pxs(4, 8)[1]))
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        self.foot = foot
        self.status = ttk.Label(foot, text="", style="Muted.TLabel", anchor="w")
        self.bar = ttk.Progressbar(foot, mode="determinate", length=px(200))
        self.recover_button = ttk.Button(foot, text="Recover...", command=self._recover)
        ttk.Label(foot, text="Note for this move (optional):").pack(side=tk.LEFT)
        self.note_var = tk.StringVar()
        self.note = ttk.Entry(foot, textvariable=self.note_var, width=30)
        self.note.pack(side=tk.LEFT, padx=px(6))
        self.cancel_button = ttk.Button(foot, text="Cancel", command=self.cancel)
        self.cancel_button.pack(side=tk.RIGHT)
        self.apply_button = ttk.Button(foot, text="Move to live", command=self.apply,
                                       state="disabled", default="active")
        self.apply_button.pack(side=tk.RIGHT, padx=pxs(0, 8))
        self.error = tk.Label(self, text="", anchor="w", justify="left", wraplength=px(820))
        self.error.pack(side=tk.BOTTOM, fill=tk.X, padx=pad)

        pick = tk.Frame(self, highlightthickness=1)                     # Your pick
        pick.pack(side=tk.BOTTOM, fill=tk.X, padx=pad, pady=pxs(4, 4))
        self.pick = pick
        self.pick_text = tk.Label(pick, text="", anchor="w", font=theme.BOLD_FONT)
        self.pick_out = tk.Label(pick, text=" OUT ", font=theme.BOLD_FONT)
        self.pick_out_text = tk.Label(pick, text="", anchor="w")
        self.pick_in = tk.Label(pick, text=" IN ", font=theme.BOLD_FONT)
        self.pick_in_text = tk.Label(pick, text="", anchor="w")
        for w in (self.pick_text, self.pick_out, self.pick_out_text, self.pick_in,
                  self.pick_in_text):
            w.pack(side=tk.LEFT, padx=px(4), pady=px(4))

        happen = ttk.LabelFrame(self, text="What will happen", padding=px(6))
        happen.pack(side=tk.BOTTOM, fill=tk.X, padx=pad, pady=pxs(4, 0))
        self.happen_text = ttk.Label(happen, text="", wraplength=px(800), justify="left")
        self.happen_text.pack(side=tk.TOP, fill=tk.X)
        self.warnings = ttk.Frame(happen)
        self.warnings.pack(side=tk.TOP, fill=tk.X)

        head = ttk.Frame(self, padding=(pad, pad, pad, 0))
        head.pack(side=tk.TOP, fill=tk.X)
        replacing = self.fixed_slot is not None
        ttk.Label(head, text=f"Replace what is in {self.fixed_slot.label}" if replacing
                  else "Move to live", style="Title.TLabel").pack(side=tk.TOP, anchor="w")
        purpose = ("Pick any version from the vault. The save in the slot now goes to the vault "
                   "first." if replacing else "Pick the live slot. Whatever is in it goes to the "
                   "vault first, so nothing is lost.")
        ttk.Label(head, text=purpose, style="Muted.TLabel").pack(side=tk.TOP, anchor="w")

        body = ttk.Frame(self, padding=(pad, px(6), pad, 0))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        right = ttk.LabelFrame(body, text="What you are putting in", padding=px(6))
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=pxs(8, 0))
        self.putting = tk.Text(right, width=38, height=12, wrap="word", relief="flat",
                               borderwidth=0, state="disabled", font="TkDefaultFont",
                               tabs=(px(96),), spacing1=px(1), spacing3=px(1))
        self.putting.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        boxes = ttk.Frame(left)
        boxes.pack(side=tk.TOP, fill=tk.X)
        self.in_box, self.in_title, self.in_name, self.in_detail = self._end_box(boxes)
        ttk.Label(boxes, text="->", font=theme.TITLE_FONT).pack(side=tk.LEFT, padx=px(8))
        self.out_box, self.out_title, self.out_name, self.out_detail = self._end_box(boxes)

        into = ttk.Frame(left)
        into.pack(side=tk.TOP, fill=tk.X, pady=pxs(6, 2))
        ttk.Label(into, text="Into", font=theme.BOLD_FONT).pack(side=tk.LEFT)
        self.account_bar = ttk.Frame(into)
        self.account_bar.pack(side=tk.LEFT, padx=px(6))
        self.account_note = ttk.Label(into, text="", style="Muted.TLabel", wraplength=px(420))
        self.account_note.pack(side=tk.LEFT)
        self.plan_strip = tk.Label(left, text="", anchor="w", justify="left",
                                   wraplength=px(560))
        self.plan_strip.pack(side=tk.TOP, fill=tk.X)

        cols = ("slot", "now", "play", "last", "vault", "note")
        tree_frame = ttk.Frame(left)
        self.picker_widget: VersionPicker | None = None
        if self.fixed_slot is not None and self.model is not None:
            slot = self.fixed_slot
            self.picker_widget = VersionPicker(
                left, model=self.model, slot_pid=slot.pid, slot_head=slot.head,
                slot_name=slot.name or slot.label,
                on_pick=self._version_picked)
            self.picker_widget.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=pxs(4, 0))
        else:
            tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=pxs(4, 0))
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="tree headings",
                                 selectmode="browse", height=9)
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=px(34), stretch=False)
        for key, title, width in (("slot", "Slot", 44), ("now", "In it now", 190),
                                  ("play", "Play time", 70), ("last", "Last played", 120),
                                  ("vault", "In the vault", 76), ("note", "", 230)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=px(width), stretch=key == "note")
        sb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._row_selected)

    def _end_box(self, parent):
        box = tk.Frame(parent, highlightthickness=2)
        box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        title = tk.Label(box, text="", anchor="w", font=theme.SMALL_FONT)
        name = tk.Label(box, text="", anchor="w", font=theme.HEADING_FONT)
        detail = tk.Label(box, text="", anchor="w", justify="left", wraplength=px(250))
        for w in (title, name, detail):
            w.pack(side=tk.TOP, fill=tk.X, padx=px(8), pady=px(1))
        return box, title, name, detail

    def apply_palette(self, p) -> None:
        self._pal = p
        for box, kind in ((self.in_box, "in"), (self.out_box, "out")):
            bg, fg = p[f"{kind}_bg"], p[f"{kind}_fg"]
            box.configure(background=bg, highlightbackground=fg, highlightcolor=fg)
            for w in box.winfo_children():
                w.configure(background=bg, foreground=p["fg"])
            box.winfo_children()[0].configure(foreground=fg)
        self.pick.configure(background=p["card"], highlightbackground=p["border"])
        for w in (self.pick_text, self.pick_out_text, self.pick_in_text):
            w.configure(background=p["card"], foreground=p["fg"])
        self.pick_out.configure(background=p["out_fg"], foreground=p["out_bg"])
        self.pick_in.configure(background=p["in_fg"], foreground=p["in_bg"])
        self.plan_strip.configure(background=p["callout_bg"], foreground=p["fg"])
        self.error.configure(background=p["bg"], foreground=p["danger"])
        self.putting.configure(background=p["card"], foreground=p["fg"])
        for tag, kw in (("h", {"font": theme.HEADING_FONT}), ("b", {"font": theme.BOLD_FONT}),
                        ("k", {"foreground": p["muted"]}), ("warn", {"foreground": p["warn"]}),
                        ("small", {"foreground": p["muted"], "font": theme.SMALL_FONT})):
            self.putting.tag_configure(tag, **kw)
        self.putting.tag_configure("hang", lmargin2=px(96))
        self.tree.tag_configure("chosen", background=p["out_bg"], foreground=p["out_fg"])
        self.tree.tag_configure("sugg", foreground=p["in_fg"])
        self.tree.tag_configure("muted", foreground=p["muted"])
        self.tree.tag_configure("risk", foreground=p["warn"])
        self._colour_warnings()
        if self.picker_widget is not None:
            self.picker_widget.apply_palette(p)

    # --- loading the picker and the plan (worker) ---------------------------------------------

    def _start_replace(self, slot: BoardSlot) -> None:
        """03b: the slot is fixed; the left side shows what is in it, and nothing is planned
        until a version is picked."""
        set_label(self.in_title, "PUTTING IN")
        set_label(self.in_name, "pick a version below")
        set_label(self.in_detail, "")
        set_label(self.out_title, f"{slot.label.upper()} NOW" + (
            "" if slot.state == "empty" else " - GOES TO THE VAULT"))
        set_label(self.out_name, slot.name or ("empty" if slot.state == "empty" else "(unnamed)"))
        where = ("already in the vault - stays there, ready to come back"
                 if slot.vault_copy == "durable" else "not in the vault yet - copied there first")
        set_label(self.out_detail, "" if slot.state == "empty" else
                  f"{play_time(slot.play_time)} - played {local_time(slot.saved_at)}\n{where}")
        self._set_idle("Pick the version to put in.")

    def _version_picked(self, pid: str, head: str) -> None:
        if self.model is None or self.fixed is None:
            return
        if self.executing:                     # a write is under way: the pick stays as it is
            if self.picker_widget is not None:
                self.picker_widget.restore(self._picked_pair)
            return
        try:
            source, name, card = source_for(self.model, pid, head)
        except KeyError:
            return
        self._picked_pair = (pid, head)
        self.source, self.name, self.card = source, name, card
        self.slot = None
        self.prefer_slot = self.fixed.slot
        self._load(self.fixed.ref)

    def _token(self) -> tuple:
        """The pick on screen: version, account, slot. A plan is good for exactly one."""
        s = self.source
        return ((s.vid_a, s.vid_b, s.load) if s is not None else None,
                self.want_account or self.account, self.slot)

    def plan_matches(self, plan: PlacePlan | None) -> bool:
        """Is ``plan`` for exactly the current picks? Apply is enabled only then."""
        s, ref = self.source, self.want_account or self.account
        if plan is None or s is None or ref is None or self.slot is None or not plan.targets:
            return False
        t = plan.targets[0].target
        src = plan.source
        return (t.ref == ref and t.slot == self.slot and src is not None
                and (src.vid_a, src.vid_b) == (s.vid_a, s.vid_b))

    def _load(self, account: LiveRef | None, keep_plan: bool = False) -> None:
        if self.source is None:
            return
        self._gen += 1
        gen = self._gen
        self._keep_gen = gen if keep_plan else None
        if account is not None:
            self.want_account = account
        if not keep_plan:
            self.plan = None
            self.error.configure(text="")
            self._render_plan(None)
        self._rows_stale = True
        self._set_busy("Reading the live slots...")
        make_context, source = self.make_context, self.source
        self.worker.submit(lambda: eviction_picker(make_context(), source, account),
                           on_done=weak(self._picked, gen),
                           on_error=weak(self._failed, gen))

    def _picked(self, gen: int, picker: EvictionPicker) -> None:
        if gen != self._gen or not self.winfo_exists():
            return
        self.picker = picker
        self.account = picker.account
        self.want_account = picker.account
        self._show_source(picker)
        self._show_accounts(picker)
        self._show_slots(picker)
        self._rows_stale = False
        self._show_strip(picker)
        if self._keep_gen == gen:              # after a stale result: keep the shown plan
            self._keep_gen = None
            cand = next((c for c in picker.candidates if c.target.slot == self.slot), None)
            if cand is not None:
                self._mark_rows(self.slot)
                self._show_out(cand)
            self._set_idle("")
            return
        if self.fixed is not None and not self._usable(self.fixed.slot):
            blocked = next((c.blocked for c in picker.candidates
                            if c.target.slot == self.fixed.slot), "")
            self.slot = None
            self._set_idle("")
            self._render_plan(None)
            self.error.configure(text=shown(self, "This version cannot go into that slot"
                                            + (f" ({blocked})." if blocked else ".")))
            return
        if self.prefer_slot is not None and not self._usable(self.prefer_slot):
            asked = next((c for c in picker.candidates if c.target.slot == self.prefer_slot), None)
            self.prefer_slot = None            # dropped on a slot that cannot take it: say why
            self.slot = None
            self._show_out(None)
            self._render_plan(None)
            self._set_idle("Pick a slot.")
            why = (asked.blocked if asked and asked.blocked else "it is not available")
            self.error.configure(text=shown(self, f"That slot cannot take this version ({why})."))
            return
        keep = self.slot if self.slot in {c.target.slot for c in picker.candidates
                                          if not c.blocked} else None
        wanted = keep or (self.prefer_slot if self._usable(self.prefer_slot) else None) \
            or picker.suggested_slot
        self.prefer_slot = None
        if self.fixed is not None:
            wanted = self.fixed.slot
        if wanted is None:
            self.slot = None
            self._show_out(None)
            self._set_idle("No slot can take this version." if all(
                c.blocked for c in picker.candidates) else "Pick a slot.")
            self._render_plan(None)
            return
        self._choose(wanted)

    def _usable(self, slot: int | None) -> bool:
        return slot is not None and self.picker is not None and any(
            c.target.slot == slot and not c.blocked for c in self.picker.candidates)

    def _choose(self, slot: int) -> None:
        assert self.picker is not None
        self.slot = slot
        cand = next(c for c in self.picker.candidates if c.target.slot == slot)
        self._mark_rows(slot)
        self._show_out(cand)
        self._gen += 1
        gen = self._gen
        self._keep_gen = None
        self.plan = None
        self.error.configure(text="")
        self._render_plan(None)
        self._set_busy("Planning...")
        make_context, source, target = self.make_context, self.source, cand.target
        token = self._token()
        self.worker.submit(lambda: plan_place(make_context(), source, target),
                           on_done=weak(self._planned, gen, token),
                           on_error=weak(self._failed, gen))

    def _planned(self, gen: int, token: tuple, plan: PlacePlan) -> None:
        if gen != self._gen or not self.winfo_exists() or token != self._token():
            return
        self.plan = plan
        self.error.configure(text="")
        self._set_idle("")
        self._render_plan(plan)

    def _failed(self, gen: int, error: BaseException) -> None:
        if gen != self._gen or not self.winfo_exists():
            return
        self.plan = None
        self._keep_gen = None
        self._rows_stale = False
        self._set_idle("")
        self._render_plan(None)
        self.error.configure(text=shown(self, f"This could not be planned: {error}"))

    # --- showing --------------------------------------------------------------------------------

    def _show_source(self, picker: EvictionPicker) -> None:
        s = picker.source
        set_label(self.in_title, "PUTTING IN - " + (s.where_label or "FROM THE VAULT").upper())
        set_label(self.in_name, self.name or s.name)
        set_label(self.in_detail, f"version {local_time(s.saved_at)} - {play_time(s.play_time)}\n"
                  f"{s.where_label}")
        text = self.putting
        text.configure(state="normal")
        text.delete("1.0", "end")
        rtext(text, "end", f"{self.name or s.name}\n", "h")
        rtext(text, "end", f"{play_time(s.play_time)}  -  saved {local_time(s.saved_at)}  -  "
              f"{'Xbox' if s.platform == 'xbox' else 'Steam'}\n\n", "k")
        for part, tags in self.card:
            tags = tuple(t for t in tags if t in ("h", "b", "k", "warn", "small"))
            rtext(text, "end", part, *tags, "hang")
        text.configure(state="disabled")

    def _show_accounts(self, picker: EvictionPicker) -> None:
        for w in self.account_bar.winfo_children():
            w.destroy()
        if self.fixed is not None:
            set_label(self.account_note, f"{self.fixed_slot.label} (fixed)")
            return
        self.accounts = [picker.account] + [c.account for c in picker.other_accounts]
        self.account_var = tk.IntVar(value=0)
        notes = []
        for i, (ref, blocked, label) in enumerate(
                [(picker.account, "", self._label(picker.account))] +
                [(c.account, c.blocked, c.label) for c in picker.other_accounts]):
            text = shown(self, label + (" (locked)" if blocked else ""))
            ttk.Radiobutton(self.account_bar, text=text, value=i, variable=self.account_var,
                            command=lambda r=ref: self._account_picked(r),
                            state="disabled" if blocked else "normal").pack(side=tk.LEFT, padx=px(2))
            if blocked:
                notes.append(f"{label}: " + (
                    "moving a save in this direction between Steam and Xbox is not "
                    "available yet." if blocked == "CROSS_PLATFORM_GATED"
                    else f"not available: {plain(blocked)}"))
        set_label(self.account_note, " ".join(notes))
        if len(self.accounts) == 1:
            self.account_bar.pack_configure(padx=px(2))

    def _label(self, ref: LiveRef) -> str:
        return self.labels.get(ref) or platform_name(ref.platform, ref.account_key)

    def _account_picked(self, ref: LiveRef) -> None:
        if self.executing:
            if self.account in self.accounts:
                self.account_var.set(self.accounts.index(self.account))
            return
        if ref != (self.want_account or self.account):
            self.slot = None
            self._load(ref)

    def _show_strip(self, picker: EvictionPicker) -> None:
        lines = [w.text for w in picker.warnings
                 if not (picker.cloud.steam_cloud and w.code == "STEAM_CLOUD_OTHER_DEVICE")]
        common = _common_block(picker)
        if common and plain(common) not in lines:
            lines.insert(0, plain(common))
        if picker.cloud.steam_cloud:
            lines.append("Steam Cloud is on. If Steam asks at the next launch which copy to "
                         "keep, choose the one on this PC.")
        elif picker.cloud.xbox_sync_busy:
            lines.append("Xbox is syncing right now; wait a moment before moving a save.")
        if lines:
            self.plan_strip.configure(text=shown(self, " ".join(lines)))
            self.plan_strip.pack(side=tk.TOP, fill=tk.X, after=self.account_bar.master)
        else:
            self.plan_strip.pack_forget()

    def _show_slots(self, picker: EvictionPicker) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        common = _common_block(picker)
        for c in picker.candidates:
            iid = f"s{c.target.slot}"
            tags = ["sugg"] if c.suggested else ["muted"] if c.blocked or c.pinned else (
                ["risk"] if c.state not in ("empty",) and c.vault_copy != "durable" else [])
            self.tree.insert("", "end", iid=iid, text="( )", values=candidate_values(c, in_banner=bool(common)),
                             tags=tags)
            self._rows[iid] = c

    def _mark_rows(self, slot: int) -> None:
        for iid in self._rows:
            chosen = iid == f"s{slot}"
            self.tree.item(iid, text="(o)" if chosen else "( )")
            tags = [t for t in self.tree.item(iid, "tags") if t != "chosen"]
            self.tree.item(iid, tags=tags + (["chosen"] if chosen else []))
        if self.tree.selection() != (f"s{slot}",):
            self.tree.selection_set(f"s{slot}")
        self.tree.see(f"s{slot}")

    def _row_selected(self, _event=None) -> None:
        sel = self.tree.selection()
        if self.executing or self._rows_stale or not sel:
            return
        cand = self._rows.get(sel[0])
        if cand is None or cand.blocked:
            if self.slot is not None:
                self._mark_rows(self.slot)
            return
        if cand.target.slot != self.slot:
            self._choose(cand.target.slot)

    def _show_out(self, cand: SlotCandidate | None) -> None:
        if cand is None:
            set_label(self.out_title, "NOTHING GOES OUT")
            set_label(self.out_name, "-")
            set_label(self.out_detail, "")
            return
        set_label(self.out_title, f"{cand.label.upper()} NOW" + (
            "" if cand.state == "empty" else " - GOES TO THE VAULT"))
        if cand.state == "empty":
            set_label(self.out_name, "empty")
            set_label(self.out_detail, "Nothing is in it. If Steam Cloud or Xbox sync fills it "
                      "again, it is checked after the next launch.")
            return
        set_label(self.out_name, cand.line_name or "(unnamed)")
        where = ("already in the vault - stays there, ready to come back"
                 if cand.vault_copy == "durable" else "not in the vault yet - copied there first")
        set_label(self.out_detail, f"{play_time(cand.play_time)} - played "
                  f"{local_time(cand.last_played)}\n{where}")

    def _render_plan(self, plan: PlacePlan | None) -> None:
        for w in self.warnings.winfo_children():
            w.destroy()
        self._confirm_vars.clear()
        if plan is None:
            set_label(self.happen_text, "")
            self._pick_line(None)
            set_label(self.apply_button, "Move to live")
            self.apply_button.state(["disabled"])
            return
        set_label(self.happen_text, outcome_paragraph(plan))
        for w in sorted(plan.warnings + plan.blocked, key=lambda w: SEVERITY_ORDER[w.severity]):
            self._warning_row(w, plan)
        self._pick_line(plan)
        set_label(self.apply_button, plan.outcome)
        self._colour_warnings()
        self._update_apply()

    def _warning_row(self, w: Warning, plan: PlacePlan) -> None:
        row = tk.Frame(self.warnings)
        row.pack(side=tk.TOP, fill=tk.X, pady=px(1))
        row.severity = w.severity
        mark = tk.Label(row, text=MARKS[w.severity], font=theme.BOLD_FONT, width=2)
        mark.pack(side=tk.LEFT, anchor="n")
        if w.severity == "confirm" or w.code in plan.confirmations:
            var = tk.BooleanVar(value=False)
            self._confirm_vars[w.code] = var
            ttk.Checkbutton(row, text=shown(self, w.text), variable=var,
                            command=self._update_apply).pack(side=tk.LEFT, anchor="w")
        else:
            tk.Label(row, text=shown(self, w.text), anchor="w", justify="left",
                     wraplength=px(780)).pack(side=tk.LEFT, anchor="w")

    def _colour_warnings(self) -> None:
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

    def _pick_line(self, plan: PlacePlan | None) -> None:
        cand = next((c for c in (self.picker.candidates if self.picker else ())
                     if c.target.slot == self.slot), None)
        if plan is None or cand is None or self.picker is None:
            for w in (self.pick_text, self.pick_out_text, self.pick_in_text):
                set_label(w, "")
            return
        why = "" if cand.suggested else (
            f" (the suggestion was slot {self.picker.suggested_slot})"
            if self.picker.suggested_slot else "")
        set_label(self.pick_text, f"{cand.label}{why}")
        if cand.state == "empty":
            set_label(self.pick_out_text, "nothing goes out (the slot is empty)")
        else:
            where = "already there" if cand.vault_copy == "durable" else "copied first"
            set_label(self.pick_out_text, f"{cand.line_name or '(unnamed)'} -> vault ({where})")
        set_label(self.pick_in_text, f"{self.name} -> {cand.label}")

    def _update_apply(self) -> None:
        plan = self.plan
        ok = (plan is not None and self.plan_matches(plan) and not plan.blocked
              and not self.busy and not self.executing and
              all(v.get() for v in self._confirm_vars.values()) and
              plan.confirmations <= {c for c, v in self._confirm_vars.items() if v.get()})
        self.apply_button.state(["!disabled"] if ok else ["disabled"])

    # --- busy / idle ---------------------------------------------------------------------------------

    def _set_busy(self, text: str) -> None:
        self.busy = True
        self.status.pack(side=tk.LEFT, padx=px(8))
        set_label(self.status, text)
        self.apply_button.state(["disabled"])

    def _set_idle(self, text: str) -> None:
        self.busy = False
        if text:
            self.status.pack(side=tk.LEFT, padx=px(8))
        else:
            self.status.pack_forget()
        set_label(self.status, text)
        self._update_apply()

    # --- executing (worker; progress is polled, never pushed to Tk from the worker) -----------------

    def apply(self) -> None:
        plan = self.plan
        if plan is None or self.busy or self.apply_button.instate(["disabled"]) \
                or not self.plan_matches(plan):
            return
        confirmed = frozenset(c for c, v in self._confirm_vars.items() if v.get())
        note = self.note_var.get().strip()
        self._gen += 1
        gen = self._gen
        self.error.configure(text="")
        self.recover_button.pack_forget()
        self._box.progress = None
        self.executing = True
        self.cancel_button.state(["disabled"])
        self.tree.state(["disabled"])
        self._set_busy("Working...")
        self.bar.pack(side=tk.LEFT)
        self.bar.configure(value=0, maximum=1)
        self.after(100, self._poll_progress)
        make_context, box = self.make_context, self._box     # no widget in the worker's closure
        self.worker.submit(
            lambda: execute(make_context(), plan, confirmed,
                            lambda p: setattr(box, "progress", p), note=note),
            on_done=finisher(weakref.ref(self), gen, self.on_done, self.on_recover),
            on_error=weak(self._exec_failed, gen))

    def _poll_progress(self) -> None:
        if not self.winfo_exists() or not self.busy or self.apply_button is None:
            return
        p = self._box.progress
        if p is not None:
            self.bar.configure(maximum=max(1, p.total), value=p.done)
            set_label(self.status, STEPS.get(p.step, p.step) + "...")
        self.after(100, self._poll_progress)

    def _outcome_after_close(self, outcome: OpOutcome) -> None:
        """The dialog is gone but the write finished: the result still reaches the Done bar,
        the rescan, Undo and Recover."""
        if outcome.status == "done" and self.on_done is not None:
            self.on_done(outcome)
        elif outcome.status in ("failed", "rolled_back") and self.on_recover is not None:
            self.on_recover()

    def _executed(self, gen: int, outcome: OpOutcome) -> None:
        if not self.winfo_exists():
            self._outcome_after_close(outcome)
            return
        if gen != self._gen:
            return
        self.outcome = outcome
        self._after_execute()
        if outcome.status == "done":
            if self.on_done is not None:
                self.on_done(outcome)
            self.destroy()
            return
        if outcome.status == "stale":
            text = "The slot changed since you opened this. Please check again."
            if outcome.replan is not None:     # the fresh plan, shown as it is
                self.plan = outcome.replan
                self._render_plan(outcome.replan)
            self.error.configure(text=shown(self, text))
            self._load(self.want_account or self.account, keep_plan=outcome.replan is not None)
            return
        why = outcome.error or " ".join(w.text for w in outcome.warnings) or outcome.outcome
        self.error.configure(text=shown(self, f"{outcome.outcome}: {why}" if outcome.error else why))
        self._set_idle("")
        if outcome.status in ("failed", "rolled_back") and self.on_recover is not None:
            self.recover_button.pack(side=tk.LEFT, padx=px(6))

    def _recover(self) -> None:
        if self.on_recover is not None:
            self.on_recover()

    def _exec_failed(self, gen: int, error: BaseException) -> None:
        if not self.winfo_exists():
            return
        if gen != self._gen:
            return
        self._after_execute()
        self.error.configure(text=shown(self, f"The move could not be done: {error}"))
        self._set_idle("")

    def _after_execute(self) -> None:
        self.executing = False
        self.bar.pack_forget()
        self.cancel_button.state(["!disabled"])
        self.tree.state(["!disabled"])

    def destroy(self) -> None:
        release_tk_variables(self, self.picker_widget)
        super().destroy()

    def cancel(self) -> None:
        if self.executing:
            return                     # a write is under way: it is never interrupted
        self._gen += 1
        self.destroy()
