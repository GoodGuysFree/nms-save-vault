"""The version picker of "Replace with..." (D6 2.4, screen 03b): Find, the chips Vault only /
Older versions of X / Live elsewhere, and a grouped tree of save files whose versions open
under them. Everything comes from the read model; the picker only chooses."""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ... import theme
from ...core.organizer.readmodel import Line, ReadModel
from ..dpi import px, pxs
from ..fmt import local_time, play_time
from ..redact import RedactingTreeview
from .words import line_name, version_numbers

VAULT, OLDER, ELSEWHERE = "vault", "older", "elsewhere"
GROUP_TITLES = {VAULT: "VAULT ONLY", OLDER: "OLDER VERSIONS OF", ELSEWHERE: "LIVE ELSEWHERE"}


def pick_groups(model: ReadModel, slot_pid: str | None, platform: str,
                text: str = "") -> dict[str, list[Line]]:
    """The save files the picker offers for a slot of ``platform``: the ones in no live slot,
    the slot's own save file (its older versions), and the ones live in another slot. Other
    platforms' saves are not listed (moving between Steam and Xbox comes later)."""
    needle = text.strip().lower()
    groups: dict[str, list[Line]] = {VAULT: [], OLDER: [], ELSEWHERE: []}
    for line in model.lines():
        if platform not in line.platforms:
            continue
        if needle and needle not in line_name(line).lower():
            continue
        if line.pid == slot_pid:
            groups[OLDER].append(line)
        elif not line.live:
            groups[VAULT].append(line)
        else:
            groups[ELSEWHERE].append(line)
    return groups


class VersionPicker(ttk.Frame):
    def __init__(self, master, *, model: ReadModel, slot_pid: str | None, slot_head: str | None,
                 platform: str, slot_name: str,
                 on_pick: Callable[[str, str], None]) -> None:
        super().__init__(master)
        self.model, self.slot_pid, self.slot_head = model, slot_pid, slot_head
        self.platform, self.on_pick = platform, on_pick
        self.slot_name = slot_name
        self.chosen: tuple[str, str] | None = None
        self.on: dict[str, tk.BooleanVar] = {}
        self.chips: dict[str, tk.Checkbutton] = {}
        bar = ttk.Frame(self)
        bar.pack(side=tk.TOP, fill=tk.X)
        for key in (VAULT, OLDER, ELSEWHERE):
            var = tk.BooleanVar(value=True)
            self.on[key] = var
            chip = tk.Checkbutton(bar, variable=var, indicatoron=False, padx=px(8), pady=px(2),
                                  command=self.refresh)
            chip.pack(side=tk.LEFT, padx=pxs(0, 4))
            self.chips[key] = chip
        self.find_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.find_var, width=18).pack(side=tk.RIGHT)
        ttk.Label(bar, text="Find:").pack(side=tk.RIGHT, padx=px(4))
        self.find_var.trace_add("write", lambda *_: self.refresh())
        frame = ttk.Frame(self)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=pxs(4, 0))
        self.tree = RedactingTreeview(frame, columns=("play", "when", "what"),
                                      show="tree headings", selectmode="browse", height=9)
        self.tree.heading("#0", text="Save file / version")
        self.tree.column("#0", width=px(210), stretch=False)
        for key, title, width in (("play", "Play time", 70), ("when", "Saved", 120),
                                  ("what", "", 220)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=px(width), stretch=key == "what")
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewOpen>>", lambda _e: self._open(self.tree.focus()))
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.refresh()

    def apply_palette(self, p) -> None:
        self.tree.tag_configure("group", font=theme.BOLD_FONT, foreground=p["muted"])
        self.tree.tag_configure("chosen", background=p["in_bg"], foreground=p["in_fg"])
        for chip in self.chips.values():
            chip.configure(background=p["btn_bg"], foreground=p["fg"], selectcolor=p["chip_on"],
                           activebackground=p["hover_bg"], relief="groove", borderwidth=1)

    def counts(self) -> dict[str, int]:
        groups = pick_groups(self.model, self.slot_pid, self.platform)
        return {key: len(lines) for key, lines in groups.items()}

    def refresh(self) -> None:
        groups = pick_groups(self.model, self.slot_pid, self.platform, self.find_var.get())
        totals = self.counts()
        names = {VAULT: "Vault only", OLDER: f"Older versions of {self.slot_name}",
                 ELSEWHERE: "Live elsewhere"}
        for key, chip in self.chips.items():
            chip.configure(text=f"{names[key]} {totals[key]}")
        self.tree.delete(*self.tree.get_children())
        for key in (VAULT, OLDER, ELSEWHERE):
            if not self.on[key].get() or not groups[key]:
                continue
            title = GROUP_TITLES[key] + (f" {self.slot_name.upper()}" if key == OLDER else
                                         f" - {len(groups[key])} SAVE FILES")
            gid = f"g:{key}"
            self.tree.insert("", "end", iid=gid, text=title, tags=("group",), open=True)
            for line in groups[key]:
                lid = f"l:{key}:{line.pid}"
                self.tree.insert(gid, "end", iid=lid, text=line_name(line),
                                 values=(play_time(line.play_time), local_time(line.last_played),
                                         f"{line.version_count} versions"))
                self.tree.insert(lid, "end", iid=f"{lid}:stub")        # the open marker
                if key == OLDER:
                    self.tree.item(lid, open=True)
                    self._open(lid)

    def _open(self, iid: str) -> None:
        if not iid.startswith("l:") or not self.tree.exists(f"{iid}:stub"):
            return
        self.tree.delete(f"{iid}:stub")
        _l, key, pid = iid.split(":", 2)
        numbers = version_numbers(self.model, pid)
        for row in self.model.versions(pid):
            if key == OLDER and row.head == self.slot_head:
                continue                                       # that is what is in the slot
            mark = "(o) " if self.chosen == (pid, row.head) else "( ) "
            self.tree.insert(iid, "end", iid=f"v:{key}:{pid}:{row.head}",
                             text=f"{mark}#{numbers[row.head]}",
                             values=(play_time(row.play_time), local_time(row.saved_at),
                                     row.summary))

    def _selected(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("l:"):
            _l, _key, pid = iid.split(":", 2)
            line = self.model.line(pid)
            self.tree.item(iid, open=True)
            self._open(iid)
            if line is not None:
                self._choose(pid, line.representative)
        elif iid.startswith("v:"):
            _v, _key, pid, head = iid.split(":", 3)
            self._choose(pid, head)

    def _choose(self, pid: str, head: str) -> None:
        if self.chosen == (pid, head):
            return
        self.chosen = (pid, head)
        for iid in self._version_ids():
            _v, _key, p, h = iid.split(":", 3)
            on = (p, h) == self.chosen
            number = self.tree.item(iid, "text").split(" ", 1)[1]
            self.tree.item(iid, text=("(o) " if on else "( ) ") + number)
            tags = [t for t in self.tree.item(iid, "tags") if t != "chosen"]
            self.tree.item(iid, tags=tags + (["chosen"] if on else []))
        self.on_pick(pid, head)

    def _version_ids(self) -> list[str]:
        out = []
        for g in self.tree.get_children(""):
            for line in self.tree.get_children(g):
                out += [c for c in self.tree.get_children(line) if c.startswith("v:")]
        return out
