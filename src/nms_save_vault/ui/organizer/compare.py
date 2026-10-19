"""The Compare window (D6 2.7, 3.7; U25): two versions of one save file side by side.

Each side is a card (when, play time, where, label). A strip names the nearest common
ancestor over the drawn edges - "no common ancestor recorded" when the two are not linked, and
nothing is guessed. The fact table lists what the read model has for each version: always the
saved time, play time, mode, where it is kept and its label; the content facts (bases, ships,
...) only when the content index has read both. Facts may be absent: a side without them shows
"not indexed", and the window says so. The field-by-field comparison is presentation logic
here; D4's ``changes`` is not asked for two versions that are not neighbours (D4 rule N1).
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import ttk

from ... import theme
from ...core.organizer.readmodel import ReadModel, VersionRow
from ..dialogs import bind_escape, bind_return, focus_first
from ..dpi import fit_to_content, px, pxs
from ..fmt import local_time, play_time
from ..redact import RedactingTreeview, rtext, rtitle, set_label
from ..themeaware import ThemeRegistry
from .history import fresh_row, where_text
from .words import line_name, version_numbers

NOT_INDEXED = "not indexed"


@dataclass(frozen=True)
class FactRow:
    name: str
    a: str
    b: str

    @property
    def differs(self) -> bool:
        return self.a != self.b


def _num(view, name: str, fmt=str) -> str:
    if view is None:
        return NOT_INDEXED
    value = getattr(view, name, None)
    return "" if value is None else fmt(value)


def _largest(view) -> str:
    if view is None:
        return NOT_INDEXED
    base = getattr(view, "largest_base", None)
    return f"{base[0]} ({base[1]:,} parts)" if base else "none"


#: (label, how to read it from a FactsView); the content facts, shown when the index has them.
CONTENT = (
    ("Bases", lambda v: _num(v, "own_bases")),
    ("Base parts", lambda v: _num(v, "base_parts", "{:,}".format)),
    ("Planet bases", lambda v: _num(v, "planet_bases")),
    ("Largest base", _largest),
    ("Ships", lambda v: _num(v, "ships")),
    ("Corvettes", lambda v: _num(v, "corvettes")),
    ("Archived ships", lambda v: _num(v, "archived_ships")),
    ("Companions", lambda v: _num(v, "companions")),
    ("Eggs", lambda v: _num(v, "eggs")),
    ("Glyphs", lambda v: _num(v, "glyphs")),
    ("Frigates", lambda v: _num(v, "frigates")),
    ("Systems discovered", lambda v: _num(v, "systems_discovered", "{:,}".format)),
    ("Galaxy", lambda v: _num(v, "current_galaxy")),
    ("Journey rank", lambda v: _num(v, "journey_rank")),
    ("Nanites", lambda v: _num(v, "nanites", "{:,}".format)),
    ("Units", lambda v: _num(v, "units", "{:,}".format)),
    ("Quicksilver", lambda v: _num(v, "quicksilver", "{:,}".format)),
    ("Deaths", lambda v: _num(v, "deaths")),
    ("Freighter", lambda v: _num(v, "freighter")),
)


def _platform(row: VersionRow) -> str:
    return "Steam" if row.platform == "steam" else "Xbox"


def compare_facts(model: ReadModel, pid: str, a: VersionRow, b: VersionRow) -> list[FactRow]:
    line = model.line(pid)
    numbers = version_numbers(model, pid)
    rows = [
        FactRow("Version", f"#{numbers[a.head]}", f"#{numbers[b.head]}"),
        FactRow("Saved", local_time(a.saved_at), local_time(b.saved_at)),
        FactRow("Play time", play_time(a.play_time) or "0h00", play_time(b.play_time) or "0h00"),
        FactRow("Platform", _platform(a), _platform(b)),
        FactRow("Mode", a.mode or a.difficulty, b.mode or b.difficulty),
        FactRow("Where", where_text(a, line), where_text(b, line)),
        FactRow("Label", a.label, b.label),
        FactRow("Locked", "yes" if a.locked else "no", "yes" if b.locked else "no"),
    ]
    rows += [FactRow(name, read(a.facts), read(b.facts)) for name, read in CONTENT]
    return rows


def named_differences(a: VersionRow, b: VersionRow) -> list[str]:
    """Ships and companions named on one side only (when both were indexed)."""
    va, vb = a.facts, b.facts
    if va is None or vb is None:
        return []
    out = []
    for title, attr in (("Ship", "ship_names"), ("Companion", "companion_names")):
        sa, sb = set(getattr(va, attr, ()) or ()), set(getattr(vb, attr, ()) or ())
        out += [f"{title}: {name} - only in A" for name in sorted(sa - sb)]
        out += [f"{title}: {name} - only in B" for name in sorted(sb - sa)]
    return out


@dataclass(frozen=True)
class Ancestor:
    head: str | None
    text: str
    a_since: int = 0        # versions on A's side since the ancestor
    b_since: int = 0


def _hm(seconds: int) -> str:
    h, rem = divmod(max(int(seconds), 0), 3600)
    return f"{h}h{rem // 60:02d}"


def common_ancestor(model: ReadModel, pid: str, a: str, b: str) -> Ancestor:
    """The nearest version both come from, over the drawn edges only. A fork point that is
    no head (a restore point) counts as its pair."""
    graph = model.history(pid, "all")
    parent = {e.child: e.parent for e in graph.edges}
    in_tree = {n.vid for n in graph.nodes}
    by_member = {v: r for r in model.versions(pid) for v in r.pair if v}
    numbers = version_numbers(model, pid)
    if a == b:
        return Ancestor(a, "the same version")
    if a not in by_member or b not in by_member:
        return Ancestor(None, "no common ancestor recorded - the two are not linked")

    def start(head: str) -> str:
        return next((v for v in by_member[head].pair if v in in_tree), head)

    def chain(vid: str) -> list[str]:
        out = [vid]
        while out[-1] in parent and parent[out[-1]] not in out:
            out.append(parent[out[-1]])
        return out

    ca, cb = chain(start(a)), chain(start(b))
    common = next((v for v in ca if v in cb), None)
    if common is None or common not in by_member:
        return Ancestor(None, "no common ancestor recorded - the two are not linked")
    ia, ib = ca.index(common), cb.index(common)
    top = by_member[common]
    if top.head == a:
        text = f"A (#{numbers[a]}) is an ancestor of B"
    elif top.head == b:
        text = f"B (#{numbers[b]}) is an ancestor of A"
    else:
        base = top.play_time
        ra, rb = by_member[a], by_member[b]
        text = (f"#{numbers[top.head]}, branch point; A played +{_hm(ra.play_time - base)}, "
                f"B +{_hm(rb.play_time - base)} since")
    return Ancestor(top.head, text, ia, ib)


def card_text(model: ReadModel, pid: str, row: VersionRow) -> tuple[str, str]:
    line = model.line(pid)
    numbers = version_numbers(model, pid)
    return (f"#{numbers[row.head]}  {local_time(row.saved_at)}",
            f"{play_time(row.play_time) or '0h00'} played - {where_text(row, line)}"
            + (f' - "{row.label}"' if row.label else ""))


class CompareWindow(tk.Toplevel):
    def __init__(self, master, *, model: ReadModel, themed: ThemeRegistry, pid: str, a: str,
                 b: str, facts: Callable[[], object | None] | None = None) -> None:
        super().__init__(master)
        self.model, self.pid, self.a, self.b = model, pid, a, b
        self.facts = facts
        self.only_differences = True
        self.transient(master)
        self._build()
        themed.register(self)
        self._fill()
        fit_to_content(self, 900, 560)
        bind_escape(self, self.destroy)
        focus_first(self, self.tree)

    def _build(self) -> None:
        top = ttk.Frame(self, padding=pxs(12, 10, 12, 4))
        top.pack(side=tk.TOP, fill=tk.X)
        self.cards = {}
        for side in ("A", "B"):
            box = ttk.Labelframe(top, text=f"Version {side}", padding=px(8))
            box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=pxs(0, 8))
            title = ttk.Label(box, text="", style="Head.TLabel", anchor="w")
            title.pack(fill=tk.X)
            sub = ttk.Label(box, text="", style="Muted.TLabel", anchor="w", wraplength=px(330))
            sub.pack(fill=tk.X)
            self.cards[side] = (title, sub)
        buttons = ttk.Frame(top)
        buttons.pack(side=tk.LEFT)
        ttk.Button(buttons, text="Swap A and B", command=self.swap).pack(fill=tk.X)
        ttk.Button(buttons, text="Pick other versions...", command=self.pick).pack(
            fill=tk.X, pady=pxs(4, 0))
        self.strip = ttk.Label(self, text="", style="Muted.TLabel", anchor="w",
                               padding=pxs(12, 2, 12, 2))
        self.strip.pack(side=tk.TOP, fill=tk.X)
        bar = ttk.Frame(self, padding=pxs(12, 0, 12, 4))
        bar.pack(side=tk.TOP, fill=tk.X)
        self.diff_button = ttk.Button(bar, command=lambda: self.set_only_differences(True))
        self.all_button = ttk.Button(bar, command=lambda: self.set_only_differences(False))
        self.diff_button.pack(side=tk.LEFT)
        self.all_button.pack(side=tk.LEFT, padx=pxs(4, 0))
        self.note = ttk.Label(bar, text="", style="Muted.TLabel")
        self.note.pack(side=tk.LEFT, padx=pxs(12, 0))
        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=px(10), pady=pxs(0, 10))
        left, right = ttk.Frame(body), ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=1)
        self.tree = RedactingTreeview(left, columns=("a", "b"), show="tree headings",
                                      selectmode="browse")
        self.tree.heading("#0", text="Fact")
        self.tree.heading("a", text="A")
        self.tree.heading("b", text="B")
        self.tree.column("#0", width=px(150), stretch=False)
        self.tree.column("a", width=px(190))
        self.tree.column("b", width=px(190))
        vsb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.named = tk.Text(right, wrap="word", width=34, height=10, relief="flat",
                             padx=px(8), pady=px(6), cursor="arrow", state="disabled")
        self.named.pack(fill=tk.BOTH, expand=True)

    def apply_palette(self, p) -> None:
        self.tree.tag_configure("diff", background=p["warn_bg"])
        self.tree.tag_configure("same", foreground=p["muted"])
        self.named.configure(background=p["card"], foreground=p["fg"])
        self.named.tag_configure("h", font=theme.HEADING_FONT, foreground=p["muted"],
                                 spacing1=px(6))
        self.named.tag_configure("k", foreground=p["muted"])

    # --- content ------------------------------------------------------------------------------

    def _rows(self) -> tuple[VersionRow, VersionRow] | None:
        rows = {r.head: r for r in self.model.versions(self.pid)}
        return (rows[self.a], rows[self.b]) if self.a in rows and self.b in rows else None

    def _fill(self) -> None:
        pair = self._rows()
        line = self.model.line(self.pid)
        rtitle(self, f"Compare - {line_name(line) if line is not None else '(gone)'}")
        self.tree.delete(*self.tree.get_children(""))
        if pair is None:
            set_label(self.strip, "One of the versions is gone.")
            return
        service = self.facts() if self.facts is not None else None
        ra, rb = fresh_row(pair[0], service), fresh_row(pair[1], service)
        for side, row in (("A", ra), ("B", rb)):
            title, sub = card_text(self.model, self.pid, row)
            set_label(self.cards[side][0], title)
            set_label(self.cards[side][1], sub)
        anc = common_ancestor(self.model, self.pid, self.a, self.b)
        set_label(self.strip, f"Nearest common ancestor: {anc.text}")
        facts = compare_facts(self.model, self.pid, ra, rb)
        shown = [f for f in facts if f.differs] if self.only_differences else facts
        for n, fact in enumerate(shown):
            self.tree.insert("", "end", f"F{n}", text=fact.name, values=(fact.a, fact.b),
                             tags=("diff",) if fact.differs else ("same",))
        differing = sum(1 for f in facts if f.differs)
        set_label(self.diff_button, f"Differences only {differing}")
        set_label(self.all_button, f"All facts {len(facts)}")
        self.diff_button.state(["disabled" if self.only_differences else "!disabled"])
        self.all_button.state(["!disabled" if self.only_differences else "disabled"])
        missing = [s for s, r in (("A", ra), ("B", rb)) if r.facts is None]
        set_label(self.note, f"content facts: not indexed yet for {' and '.join(missing)}"
                  if missing else "")
        self._fill_named(ra, rb, anc)

    def _fill_named(self, ra: VersionRow, rb: VersionRow, anc: Ancestor) -> None:
        t = self.named
        t.configure(state="normal")
        t.delete("1.0", "end")
        rtext(t, "end", "Named things that differ\n", "h")
        items = named_differences(ra, rb)
        if not items:
            rtext(t, "end", ("none" if ra.facts is not None and rb.facts is not None
                             else "needs the content index for both versions") + "\n", "k")
        for item in items:
            rtext(t, "end", item + "\n")
        rtext(t, "end", "\nVersions since the ancestor\n", "h")
        if anc.head is None or anc.head in (self.a, self.b):
            rtext(t, "end", "-\n", "k")
        else:
            rtext(t, "end", f"A: {anc.a_since}\nB: {anc.b_since}\n")
        t.configure(state="disabled")

    def on_index_events(self, events: list) -> None:
        """Index events (already on the Tk thread): redraw when one is about a shown version."""
        mine = {self.a, self.b}
        if any(e.kind in ("changes", "indexed", "failed", "cleared", "warmed") and
               (e.vid is None or e.vid in mine) for e in events):
            self._fill()

    def set_only_differences(self, only: bool) -> None:
        self.only_differences = only
        self._fill()

    def swap(self) -> None:
        self.a, self.b = self.b, self.a
        self._fill()

    def set_versions(self, a: str, b: str) -> None:
        self.a, self.b = a, b
        self._fill()

    def set_model(self, model: ReadModel) -> None:
        self.model = model
        self._fill()

    def pick(self) -> None:
        PickDialog(self)

    def table(self) -> list[tuple]:
        return [(self.tree.item(i, "text"), *self.tree.item(i, "values"))
                for i in self.tree.get_children("")]


class PickDialog(tk.Toplevel):
    """Choose the two versions to compare from the save file's versions."""

    def __init__(self, owner: CompareWindow) -> None:
        super().__init__(owner)
        self.owner = owner
        rtitle(self, "Pick two versions")
        numbers = version_numbers(owner.model, owner.pid)
        self.labels = {f"#{numbers[r.head]}  {local_time(r.saved_at)}  "
                       f"{play_time(r.play_time) or '0h00'}": r.head
                       for r in owner.model.versions(owner.pid)}
        heads = {h: n for n, h in self.labels.items()}
        frame = ttk.Frame(self, padding=px(12))
        frame.pack(fill=tk.BOTH, expand=True)
        self.boxes = []
        for n, (side, head) in enumerate((("A", owner.a), ("B", owner.b))):
            ttk.Label(frame, text=side).grid(row=n, column=0, sticky="w", pady=px(3))
            box = ttk.Combobox(frame, values=list(self.labels), state="readonly", width=38)
            box.set(heads.get(head, ""))
            box.grid(row=n, column=1, padx=pxs(8, 0), pady=px(3))
            self.boxes.append(box)
        row = ttk.Frame(frame)
        row.grid(row=2, column=0, columnspan=2, sticky="e", pady=pxs(8, 0))
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(row, text="Compare", command=self.ok).pack(side=tk.RIGHT, padx=pxs(0, 6))
        self.transient(owner)
        bind_escape(self, self.destroy)
        bind_return(self, self.ok)             # Compare only opens a read-only view
        focus_first(self, self.boxes[0])

    def ok(self) -> None:
        a, b = (self.labels.get(box.get()) for box in self.boxes)
        if a and b:
            self.owner.set_versions(a, b)
            self.destroy()
