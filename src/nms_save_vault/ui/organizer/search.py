"""The Search window: find text anywhere in the vault - save file names, notes, labels, and what
the content index read from the saves (bases and their galaxies, ships, companions, ...).

The search itself is the core's (``core.index.search``) and runs in the UI worker, since the
index's facts are a cache read; the window shows one row per save file, field and value, says
where to look (a live slot such as S9, else Vault), which versions it is in, and how much the
index had read. Double-click (or Enter) shows the
newest of those versions in the table.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ...core.index import search as core_search
from ...core.index.search import Hit, SearchResult
from ...core.organizer.readmodel import ReadModel
from ..dialogs import bind_escape, focus_first
from ..dpi import fit_to_content, px, pxs
from ..redact import RedactingTreeview, rtitle, set_label
from ..themeaware import ThemeRegistry
from ..worker import UiWorker
from .history import where_text
from .words import find_row, line_name, version_numbers

EVERYTHING = "Everything"
COLUMNS = (("where", "Where", 90, "w"), ("field", "Found in", 100, "w"),
           ("value", "Text", 220, "w"), ("detail", "Detail", 200, "w"),
           ("versions", "Versions", 130, "w"))
MAX_GROUPS = 6           # version ranges a row lists before "+n more"


def versions_text(heads: tuple[str, ...], numbers: dict[str, int]) -> str:
    """Which versions a hit is in: "all 5", "#5 (newest)", "#1-#3, #5"; "" for the save file
    itself (its name, its note)."""
    if not heads:
        return ""
    total = len(numbers)
    found = sorted(numbers[h] for h in heads if h in numbers)
    if len(found) == total and total > 1:
        return f"all {total}"
    if found == [total]:
        return f"#{total} (newest)"
    groups: list[str] = []
    start = prev = found[0]
    for n in found[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        groups.append(f"#{start}" if start == prev else f"#{start}-#{prev}")
        if n is not None:
            start = prev = n
    shown = ", ".join(groups[:MAX_GROUPS])
    return shown + (f" +{len(groups) - MAX_GROUPS} more" if len(groups) > MAX_GROUPS else "")


def status_text(result: SearchResult, index_on: bool) -> str:
    files = len({h.pid for h in result.hits})
    n = len(result.hits)
    text = (f"{n} match{'es' if n != 1 else ''} in {files} save file{'s' if files != 1 else ''}"
            if n else "No matches")
    text += f" - {result.versions} version{'s' if result.versions != 1 else ''} searched"
    if not index_on:
        text += ("; the content index is off, so bases, ships, galaxies and the like are not "
                 "searched")
    elif result.unindexed:
        text += (f"; {result.unindexed} not read by the content index yet (their bases, ships "
                 "and the like are not searched)")
    return text


def where_found(model: ReadModel, hit: Hit) -> str:
    """Where to look: the live slots holding one of the versions it is in (S9, X4), else where
    the newest of them is kept (Vault); for the save file itself, its live slots or Vault."""
    line = model.line(hit.pid)
    if line is None:
        return ""
    if not hit.heads:
        return " ".join(b.label for b in line.live) or "Vault"
    live = [b.label for b in line.live if b.head in hit.heads]
    if live:
        return " ".join(live)
    row = find_row(model, hit.pid, hit.heads[0])
    return where_text(row, line) if row is not None else ""


def result_rows(model: ReadModel, result: SearchResult) -> list[tuple[str, str | None, str, tuple]]:
    """(pid, head to show, save file name, the row's values), by save file name."""
    out = []
    numbers: dict[str, dict[str, int]] = {}
    for hit in result.hits:
        line = model.line(hit.pid)
        if line is None:
            continue
        nums = numbers.setdefault(hit.pid, version_numbers(model, hit.pid))
        out.append((hit.pid, hit.heads[0] if hit.heads else None, line_name(line),
                    (where_found(model, hit), hit.field, hit.value, hit.detail,
                     versions_text(hit.heads, nums))))
    out.sort(key=lambda r: r[2].casefold())
    return out


class SearchWindow(tk.Toplevel):
    """Non-modal. ``facts()`` is D4's IndexService (or None when it is off); ``on_show(pid,
    head)`` shows a result in the table (head None: the save file itself)."""

    def __init__(self, master, *, model: ReadModel, worker: UiWorker, themed: ThemeRegistry,
                 facts: Callable[[], object | None],
                 on_show: Callable[[str, str | None], None]) -> None:
        super().__init__(master)
        self.model = model
        self.worker = worker
        self.facts = facts
        self.on_show = on_show
        self.result: SearchResult | None = None
        self.refs: dict[str, tuple[str, str | None]] = {}
        self._asked = 0                       # the newest search: an older answer is dropped
        self.transient(master)
        rtitle(self, "Search")
        self._build()
        themed.register(self)
        fit_to_content(self, 860, 480)
        bind_escape(self, self.destroy)
        focus_first(self, self.entry)

    def _build(self) -> None:
        bar = ttk.Frame(self, padding=pxs(12, 10, 12, 4))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(bar, text="Find:").pack(side=tk.LEFT)
        self.text_var = tk.StringVar(value="")
        self.entry = ttk.Entry(bar, textvariable=self.text_var, width=28)
        self.entry.pack(side=tk.LEFT, padx=pxs(6, 10))
        self.entry.bind("<Return>", lambda _e: (self.run(), "break")[1])
        ttk.Label(bar, text="In:").pack(side=tk.LEFT)
        self.field_var = tk.StringVar(value=EVERYTHING)
        self.field_box = ttk.Combobox(bar, textvariable=self.field_var, state="readonly",
                                      values=(EVERYTHING, *core_search.FIELDS), width=14)
        self.field_box.pack(side=tk.LEFT, padx=pxs(6, 10))
        self.newest_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Newest version of each save file only",
                        variable=self.newest_var).pack(side=tk.LEFT)
        self.search_button = ttk.Button(bar, text="Search", command=self.run)
        self.search_button.pack(side=tk.RIGHT)
        self.status = ttk.Label(self, text="Type a name, a galaxy, a ship... and press Enter. "
                                           "With a field picked, an empty Find lists all of it.",
                                style="Muted.TLabel", anchor="w", padding=pxs(12, 0, 12, 4))
        self.status.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(self, text="Double-click a row (or press Enter) to show it in the table.",
                  style="Muted.TLabel", padding=pxs(12, 0, 12, 8)).pack(side=tk.BOTTOM, fill=tk.X)
        body = ttk.Frame(self, padding=pxs(10, 0, 10, 0))
        body.pack(fill=tk.BOTH, expand=True)
        self.tree = RedactingTreeview(body, columns=[c[0] for c in COLUMNS],
                                      show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Save file")
        self.tree.column("#0", width=px(180), stretch=False)
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=px(width), anchor=anchor, stretch=key == "detail")
        vsb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.show_selected())
        self.tree.bind("<Return>", lambda _e: (self.show_selected(), "break")[1])

    def apply_palette(self, p) -> None:
        self.tree.tag_configure("odd", background=p["zebra"])

    # --- searching ------------------------------------------------------------------------

    def run(self) -> None:
        """Search in the worker; the answer replaces the rows when it is still the newest."""
        field = self.field_var.get()
        field = None if field == EVERYTHING else field
        text = self.text_var.get()
        if not text.strip() and field is None:
            set_label(self.status, "Type something to find, or pick a field to list.")
            return
        service = self.facts()
        reader = getattr(service, "facts", None) if service is not None else None
        self._asked += 1
        asked, model = self._asked, self.model
        set_label(self.status, "Searching...")
        self.worker.submit(core_search.search, model, text, field=field, facts=reader,
                           newest_only=self.newest_var.get(), low=True,
                           on_done=lambda r: self._found(asked, model, r, reader is not None),
                           on_error=self._failed)

    def _found(self, asked: int, model: ReadModel, result: SearchResult, index_on: bool) -> None:
        if asked != self._asked or not self.winfo_exists():
            return
        self.result = result
        tree = self.tree
        tree.delete(*tree.get_children(""))
        self.refs.clear()
        for n, (pid, head, name, values) in enumerate(result_rows(model, result)):
            iid = f"R{n}"
            tree.insert("", "end", iid, text=name, values=values, tags=("odd",) if n % 2 else ())
            self.refs[iid] = (pid, head)
        set_label(self.status, status_text(result, index_on))
        first = next(iter(self.refs), None)
        if first is not None:
            tree.selection_set(first)
            tree.focus(first)

    def _failed(self, error: BaseException) -> None:
        if self.winfo_exists():
            set_label(self.status, f"The search failed: {error}")

    def set_model(self, model: ReadModel) -> None:
        """A newer read model: the next search uses it (the rows shown stay)."""
        self.model = model

    # --- showing ------------------------------------------------------------------------------

    def show_selected(self) -> None:
        ref = self.refs.get(self.tree.focus())
        if ref is not None and self.model.line(ref[0]) is not None:
            self.on_show(*ref)

    def rows(self) -> list[tuple]:
        return [(self.tree.item(i, "text"), *self.tree.item(i, "values"))
                for i in self.tree.get_children("")]
