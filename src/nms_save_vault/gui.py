"""Tkinter desktop UI for NMS Save Vault.

A single tree shows the LIVE folder and every catalog backup; each entry expands to its
occupied slots, and each slot to its two saves -- the periodic Auto-Save and the
Restore-Point -- with the game-current one marked '*'. Toolbar actions cover all three
features plus promote and undo. Every write goes through the safety-wrapped core
(auto-snapshot + validate).
"""
from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .core import aliases, catalog, discover, locations, slotmap
from .core import operations as ops
from .core import savedir
from .core import state as appstate
from .core.catalog import Vault


def _icon_path() -> Path | None:
    """Locate nmsvault.ico — beside the packaged launcher, or in the repo's packaging/."""
    candidates = [
        Path(sys.executable).resolve().parent / "nmsvault.ico",
        Path(__file__).resolve().parents[2] / "packaging" / "nmsvault.ico",
    ]
    return next((c for c in candidates if c.is_file()), None)


def _fmt_ts(unix: int) -> str:
    if not unix:
        return ""
    try:
        return datetime.fromtimestamp(unix).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(unix)


def _fmt_play(seconds: int) -> str:
    if not seconds:
        return ""
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60:02d}"


def _fmt_size(nbytes: int) -> str:
    if not nbytes:
        return "-"
    mb = nbytes / (1024 * 1024)
    return f"{mb:.1f} MB" if mb >= 1 else f"{nbytes / 1024:.0f} KB"


def _fmt_created(iso: str) -> str:
    """Catalog timestamps are ISO-8601; show them like every other date in the UI."""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso or ""


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    width: int


LIVE_COLUMNS = (
    Column("name", "Save name", 230),
    Column("mode", "Difficulty", 90),
    Column("play", "Play Time", 90),
    Column("saved", "Saved", 130),
    Column("status", "Status", 170),
)

BACKUP_COLUMNS = (
    Column("type", "Type", 90),
    Column("slots", "Slots", 55),
    Column("name", "Name / Label", 230),
    Column("mode", "Difficulty", 90),
    Column("play", "Play Time", 90),
    Column("saved", "Saved", 130),
)

_SAVE_TYPE_HELP = {
    "Auto-Save": "the game writes this one by itself, every few minutes",
    "Restore-Point": "written when you leave your ship, or use a save point, save beacon,\n"
                     "or point-of-interest save",
}

_KIND_HELP = {
    catalog.KIND_FULL: "full — a complete snapshot of a save folder",
    catalog.KIND_SNAPSHOT: "snapshot — taken automatically just before an operation",
    catalog.KIND_EXTRACT: "extract — a single slot lifted aside",
    catalog.KIND_IMPORTED: "imported — your own backup, copied into the vault",
    catalog.KIND_INPLACE: "in place — catalogued where it already lives, not copied",
}

# Tooltip colours; the theme swaps these so hover text stays readable in dark mode.
TOOLTIP_STYLE = {"background": "#ffffe0", "foreground": "#1a1a1a", "border": "#9a9a7a"}


class Tooltip:
    """Hover text for tree rows.

    ``text_for(tree, row_id)`` supplies the text (''  to show nothing). The text is built
    when the row is created, not on hover, so moving the mouse never re-scans anything.
    """

    DELAY_MS = 450

    def __init__(self, tree, text_for):
        self.tree = tree
        self.text_for = text_for
        self.window: tk.Toplevel | None = None
        self.row: str | None = None
        self._after: str | None = None
        tree.bind("<Motion>", self._on_motion, add="+")
        tree.bind("<Leave>", self.hide, add="+")
        tree.bind("<Button>", self.hide, add="+")

    def _on_motion(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row == self.row:
            return
        self.row = row
        self._close()
        self._cancel()
        if row:
            x, y = event.x_root, event.y_root
            self._after = self.tree.after(self.DELAY_MS, lambda: self._show(row, x, y))

    def _show(self, row: str, x: int, y: int) -> None:
        self._after = None
        text = self.text_for(self.tree, row)
        if not text or row != self.row:
            return
        self.window = tk.Toplevel(self.tree)
        self.window.wm_overrideredirect(True)  # no title bar / border
        tk.Label(
            self.window,
            text=text,
            justify="left",
            relief="solid",
            borderwidth=1,
            background=TOOLTIP_STYLE["background"],
            foreground=TOOLTIP_STYLE["foreground"],
            highlightbackground=TOOLTIP_STYLE["border"],
            padx=8,
            pady=6,
            font=("TkDefaultFont", 9),
        ).pack()
        self.window.wm_geometry(f"+{x + 16}+{y + 18}")

    def hide(self, _event=None) -> None:
        self.row = None
        self._cancel()
        self._close()

    def _cancel(self) -> None:
        if self._after is not None:
            self.tree.after_cancel(self._after)
            self._after = None

    def _close(self) -> None:
        if self.window is not None:
            self.window.destroy()
            self.window = None


def _source_caption(source) -> str:
    """How a live source is named in the tree and the dropdown.

    Normally "Steam (<id>)  (st_<id>)" -- label plus folder. Once a display name replaces
    both halves they read as "Steam (Main)  (Main)", so the folder is dropped when the
    label already says it.
    """
    label = aliases.redact(source.label)
    folder = aliases.redact(Path(source.path).name)
    return label if folder in label else f"{label}  ({folder})"


def _filtered(dialog):
    """Wrap a messagebox call so its message passes through the account-alias filter."""

    def show(title, message, **kwargs):
        return dialog(title, aliases.redact(message), **kwargs)

    return show


_showinfo = _filtered(messagebox.showinfo)
_showwarning = _filtered(messagebox.showwarning)
_showerror = _filtered(messagebox.showerror)
_askyesno = _filtered(messagebox.askyesno)
_askyesnocancel = _filtered(messagebox.askyesnocancel)


class RedactingTreeview(ttk.Treeview):
    """A tree whose row text and cell values pass through the account-alias filter.

    Filtering here rather than at each call site is what makes the guarantee structural:
    no row added now or later can leak a real account id once an alias is configured.
    """

    def insert(self, parent, index, iid=None, **kw):
        if "text" in kw:
            kw["text"] = aliases.redact(kw["text"])
        if "values" in kw:
            kw["values"] = tuple(aliases.redact(v) for v in kw["values"])
        return super().insert(parent, index, iid, **kw)


class App(tk.Tk):
    def __init__(self, live: str | Path | None = None, vault: str | Path | None = None):
        super().__init__()
        self.title("NMS Save Vault")
        self.geometry("1000x640")
        _ico = _icon_path()
        if _ico:
            try:
                self.iconbitmap(str(_ico))
            except tk.TclError:
                pass

        # Load the config (or build it on first run by auto-discovering save folders).
        self.state = self._load_or_bootstrap_state()
        vault_root = Path(vault) if vault else (self.state.vault or locations.default_vault_dir())
        self.vault = Vault(vault_root)
        self.vault.ensure()
        self.vault.load()

        # The active *writable* live source is the target of write operations. An
        # explicit --live wins; otherwise the first writable (Steam) source.
        self.active_source_id: str | None = None
        if live:
            self.live_dir: Path | None = Path(live)
        else:
            first = self._writable_sources()
            self.active_source_id = first[0].id if first else None
            self.live_dir = Path(first[0].path) if first else locations.default_live_save_dir()

        self._meta: dict[str, dict] = {}
        self._build_widgets()
        self.refresh()

    # --- state ---------------------------------------------------------------

    def _load_or_bootstrap_state(self) -> appstate.AppState:
        st = appstate.load()
        if st is None:  # first run: discover everything and write state.json
            st = discover.bootstrap_state()
            try:
                appstate.save(st)
            except OSError:
                pass  # read-only location; carry on with the in-memory state
        return st

    def _writable_sources(self) -> list[appstate.Source]:
        return [s for s in self.state.live_sources if s.writable and s.exists]

    def _active_source(self) -> appstate.Source | None:
        return self.state.get(self.active_source_id) if self.active_source_id else None

    def _refresh_source_combo(self) -> None:
        """Populate the active-live dropdown with the writable sources (Steam + Xbox)."""
        writable = self._writable_sources()
        self._source_choices = {}
        for s in writable:
            label = _source_caption(s)
            # Two accounts may be given the same display name; keep both selectable.
            unique, n = label, 2
            while unique in self._source_choices:
                unique, n = f"{label} #{n}", n + 1
            self._source_choices[unique] = s.id
        self.active_combo["values"] = list(self._source_choices)
        active = self._active_source()
        if active is not None:
            for label, sid in self._source_choices.items():
                if sid == active.id:
                    self.active_var.set(label)
                    break
        else:
            self.active_var.set("")
        # Nothing selectable -> disable the control so its state is obvious.
        self.active_combo.configure(state="readonly" if writable else "disabled")

    def _on_active_changed(self, _event=None) -> None:
        sid = self._source_choices.get(self.active_var.get())
        if sid:
            self._set_active(sid)

    def _set_active(self, source_id: str) -> None:
        src = self.state.get(source_id)
        if src is None:
            return
        self.active_source_id = source_id
        self.live_dir = Path(src.path)
        self.refresh()

    # --- layout --------------------------------------------------------------

    def _build_widgets(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        for text, cmd in [
            ("Backup live", self.on_backup),
            ("Restore", self.on_restore),
            ("Extract slot", self.on_extract),
            ("Repopulate → live", self.on_repopulate),
            ("Promote", self.on_promote),
            ("Import…", self.on_import),
            ("Rescan", self.on_rescan),
            ("Discover", self.on_discover),
            ("Undo", self.on_undo),
            ("Refresh", self.refresh),
            ("Accounts…", self.on_accounts),
            ("Help", self.on_help),
        ]:
            ttk.Button(bar, text=text, command=cmd).pack(side=tk.LEFT, padx=2)

        # Active live (write target) selector -- the writable accounts (Steam + Xbox).
        self._source_choices: dict[str, str] = {}  # label -> source id
        self.active_var = tk.StringVar(value="")
        ttk.Label(bar, text="  Active live:").pack(side=tk.LEFT)
        self.active_combo = ttk.Combobox(bar, textvariable=self.active_var, state="readonly", width=22)
        self.active_combo.pack(side=tk.LEFT, padx=2)
        self.active_combo.bind("<<ComboboxSelected>>", self._on_active_changed)

        self.status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken").pack(
            side=tk.BOTTOM, fill=tk.X
        )

        # Two independent trees, one per pane: live saves are browsed, backups are
        # searched and sorted, and mixing them in one tree made both worse.
        panes = ttk.PanedWindow(self, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        self.live_tree = self._make_pane(
            panes,
            "● LIVE SAVES",
            LIVE_COLUMNS,
            "Save folder / Slot / Save",
            weight=3,
        )
        self.backup_tree = self._make_pane(
            panes,
            "■ BACKUPS",
            BACKUP_COLUMNS,
            "Backup / Slot / Save",
            weight=2,
            sortable=True,
        )
        # Which tree the toolbar acts on: whichever the user last selected in.
        self._active_tree = self.live_tree
        # Backups start newest-first; clicking a heading re-sorts.
        self._sort_key: str = "saved"
        self._sort_reverse: bool = True

        for tree in (self.live_tree, self.backup_tree):
            tree.bind("<Button-3>", self._on_right_click)
            tree.bind("<<TreeviewSelect>>", self._on_tree_select)
            Tooltip(tree, self._tip_for_row)
            # Row styling: groups bold, active live emphasised, read-only amber, backups grey.
            tree.tag_configure("group", font=("TkDefaultFont", 10, "bold"))
            tree.tag_configure("live", foreground="#0a6b2f")
            tree.tag_configure("active", foreground="#0a6b2f", font=("TkDefaultFont", 9, "bold"))
            tree.tag_configure("readonly", foreground="#7a5b00")
            tree.tag_configure("backup", foreground="#333333")

    def _make_pane(self, panes, title, columns, tree_heading, *, weight, sortable=False):
        """One titled pane holding a scrolled tree; returns the tree."""
        frame = ttk.Frame(panes)
        panes.add(frame, weight=weight)
        ttk.Label(frame, text=title, font=("TkDefaultFont", 10, "bold"), anchor="w").pack(
            side=tk.TOP, fill=tk.X, pady=(0, 2)
        )
        body = ttk.Frame(frame)
        body.pack(fill=tk.BOTH, expand=True)
        tree = RedactingTreeview(body, columns=[c.key for c in columns], show="tree headings")
        tree.heading("#0", text=tree_heading)
        tree.column("#0", width=300, anchor="w")
        for col in columns:
            if sortable:
                tree.heading(col.key, text=col.title,
                             command=lambda k=col.key: self._sort_backups(k))
            else:
                tree.heading(col.key, text=col.title)
            tree.column(col.key, width=col.width, anchor="w")
        vsb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    def _on_tree_select(self, event) -> None:
        """Keep exactly one selection across both trees, so 'the selected row' is never
        ambiguous when a toolbar button fires."""
        self._active_tree = event.widget
        other = self.backup_tree if event.widget is self.live_tree else self.live_tree
        if other.selection():
            other.selection_remove(*other.selection())

    # --- populate ------------------------------------------------------------

    def refresh(self) -> None:
        for tree in (self.live_tree, self.backup_tree):
            tree.delete(*tree.get_children())
        self._meta.clear()

        # --- LIVE pane: every live source, one root row per account -----------
        sources = self.state.live_sources
        if not sources and self.live_dir and Path(self.live_dir).is_dir():
            # No state (e.g. explicit --live): fall back to the single folder.
            sources = [appstate.Source(id="live", platform="steam", account="",
                                       path=str(self.live_dir), label=Path(self.live_dir).name)]
        for s in sources:
            if not Path(s.path).is_dir():
                continue
            active = s.id == self.active_source_id
            badge = " — read-only (Xbox)" if not s.writable else (" — ACTIVE" if active else "")
            tags = ("active",) if active else (("readonly",) if not s.writable else ("live",))
            view = savedir.scan_any(s.path)
            node = self.live_tree.insert(
                "", "end", open=active,
                text=f"{_source_caption(s)}{badge}",
                values=("", "", "", "", "writable" if s.writable else "read-only"),
                tags=tags,
            )
            self._meta[node] = {
                "type": "live", "dir": s.path, "writable": s.writable, "source_id": s.id,
                "tip": self._source_tip(s, view),
            }
            self._add_view(node, view, writable=s.writable)

        # --- BACKUPS pane: catalog entries, sortable by any column ------------
        for e in self.vault.entries:
            node = self.backup_tree.insert(
                "", "end", text=e.id,
                values=(
                    e.kind,
                    len(e.occupied_slots),
                    e.label,
                    "",
                    "",
                    _fmt_created(e.created),
                ),
                tags=("backup",),
            )
            self._meta[node] = {"type": "entry", "entry": e, "tip": self._entry_tip(e)}
            self._add_entry(node, e)
        self._apply_backup_sort()

        self._refresh_source_combo()
        running = ops.safety.is_game_running()
        game = {True: "RUNNING (writes blocked)", False: "closed", None: "unknown"}[running]
        active = self._active_source()
        active_txt = active.label if active else (str(self.live_dir) if self.live_dir else "none")
        self.status.set(
            aliases.redact(
                f"Active live: {active_txt}   |   Sources: {len(sources)}   |   "
                f"Vault: {self.vault.root}   |   Game: {game}"
            )
        )

    def _add_view(self, parent: str, view: savedir.SaveDirView, writable: bool) -> None:
        for slot in sorted(view.slots):
            sv = view.slots[slot]
            if not sv.occupied:
                continue
            n = sv.newest
            node = self.live_tree.insert(
                parent,
                "end",
                text=f"Slot {slot}",
                values=(
                    sv.display_name,
                    (n.info.difficulty_label if n and n.info else ""),
                    _fmt_play(n.info.total_play_time if n and n.info else 0),
                    _fmt_ts(n.effective_timestamp if n else 0),
                    "",
                ),
            )
            self._meta[node] = {
                "type": "slot", "dir": str(view.path), "slot": slot, "live": writable,
                "tip": self._slot_tip(sv, view),
            }
            for m in sv.members:
                if not m.exists:
                    continue
                star = " *" if (n and m.label == n.label) else ""
                status = ("valid" if m.valid else "INVALID") + (" / moved" if m.moved else "")
                if m.cloud_status:
                    status += f" / cloud {m.cloud_status}"
                mid = self.live_tree.insert(
                    node,
                    "end",
                    text=f"   {m.save_type_label}{star}",
                    values=(
                        m.save_name,
                        (m.info.difficulty_label if m.info else ""),
                        _fmt_play(m.info.total_play_time if m.info else 0),
                        _fmt_ts(m.effective_timestamp),
                        status,
                    ),
                )
                self._meta[mid] = {
                    "type": "member",
                    "dir": str(view.path),
                    "slot": slot,
                    "member": slotmap.member_index(m.label),
                    "live": writable,
                    "tip": self._member_tip(m, sv),
                }

    def _add_entry(self, parent: str, entry) -> None:
        for s in entry.slots:
            if not s.occupied:
                continue
            ts = max((m.timestamp for m in s.members if m.present), default=0)
            newest = next((m for m in s.members if m.label == s.newest_label), None)
            node = self.backup_tree.insert(
                parent,
                "end",
                text=f"Slot {s.slot}",
                values=("", "", s.name, newest.difficulty_label if newest else "", "", _fmt_ts(ts)),
            )
            self._meta[node] = {
                "type": "slot", "dir": entry.path, "slot": s.slot, "live": False, "entry": entry,
                "tip": self._backup_slot_tip(s, entry),
            }
            for m in s.members:
                if not m.present:
                    continue
                star = " *" if m.label == s.newest_label else ""
                mid = self.backup_tree.insert(
                    node,
                    "end",
                    text=f"   {m.save_type_label}{star}",
                    values=(
                        "",
                        "",
                        m.name,
                        m.difficulty_label,
                        _fmt_play(m.play_time),
                        _fmt_ts(m.timestamp),
                    ),
                )
                self._meta[mid] = {
                    "type": "member",
                    "dir": entry.path,
                    "slot": s.slot,
                    "member": slotmap.member_index(m.label),
                    "live": False,
                    "tip": self._backup_member_tip(m, s),
                }

    # --- sorting (backups pane) ----------------------------------------------

    def _sort_backups(self, key: str) -> None:
        """Clicking a heading sorts the backups; clicking the same one flips direction."""
        if key == self._sort_key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_key, self._sort_reverse = key, key == "saved"
        self._apply_backup_sort()

    def _apply_backup_sort(self) -> None:
        """Reorder the top-level backup rows only; their slots/saves ride along."""
        column = self.backup_tree["columns"].index(self._sort_key)

        def sort_value(row: str):
            entry = (self._meta.get(row) or {}).get("entry")
            if self._sort_key == "saved" and entry is not None:
                return entry.created  # ISO-8601: sorts correctly as text
            if self._sort_key == "slots" and entry is not None:
                return len(entry.occupied_slots)
            return str(self.backup_tree.set(row, column)).lower()

        rows = sorted(self.backup_tree.get_children(""), key=sort_value, reverse=self._sort_reverse)
        for position, row in enumerate(rows):
            self.backup_tree.move(row, "", position)
        arrow = " ▼" if self._sort_reverse else " ▲"
        for col in BACKUP_COLUMNS:
            self.backup_tree.heading(
                col.key, text=col.title + (arrow if col.key == self._sort_key else "")
            )

    # --- tooltips ------------------------------------------------------------

    def _tip_for_row(self, tree, row_id: str) -> str:
        meta = self._meta.get(row_id)
        return meta.get("tip", "") if meta else ""

    def _source_tip(self, source, view: savedir.SaveDirView) -> str:
        lines = [
            f"{aliases.redact(source.label)}",
            f"Platform: {'Xbox / Game Pass' if source.platform == 'xbox' else 'Steam'}",
            f"Folder:   {aliases.redact(source.path)}",
            f"Writes:   {'allowed' if source.writable else 'read-only'}",
            f"Slots in use: {len(view.occupied_slots)} of {len(view.slots)}",
        ]
        if view.steam_cloud:
            lines.append("Steam Cloud: enabled for this folder (Steam syncs it as a whole,")
            lines.append("             so there is no per-save cloud state to show)")
        return "\n".join(lines)

    def _slot_tip(self, sv, view: savedir.SaveDirView) -> str:
        newest = sv.newest
        lines = [f"Slot {sv.slot}: {sv.display_name}"]
        if newest and newest.info:
            lines.append(f"Difficulty: {newest.info.difficulty_label or 'unknown'}")
            lines.append(f"Play time:  {_fmt_play(newest.info.total_play_time) or '-'}")
            if newest.info.save_summary:
                lines.append(f"Where:      {newest.info.save_summary}")
        lines.append("")
        for m in sv.members:
            if not m.exists:
                lines.append(f"{m.save_type_label:<14} (not present)")
                continue
            mark = "  <- the game loads this one" if newest and m.label == newest.label else ""
            lines.append(f"{m.save_type_label:<14} {_fmt_ts(m.effective_timestamp)}{mark}")
        return "\n".join(lines)

    def _member_tip(self, m, sv) -> str:
        newest = sv.newest
        lines = [
            f"{m.save_type_label} — {_SAVE_TYPE_HELP[m.save_type_label]}",
            "",
            f"Save name:  {m.save_name or '<unnamed>'}",
        ]
        if m.info:
            lines.append(f"Difficulty: {m.info.difficulty_label or 'unknown'}")
            lines.append(f"Play time:  {_fmt_play(m.info.total_play_time) or '-'}")
            if m.info.save_summary:
                lines.append(f"Where:      {m.info.save_summary}")
        lines.append(f"Saved:      {_fmt_ts(m.effective_timestamp)}")
        lines.append(f"Current:    {'yes — this is what the game loads' if newest and m.label == newest.label else 'no'}")
        lines.append(f"File:       {m.ref.data_name}")
        lines.append(f"Size:       {_fmt_size(m.data_size)}")
        lines.append(f"Integrity:  {'valid' if m.valid else 'INVALID'}")
        if m.cloud_status:
            lines.append(f"Cloud:      {m.cloud_status}")
        if m.note:
            lines.append(f"Note:       {m.note}")
        return "\n".join(lines)

    def _entry_tip(self, entry) -> str:
        return "\n".join([
            f"{entry.id}",
            f"Type:    {_KIND_HELP.get(entry.kind, entry.kind)}",
            f"Created: {_fmt_created(entry.created)}",
            f"Label:   {aliases.redact(entry.label) or '-'}",
            f"Slots:   {len(entry.occupied_slots)} occupied",
            f"Stored:  {'in the vault' if entry.managed else 'indexed where it already lives'}",
            f"Folder:  {aliases.redact(entry.path)}",
        ])

    def _backup_slot_tip(self, s, entry) -> str:
        lines = [f"Slot {s.slot}: {s.name}", f"In backup: {entry.id}", ""]
        for m in s.members:
            if not m.present:
                lines.append(f"{m.save_type_label:<14} (not present)")
                continue
            mark = "  <- newest in this backup" if m.label == s.newest_label else ""
            lines.append(f"{m.save_type_label:<14} {_fmt_ts(m.timestamp)}{mark}")
        return "\n".join(lines)

    def _backup_member_tip(self, m, s) -> str:
        lines = [
            f"{m.save_type_label} — {_SAVE_TYPE_HELP[m.save_type_label]}",
            "",
            f"Save name:  {m.name or '<unnamed>'}",
            f"Difficulty: {m.difficulty_label or 'unknown'}",
            f"Play time:  {_fmt_play(m.play_time) or '-'}",
        ]
        if m.summary:
            lines.append(f"Where:      {m.summary}")
        lines.append(f"Saved:      {_fmt_ts(m.timestamp)}")
        lines.append(f"Newest:     {'yes' if m.label == s.newest_label else 'no'}")
        lines.append(f"Size:       {_fmt_size(m.data_size)}")
        lines.append(f"Integrity:  {'valid' if m.valid else 'INVALID'}")
        if m.note:
            lines.append(f"Note:       {m.note}")
        return "\n".join(lines)

    # --- selection helpers ---------------------------------------------------

    def _selected(self) -> dict | None:
        """The row the toolbar acts on: the selection in whichever tree was last used."""
        for tree in (self._active_tree, self.live_tree, self.backup_tree):
            sel = tree.selection()
            if sel:
                return self._meta.get(sel[0])
        return None

    def _on_right_click(self, event) -> None:
        """Build a context-sensitive menu for the right-clicked row."""
        tree = event.widget
        row = tree.identify_row(event.y)
        if not row:
            return
        tree.selection_set(row)  # fires <<TreeviewSelect>>, which clears the other tree
        meta = self._meta.get(row)
        if not meta:
            return
        menu = tk.Menu(self, tearoff=0)
        kind = meta.get("type")
        if kind == "live":
            sid = meta.get("source_id")
            if meta.get("writable") and sid and sid != self.active_source_id:
                menu.add_command(label="Set as active live target", command=lambda: self._set_active(sid))
                menu.add_separator()
            if meta.get("writable"):
                menu.add_command(label="Backup this live folder now", command=lambda: self._backup_dir(meta["dir"]))
                menu.add_command(label="Undo last operation", command=self.on_undo)
            else:
                menu.add_command(label="Import this Xbox folder as a backup",
                                 command=lambda: self._import_dir(meta["dir"]))
        elif kind == "entry":
            menu.add_command(label=f"Restore '{meta['entry'].id}' into live", command=lambda: self.on_restore(meta))
        elif kind == "slot":
            target = {"type": "slot", "dir": meta["dir"], "slot": meta["slot"]}
            menu.add_command(label=f"Extract slot {meta['slot']} aside", command=lambda: self.on_extract(target))
            menu.add_command(label=f"Repopulate a live slot from slot {meta['slot']}…", command=lambda: self.on_repopulate(target))
        elif kind == "member":
            slot = meta["slot"]
            target = {"type": "slot", "dir": meta["dir"], "slot": slot}
            menu.add_command(label=f"Extract slot {slot} aside", command=lambda: self.on_extract(target))
            menu.add_command(label=f"Repopulate a live slot from slot {slot}…", command=lambda: self.on_repopulate(target))
            if meta.get("live"):
                menu.add_separator()
                label = slotmap.save_type_label(meta["member"])
                menu.add_command(label=f"Make the {label} the newest (promote)", command=lambda: self.on_promote(meta))
        menu.add_separator()
        menu.add_command(label="Refresh", command=self.refresh)
        menu.add_command(label="Account display names…", command=self.on_accounts)
        menu.add_command(label="Help", command=self.on_help)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # --- actions -------------------------------------------------------------

    def _run(self, fn, *, success: str, message: str = "Working — please wait…") -> None:
        """Run a core operation on a worker thread behind a modal wait dialog, prompting to
        override the game-running guard if the op reports the game is open."""
        holder = self._run_worker(fn, False, message)
        if isinstance(holder.get("error"), ops.GameRunningError):
            if not _askyesno("Game running", "No Man's Sky appears to be running.\nProceed anyway (risky)?"):
                return
            holder = self._run_worker(fn, True, message)

        err = holder.get("error")
        if isinstance(err, ops.FeatureNotYetAvailableError):
            _showinfo("Coming soon", str(err))
            return
        if isinstance(err, ops.OperationError):
            _showerror("Operation failed", str(err))
            return
        if err is not None:
            _showerror("Unexpected error", repr(err))
            return
        self.refresh()
        self._present_result(holder.get("result"), success)

    def _present_result(self, result, success: str) -> None:
        if isinstance(result, ops.OpResult):
            msg = result.detail + (f"\n\nUndo available (snapshot {result.snapshot_id})." if result.snapshot_id else "")
            if result.warnings:
                msg += "\n\nWarnings:\n - " + "\n - ".join(result.warnings)
            _showinfo("Done", msg)
        else:
            _showinfo("Done", success)

    def _busy(self, message: str) -> tk.Toplevel:
        """A small modal 'please wait' window with an animated indeterminate bar."""
        win = tk.Toplevel(self)
        win.title("Please wait")
        win.transient(self)
        win.resizable(False, False)
        ttk.Label(win, text=message, padding=(24, 18, 24, 8)).pack()
        pb = ttk.Progressbar(win, mode="indeterminate", length=260)
        pb.pack(padx=24, pady=(0, 20))
        pb.start(12)
        win.update_idletasks()
        # centre over the main window
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.protocol("WM_DELETE_WINDOW", lambda: None)  # not closable mid-op
        win.grab_set()
        win.update()
        return win

    def _run_worker(self, fn, force: bool, message: str) -> dict:
        """Run ``fn(force)`` off the Tk thread behind a modal wait dialog. Returns a holder
        dict with 'result' or 'error'. Only the core file work runs off-thread (it never
        touches Tk); all result/error UI stays on the main thread in the caller."""
        win = self._busy(message)
        holder: dict = {}

        def worker() -> None:
            try:
                holder["result"] = fn(force)
            except Exception as exc:  # noqa: BLE001 - reported by the caller on the main thread
                holder["error"] = exc

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            while t.is_alive():
                self.update()  # keep the bar animating / window responsive
                time.sleep(0.02)
        finally:
            win.grab_release()
            win.destroy()
        return holder

    def _require_live(self) -> bool:
        if not (self.live_dir and Path(self.live_dir).is_dir()):
            _showerror("No live folder", "Could not locate the live save folder.")
            return False
        return True

    def on_backup(self) -> None:
        if not self._require_live():
            return
        self._backup_dir(self.live_dir)

    def _backup_dir(self, directory) -> None:
        platform = savedir.platform_of(directory)
        prefix = "Xbox" if platform == "xbox" else "Steam"
        suggested = f"{prefix}{datetime.now():%Y%m%d-%H%M}"
        label = simpledialog.askstring("Backup", "Optional label:", initialvalue=suggested)
        if label is None:  # user cancelled
            return
        self._run(
            lambda _force: ops.create_full_backup(self.vault, Path(directory), label=label),
            success="Backup created.",
            message="Creating backup — please wait…",
        )

    def on_restore(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "entry":
            _showinfo("Select a backup", "Select a catalog entry (top-level backup) to restore.")
            return
        if not self._require_live():
            return
        entry = sel["entry"]
        if not _askyesno("Restore", f"Replace the live saves with backup '{entry.id}'?\n(The current state is auto-snapshotted first.)"):
            return
        self._run(
            lambda force: ops.restore_full(self.vault, entry, self.live_dir, allow_game_running=force),
            success="Restored.",
            message="Restoring — please wait…",
        )

    def on_extract(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "slot":
            _showinfo("Select a slot", "Select a slot (under LIVE or any backup) to extract.")
            return
        self._run(
            lambda _force: ops.extract_slot(self.vault, Path(sel["dir"]), sel["slot"], label=""),
            success=f"Extracted slot {sel['slot']}.",
            message=f"Extracting slot {sel['slot']} — please wait…",
        )

    def on_repopulate(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "slot":
            _showinfo("Select a source slot", "Select the source slot (under a backup or LIVE) first.")
            return
        if not self._require_live():
            return
        dest = simpledialog.askinteger("Repopulate", "Destination live slot (1-15):", minvalue=1, maxvalue=15)
        if not dest:
            return
        if not _askyesno(
            "Repopulate",
            f"Write slot {sel['slot']} from\n{sel['dir']}\ninto LIVE slot {dest}?\n(The current state is auto-snapshotted first.)",
        ):
            return
        self._run(
            lambda force: ops.repopulate_slot(self.vault, Path(sel["dir"]), sel["slot"], self.live_dir, dest, allow_game_running=force),
            success=f"Repopulated live slot {dest}.",
            message=f"Writing live slot {dest} — please wait…",
        )

    def on_promote(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "member" or not sel.get("live"):
            _showinfo("Select a live save", "Select the Auto-Save or Restore-Point under a LIVE slot to make it the newest.")
            return
        self._run(
            lambda force: ops.promote_member(self.vault, self.live_dir, sel["slot"], sel["member"], allow_game_running=force),
            success="Promoted.",
            message="Promoting — please wait…",
        )

    def on_import(self) -> None:
        folder = filedialog.askdirectory(title="Select a save-folder backup to import")
        if folder:
            self._import_dir(folder)

    def _import_dir(self, directory) -> None:
        directory = Path(directory)
        if catalog.looks_like_vault_dir(directory):
            self._import_vault_dir(directory)
            return
        copy = _askyesno("Import", "Copy the backup into the vault?\n(No = index it where it is.)")
        self._run(
            lambda _force: ops.import_backup(self.vault, directory, copy_into_vault=copy),
            success="Imported.",
            message="Importing — please wait…",
        )

    def _import_vault_dir(self, directory: Path) -> None:
        """Import another Save Vault folder: compare it to the current vault, then offer to
        copy its new entries in or index them in place."""
        try:
            pv = ops.preview_vault_import(self.vault, directory)
        except ops.OperationError as exc:
            _showerror("Import vault", str(exc))
            return
        total, new, existing = pv["total"], pv["new"], pv["existing"]
        plural = "y" if total == 1 else "ies"
        if not new:
            _showinfo(
                "Import vault",
                f"'{directory.name}' is a Save Vault with {total} entr{plural}, "
                "all already in your vault. Nothing to import.",
            )
            return
        ans = _askyesnocancel(
            "Import vault",
            f"'{directory.name}' is a Save Vault with {total} entr{plural}:\n"
            f"  - {len(new)} new\n"
            f"  - {len(existing)} already in your vault\n\n"
            "Copy the new entries' files INTO your vault?\n\n"
            "Yes  = copy in (self-contained)\n"
            "No  = index in place (reference that folder)\n"
            "Cancel = don't import",
        )
        if ans is None:
            return
        self._run(
            lambda _force: ops.import_vault(self.vault, directory, copy_into_vault=bool(ans)),
            success="Vault imported.",
            message="Importing vault — please wait…",
        )

    def on_rescan(self) -> None:
        """Re-scan AppData for live sources and merge any new accounts into the config."""
        added = discover.merge_live_sources(self.state)
        try:
            appstate.save(self.state)
        except OSError as exc:
            _showwarning("Rescan", f"Found sources but could not save config:\n{exc}")
        # If we had no active writable source yet, adopt the first one found.
        if self.active_source_id is None:
            writable = self._writable_sources()
            if writable:
                self.active_source_id = writable[0].id
                self.live_dir = Path(writable[0].path)
        self.refresh()
        _showinfo(
            "Rescan",
            f"Added {added} new live source(s).\n"
            f"Now tracking {len(self.state.live_sources)} live source(s).\n\n"
            "Tip: use Discover to also catalog copy-paste backups.",
        )

    def on_discover(self) -> None:
        # Exclude every live source (so a copy-paste "st_... - Copy" is still seen as a
        # backup, but the real live folders are not) plus the vault itself.
        exclude = [s.path for s in self.state.live_sources] + [self.vault.root]
        dirs = discover.discover_inplace_backups(exclude=exclude)
        known = {Path(e.path).resolve() for e in self.vault.entries}
        added = 0
        for d in dirs:
            if d.resolve() not in known:
                ops.import_backup(self.vault, d, label=d.name, copy_into_vault=False)
                added += 1
        self.refresh()
        _showinfo("Discover", f"Added {added} newly found backup(s) to the catalog.")

    def on_undo(self) -> None:
        if not self._require_live():
            return
        if not _askyesno("Undo", "Undo the last operation by restoring its auto-snapshot?"):
            return
        self._run(
            lambda force: ops.undo_last(self.vault, self.live_dir, allow_game_running=force),
            success="Undone.",
            message="Undoing — please wait…",
        )

    def on_accounts(self) -> None:
        try:
            AccountsDialog(self)
        except aliases.AliasConfigError as exc:
            messagebox.showerror(  # raw: the alias file is what failed to parse
                "Account display names",
                f"Could not read the account display names.\n\nFix or delete this file:\n{exc}",
            )

    def on_help(self) -> None:
        win = tk.Toplevel(self)
        win.title("NMS Save Vault — Help")
        win.geometry("760x620")
        ttk.Button(win, text="Close", command=win.destroy).pack(side=tk.BOTTOM, pady=6)
        vsb = ttk.Scrollbar(win, orient="vertical")
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        txt = tk.Text(win, wrap="word", padx=10, pady=10, yscrollcommand=vsb.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.config(command=txt.yview)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")


class AccountsDialog(tk.Toplevel):
    """Map each account id to the name shown in its place.

    This dialog is the one place in the app that shows the real identifiers -- it is
    where they are configured -- so its fields are deliberately not filtered.
    """

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Account display names")
        self.transient(app)
        self.resizable(False, False)
        self.amap = aliases.load()
        self._vars: dict[str, tk.StringVar] = {}

        ttk.Label(
            self,
            padding=(14, 12, 14, 8),
            justify="left",
            text=(
                "Show a name of your choosing instead of the real account id.\n"
                "Once a name is set, the app shows only that name -- in folder names,\n"
                "paths, labels and messages. This is the only place the real ids appear.\n"
                "Clear a name to go back to showing the real id."
            ),
        ).pack(anchor="w")

        grid = ttk.Frame(self, padding=(14, 0, 14, 8))
        grid.pack(fill=tk.X)
        accounts = self._known_accounts()
        if not accounts:
            ttk.Label(grid, text="No accounts found yet -- use Rescan first.").grid(sticky="w")
        for column, heading in enumerate(("Platform", "Real account id", "Shows as")):
            ttk.Label(grid, text=heading, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=column, sticky="w", padx=(0, 10), pady=(0, 4)
            )
        for row, (platform, account) in enumerate(accounts, start=1):
            ttk.Label(grid, text=platform).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            real = ttk.Entry(grid, width=52)  # fits a 49-char Xbox <xuid>_<titleid> whole
            real.insert(0, account)
            real.configure(state="readonly")  # selectable so it can be copied, not edited
            real.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=2)
            var = tk.StringVar(value=self.amap.alias_for(account))
            self._vars[account] = var
            ttk.Entry(grid, textvariable=var, width=26).grid(row=row, column=2, sticky="w", pady=2)

        buttons = ttk.Frame(self, padding=(14, 0, 14, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", command=self._save).pack(side=tk.RIGHT, padx=6)

        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    def _known_accounts(self) -> list[tuple[str, str]]:
        """Every account the app knows about, plus any left over in the file."""
        known = [(s.platform, s.account) for s in self.app.state.sources if s.account]
        seen = {account for _platform, account in known}
        return known + [("-", a) for a in self.amap.entries if a not in seen]

    def _save(self) -> None:
        for account, var in self._vars.items():
            self.amap.set(account, var.get())
        try:
            aliases.save(self.amap)
        except OSError as exc:
            _showerror("Account display names", f"Could not save the account names:\n{exc}")
            return
        aliases.set_active(self.amap)
        self.destroy()
        self.app.refresh()


HELP_TEXT = """NMS Save Vault — Help

The window is split into two panes. Drag the divider to give either one more room.
  ● LIVE SAVES (top)  -- every save folder found on this PC: each Steam account and each
     Xbox / Game Pass account. The ACTIVE one (green, bold) is the target of write
     actions; pick it from the "Active live" dropdown or right-click a folder to set it.
     Xbox folders are writable for same-platform actions (backup, restore, repopulate,
     promote within Xbox). Transferring a save between Steam and Xbox is not yet supported.
  ■ BACKUPS (bottom)  -- every backup in the catalog (full snapshots, extracts, imported
     and auto-discovered copy-paste backups). Click any column heading to sort by it --
     Saved, Type, Slots, Name -- and click the same heading again to reverse the order.
     Backups start newest-first.

Selecting a row in one pane clears the selection in the other, so the toolbar buttons
always act on the row you last clicked.

Hover over any row for details: for a save, what it is, its difficulty, play time, where
you were, its file, size and integrity; for a backup, its type, when it was made, whether
it lives in the vault or was catalogued in place, and where it is.

The Difficulty column is the save's difficulty preset (Normal, Creative, Custom, Relaxed,
Survival, Permadeath). Saves made before the Waypoint update stored this as a game mode
instead, so for those the old value is shown.

Cloud: Xbox / Game Pass records a sync state per save, shown in the Status column. Steam
gives no per-save state -- it syncs the whole folder -- so a Steam folder is only marked
as Steam Cloud enabled.
Expand a folder to see its slots; expand a slot to see its two saves:
  - Auto-Save -- the one the game writes by itself every few minutes.
  - Restore-Point -- the one written when you leave your ship, or use a save point, a
    save beacon, or a point-of-interest save.
    The game keeps these two separate so neither overwrites the other, so EITHER can be
    the newer one: exit your ship just after an auto-save and the Restore-Point is newer.
  - The one marked * is the NEWEST -- the one the game loads for that slot.
Right-click any row to get the same actions as the buttons, in context.

First run auto-detects your save folders and writes a small config (state.json) in the
program's own install folder (next to the .exe). Use Rescan to pick up a newly added
account or a freshly pasted backup later on.

BUTTONS
- Backup live: Full snapshot of the entire live folder into the vault. Do this before
  any risky change.
- Restore: Select a backup (a top-level row), then replace the live folder with it. The
  current state is auto-snapshotted first, so it is reversible with Undo.
- Extract slot: Select a slot (under LIVE or a backup) to copy just that one slot aside
  into the vault, so you can free the slot now and bring it back later.
- Repopulate -> live: Select a SOURCE slot (in any backup or LIVE), then choose a
  destination live slot (1-15). The save data is copied exactly and the small meta is
  re-keyed for the new slot. This loads an archived save back into the game, in any slot.
- Promote: Select one of a live slot's two saves (Auto-Save or Restore-Point) to force it
  to be the newest, so the game loads it instead of the other -- e.g. to roll back to the
  Restore-Point.
- Import: Register an existing save folder you made yourself (or an Xbox / Game Pass
  save) into the catalog -- either in place or copied into the vault. You can also point
  Import at a whole copied Save Vault folder: it compares that vault's entries with yours
  and offers to copy the new ones in or index them in place (re-importing is harmless).
- Rescan: Re-detect live save folders (new Steam account, new Xbox account) and add them
  to the LIVE SAVES list. Your manual entries are left untouched.
- Discover: Scan the NMS folder for existing backups and add any new ones found.
- Undo: Restore the auto-snapshot taken just before the last operation.
- Refresh: Re-scan the live folder and the catalog.
- Accounts: Give each account a display name to show instead of its real id (the Steam
  st_<steamid64> folder, the Xbox <xuid>_<titleid> folder). Once a name is set, the app
  shows only that name everywhere -- folder names, paths, labels, messages -- and that
  dialog is the only place the real ids still appear. The names are stored in
  accounts.ini next to state.json; clearing a name shows the real id again.
- Help: This dialog.

WORKFLOWS
- More than 15 slots: Extract the slots you are not using into the vault, then Repopulate
  them into a live slot whenever you want to play them again. Your library is unlimited;
  only 15 are live at a time.
- Safe experimenting: Backup live (or Extract the slot), make changes in-game, then
  Restore / Repopulate / Undo if you do not like the result.
- Move a save to another slot: Repopulate -- pick the source slot and the destination.
- Roll back within a slot: expand the live slot, right-click the save you want (usually
  the older one, or the Restore-Point), Promote it.
- Bring in an outside save: Import the folder, then Repopulate the slot you want.

SAFETY
- Writes are blocked while No Man's Sky is running -- close the game first.
- Every change auto-snapshots the live state first and can be reversed with Undo.
- Steam Cloud: operate with the game closed; if Steam shows a conflict on next launch,
  keep the local copy.
"""


def main(argv=None) -> int:
    try:
        aliases.active()  # fail before any window shows a real id we were told to hide
    except aliases.AliasConfigError as exc:
        root = tk.Tk()
        root.withdraw()
        # Raw messagebox: the filter itself is what failed, so it cannot be used here.
        messagebox.showerror(
            "Account display names",
            "Could not read the account display names, so the app stopped rather than "
            f"show account ids you asked it to hide.\n\nFix or delete this file:\n{exc}",
        )
        root.destroy()
        return 1
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
