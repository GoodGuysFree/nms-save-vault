"""The History window (D6 2.5, 3.4; U24): one save file's history on the web mockup's layout.

A header with the legend, a left card (Key / All, "How it is linked", the table with its lane
images, the branch tips) and a right panel for the selected version. Only what the app recorded
or saw is drawn as a tree (its own moves, the live slot, two consecutive app backups of the
same slot, the owner's corrections); every other version is a dated list - no link is guessed.
Each platform's live copy is its own tip, and a copy that lags says "behind".

The rows, the tips and the panel are pure functions of D1's ``HistoryGraph`` and the read model
(below); the window only shows them. Facts (D4) are optional: without them the Parts and
Bases columns are empty and "what changed" says what the read model knows (play time).
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Collection
from dataclasses import dataclass
from tkinter import ttk

from ... import theme
from ...core.organizer.readmodel import Line, ReadModel, VersionRow
from .. import dpi, raster
from ..dpi import fit_to_content, px, pxs
from ..fmt import local_time, play_time
from ..redact import RedactingTreeview, rtext, rtitle, set_label
from ..themeaware import ThemeRegistry
from .lanes import LaneCell, lane_cells, lane_width
from .words import line_name, version_numbers

STYLE = "Organizer.Treeview"
MODES = ("key", "all")
HOW = {"op": "recorded move", "oplog": "recorded move", "journal": "seen in the slot",
       "capture-sequence": "seen in backups", "owner": "set by you"}
EXPLAIN = ("Two versions are linked only where the app saw it: its own moves, the live slot, or "
           "two consecutive app backups of the same slot ('seen in backups'), and what you "
           "told it. The rest is listed by date - no link is guessed.")
COLUMNS = (("saved", "Saved", 130, "w"), ("play", "Play time", 76, "e"),
           ("where", "Where", 130, "w"), ("how", "How it is linked", 120, "w"),
           ("parts", "Parts", 64, "e"), ("bases", "Bases", 50, "e"),
           ("note", "Label / what changed", 360, "w"))

#: A segment of the version panel: its text and its tags (the detail pane's convention).
Segment = tuple[str, tuple]


@dataclass(frozen=True)
class HRow:
    """One row of the history table. ``kind``: "sec" a section row, "ver" a version, "run" a
    folded run of versions (click to show them)."""
    key: tuple
    kind: str
    head: str | None
    text: str
    values: tuple
    tags: tuple
    image: object = ""
    run: tuple[str, ...] = ()       # the versions a run row folds


@dataclass(frozen=True)
class Tip:
    head: str
    lane: int
    name: str
    saved: str
    play: str
    where: str
    since: str
    live: bool


# --- the rows (pure) ---------------------------------------------------------------------


def where_text(row: VersionRow, line: Line | None) -> str:
    """The live slots the version is in (a lagging copy says how far behind), else where it
    is kept."""
    badges = [b for b in (line.live if line else ()) if b.head == row.head]
    if badges:
        text = " ".join(b.label for b in badges)
        behind = max(b.behind_by for b in badges)
        return f"{text} (behind {play_time(behind) or '0h00'})" if behind else text
    if row.durable_copies:
        return "Vault"
    return "outside the vault" if "outside-only" in row.flags else "no copy left"


def _facts(row: VersionRow) -> tuple[str, str]:
    view = row.facts
    if view is None:
        return "", ""
    parts, bases = getattr(view, "base_parts", None), getattr(view, "own_bases", None)
    return (f"{parts:,}" if isinstance(parts, int) else "",
            str(bases) if isinstance(bases, int) else "")


def _note(row: VersionRow, since: str = "") -> str:
    text = row.label or next(iter(row.comment.splitlines()), "")
    if row.label:
        text = f'"{text}"'
    return f"{text}  {since}".strip()


def key_counts(model: ReadModel, pid: str) -> tuple[int, int]:
    """(key versions, all versions) for the two switch buttons."""
    rows = model.versions(pid)
    return sum(1 for r in rows if r.key), len(rows)


def _ver_row(row: VersionRow, number: int, how: str, line: Line | None, image, *,
             tags: tuple = ("ver",), since: str = "") -> HRow:
    parts, bases = _facts(row)
    bold = ("recorded",) if how == "recorded move" else ()
    return HRow(("v", row.head), "ver", row.head, f"#{number}",
                (local_time(row.saved_at), play_time(row.play_time) or "0h00",
                 where_text(row, line), how, parts, bases, _note(row, since)),
                tags + bold + (("locked",) if row.locked else ()), image)


def _run_text(rows: list[VersionRow]) -> tuple[str, str]:
    first, last = rows[0], rows[-1]            # newest first
    span = f"{local_time(last.saved_at)[:10]} to {local_time(first.saved_at)[:10]}"
    played = f"({play_time(last.play_time) or '0h00'} - {play_time(first.play_time) or '0h00'})"
    return (f"> {len(rows)} more version{'s' if len(rows) != 1 else ''}",
            f"{span} {played} - click to show")


def _run_row(key: tuple, rows: list[VersionRow], is_open: bool, image) -> HRow:
    saved, note = _run_text(rows)
    if is_open:
        saved, note = saved.replace(">", "v", 1), note.replace("click to show", "click to hide")
    return HRow(key, "run", None, "", (saved, "", "", "", "", "", note), ("run",), image,
                tuple(r.head for r in rows))


def history_rows(model: ReadModel, pid: str, mode: str, open_runs: Collection[tuple] = ()
                 ) -> list[HRow]:
    """The table: section rows, the drawn tree (newest first), then the dated list. In "key"
    mode runs of versions that are not key versions fold into one clickable row."""
    graph = model.history(pid, mode)
    full = model.history(pid, "all") if mode != "all" else graph
    line = model.line(pid)
    numbers = version_numbers(model, pid)
    cells = lane_cells(graph)
    parent_of = {e.child: e.parent for e in full.edges}
    by_vid = {n.vid: n for n in full.nodes}
    edge_into = {e.child: e for e in graph.edges}
    out: list[HRow] = []

    nodes = [n for n in graph.nodes if n.kind == "version"]
    if nodes:
        tips = sum(1 for n in full.nodes if "tip" in n.flags and n.kind == "version")
        out.append(HRow(("sec", "linked"), "sec", None, "LINKED HISTORY",
                        ("", "", "", "", "", "",
                         f"what the app recorded or saw; {tips} branch tip"
                         f"{'s' if tips != 1 else ''}"), ("sec",)))
    for n in nodes:
        row, e = n.version, edge_into.get(n.vid)
        if e is not None:
            how = HOW.get(e.method, "linked")
        else:
            how = "branch starts here" if "fork" in n.flags else "first linked"
        since = ""
        parent = by_vid.get(parent_of.get(n.vid, ""))
        if parent is not None and row.play_time > parent.version.play_time:
            since = f"+{_played(row.play_time - parent.version.play_time)} played"
        out.append(_ver_row(row, numbers[row.head], how, line, ("lane", cells[n.vid]),
                            since=since))
        if e is not None and e.hidden:
            hidden, vid = [], parent_of.get(n.vid)
            for _ in range(e.hidden):
                node = by_vid.get(vid)
                if node is None:
                    break
                hidden.append(node.version)
                vid = parent_of.get(vid)
            if hidden:
                cell = cells[n.vid]
                run_cell = LaneCell(cell.lane, cell.lanes, cell.passing, True, True, (), "run")
                key = ("run", "l", hidden[0].head)
                is_open = key in open_runs
                out.append(_run_row(key, hidden, is_open, ("lane", run_cell)))
                if is_open:
                    dot = LaneCell(cell.lane, cell.lanes, cell.passing, True, True, (), "dot")
                    for h in hidden:
                        out.append(_ver_row(h, numbers[h.head], HOW.get(
                            _edge_method(full, h.head), "linked"), line, ("lane", dot)))

    dated = [r for r in graph.dated if mode == "all" or r.folded_into is None]
    if dated:
        title = "NOT LINKED - BY DATE" if nodes else "NO RECORDED HISTORY - BY DATE"
        out.append(HRow(("sec", "dated"), "sec", None, title,
                        ("", "", "", "", "", "",
                         f"{len(dated)} version{'s' if len(dated) != 1 else ''}; "
                         "no recorded move or app backup links them"), ("sec",)))
        out += _dated_rows(dated, mode, numbers, line, open_runs)
    return out


def _edge_method(graph, vid: str) -> str:
    return next((e.method for e in graph.edges if e.child == vid), "")


def _played(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds < 3600 else \
        f"{seconds / 3600:.1f} h".replace(".0 h", " h")


def _dated_rows(dated: list[VersionRow], mode: str, numbers: dict, line: Line | None,
                open_runs: Collection[tuple]) -> list[HRow]:
    out: list[HRow] = []
    last = len(dated) - 1
    pending: list[VersionRow] = []

    def flush() -> None:
        if not pending:
            return
        key = ("run", "d", pending[0].head)
        is_open = key in open_runs
        out.append(_run_row(key, list(pending), is_open, ("dot", "muted")))
        if is_open:
            out.extend(_ver_row(r, numbers[r.head], "by date", line, ("dot", "muted"),
                                tags=("ver", "dim")) for r in pending)
        pending.clear()

    for i, row in enumerate(dated):
        keep = mode == "all" or row.key or i in (0, last) or row.locked or row.label
        if not keep:
            pending.append(row)
            continue
        flush()
        how = "first seen" if i == last else "by date"
        out.append(_ver_row(row, numbers[row.head], how, line, ("dot", "muted"),
                            tags=("ver", "dim")))
    flush()
    return out


# --- branch tips (pure) -----------------------------------------------------------------


def branch_tips(model: ReadModel, pid: str) -> list[Tip]:
    """One per tip of the drawn tree, newest first: its lane, where it is, and where its
    branch started (a platform's live copy is its own tip)."""
    graph = model.history(pid, "all")
    line = model.line(pid)
    parent_of = {e.child: e.parent for e in graph.edges}
    kids: dict[str, int] = {}
    for e in graph.edges:
        kids[e.parent] = kids.get(e.parent, 0) + 1
    by_vid = {n.vid: n for n in graph.nodes}
    out = []
    for n in graph.nodes:
        if n.kind != "version" or kids.get(n.vid):
            continue
        chain, cur = [n.vid], n.vid
        while True:                       # up to the fork or the root: this branch
            up = parent_of.get(cur)
            if up is None:
                origin = cur
                break
            if kids.get(up, 0) > 1:
                origin = up
                break
            chain.append(up)
            cur = up
        count = len(chain) - (1 if origin in chain else 0)
        row = n.version
        name = "main line" if n.lane == 0 else (row.label[:24] or f"branch {n.lane + 1}")
        out.append(Tip(row.head, n.lane, name, local_time(row.saved_at),
                       play_time(row.play_time) or "0h00", where_text(row, line),
                       f"from {local_time(by_vid[origin].version.saved_at)}, {count} "
                       f"version{'s' if count != 1 else ''} since", bool(row.live_in)))
    return out


# --- the version panel (pure) -----------------------------------------------------------


def panel_segments(model: ReadModel, pid: str, head: str) -> list[Segment]:
    """The right panel for one version: callouts, what changed, events, where, contents."""
    row = next((r for r in model.versions(pid) if r.head == head), None)
    if row is None:
        return []
    numbers = version_numbers(model, pid)
    rows = {r.head: r for r in model.versions(pid)}
    out: list[Segment] = []
    if row.label:
        out += [(f"{row.label}\n", ("callout", "b")), ("label\n", ("k",))]
    kids = [r for r in model.versions(pid) if r.parent == head]
    if len(kids) > 1:
        names = ", ".join(f"#{numbers[r.head]} {local_time(r.saved_at)}" for r in kids)
        out += [(f"{len(kids)} branches continue from here\n", ("branch", "b")),
                (f"{names}\n", ("branch",))]
    parent = rows.get(row.parent) if row.parent else None
    out.append((f"WHAT CHANGED SINCE #{numbers[parent.head]}\n" if parent
                else "WHAT CHANGED\n", ("h",)))
    if parent is None:
        out.append(("nothing shown - no recorded parent\n", ("k",)))
    else:
        how = HOW.get(row.parent_method or "", "linked")
        out.append((f"its parent, {how}\n", ("k",)))
        delta = row.play_time - parent.play_time
        out.append((f"+{_played(delta)} played\n" if delta > 0
                    else "no play time since\n", ("item",)))
    events = [e for e in model.events(pid) if head in e.vids]
    if events:
        out.append(("EVENTS\n", ("h",)))
        for e in events:
            out.append((f"{local_time(e.at)}  {e.text}\n",
                        ("warn",) if e.kind == "reverted" else ("event",)))
    out.append(("WHERE IT IS\n", ("h",)))
    for loc in row.locations or ():
        out.append((f"{loc.label}\n", ()))
    if not row.locations:
        out.append(("no copy left anywhere\n", ("warn",)))
    out.append(("CONTENTS\n", ("h",)))
    out += _contents(row)
    return out


def _contents(row: VersionRow) -> list[Segment]:
    view = row.facts
    if view is None:
        return [("not indexed yet\n", ("k",))]
    items = [("Difficulty", row.difficulty or ""),
             ("Bases", f"{getattr(view, 'own_bases', 0)} "
                       f"({getattr(view, 'base_parts', 0):,} parts)"),
             ("Ships", str(getattr(view, "ships", 0))),
             ("Companions", str(getattr(view, "companions", 0)))]
    return [(f"{k}\t{v}\n", ()) for k, v in items if v]


# --- the window --------------------------------------------------------------------------


class HistoryWindow(tk.Toplevel):
    """Non-modal. Callbacks (all optional): ``on_move(pid, head)``, ``on_compare(pid, a, b)``
    (``b`` None: the line's live version), ``on_label(pid, head)``,
    ``on_lock(pid, head, locked)``, ``on_correct(pid, head)``."""

    def __init__(self, master, *, model: ReadModel, themed: ThemeRegistry, pid: str,
                 head: str | None = None, on_move: Callable | None = None,
                 on_compare: Callable | None = None, on_label: Callable | None = None,
                 on_lock: Callable | None = None, on_correct: Callable | None = None) -> None:
        super().__init__(master)
        self.model = model
        self.pid = pid
        self.mode = "key"
        self.on_move, self.on_compare = on_move, on_compare
        self.on_label, self.on_lock, self.on_correct = on_label, on_lock, on_correct
        self.open_runs: set[tuple] = set()
        self.rows: dict[str, HRow] = {}
        self._iid_of_head: dict[str, str] = {}
        self._images: dict = {}
        self._palette: dict = {}
        self.head: str | None = None
        self._build()
        themed.register(self)
        self.show(pid, head)
        fit_to_content(self, 1180, 640)

    # --- building ---------------------------------------------------------------------

    def _build(self) -> None:
        head = ttk.Frame(self, padding=pxs(12, 10, 12, 4))
        head.pack(side=tk.TOP, fill=tk.X)
        self.title_label = ttk.Label(head, text="", style="Title.TLabel")
        self.title_label.pack(side=tk.LEFT)
        self.badge = ttk.Label(head, text="", style="Muted.TLabel")
        self.badge.pack(side=tk.LEFT, padx=pxs(8, 0))
        self.counts = ttk.Label(head, text="", style="Muted.TLabel")
        self.counts.pack(side=tk.LEFT, padx=pxs(10, 0))
        self.legend = tk.Canvas(head, height=px(22), highlightthickness=0, width=px(520))
        self.legend.pack(side=tk.RIGHT)
        self.problems = ttk.Label(self, text="", style="Alert.TLabel", anchor="w",
                                  wraplength=px(1100))
        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.body = body
        body.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, padx=px(10), pady=pxs(4, 10))
        left, right = ttk.Frame(body), ttk.Frame(body)
        body.add(left, weight=4)
        body.add(right, weight=1)

        bar = ttk.Frame(left)
        bar.pack(fill=tk.X, pady=pxs(0, 4))
        self.key_button = ttk.Button(bar, text="Key versions", command=lambda: self.set_mode("key"))
        self.all_button = ttk.Button(bar, text="All", command=lambda: self.set_mode("all"))
        self.key_button.pack(side=tk.LEFT)
        self.all_button.pack(side=tk.LEFT, padx=pxs(4, 8))
        self.explain = ttk.Label(bar, text=EXPLAIN, style="Muted.TLabel", wraplength=px(720),
                                 justify="left")
        self.explain.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tips = ttk.Labelframe(left, text="Branch tips", padding=px(6))
        tips.pack(side=tk.BOTTOM, fill=tk.X, pady=pxs(6, 0))
        self.tips_frame = tips
        wrap = ttk.Frame(left)
        wrap.pack(fill=tk.BOTH, expand=True)
        keys = [c[0] for c in COLUMNS]
        self.tree = RedactingTreeview(wrap, columns=keys, show="tree headings",
                                      selectmode="extended", style=STYLE)
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=px(150), stretch=False, anchor="w")
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=px(width), anchor=anchor, stretch=key == "note")
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Button-1>", self._clicked, add="+")

        self.panel_title = ttk.Label(right, text="", style="Head.TLabel", anchor="w",
                                     wraplength=px(300))
        self.panel_title.pack(fill=tk.X)
        self.panel_sub = ttk.Label(right, text="", style="Muted.TLabel", anchor="w",
                                   wraplength=px(300))
        self.panel_sub.pack(fill=tk.X)
        actions = ttk.Frame(right)
        actions.pack(fill=tk.X, pady=pxs(6, 4))
        self.move_button = ttk.Button(actions, text="Move to live...", command=self.move)
        self.compare_button = ttk.Button(actions, text="Compare...", command=self.compare)
        self.label_button = ttk.Button(actions, text="Label", command=self.label)
        self.lock_button = ttk.Button(actions, text="Lock", command=self.lock)
        self.correct_button = ttk.Button(actions, text="Correct...", command=self.correct)
        for b in (self.move_button, self.compare_button, self.label_button, self.lock_button,
                  self.correct_button):
            b.pack(side=tk.LEFT, padx=pxs(0, 4))
        self.panel = tk.Text(right, wrap="word", height=12, relief="flat", borderwidth=0,
                             highlightthickness=0, padx=px(8), pady=px(6), cursor="arrow",
                             font="TkDefaultFont", tabs=(px(96),), width=44, state="disabled")
        self.panel.pack(fill=tk.BOTH, expand=True)
        self.keys = ttk.Label(self, text="Up / Down version   Left / Right branch   C compare   "
                                         "M move to live   K key versions / all",
                              style="Muted.TLabel", padding=pxs(12, 0, 12, 6))
        self.keys.pack(side=tk.BOTTOM, fill=tk.X)
        for seq, command in (("<Left>", lambda: self._branch(-1)),
                             ("<Right>", lambda: self._branch(1)),
                             ("<c>", self.compare), ("<m>", self.move),
                             ("<k>", self.toggle_mode)):
            self.bind(seq, lambda _e, c=command: (c(), "break")[1])

    def apply_palette(self, p) -> None:
        self._palette = dict(p)
        self.panel.configure(background=p["card"], foreground=p["fg"],
                             selectbackground=p["sel_bg"], selectforeground=p["sel_fg"])
        for tag, kw in (("h", dict(font=theme.HEADING_FONT, spacing1=px(8), spacing3=px(2),
                                   foreground=p["muted"])),
                        ("b", dict(font=theme.BOLD_FONT)),
                        ("k", dict(foreground=p["muted"], font=theme.SMALL_FONT)),
                        ("warn", dict(foreground=p["warn"])),
                        ("event", dict(foreground=p["event"])),
                        ("callout", dict(background=p["warn_bg"], lmargin1=px(6),
                                         lmargin2=px(6), spacing1=px(4))),
                        ("branch", dict(background=p["group_bg"], lmargin1=px(6),
                                        lmargin2=px(6))),
                        ("item", dict(lmargin1=px(12), lmargin2=px(12)))):
            self.panel.tag_configure(tag, **kw)
        tree = self.tree
        tree.tag_configure("sec", foreground=p["muted"], background=p["group_bg"],
                           font=theme.BOLD_FONT)
        tree.tag_configure("run", foreground=p["muted"], font=theme.SMALL_FONT)
        tree.tag_configure("dim", foreground=p["muted"])
        tree.tag_configure("recorded", font=theme.BOLD_FONT)
        self._draw_legend()
        self._images = {}
        self._fill()

    def _draw_legend(self) -> None:
        p, c, s = self._palette, self.legend, dpi.factor()
        c.configure(background=p["bg"])
        c.delete("all")
        x, mid = px(4), px(11)
        c.create_line(x, mid, x + px(22), mid, fill=p["lane0"], width=max(2, px(2)))
        c.create_text(x + px(26), mid, anchor="w", text="linked", fill=p["fg"])
        x += px(76)
        r = 5 * s
        c.create_oval(x - r, mid - r, x + r, mid + r, fill=p["steam"], outline=p["steam"])
        c.create_oval(x - 2 * s, mid - 2 * s, x + 2 * s, mid + 2 * s, fill=p["card"],
                      outline=p["card"])
        c.create_text(x + px(10), mid, anchor="w", text="live in a slot", fill=p["fg"])
        x += px(130)
        c.create_polygon(x, mid - r, x + r, mid, x, mid + r, x - r, mid, fill=p["lane1"])
        c.create_text(x + px(10), mid, anchor="w", text="fork point", fill=p["fg"])
        x += px(100)
        c.create_oval(x - 3 * s, mid - 3 * s, x + 3 * s, mid + 3 * s, fill=p["muted"],
                      outline=p["muted"])
        c.create_text(x + px(8), mid, anchor="w", text="not linked: by date", fill=p["fg"])

    # --- showing ----------------------------------------------------------------------------

    def show(self, pid: str, head: str | None = None) -> None:
        self.pid = pid
        self.open_runs.clear()
        self._fill(select=head)

    def set_model(self, model: ReadModel) -> None:
        """A newer read model: rebuild, keeping the mode, the opened runs and the selection."""
        self.model = model
        self._fill(select=self.head)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode
        self.open_runs.clear()
        self._fill(select=self.head)

    def toggle_mode(self) -> None:
        self.set_mode("all" if self.mode == "key" else "key")

    def _image(self, key):
        if isinstance(key, tuple) and key and key[0] == "lane":
            if key not in self._images:
                p, cell = self._palette, key[1]
                self._images[key] = raster.lane_row(
                    px(lane_width(cell.lanes)), self._row_height(), cell, theme.lanes(p),
                    scale=dpi.factor(), live_colour=p[cell.platform], centre=p["card"]
                ).image(self)
            return self._images[key]
        if isinstance(key, tuple) and key and key[0] == "dot":
            if key not in self._images:
                self._images[key] = raster.version_dot(
                    px(18), self._row_height(), "dot", colour=self._palette[key[1]],
                    scale=dpi.factor()).image(self)
            return self._images[key]
        return ""

    def _row_height(self) -> int:
        try:
            return int(ttk.Style(self).lookup("Treeview", "rowheight")) or px(24)
        except (tk.TclError, ValueError):
            return px(24)

    def _fill(self, select: str | None = None) -> None:
        if not self._palette:
            return
        line = self.model.line(self.pid)
        graph = self.model.history(self.pid, self.mode)
        name = line_name(line) if line is not None else "(gone)"
        rtitle(self, f"History - {name}")
        set_label(self.title_label, name)
        set_label(self.badge, " ".join(b.label for b in line.live) if line else "")
        tips = branch_tips(self.model, self.pid)
        total = len(self.model.versions(self.pid))
        set_label(self.counts, f"{total} versions - {len(tips)} branch tip"
                  f"{'s' if len(tips) != 1 else ''} - "
                  f"{play_time(line.play_time) if line else ''} played".rstrip())
        key_n, all_n = key_counts(self.model, self.pid)
        set_label(self.key_button, f"Key versions {key_n}")
        set_label(self.all_button, f"All {all_n}")
        self.key_button.state(["disabled" if self.mode == "key" else "!disabled"])
        self.all_button.state(["disabled" if self.mode == "all" else "!disabled"])
        if graph.problems:
            self.problems.configure(text=shown_problems(graph.problems))
            self.problems.pack(side=tk.TOP, fill=tk.X, padx=px(12), before=self.body)
        else:
            self.problems.pack_forget()
        tree = self.tree
        tree.delete(*tree.get_children(""))
        self.rows.clear()
        self._iid_of_head.clear()
        for n, row in enumerate(history_rows(self.model, self.pid, self.mode, self.open_runs)):
            iid = f"H{n}"
            tree.insert("", "end", iid, text=row.text, values=row.values, tags=row.tags,
                        image=self._image(row.image))
            self.rows[iid] = row
            if row.head is not None:
                self._iid_of_head.setdefault(row.head, iid)
        self._fill_tips(tips)
        target = select if select in self._iid_of_head else None
        if target is None and self._iid_of_head:
            target = next(r.head for r in self.rows.values() if r.head)
        if target is not None:
            self._select_head(target)
        else:
            self.head = None
            self._show_panel()

    def _fill_tips(self, tips: list[Tip]) -> None:
        for child in self.tips_frame.winfo_children():
            child.destroy()
        p = self._palette
        colours = theme.lanes(p)
        for n, tip in enumerate(tips):
            col = colours[tip.lane % len(colours)]
            tk.Label(self.tips_frame, text=tip.name, background=col, foreground="#ffffff",
                     padx=px(6)).grid(row=n, column=0, sticky="we", padx=px(2), pady=px(1))
            for c, text in enumerate((tip.saved, tip.play, tip.where, tip.since), start=1):
                ttk.Label(self.tips_frame, text=text).grid(row=n, column=c, sticky="w",
                                                            padx=px(8))
            if tip.live:
                ttk.Label(self.tips_frame, text="live now", style="Muted.TLabel").grid(
                    row=n, column=5, sticky="e")
            else:
                box = ttk.Frame(self.tips_frame)
                box.grid(row=n, column=5, sticky="e")
                ttk.Button(box, text="Move to live...",
                           command=lambda h=tip.head: self._move(h)).pack(side=tk.LEFT)
                ttk.Button(box, text="Compare with live",
                           command=lambda h=tip.head: self._compare(h, None)).pack(
                    side=tk.LEFT, padx=pxs(4, 0))
        self.tips_frame.columnconfigure(4, weight=1)

    # --- the selection ----------------------------------------------------------------------

    def _select_head(self, head: str) -> None:
        iid = self._iid_of_head.get(head)
        if iid is None:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self.head = head
        self._show_panel()

    def selected_heads(self) -> list[str]:
        heads = []
        for iid in self.tree.selection():
            row = self.rows.get(iid)
            if row is not None and row.head is not None:
                heads.append(row.head)
        return heads

    def _selected(self, _event=None) -> None:
        heads = self.selected_heads()
        if heads:
            focus = self.rows.get(self.tree.focus())
            self.head = focus.head if focus is not None and focus.head in heads else heads[-1]
            self._show_panel()

    def _clicked(self, event) -> str | None:
        """A run row opens or closes where it was clicked."""
        iid = self.tree.identify_row(event.y)
        row = self.rows.get(iid)
        if row is not None and row.kind == "run":
            self.toggle_run(row.key)
            return "break"
        return None

    def toggle_run(self, key: tuple) -> None:
        self.open_runs ^= {key}
        self._fill(select=self.head)

    def _branch(self, step: int) -> None:
        """Left / Right: the next version in the next lane over (newest at or after the
        selected row)."""
        heads = [r for r in self.rows.values() if r.kind == "ver" and r.image
                 and isinstance(r.image, tuple) and r.image[0] == "lane"]
        cur = next((r for r in heads if r.head == self.head), None)
        if cur is None:
            return
        lane = cur.image[1].lane + step
        order = heads.index(cur)
        for r in heads[order:] + heads[:order][::-1]:
            if r.image[1].lane == lane:
                self._select_head(r.head)
                return

    def _show_panel(self) -> None:
        row = next((r for r in self.model.versions(self.pid) if r.head == self.head), None)
        p = self.panel
        p.configure(state="normal")
        p.delete("1.0", "end")
        if row is None:
            set_label(self.panel_title, "")
            set_label(self.panel_sub, "Select a version.")
            p.configure(state="disabled")
            return
        numbers = version_numbers(self.model, self.pid)
        line = self.model.line(self.pid)
        set_label(self.panel_title, f"{local_time(row.saved_at)}  "
                                    f"{play_time(row.play_time) or '0h00'}"
                                    f"{'  [locked]' if row.locked else ''}")
        bits = [f"#{numbers[row.head]}", "Steam" if row.platform == "steam" else "Xbox"]
        if row.mode or row.difficulty:
            bits.append(row.mode or row.difficulty)
        if line is not None:
            bits.insert(0, line_name(line))
        set_label(self.panel_sub, " - ".join(bits))
        set_label(self.lock_button, "Unlock" if row.locked else "Lock")
        for part, tags in panel_segments(self.model, self.pid, row.head):
            rtext(p, "end", part, *tags)
        p.configure(state="disabled")

    def panel_text(self) -> str:
        return self.panel.get("1.0", "end")

    # --- actions ----------------------------------------------------------------------------

    def _move(self, head: str | None) -> None:
        if self.on_move is not None and head is not None:
            self.on_move(self.pid, head)

    def move(self) -> None:
        self._move(self.head)

    def _compare(self, a: str | None, b: str | None) -> None:
        if self.on_compare is not None and a is not None:
            self.on_compare(self.pid, a, b)

    def compare(self) -> None:
        """Two selected versions (Ctrl+click adds the second), or this one with the live
        one."""
        heads = self.selected_heads()
        if len(heads) >= 2:
            self._compare(heads[0], heads[1])
        else:
            self._compare(self.head, None)

    def label(self) -> None:
        if self.on_label is not None and self.head is not None:
            self.on_label(self.pid, self.head)

    def lock(self) -> None:
        row = next((r for r in self.model.versions(self.pid) if r.head == self.head), None)
        if self.on_lock is not None and row is not None:
            self.on_lock(self.pid, row.head, not row.locked)

    def correct(self) -> None:
        if self.on_correct is not None and self.head is not None:
            self.on_correct(self.pid, self.head)


def shown_problems(problems: Collection[str]) -> str:
    """The notes about corrections that were left out (``HistoryGraph.problems``)."""
    return "Left out of the history: " + " ".join(f"{p.rstrip('.')}." for p in problems)
