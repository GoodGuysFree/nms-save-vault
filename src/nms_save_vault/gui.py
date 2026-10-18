"""Tkinter desktop UI for NMS Save Vault.

A single tree shows the LIVE folder and every catalog backup; each entry expands to its
occupied slots, and each slot to its two saves -- the periodic Auto-Save and the
Restore-Point -- with the game-current one marked '*'. Toolbar actions cover all three
features plus promote and undo. Every write goes through the safety-wrapped core
(auto-snapshot + validate).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__, theme, updates
from .core import aliases, catalog, discover, locations, slotmap
from .core import operations as ops
from .core import liveops
from .core.liveops import classic
from .core import platform as host_platform
from .core import playtime, savedir
from .core import state as appstate
from .core.catalog import Vault
from .ui import dpi, logfile
from .core.organizer import Organizer
from .ui.fmt import local_time as _fmt_ts
from .ui.fmt import play_time as _fmt_play
from .ui.redact import RedactingTreeview
from .ui.menuhint import bind_menu_hints
from .ui.tooltip import TOOLTIP_STYLE, Tooltip
from .ui.organizer.words import PROMOTE_TIP, SLOT_MARKERS_TIP, VAULT_UPGRADE_TIP
from .ui.organizer.ops import OpsLogWindow, PlanDialog, StepsWindow
from .ui.organizer.panic import PanicDialog
from .ui.organizer.vaultwin import VaultEnv, VaultWindow, needs_attention
from .ui.organizer.view import OrganizerView
from .ui.themeaware import ThemeRegistry
from .ui.worker import UiWorker
from .ui.dpi import px, pxs


def _icon_path() -> Path | None:
    """Locate nmsvault.ico - beside the packaged launcher, or in the repo's packaging/."""
    candidates = [
        Path(sys.executable).resolve().parent / "nmsvault.ico",
        Path(__file__).resolve().parents[2] / "packaging" / "nmsvault.ico",
    ]
    return next((c for c in candidates if c.is_file()), None)


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


log = logging.getLogger(__name__)

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

ALL_SAVES = "All saves"

@dataclass(frozen=True)
class SlotSource:
    """One slot's worth of saves that can be copied into a live slot.

    Copying is slot-granular: the core writes *both* of a slot's saves (the Auto-Save and
    the Restore-Point) and re-keys each meta for the destination. So a source is always a
    slot, even when the user right-clicked one of the two saves inside it.
    """

    folder: str      # the folder holding the saves (a live folder, backup, or extract)
    slot: int
    where: str = ""  # human name of the folder: a backup id, or a live source's caption

    @property
    def caption(self) -> str:
        return f"slot {self.slot} of {self.where}" if self.where else f"slot {self.slot}"


_SAVE_TYPE_HELP = {
    "Auto-Save": "the game writes this one by itself, every few minutes",
    "Restore-Point": "written when you leave your ship, or use a save point, save beacon,\n"
                     "or point-of-interest save",
}

_KIND_HELP = {
    catalog.KIND_FULL: "full - a complete snapshot of a save folder",
    catalog.KIND_SNAPSHOT: "snapshot - taken automatically just before an operation",
    catalog.KIND_EXTRACT: "extract - a single slot lifted aside",
    catalog.KIND_IMPORTED: "imported - your own backup, copied into the vault",
    catalog.KIND_INPLACE: "in place - catalogued where it already lives, not copied",
}

def _source_caption(source) -> str:
    """How a live source is named in the tree and the dropdown.

    Normally "Steam (<id>)  (st_<id>)" -- label plus folder. Once a display name replaces
    both halves they read as "Steam (Main)  (Main)", so the folder is dropped when the
    label already says it.
    """
    label = aliases.redact(source.label)
    folder = aliases.redact(Path(source.path).name)
    return label if folder in label else f"{label}  ({folder})"


def _extract_slot_number(entry) -> int | None:
    """The one slot a single-slot extract holds, else None (it is not that kind of entry)."""
    if entry.kind != catalog.KIND_EXTRACT:
        return None
    slots = [s.slot for s in entry.occupied_slots]
    return slots[0] if len(slots) == 1 else None


def _restore_menu_label(entry) -> str:
    """Say what Restore will actually do, since the two cases differ enormously: an extract
    puts one slot back, a backup replaces the whole folder."""
    slot = _extract_slot_number(entry)
    if slot is not None:
        return f"Put slot {slot} back into live slot {slot}"
    return f"Restore all of '{entry.id}' into live (slots it lacks are left alone)"


#: The account-data choice Restore and Undo offer; unticked unless the user ticks it.
ACCOUNT_DATA_CHOICE = "Also put back the account data (unlocks, settings) - this rolls them back too"

#: What a copy into a live slot writes.
COPY_RULE = (
    "Both saves of the slot are written, replacing what is in the destination. A source\n"
    "with one save fills the other position with a copy of it, so a slot never holds two\n"
    "different saves. What the slot held is kept in the vault first, and the live folder is\n"
    "auto-snapshotted, so Undo puts it back."
)

#: How the undo confirmation names a recorded change.
_OP_NAMES = {
    "restore_full": "a restore",
    "repopulate_slot": "a copy into a slot",
    "promote_member": "a promote",
    "panic_restore": "a whole-folder restore",
}


def _undo_question(plan, directory) -> str:
    """The undo confirmation: which folder, which change, and what it goes back to."""
    what = _OP_NAMES.get(plan.op, plan.op.replace("_", " "))
    detail = f" ({plan.detail})" if plan.detail else ""
    lines = [
        f"Undo the last change to {Path(directory).name}?",
        "",
        f"That change: {what}{detail}, at {plan.ts}.",
        f"The folder is put back as it was just before it, from auto-snapshot {plan.snapshot_id}.",
    ]
    if plan.with_accountdata:
        lines.append("That restore put back the account data too, so undoing it does as well.")
    lines += ["", "Each Undo steps one change further back (it never undoes an Undo). Slots the snapshot lacks are left alone. The current state is auto-snapshotted first."]
    return "\n".join(lines)


def _undo_damage_question(plan) -> str:
    return (
        "The undo history has damaged lines newer than this change. One of them may be a "
        "later change to this folder, so undo could put back an older state than you "
        f"expect.\n\n{plan.log_warning}\n\nUndo anyway?"
    )


def _undo_note(snapshot_id: str | None) -> str:
    return f"\n\nUndo available (snapshot {snapshot_id})." if snapshot_id else ""


def _with_warnings(message: str, warnings) -> str:
    return message + ("\n\nWarnings:\n - " + "\n - ".join(warnings) if warnings else "")


def _flow_rows(widths: list[int], available: int) -> list[int]:
    """The row each item lands on when laid out left to right and wrapped at ``available``
    pixels; an item wider than a whole row still gets a row of its own."""
    rows: list[int] = []
    row = used = 0
    for width in widths:
        if used and used + width > available:
            row, used = row + 1, 0
        rows.append(row)
        used += width
    return rows


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


class ConfirmDialog(tk.Toplevel):
    """A yes/no question with an optional checkbox, which starts unticked."""

    def __init__(self, master, title: str, message: str, check: str | None = None):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.resizable(False, False)
        self.answer = False
        self.ticked = False
        self._check = tk.BooleanVar(value=False)
        # Buttons and checkbox first, from the bottom: a long message can then never push
        # them out of the window.
        buttons = ttk.Frame(self, padding=pxs(14, 0, 14, 12))
        buttons.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(buttons, text="No", command=self.destroy).pack(side=tk.RIGHT)
        yes = ttk.Button(buttons, text="Yes", command=self._yes)
        yes.pack(side=tk.RIGHT, padx=px(6))
        if check:
            ttk.Checkbutton(self, text=check, variable=self._check).pack(
                side=tk.BOTTOM, anchor="w", padx=px(14), pady=pxs(0, 10)
            )
        ttk.Label(self, text=message, justify="left", wraplength=px(620)).pack(
            side=tk.TOP, anchor="w", padx=px(14), pady=pxs(14, 10)
        )
        self.bind("<Escape>", lambda _e: self.destroy())
        # Enter presses the focused button (Yes to start with, No once tabbed to).
        self.bind("<Return>", lambda e: e.widget.invoke() if isinstance(e.widget, ttk.Button) else None)
        # Centred over the main window and focused, as _busy is.
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        yes.focus_set()
        try:
            if master.winfo_viewable():
                self.wait_visibility()             # a grab needs a mapped window
            self.grab_set()
        except tk.TclError:
            pass                                   # still answerable, only not modal
        self.wait_window()

    def _yes(self) -> None:
        self.answer = True
        self.ticked = bool(self._check.get())
        self.destroy()


def _confirm(master, title: str, message: str, check: str | None = None) -> tuple[bool, bool]:
    """Ask yes/no, with an optional checkbox. Returns (yes, ticked). The message passes
    through the account-alias filter, like every other dialog's."""
    dialog = ConfirmDialog(master, title, aliases.redact(message), check)
    return dialog.answer, dialog.ticked


class FlowBar(ttk.Frame):
    """A toolbar that wraps its buttons onto more rows when one row does not fit (P10: at
    a 120% font zoom one row needs about 1,400 px, and the buttons past the window edge,
    Undo among them, could not be reached)."""

    PAD = 2  # design pixels; px() at layout time

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.buttons: list[ttk.Button] = []
        self._rows: list[ttk.Frame] = []
        self._layout: list[int] | None = None
        self.bind("<Configure>", lambda _e: self.reflow())

    def add(self, text: str, command) -> ttk.Button:
        button = ttk.Button(self, text=text, command=command)
        self.buttons.append(button)
        self._place([0] * len(self.buttons))      # one row until the bar knows its width
        return button

    def reflow(self, width: int | None = None) -> None:
        width = width or self.winfo_width()
        if width <= 1:
            return                                 # not laid out yet
        self._place(_flow_rows([b.winfo_reqwidth() + 2 * px(self.PAD) for b in self.buttons], width))

    def _place(self, layout: list[int]) -> None:
        if layout == self._layout:
            return
        self._layout = layout
        needed = max(layout, default=-1) + 1
        while len(self._rows) < needed:
            self._rows.append(ttk.Frame(self))
        for widget in (*self._rows, *self.buttons):
            widget.pack_forget()
        for frame in self._rows[:needed]:
            frame.pack(side=tk.TOP, fill=tk.X)
        for button, row in zip(self.buttons, layout):
            button.pack(in_=self._rows[row], side=tk.LEFT, padx=px(self.PAD), pady=px(1))
            button.lift()                          # above the row frame made after it


#: The two tabs of the window (shell 10a) and their titles, in tab order.
CLASSIC = "classic"
ORGANIZER = "organizer"
VIEW_TITLES = {CLASSIC: "Live saves and backups", ORGANIZER: "Organizer"}


class App(tk.Tk):
    def __init__(self, live: str | Path | None = None, vault: str | Path | None = None,
                 state_path: str | Path | None = None, scan_on_start: bool = True):
        super().__init__()
        # Where the config is read and written; None is the install dir's state.json.
        self.state_path = Path(state_path) if state_path else None
        self.title(f"NMS Save Vault v{__version__}")
        # Whatever ttk chose for this OS; light mode restores it so the app keeps the
        # native look it has always had (see theme.apply).
        self._native_ttk_theme = ttk.Style(self).theme_use()
        theme.init_fonts(self)  # baseline sizes, captured before any zoom is applied
        _ico = _icon_path()
        if _ico:
            try:
                self.iconbitmap(str(_ico))
            except tk.TclError:
                pass

        # Load the config (or build it on first run by auto-discovering save folders).
        self.state = self._load_or_bootstrap_state()
        # Every pixel constant goes through px(): this screen's DPI times the font zoom, so
        # the zoom is known before the first widget is built.
        dpi.configure(self, zoom=theme.clamp_font_scale(self.state.font_scale))
        # The minimum is logical pixels at this DPI, not zoomed: a large font must not push
        # the first window off a small screen.
        width, height = dpi.first_start_size(
            _work_area(self), (dpi.screen_px(1000), dpi.screen_px(640)),
            _window_chrome(self))
        self.geometry(f"{width}x{height}")
        vault_root =Path(vault) if vault else (self.state.vault or locations.default_vault_dir())
        self.vault = Vault(vault_root)
        self.vault.ensure()
        self.vault.load()
        # D3 4.1: an upgrade interrupted by a crash is cleaned up (its own leftovers only).
        from .core.store.upgrade import startup_notice

        notice = startup_notice(vault_root)
        if notice:
            self.after(400, lambda: _showinfo("Vault upgrade", notice, parent=self))
        # D3 4.4: an undo of the upgrade that was interrupted asks, at this start, to finish it
        # or to keep the upgrade (the vault refuses writes until the owner chooses).
        self.after(700, self._offer_interrupted_undo)

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
        self._report: playtime.PlaytimeReport | None = None
        self._history_key: str | None = None  # the save file the backups are narrowed to
        self._history_choices: dict[str, str] = {}  # dropdown label -> playthrough key
        self._dark = False
        # The organizer's slow work runs here, one job at a time (D6 3.9); classic widgets
        # and images drawn in code take each new palette from the registry (D6 3.12).
        self.worker = UiWorker(self)
        self.themed = ThemeRegistry()
        # Every window opened later gets the title bar of the current theme when it maps.
        for window_class in ("Toplevel", self.winfo_class()):
            self.bind_class(window_class, "<Map>", self._on_window_mapped, add="+")
        self._build_widgets()
        self._apply_theme()
        self._open_last_view()
        if scan_on_start:          # the organizer's first look, in the worker (D6 U12)
            self.after(200, self.organizer_view.rescan)
        self.refresh()
        self.after(300, self._startup_update_check)  # after the window is on screen
        # Sweep up after a previous update. Deliberately late: when this run *is* the
        # update, the script that started it is still finishing its own cleanup.
        self.after(30_000, updates.clean_temp)

    # --- state ---------------------------------------------------------------

    def _load_or_bootstrap_state(self) -> appstate.AppState:
        st = appstate.load(self.state_path)
        if st is None:  # first run: discover everything and write state.json
            st = discover.bootstrap_state()
            try:
                appstate.save(st, self.state_path)
            except OSError:
                pass  # read-only location; carry on with the in-memory state
        elif not any(s.role == appstate.ROLE_LIVE for s in st.sources):
            # A config that lists no live folder (not even a disabled one) looks them up
            # as a first run does, quietly: the Organizer would show nothing as live.
            try:
                if discover.merge_live_sources(st):
                    appstate.save(st, self.state_path)
            except OSError:
                pass  # read-only location; the sources stay in memory
        return st

    def _live_paths(self) -> list[Path]:
        """Every live folder, configured (enabled or not) or on disk: what undo and the
        clear plan must never take for another folder of the same name, or for a backup.
        The core adds each one's xgs twin (msstore.with_xgs_twins)."""
        return [Path(p) for p in discover.all_live_paths(self.state)]

    def _writable_dir(self, directory) -> bool:
        """Whether writes may go into ``directory``: an enabled, writable live source, the
        rule of the active-live dropdown and Copy. Only with no source configured at all is
        the folder the app was started on (--live) taken as that source."""
        if self.state.sources:
            folders = [Path(s.path) for s in self._writable_sources()]
        else:
            folders = [Path(self.live_dir)] if self.live_dir else []
        try:
            wanted = Path(directory).resolve()
            return any(wanted == f.resolve() for f in folders)
        except OSError:
            return False

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

    def _build_menubar(self) -> None:
        """The native menu bar (D6 2.2): every action has a keyboard path (Alt, then the
        underlined letter) whatever the window width or zoom, and the preferences that
        are not actions (theme, zoom) live here instead of in the toolbar row."""
        bar = tk.Menu(self, tearoff=False)
        self.menubar = bar

        def menu(label: str) -> tk.Menu:
            sub = tk.Menu(bar, tearoff=False)
            bar.add_cascade(label=label, menu=sub, underline=0)
            return sub

        file = menu("File")
        file.add_command(label="Back up live folder now", command=self.on_backup, underline=0)
        file.add_command(label="Restore whole folder from a backup...", command=self.on_restore,
                         underline=0)
        file.add_command(label="Import...", command=self.on_import, underline=0)
        file.add_command(label="Discover backups", command=self.on_discover, underline=0)
        file.add_separator()
        file.add_command(label="Exit", command=self.destroy, underline=1)

        edit = menu("Edit")
        edit.add_command(label="Undo the last change to the active live folder",
                         command=self.on_edit_undo,
                         accelerator="Ctrl+Z", underline=0)

        view = menu("View")
        for name, key in ((ORGANIZER, "Ctrl+1"), (CLASSIC, "Ctrl+2")):
            view.add_radiobutton(label=VIEW_TITLES[name], variable=self.view_var, value=name,
                                 command=lambda n=name: self.show_view(n), accelerator=key)
        view.add_separator()
        view.add_command(label="Refresh", command=self.refresh, accelerator="F5", underline=0)
        view.add_separator()
        themes = tk.Menu(view, tearoff=False)
        for choice in theme.CHOICES:
            themes.add_radiobutton(label=theme.CHOICE_LABELS[choice], variable=self.theme_var,
                                   value=theme.CHOICE_LABELS[choice],
                                   command=self._on_theme_changed)
        view.add_cascade(label="Theme", menu=themes, underline=0)
        view.add_command(label="Zoom in", command=lambda: self._zoom(1), accelerator="Ctrl++")
        view.add_command(label="Zoom out", command=lambda: self._zoom(-1), accelerator="Ctrl+-")
        view.add_command(label="Actual size", command=self._zoom_reset, accelerator="Ctrl+0")

        live = menu("Live")
        live.add_command(label="Accounts...", command=self.on_accounts, underline=0)
        live.add_command(label="Rescan live folders", command=self.on_rescan, underline=0)
        live.add_command(label="Extract a slot", command=self.on_extract, underline=0)
        live.add_command(label="Empty a slot (steps)...", command=self.on_clear, underline=0)
        live.add_command(label="Copy into a live slot...", command=self.on_copy_slot,
                         underline=5)
        live.add_command(label="Promote", command=self.on_promote, underline=0)

        bind_menu_hints(live, {"Promote": PROMOTE_TIP}, self._menu_hint)

        tools = menu("Tools")
        tools.add_command(label="Vault upgrade...", command=self.open_vault_window, underline=0)
        tools.add_command(label="Play time...", command=self.on_playtime, underline=0)
        tools.add_command(label="Check for updates...", command=self.on_check_updates,
                          underline=0)
        bind_menu_hints(tools, {"Vault upgrade...": VAULT_UPGRADE_TIP}, self._menu_hint)

        about = menu("Help")
        about.add_command(label="Help", command=self.on_help, accelerator="F1", underline=0)
        about.add_command(label="About NMS Save Vault", command=self.on_about, underline=0)
        self.configure(menu=bar)
        self.bind_all("<F1>", self._shortcut(self.on_help))
        self.bind_all("<F5>", self._shortcut(self.refresh))
        self.bind_all("<Control-Key-0>", self._shortcut(self._zoom_reset))
        self.bind_all("<Control-Key-z>", self._shortcut(self._undo_shortcut))
        self.bind_all("<Control-Key-Z>", self._shortcut(self._undo_shortcut))

    def _undo_shortcut(self) -> None:
        """Ctrl+Z (through the grab check): a text field keeps its own undo; elsewhere it is
        Edit > Undo."""
        if not isinstance(self.focus_get(), (tk.Text, tk.Entry, ttk.Entry)):
            self.on_edit_undo()

    def on_edit_undo(self) -> None:
        """Edit > Undo: on the Organizer tab the same Undo as the Done bar; on the classic
        tab the classic undo of the active live folder."""
        if self.current_view() == ORGANIZER:
            self.organizer_view.undo_last()
        else:
            self.on_undo()

    def open_vault_window(self) -> VaultWindow:
        """The vault upgrade window (D6 row 20): only ever opened by the owner or by the
        start-up offer after an interrupted undo; it never starts anything by itself."""
        env = VaultEnv(self.vault.root, [Path(s.path) for s in self.state.live_sources],
                       self.state, __version__)
        return VaultWindow(self, worker=self.worker, themed=self.themed, env=env,
                           on_changed=lambda: self.organizer_view.vault_changed())

    def _offer_interrupted_undo(self) -> None:
        if needs_attention(self.vault.root):
            self.open_vault_window()

    def _shortcut(self, action):
        """A window-wide key. Ignored while a dialog or the "Please wait" window holds the
        grab: F5 must not refresh the panes under an open dialog, nor zoom rebuild them."""
        def run(_event=None):
            if self.grab_current() is None:
                action()
            return "break"
        return run

    def _build_widgets(self) -> None:
        """Shell 10a (D6 2.11): the menu bar, the update banner, the tabs and the status bar;
        the tabs hold today's window content (Live saves and backups) and the organizer."""
        self.theme_var = tk.StringVar(value=theme.CHOICE_LABELS.get(self.state.theme, "System"))
        self.view_var = tk.StringVar(value=CLASSIC)
        self._build_menubar()

        self.status = tk.StringVar(value="")
        self.last_change = tk.StringVar(value="")
        bottom = ttk.Frame(self)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(bottom, textvariable=self.last_change, anchor="e", relief="sunken",
                  style="Status.TLabel").pack(side=tk.RIGHT)
        ttk.Label(
            bottom, textvariable=self.status, anchor="w", relief="sunken", style="Status.TLabel"
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Shown only when a newer release is found; packed above the tabs at that point.
        self.banner = ttk.Frame(self, style="Banner.TFrame", padding=pxs(8, 6))
        self.banner_var = tk.StringVar(value="")
        ttk.Label(self.banner, textvariable=self.banner_var, style="Banner.TLabel").pack(side=tk.LEFT)
        ttk.Button(self.banner, text="Dismiss", command=self._hide_banner).pack(side=tk.RIGHT, padx=pxs(6, 0))
        ttk.Button(self.banner, text="Open download page", command=self._open_releases).pack(side=tk.RIGHT)
        # Packed only when this build can actually replace itself -- see _show_update_banner.
        self.install_button = ttk.Button(
            self.banner, text="Install update", command=self.on_install_update
        )

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=pxs(2, 0))
        # The classic tab: today's toolbar, "Active live" and the two panes, unchanged. Its
        # handlers stay on the App until phase 2 re-routes them (D6 step 19).
        self.classic_view = ttk.Frame(self.notebook)
        self.organizer_view = OrganizerView(
            self.notebook, worker=self.worker, themed=self.themed, prefs=self.state.ui,
            save_prefs=self._save_state_quietly, make_organizer=self._make_organizer,
            on_last_change=self.last_change.set, on_hint=self._menu_hint)
        self.notebook.add(self.classic_view, text=VIEW_TITLES[CLASSIC])
        self.notebook.add(self.organizer_view, text=VIEW_TITLES[ORGANIZER])
        self._build_classic_view(self.classic_view)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.bind_all("<Control-Key-1>", self._shortcut(lambda: self.show_view(ORGANIZER)))
        self.bind_all("<Control-Key-2>", self._shortcut(lambda: self.show_view(CLASSIC)))

        # Both spellings of each key: Ctrl+= and Ctrl+- are what those keys report
        # unshifted, Ctrl++ and Ctrl+_ what they report with Shift held, plus the keypad.
        for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            self.bind_all(seq, self._shortcut(lambda: self._zoom(1)))
        for seq in ("<Control-minus>", "<Control-underscore>", "<Control-KP_Subtract>"):
            self.bind_all(seq, self._shortcut(lambda: self._zoom(-1)))

    # --- the tabs --------------------------------------------------------------

    def show_view(self, name: str) -> None:
        """Bring a tab to the front: ``"organizer"`` or ``"classic"``."""
        tab = self.organizer_view if name == ORGANIZER else self.classic_view
        self.notebook.select(tab)

    def _open_last_view(self) -> None:
        """Open on the tab used last (the classic one the first time). Opening a tab is not
        changing it: nothing is saved until the user switches."""
        last = self.state.ui.get("last_view")
        self.state.ui["last_view"] = last if last in VIEW_TITLES else CLASSIC
        self.show_view(self.state.ui["last_view"])
        self._on_tab_changed()

    def current_view(self) -> str:
        return ORGANIZER if self.notebook.select() == str(self.organizer_view) else CLASSIC

    def _on_tab_changed(self, _event=None) -> None:
        """Remember the tab (``ui.last_view``): the app opens in the one used last."""
        name = self.current_view()
        self.view_var.set(name)
        self.title(f"NMS Save Vault v{__version__} - {VIEW_TITLES[name]}")
        if self.state.ui.get("last_view") != name:
            self.state.ui["last_view"] = name
            self._save_state_quietly()
            if name == ORGANIZER:           # back on the tab: look again unless fresh
                self.organizer_view.shown()

    def _make_organizer(self) -> Organizer:
        """The organizer over this vault and the configured live folders (worker thread)."""
        organizer = Organizer(self.vault.root, self.state.live_sources)
        from .core.index import vaultlink

        if vaultlink.index_enabled(self.state.ui.get("index")):
            try:
                vaultlink.attach_service(organizer, self.state.live_sources)
            except Exception:  # noqa: BLE001 - no facts is a smaller window, not a failed one
                import logging
                logging.getLogger(__name__).exception("the content index could not start")
        return organizer

    def _menu_hint(self, text: str) -> None:
        """A menu entry's explanation goes to the status bar while it is highlighted."""
        self.status.set(aliases.scrub(text))

    def _build_classic_view(self, parent: ttk.Frame) -> None:
        self.toolbar = FlowBar(parent)
        self.toolbar.pack(side=tk.TOP, fill=tk.X, padx=px(6), pady=px(4))
        for text, cmd in [
            ("Backup live", self.on_backup),
            ("Restore", self.on_restore),
            ("Extract slot", self.on_extract),
            ("Empty slot (steps)...", self.on_clear),
            ("Copy into live slot...", self.on_copy_slot),
            ("Promote", self.on_promote),
            ("Import...", self.on_import),
            ("Play time...", self.on_playtime),
            ("Rescan", self.on_rescan),
            ("Discover", self.on_discover),
            ("Undo", self.on_undo),
            ("Refresh", self.refresh),
            ("Accounts...", self.on_accounts),
            ("Help", self.on_help),
        ]:
            button = self.toolbar.add(text, cmd)
            if text == "Promote":
                self.promote_tip = Tooltip(button, lambda _w, _k: PROMOTE_TIP, lambda _e: "promote")

        controls = ttk.Frame(parent)
        controls.pack(side=tk.TOP, fill=tk.X, padx=px(6), pady=pxs(0, 4))

        # Active live (write target) selector -- the writable accounts (Steam + Xbox).
        self._source_choices: dict[str, str] = {}  # label -> source id
        self.active_var = tk.StringVar(value="")
        ttk.Label(controls, text="Active live:").pack(side=tk.LEFT)
        self.active_combo = ttk.Combobox(controls, textvariable=self.active_var, state="readonly", width=30)
        self.active_combo.pack(side=tk.LEFT, padx=pxs(4, 16))
        self.active_combo.bind("<<ComboboxSelected>>", self._on_active_changed)

        # Two independent trees, one per pane: live saves are browsed, backups are
        # searched and sorted, and mixing them in one tree made both worse.
        panes = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=px(6), pady=px(4))
        self._panes = panes

        self.live_tree = self._make_pane(
            panes,
            "* LIVE SAVES",
            LIVE_COLUMNS,
            "Save folder / Slot / Save",
            weight=3,
        )
        self.backup_tree = self._make_pane(
            panes,
            "# BACKUPS",
            BACKUP_COLUMNS,
            "Backup / Slot / Save",
            weight=2,
            sortable=True,
        )
        body = self.backup_tree.master
        filter_row = ttk.Frame(body.master)
        filter_row.pack(side=tk.TOP, fill=tk.X, pady=pxs(0, 2), before=body)
        ttk.Label(filter_row, text="Save file:").pack(side=tk.LEFT)
        self.history_var = tk.StringVar(value=ALL_SAVES)
        self.history_combo = ttk.Combobox(
            filter_row, textvariable=self.history_var, state="readonly", width=80,
            values=[ALL_SAVES], postcommand=self._fill_history_combo,
        )
        self.history_combo.pack(side=tk.LEFT, padx=pxs(4, 0))
        self.history_combo.bind("<<ComboboxSelected>>", self._on_history_changed)
        # Which tree the toolbar acts on: whichever the user last selected in.
        self._active_tree = self.live_tree
        # Backups start newest-first; clicking a heading re-sorts.
        self._sort_key: str = "saved"
        self._sort_reverse: bool = True

        for tree in (self.live_tree, self.backup_tree):
            tree.bind("<Button-3>", self._on_right_click)
            tree.bind("<<TreeviewSelect>>", self._on_tree_select)
            Tooltip(tree, self._tip_for_row)

    # --- theme ---------------------------------------------------------------

    def _apply_theme(self, *, persist: bool = False) -> None:
        """Restyle everything for the current choice and re-colour the row tags.

        Tag colours cannot live in the ttk style, so they are re-applied here: the
        light-mode green and amber are unreadable on a dark tree background.
        """
        self.state.font_scale = theme.apply_font_scale(self, self.state.font_scale)
        self._rezoom_pixels(self.state.font_scale)
        self.after_idle(self.toolbar.reflow)       # the buttons' widths follow the font
        name = theme.apply(self, self.state.theme, self._native_ttk_theme)
        p = theme.PALETTES[name]
        for tree in (self.live_tree, self.backup_tree):
            tree.tag_configure("group", font=theme.HEADING_FONT)
            tree.tag_configure("live", foreground=p["live"])
            tree.tag_configure("active", foreground=p["active"], font=theme.BOLD_FONT)
            tree.tag_configure("readonly", foreground=p["readonly"])
            tree.tag_configure("backup", foreground=p["backup"])
        TOOLTIP_STYLE.update(
            background=p["tip_bg"], foreground=p["tip_fg"], border=p["tip_border"]
        )
        self._dark = name == theme.DARK
        for window in self._windows():
            theme.dark_title_bar(window, self._dark)
        self.themed.apply(p)
        if persist:
            try:
                appstate.save(self.state, self.state_path)
            except OSError as exc:
                _showwarning("Theme", f"Could not save your theme choice:\n{exc}")

    def destroy(self) -> None:
        view = self.__dict__.get("organizer_view")
        if view is not None and not view.confirm_close():
            return                       # an unsaved note: the owner chose to keep editing
        worker = self.__dict__.get("worker")   # absent if __init__ failed early (no getattr:
        # Tk forwards unknown attributes to its interpreter, which is not there yet either)
        if view is not None:
            view.index_strip.stop()
        if worker is not None:
            worker.shutdown()
        organizer = getattr(view, "organizer", None) if view is not None else None
        service = getattr(organizer, "facts", None)
        if service is not None and hasattr(service, "close"):
            try:
                service.close()           # joins the index's writer and dispatcher
            except Exception:  # noqa: BLE001 - closing must never keep the window open
                log.exception("the content index did not close cleanly")
        super().destroy()

    def _windows(self) -> list[tk.Misc]:
        """The main window and every open window of the app."""
        out: list[tk.Misc] = [self]
        stack = list(self.winfo_children())
        while stack:
            widget = stack.pop()
            if isinstance(widget, tk.Toplevel):
                out.append(widget)
            stack.extend(widget.winfo_children())
        return out

    def _on_window_mapped(self, event) -> None:
        if self._dark and isinstance(event.widget, (tk.Tk, tk.Toplevel)):
            theme.dark_title_bar(event.widget, True)

    def _rezoom_pixels(self, zoom: float) -> None:
        """Move px() to ``zoom`` and stretch the tree columns by the same ratio.

        By ratio rather than back to the design widths, so a column the user dragged wider
        stays wider. Paddings of widgets already built keep their size until rebuilt;
        styles (theme.apply) and every dialog opened from now on use the new factor.
        """
        before = dpi.zoom()
        if zoom == before:
            return
        dpi.configure(zoom=zoom)
        ratio = zoom / before
        for tree in (self.live_tree, self.backup_tree):
            for column in ("#0", *tree["columns"]):
                width = int(tree.column(column, "width"))
                tree.column(column, width=max(1, int(round(width * ratio))))
        self.themed.rezoom(ratio)               # the organizer's tables likewise

    def _zoom(self, steps: int) -> None:
        """Ctrl+/Ctrl-: step the font zoom; does nothing at the ends of the range.

        Saved quietly: a keystroke should not raise a modal because the install folder
        is read-only, and the theme menu already reports that failure loudly.
        """
        wanted = theme.clamp_font_scale(
            self.state.font_scale + steps * theme.FONT_SCALE_STEP
        )
        self._set_zoom(wanted)

    def _zoom_reset(self) -> None:
        """Ctrl+0 / View > Actual size."""
        self._set_zoom(1.0)

    def _set_zoom(self, wanted: float) -> None:
        if wanted == self.state.font_scale:
            return
        self.state.font_scale = wanted
        self._apply_theme()
        self._save_state_quietly()

    # --- update checking -----------------------------------------------------

    def _save_state_quietly(self) -> None:
        """Persist preferences. A read-only install dir is not worth a modal at startup;
        the setting simply will not stick, and the user is told if they try to change it
        from the theme menu (which reports the same failure loudly)."""
        try:
            appstate.save(self.state, self.state_path)
        except OSError:
            pass

    def _startup_update_check(self) -> None:
        """Ask once, then check quietly at most once a day."""
        if self.state.update_check == updates.ASK:
            self._ask_about_update_checks()
        if self.state.update_check != updates.ON:
            return
        if updates.due(self.state.update_last_check):
            self._check_for_updates(quiet=True)

    def _ask_about_update_checks(self) -> None:
        wants = _askyesno(
            "Check for updates?",
            "Would you like NMS Save Vault to check for new versions?\n\n"
            "If you say yes it asks api.github.com once a day, on startup, whether a newer "
            "release exists, and shows a bar at the top when there is one. Nothing about "
            "you or your saves is sent, and nothing is downloaded unless you press "
            "Install update on that bar.\n\n"
            "This is the only thing the app uses the network for. You can change this "
            "later from any right-click menu.",
        )
        self.state.update_check = updates.ON if wants else updates.OFF
        self._save_state_quietly()

    def on_check_updates(self) -> None:
        """Check now, regardless of the daily throttle."""
        if self.state.update_check != updates.ON:
            if not _askyesno(
                "Check for updates?",
                "This asks api.github.com whether a newer release exists, and turns on the "
                "daily check on startup.\n\nContact GitHub now?",
            ):
                return
            self.state.update_check = updates.ON
            self._save_state_quietly()
        self._check_for_updates(quiet=False)

    def _check_for_updates(self, *, quiet: bool) -> None:
        """Run the check off the Tk thread so a slow network never freezes the window."""

        def worker() -> None:
            try:
                outcome = updates.check()
            except updates.UpdateCheckError as exc:
                outcome = exc
            try:
                self.after(0, lambda: self._update_check_done(outcome, quiet))
            except (tk.TclError, RuntimeError):
                pass  # window closed while the check was still in flight

        threading.Thread(target=worker, daemon=True).start()

    def _update_check_done(self, outcome, quiet: bool) -> None:
        self.state.update_last_check = date.today().isoformat()
        self._save_state_quietly()
        if isinstance(outcome, Exception):
            if not quiet:  # a startup check failing offline should say nothing
                _showwarning("Check for updates", f"Could not check for updates:\n{outcome}")
            return
        if outcome is None:
            if not quiet:
                _showinfo("Check for updates", f"You are up to date (version {__version__}).")
            return
        self._show_update_banner(outcome)

    def _show_update_banner(self, release) -> None:
        self._release = release
        self._release_url = release.page_url
        self.banner_var.set(
            f"Version {release.version} is available - you have {__version__}."
        )
        # "Install update" only appears when it would actually work: the packaged app, on
        # Windows, and a release with a zip attached. Offering a button that can only
        # apologise is worse than not offering it.
        if updates.can_install(release)[0]:
            self.install_button.pack(side=tk.RIGHT, padx=pxs(0, 6))
        else:
            self.install_button.pack_forget()
        self.banner.pack(side=tk.TOP, fill=tk.X, padx=px(6), pady=pxs(0, 4), before=self.notebook)

    def _hide_banner(self) -> None:
        self.banner.pack_forget()

    def _open_releases(self) -> None:
        webbrowser.open(getattr(self, "_release_url", updates.RELEASES_PAGE))

    # --- installing an update ------------------------------------------------

    def on_install_update(self) -> None:
        """Download the new release, verify it, and hand off to the updater.

        The program cannot overwrite its own running files, so the last step is to start a
        small script that waits for this process to exit, swaps the folders and starts the
        new build. Everything before that point is reversible by doing nothing: the
        install folder is not touched until this window has closed.
        """
        release = getattr(self, "_release", None)
        if release is None:
            _showinfo("Install update", "Check for updates first.")
            return
        allowed, why = updates.can_install(release)
        if not allowed:
            _showinfo("Install update", f"{why}\n\nYou can still download it from the releases page.")
            return

        size = f" (about {release.asset_size / 1_048_576:.0f} MB)" if release.asset_size else ""
        if not _askyesno(
            "Install update",
            f"Download and install version {release.version}{size}?\n\n"
            f"It is downloaded from GitHub and checked before anything is replaced. "
            f"NMS Save Vault will then close and reopen on the new version.\n\n"
            f"Your settings, your vault and your saves are not touched.",
        ):
            return

        staged = self._download_update(release)
        if staged is None:
            return
        try:
            updates.launch(staged)
        except updates.UpdateInstallError as exc:
            _showerror("Install update", f"{exc}\n\nNothing was changed.")
            return
        # The updater is now waiting on this PID. Leave promptly so it can have the files.
        self.destroy()

    def _download_update(self, release):
        """Fetch and verify the release behind a progress bar. ``None`` on any failure."""
        win, bar, label = self._download_progress(release.version)
        holder: dict = {}

        def report(done: int, total: int) -> None:
            holder["progress"] = (done, total)

        def worker() -> None:
            try:
                holder["staged"] = updates.prepare(release, progress=report)
            except Exception as exc:  # noqa: BLE001 - reported on the main thread below
                holder["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            measured = False
            while thread.is_alive():
                done, total = holder.get("progress", (0, 0))
                if total:
                    if not measured:
                        # Stop the marching animation first, or it keeps driving the value
                        # it is no longer meant to own.
                        bar.stop()
                        bar.configure(mode="determinate")
                        measured = True
                    bar.configure(maximum=total, value=done)
                    label.configure(
                        text=f"Downloading version {release.version} - "
                        f"{done / 1_048_576:.1f} of {total / 1_048_576:.1f} MB"
                    )
                elif done:
                    label.configure(
                        text=f"Downloading version {release.version} - "
                        f"{done / 1_048_576:.1f} MB"
                    )
                self.update()
                time.sleep(0.05)
        except tk.TclError:
            # self.update() runs the event loop reentrantly, so the window manager can
            # close the app underneath this loop. Nothing has been installed at this
            # point; there is simply no longer a window to report to.
            return None
        finally:
            try:
                win.grab_release()
                win.destroy()
            except tk.TclError:
                pass

        if "error" in holder:
            _showerror("Install update", f"{holder['error']}\n\nNothing was changed.")
            return None
        return holder.get("staged")

    def _download_progress(self, version: str):
        """A wait window with a real progress bar; a download is worth measuring."""
        win = tk.Toplevel(self)
        win.title("Installing update")
        win.transient(self)
        win.resizable(False, False)
        label = ttk.Label(win, text=f"Contacting GitHub for version {version}...", padding=pxs(24, 18, 24, 8))
        label.pack()
        bar = ttk.Progressbar(win, mode="indeterminate", length=px(320))
        bar.pack(padx=px(24), pady=pxs(0, 20))
        bar.start(12)
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.protocol("WM_DELETE_WINDOW", lambda: None)  # closing mid-download helps nobody
        win.grab_set()
        win.update()
        return win, bar, label

    def _on_theme_changed(self, _event=None) -> None:
        label = self.theme_var.get()
        choice = next(
            (c for c, text in theme.CHOICE_LABELS.items() if text == label), theme.SYSTEM
        )
        if choice == self.state.theme:
            return
        self.state.theme = choice
        self._apply_theme(persist=True)

    def _make_pane(self, panes, title, columns, tree_heading, *, weight, sortable=False):
        """One titled pane holding a scrolled tree; returns the tree."""
        frame = ttk.Frame(panes)
        panes.add(frame, weight=weight)
        ttk.Label(frame, text=title, font=theme.HEADING_FONT, anchor="w").pack(
            side=tk.TOP, fill=tk.X, pady=pxs(0, 2)
        )
        body = ttk.Frame(frame)
        body.pack(fill=tk.BOTH, expand=True)
        tree = RedactingTreeview(body, columns=[c.key for c in columns], show="tree headings")
        tree.heading("#0", text=tree_heading)
        tree.column("#0", width=px(300), anchor="w")
        for col in columns:
            if sortable:
                tree.heading(col.key, text=col.title,
                             command=lambda k=col.key: self._sort_backups(k))
            else:
                tree.heading(col.key, text=col.title)
            tree.column(col.key, width=px(col.width), anchor="w")
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

    def _remember(self, tree, row: str, meta: dict) -> None:
        """Record what a row means.

        Keyed by (tree, row) rather than row alone: Tk numbers items per widget, so the
        live tree and the backups tree both produce "I001", "I002", ... and a single
        row-keyed dict silently let the second-populated tree overwrite the first.
        """
        self._meta[(str(tree), row)] = meta

    def _meta_for(self, tree, row: str) -> dict | None:
        return self._meta.get((str(tree), row))

    def refresh(self) -> None:
        for tree in (self.live_tree, self.backup_tree):
            tree.delete(*tree.get_children())
        self._meta.clear()
        self._report = None  # the saves may have changed; re-read on next use

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
            badge = " - read-only (Xbox)" if not s.writable else (" - ACTIVE" if active else "")
            tags = ("active",) if active else (("readonly",) if not s.writable else ("live",))
            view = savedir.scan_any(s.path)
            node = self.live_tree.insert(
                "", "end", open=active,
                text=f"{_source_caption(s)}{badge}",
                values=("", "", "", "", "writable" if s.writable else "read-only"),
                tags=tags,
            )
            self._remember(self.live_tree, node, {
                "type": "live", "dir": s.path, "writable": s.writable, "source_id": s.id,
                "tip": self._source_tip(s, view),
            })
            self._add_view(node, view, writable=s.writable)

        self._populate_backups()
        self._refresh_source_combo()
        running = ops.safety.is_game_running()
        game = {True: "RUNNING (writes blocked)", False: "closed", None: "unknown"}[running]
        active = self._active_source()
        active_txt = active.label if active else (str(self.live_dir) if self.live_dir else "none")
        self.status.set(
            aliases.scrub(      # the home folder and account ids, not only the aliased accounts
                f"Active live: {active_txt}   |   Sources: {len(sources)}   |   "
                f"Vault: {self.vault.root}   |   Game: {game}"
            )
        )

    def _backup_entries(self):
        """The BACKUPS pane's rows: every entry of the vault through D3's one listing
        (catalog and store, a folded entry once, a live folder never), catalog-shaped."""
        from .core.store import classic as store_classic

        try:
            return store_classic.list_entries(self.vault)
        except Exception:  # noqa: BLE001 - the pane must still show the catalog
            log.exception("the Backups pane could not read the vault listing (vault %s); "
                          "showing the catalog entries only", self.vault.root.name)
            return list(self.vault.entries)

    def _populate_backups(self) -> None:
        """BACKUPS pane: catalog entries, sortable by any column, optionally narrowed to
        the backups holding one save file -- and within each, to the slot(s) holding it."""
        tree = self.backup_tree
        tree.delete(*tree.get_children())
        self._meta = {k: v for k, v in self._meta.items() if k[0] != str(tree)}
        shown = self._history_playthrough()
        for e in self._backup_entries():
            only = None
            if shown is not None:
                only = {slot for folder, slot in shown.locations if folder == str(Path(e.path))}
                if not only:
                    continue
            node = tree.insert(
                "", "end", text=e.id, open=shown is not None,
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
            self._remember(tree, node, {"type": "entry", "entry": e, "tip": self._entry_tip(e)})
            self._add_entry(node, e, only)
        self._apply_backup_sort()
        self._fill_history_combo(compute=False)

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
            self._remember(self.live_tree, node, {
                "type": "slot", "dir": str(view.path), "slot": slot, "live": writable,
                "tip": self._slot_tip(sv, view),
            })
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
                self._remember(self.live_tree, mid, {
                    "type": "member",
                    "dir": str(view.path),
                    "slot": slot,
                    "member": slotmap.member_index(m.label),
                    "live": writable,
                    "tip": self._member_tip(m, sv),
                })

    def _add_entry(self, parent: str, entry, only: set[int] | None = None) -> None:
        for s in entry.slots:
            if not s.occupied or (only is not None and s.slot not in only):
                continue
            ts = max((m.timestamp for m in s.members if m.present), default=0)
            newest = next((m for m in s.members if m.label == s.newest_label), None)
            node = self.backup_tree.insert(
                parent,
                "end",
                text=f"Slot {s.slot}",
                values=("", "", s.name, newest.difficulty_label if newest else "", "", _fmt_ts(ts)),
            )
            self._remember(self.backup_tree, node, {
                "type": "slot", "dir": entry.path, "slot": s.slot, "live": False, "entry": entry,
                "tip": self._backup_slot_tip(s, entry),
            })
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
                self._remember(self.backup_tree, mid, {
                    "type": "member",
                    "dir": entry.path,
                    "slot": s.slot,
                    "member": slotmap.member_index(m.label),
                    "live": False,
                    "entry": entry,
                    "tip": self._backup_member_tip(m, s),
                })

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
            entry = (self._meta_for(self.backup_tree, row) or {}).get("entry")
            if self._sort_key == "saved" and entry is not None:
                return entry.created  # ISO-8601: sorts correctly as text
            if self._sort_key == "slots" and entry is not None:
                return len(entry.occupied_slots)
            return str(self.backup_tree.set(row, column)).lower()

        rows = sorted(self.backup_tree.get_children(""), key=sort_value, reverse=self._sort_reverse)
        for position, row in enumerate(rows):
            self.backup_tree.move(row, "", position)
        arrow = " v" if self._sort_reverse else " ^"
        for col in BACKUP_COLUMNS:
            self.backup_tree.heading(
                col.key, text=col.title + (arrow if col.key == self._sort_key else "")
            )

    # --- save-file history (backups pane) -------------------------------------

    def _playthrough_report(self) -> playtime.PlaytimeReport:
        """Every save folded onto its playthrough; read once per refresh, on demand."""
        if self._report is None:
            self.config(cursor="watch")
            self.update_idletasks()
            try:
                live = [s.path for s in self.state.live_sources if s.exists]
                self._report = playtime.collect(self.vault, live)
            finally:
                self.config(cursor="")
        return self._report

    def _history_playthrough(self) -> playtime.Playthrough | None:
        if self._history_key is None:
            return None
        found = next((p for p in self._playthrough_report().playthroughs if p.key == self._history_key), None)
        if found is None:  # its last copy is gone; fall back to showing everything
            self._history_key = None
        return found

    def _fill_history_combo(self, compute: bool = True) -> None:
        """List every save file that has a backup, under its current name and the names
        it had before. Without ``compute`` it only relabels from a report already read."""
        if self._report is None and not compute:
            if self._history_key is None:
                self.history_var.set(ALL_SAVES)
            return
        self._history_choices = {}
        runs = [p for p in self._playthrough_report().playthroughs if p.backups]
        for p in sorted(runs, key=lambda p: p.name.lower()):
            was = f" - was: {', '.join(p.former_names)}" if p.former_names else ""
            n = p.backups
            # Play time is what tells apart runs that share a name, or have none.
            label = aliases.redact(
                f"{p.name}{was}  ({', '.join(p.platforms)}, {_fmt_play(p.play_time) or '0h00'}, "
                f"{n} backup{'s' if n != 1 else ''})"
            )
            unique, i = label, 2
            while unique in self._history_choices:
                unique, i = f"{label} #{i}", i + 1
            self._history_choices[unique] = p.key
        self.history_combo["values"] = [ALL_SAVES, *self._history_choices]
        current = next((lbl for lbl, k in self._history_choices.items() if k == self._history_key), None)
        self.history_var.set(current or ALL_SAVES)

    def _on_history_changed(self, _event=None) -> None:
        self._history_key = self._history_choices.get(self.history_var.get())
        self._populate_backups()

    def _show_history_of(self, folder: str, slot: int) -> None:
        """Right-click: narrow the backups to every copy of the save in this slot."""
        p = self._playthrough_report().find(folder, slot)
        if p is None or not p.backups:
            _showinfo("Save file history", f"There is no backup of the save in slot {slot} yet.")
            return
        self._history_key = p.key
        self._populate_backups()

    # --- tooltips ------------------------------------------------------------

    def _tip_for_row(self, tree, row_id: str) -> str:
        meta = self._meta_for(tree, row_id)
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
        lines += ["", SLOT_MARKERS_TIP]
        return "\n".join(lines)

    def _member_tip(self, m, sv) -> str:
        newest = sv.newest
        lines = [
            f"{m.save_type_label} - {_SAVE_TYPE_HELP[m.save_type_label]}",
            "",
            f"Save name:  {m.save_name or '<unnamed>'}",
        ]
        if m.info:
            lines.append(f"Difficulty: {m.info.difficulty_label or 'unknown'}")
            lines.append(f"Play time:  {_fmt_play(m.info.total_play_time) or '-'}")
            if m.info.save_summary:
                lines.append(f"Where:      {m.info.save_summary}")
        lines.append(f"Saved:      {_fmt_ts(m.effective_timestamp)}")
        lines.append(f"Current:    {'yes - this is what the game loads' if newest and m.label == newest.label else 'no'}")
        lines.append(f"File:       {m.ref.data_name}")
        lines.append(f"Size:       {_fmt_size(m.data_size)}")
        lines.append(f"Integrity:  {'valid' if m.valid else 'INVALID'}")
        if m.cloud_status:
            lines.append(f"Cloud:      {m.cloud_status}")
        if m.note:
            lines.append(f"Note:       {m.note}")
        lines += ["", SLOT_MARKERS_TIP]
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
            f"{m.save_type_label} - {_SAVE_TYPE_HELP[m.save_type_label]}",
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
                return self._meta_for(tree, sel[0])
        return None

    def _source_from(self, meta: dict) -> SlotSource | None:
        """The slot a row can be copied FROM, or None if the row is not a slot at all.

        A save row resolves to its slot, because copying is slot-granular; a single-slot
        extract resolves to the slot it holds.
        """
        kind = meta.get("type")
        if kind in ("slot", "member"):
            entry = meta.get("entry")
            where = entry.id if entry is not None else Path(meta["dir"]).name
            return SlotSource(meta["dir"], meta["slot"], where)
        if kind == "entry":
            slot = _extract_slot_number(meta["entry"])
            if slot is not None:
                return SlotSource(meta["entry"].path, slot, meta["entry"].id)
        return None

    def _on_right_click(self, event) -> None:
        """Build a context menu offering exactly what makes sense for the row clicked."""
        tree = event.widget
        row = tree.identify_row(event.y)
        if not row:
            return
        tree.selection_set(row)  # fires <<TreeviewSelect>>, which clears the other tree
        meta = self._meta_for(tree, row)
        if not meta:
            return
        menu = tk.Menu(self, tearoff=0)
        kind = meta.get("type")

        if kind == "live":
            self._menu_for_live_folder(menu, meta)
        elif kind == "entry":
            self._menu_for_backup(menu, meta)
        elif kind in ("slot", "member"):
            self._menu_for_slot(menu, meta)

        menu.add_separator()
        menu.add_command(label="Refresh", command=self.refresh)
        menu.add_command(label="Account display names...", command=self.on_accounts)
        menu.add_command(label="Check for updates...", command=self.on_check_updates)
        menu.add_command(label="Help", command=self.on_help)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # A live save folder: back it up, aim writes at it, or pull a backup into it.
    def _menu_for_live_folder(self, menu, meta: dict) -> None:
        sid = meta.get("source_id")
        writable = meta.get("writable")
        if writable and sid and sid != self.active_source_id:
            menu.add_command(label="Set as active live target", command=lambda: self._set_active(sid))
            menu.add_separator()
        if writable:
            menu.add_command(label="Back up this folder now...", command=lambda: self._backup_dir(meta["dir"]))
            menu.add_command(
                label="Copy a save into one of its slots...",
                command=lambda: self.on_copy_slot(dest_dir=meta["dir"]),
            )
            menu.add_separator()
            menu.add_command(
                label="Undo the last change to live saves", command=lambda: self.on_undo(meta["dir"])
            )
        else:
            menu.add_command(
                label="Import this Xbox folder as a backup",
                command=lambda: self._import_dir(meta["dir"]),
            )

    # A backup: a whole folder goes back wholesale, a one-slot extract goes into a slot.
    def _menu_for_backup(self, menu, meta: dict) -> None:
        entry = meta["entry"]
        slot = _extract_slot_number(entry)
        menu.add_command(label=_restore_menu_label(entry), command=lambda: self.on_restore(meta))
        if slot is not None:
            # An extract is one slot: it can go back where it came from (above), or into
            # any slot you choose. Only offering the former was too narrow.
            menu.add_command(
                label=f"Copy slot {slot} into a different live slot...",
                command=lambda: self.on_copy_slot(source=self._source_from(meta)),
            )

    # A slot, or one of the two saves in it, in either pane.
    def _menu_for_slot(self, menu, meta: dict) -> None:
        slot = meta["slot"]
        live = meta.get("live")
        source = self._source_from(meta)
        # A member row's actions act on its whole slot, so the target is narrowed to the
        # slot -- but it must still say whether that slot is live, or an action that only
        # makes sense on live saves (Clear) cannot tell and refuses its own menu entry.
        target = {"type": "slot", "dir": meta["dir"], "slot": slot, "live": live}

        if meta.get("type") == "member" and live:
            label = slotmap.save_type_label(meta["member"])
            menu.add_command(
                label=f"Make the {label} the one the game loads (promote)",
                command=lambda: self.on_promote(meta),
            )
            menu.add_separator()

        if live:
            menu.add_command(
                label=f"Undo the last move into live slot {slot}...",
                command=lambda: self.on_undo_slot(meta["dir"], slot),
            )
            menu.add_command(
                label=f"Replace live slot {slot} with a save from anywhere...",
                command=lambda: self.on_copy_slot(dest_dir=meta["dir"], dest_slot=slot),
            )
            menu.add_command(
                label=f"Copy live slot {slot} into another live slot...",
                command=lambda: self.on_copy_slot(source=source),
            )
            menu.add_separator()
            menu.add_command(
                label=f"Extract live slot {slot} to the vault",
                command=lambda: self.on_extract(target),
            )
            menu.add_command(
                label=f"Empty live slot {slot} (manual steps)...",
                command=lambda: self.on_clear(target),
            )
        else:
            menu.add_command(
                label=f"Copy slot {slot} into live slot {slot}",
                command=lambda: self.on_copy_slot(source=source, dest_slot=slot, ask=False),
            )
            menu.add_command(
                label=f"Copy slot {slot} into a live slot...",
                command=lambda: self.on_copy_slot(source=source),
            )
            menu.add_separator()
            menu.add_command(
                label=f"Extract slot {slot} to the vault as its own backup",
                command=lambda: self.on_extract(target),
            )
        menu.add_separator()
        menu.add_command(
            label="Show this save's history (every backup of it)",
            command=lambda: self._show_history_of(meta["dir"], slot),
        )

    # --- actions -------------------------------------------------------------

    def _run(
        self, fn, *, success: str, message: str = "Working - please wait...",
        warnings: list[str] | None = None,
    ) -> None:
        """Run a core operation on a worker thread behind a modal wait dialog, prompting to
        override the game-running guard if the op reports the game is open. ``warnings`` is
        the list an operation without an OpResult (backup, extract, import) fills."""
        holder = self._run_worker(fn, False, message)
        if isinstance(holder.get("error"), ops.GameRunningError):
            if not _askyesno("Game running", "No Man's Sky appears to be running.\nProceed anyway (risky)?"):
                return
            holder = self._run_worker(fn, True, message)

        err = holder.get("error")
        if isinstance(err, ops.FeatureNotYetAvailableError):
            _showinfo("Coming soon", str(err))
            return
        if isinstance(err, ops.WrittenButInvalidError):
            self.refresh()                         # the live folder WAS changed
            _showerror(
                "Only partly done - the live folder was changed",
                _with_warnings(str(err) + _undo_note(err.result.snapshot_id), err.result.warnings),
            )
            return
        if isinstance(err, ops.ForeignAccountError):
            _showerror("Another account's backup - refused", str(err))
            return
        if isinstance(err, ops.OperationError):
            _showerror("Operation failed", str(err))
            return
        if err is not None:
            _showerror("Unexpected error", repr(err))
            return
        self.refresh()
        self._present_result(holder.get("result"), success, warnings or [])

    def _present_result(self, result, success: str, warnings: list[str] = ()) -> None:
        if isinstance(result, ops.OpResult):
            _showinfo("Done", _with_warnings(result.detail + _undo_note(result.snapshot_id), result.warnings))
        else:
            _showinfo("Done", _with_warnings(success, list(warnings)))

    def _busy(self, message: str) -> tk.Toplevel:
        """A small modal 'please wait' window with an animated indeterminate bar."""
        win = tk.Toplevel(self)
        win.title("Please wait")
        win.transient(self)
        win.resizable(False, False)
        ttk.Label(win, text=message, padding=pxs(24, 18, 24, 8)).pack()
        pb = ttk.Progressbar(win, mode="indeterminate", length=px(260))
        pb.pack(padx=px(24), pady=pxs(0, 20))
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
        warns: list[str] = []
        self._run(
            lambda _force: ops.create_full_backup(self.vault, Path(directory), label=label, warnings=warns),
            success="Backup created.",
            message="Creating backup - please wait...",
            warnings=warns,
        )

    def on_restore(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "entry":
            _showinfo(
                "Select a backup",
                "Select a backup - one of the top-level rows in the BACKUPS pane. To put "
                "back a single slot instead, select that slot and use Copy into live slot.",
            )
            return
        if not self._require_live():
            return
        entry = sel["entry"]
        slots = [s.slot for s in entry.occupied_slots]
        with_account = False
        if _extract_slot_number(entry) is not None:
            # An extract is one slot lifted aside, not a whole folder: it goes back where
            # it came from, and nothing else in the live folder is touched.
            question = (
                f"Put slot {slots[0]} back into live slot {slots[0]}, from '{entry.id}'?\n\n"
                f"Only slot {slots[0]} is overwritten - every other save is left alone.\n"
                "The current state is auto-snapshotted first."
            )
            if not _askyesno("Restore", question):
                return
        else:
            self.open_panic_restore(entry)      # the whole-folder restore is the panic restore
            return
        live_dir = self.live_dir
        self._run(
            lambda force: ops.restore_entry(
                self.vault, entry, live_dir, allow_game_running=force, restore_accountdata=with_account
            ),
            success="Restored.",
            message="Restoring - please wait...",
        )

    def open_panic_restore(self, entry=None) -> PanicDialog | None:
        """The whole-folder restore dialog (D5 4.3) for the active live folder, with ``entry``
        (a backup or snapshot) pre-picked; the dialog names the folder it writes into."""
        if not self._require_live():
            return None
        vault, live_dir = self.vault, Path(self.live_dir)
        snapshot = None if entry is None else (entry.id, aliases.scrub(
            f"{entry.label or entry.id}  ({entry.kind}, {entry.created})"))

        def done(outcome) -> None:
            self.status.set(aliases.scrub(f"Restored {live_dir.name}."))
            notes = [w.text for w in outcome.warnings]
            if notes:
                _showinfo("Restored", "\n\n".join(notes))
            self.last_change.set("Last change: whole folder restored")
            self.refresh()
            self.organizer_view.rescan()
        self.panic_dialog = PanicDialog(
            self, worker=self.worker, themed=self.themed,
            open_context=lambda: classic.open_context(vault, live_dir),
            vault=vault, live_dir=live_dir, folder_name=aliases.redact(live_dir.name),
            snapshot=snapshot, on_done=done)
        return self.panic_dialog

    def on_extract(self, target: dict | None = None) -> None:
        """Lift a slot aside into the vault. Works from a slot row or either save in it."""
        sel = target or self._selected()
        if not sel or sel.get("type") not in ("slot", "member"):
            _showinfo(
                "Select a slot",
                "Select a slot - or either of the two saves inside it - in the LIVE SAVES "
                "pane or inside a backup, then press Extract slot.",
            )
            return
        warns: list[str] = []
        live_dirs = self._live_paths()
        vault, entry, slot = self.vault, sel.get("entry"), sel["slot"]
        if not str(sel.get("dir") or "").strip() and entry is not None:
            extract = lambda _force: classic.extract_from_entry(  # noqa: E731
                vault, entry.id, slot, warnings=warns, live_dirs=live_dirs)
        else:
            folder = Path(sel["dir"])
            extract = lambda _force: ops.extract_slot(  # noqa: E731
                vault, folder, slot, label="", warnings=warns, live_dirs=live_dirs)
        self._run(
            extract,
            success=f"Extracted slot {sel['slot']}.",
            message=f"Extracting slot {sel['slot']} - please wait...",
            warnings=warns,
        )

    def on_clear(self, target: dict | None = None) -> None:
        """The app never empties a slot: show the steps to do it by hand, after offering to
        keep a copy of what the slot holds in the vault (D5 2.4.2)."""
        sel = target or self._selected()
        if not sel or sel.get("type") not in ("slot", "member") or not sel.get("live"):
            _showinfo(
                "Select a live slot",
                "Emptying a slot needs a LIVE one: select a slot - or either of the two "
                "saves inside it - in the LIVE SAVES pane.",
            )
            return
        self._empty_steps(Path(sel["dir"]), sel["slot"])

    def _empty_steps(self, directory: Path, slot: int) -> None:
        """The steps to empty a live slot by hand (never an op), after a kept copy of what it
        holds when that is not in the vault yet (F9: no copy, no steps). The guide is built in
        the worker."""
        holder = self._run_worker(
            lambda _force: classic.empty_guide(self.vault, directory, slot), False,
            "Looking at the slot - please wait...",
        )
        if "error" in holder:
            _showerror("Empty a slot", f"Could not look at the slot:\n{holder['error']}")
            return
        guide = holder["result"]
        if not guide.occupied:
            _showinfo("Already empty", f"{guide.label} holds no saves.")
            return
        if not guide.durable and _askyesno(
                "Keep a copy first?",
                f"{guide.label} holds a save with no copy in the vault. Keep a copy in the "
                "vault now?"):
            kept = self._run_worker(
                lambda _force: classic.keep_copy(self.vault, directory, slot), False,
                f"Keeping slot {slot} - please wait...")
            if "error" in kept:      # no copy, no steps: the save is not yet safe (F9)
                _showerror("Keep a copy", "The copy could not be kept, so no steps are "
                           "shown and nothing was emptied:" + chr(10) + str(kept["error"]))
                return
        self.steps_window = StepsWindow(self, themed=self.themed, guide=guide)

    def on_copy_slot(self, source=None, dest_dir=None, dest_slot=None, ask: bool = True) -> None:
        """Copy one slot's saves into a live slot.

        Either end may be supplied by the caller and the other asked for, because the user
        can sensibly start from either: "put this backup somewhere" or "fill this live slot
        from something". With ``ask=False`` and both ends known it only confirms.
        """
        if source is None and dest_dir is None and dest_slot is None:
            # Toolbar with a selection: use whatever the selected row can be.
            sel = self._selected()
            if sel:
                source = self._source_from(sel)
                if source is None and sel.get("type") == "live":
                    dest_dir = sel["dir"]
                elif sel.get("live") and sel.get("type") in ("slot", "member"):
                    # A live slot is ambiguous -- it is a perfectly good source AND a
                    # perfectly good destination -- so let the dialog show both ends.
                    dest_dir, dest_slot = sel["dir"], sel["slot"]

        if not self._writable_sources():
            _showerror("No writable live folder", "There is no live save folder to copy into.")
            return

        if ask or source is None or dest_slot is None:
            outcome = CopySlotDialog(self, source=source, dest_dir=dest_dir, dest_slot=dest_slot).result
            if outcome is None:
                return
            source, dest_dir, dest_slot = outcome
        else:
            dest_dir = Path(dest_dir or self.live_dir)
            view = savedir.scan_any(dest_dir)
            occupant = view.slots[dest_slot].display_name if dest_slot in view.slots else ""
            if not _askyesno(
                "Copy into a live slot",
                f"Copy {source.caption}\ninto live slot {dest_slot} of {Path(dest_dir).name}?\n\n"
                f"Live slot {dest_slot} currently holds: {occupant or 'nothing'}\n\n"
                + COPY_RULE.replace("\n", " "),
            ):
                return

        dest_folder, slot, vault = Path(dest_dir), dest_slot, self.vault
        if not str(source.folder).strip():
            # A backup held by the version store has no folder of its own: its slot is read
            # through the store, never from a path built out of nothing (the current directory).
            entry_id, source_slot = source.where, source.slot
            run = lambda force: classic.repopulate_entry(  # noqa: E731
                vault, entry_id, source_slot, dest_folder, slot, allow_game_running=force)
        else:
            src_folder = Path(source.folder)
            run = lambda force: ops.repopulate_slot(  # noqa: E731
                vault, src_folder, source.slot, dest_folder, slot, allow_game_running=force)
        self._run(
            run,
            success=f"Copied into live slot {slot}.",
            message=f"Writing live slot {slot} - please wait...",
        )

    def on_promote(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "member" or not sel.get("live"):
            _showinfo(
                "Select a live save",
                "Promote changes WHICH of a slot's two saves the game loads, so it needs "
                "one of them: expand a slot in the LIVE SAVES pane and select its "
                "Auto-Save or Restore-Point.",
            )
            return
        directory = Path(sel["dir"])               # the row's own folder, not the active one (P3)
        if not self._writable_dir(directory):
            _showinfo(
                "Read-only folder",
                f"{directory.name} is read-only here (an Xbox folder without its save index, "
                "or a live folder that is switched off), so its saves cannot be promoted.",
            )
            return
        self._run(
            lambda force: ops.promote_member(self.vault, directory, sel["slot"], sel["member"], allow_game_running=force),
            success="Promoted.",
            message="Promoting - please wait...",
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
        warns: list[str] = []
        self._run(
            lambda _force: ops.import_backup(self.vault, directory, copy_into_vault=copy, warnings=warns),
            success="Imported.",
            message="Importing - please wait...",
            warnings=warns,
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
            message="Importing vault - please wait...",
        )

    def on_rescan(self) -> None:
        """Re-scan AppData for live sources and merge any new accounts into the config."""
        added = discover.merge_live_sources(self.state)
        try:
            appstate.save(self.state, self.state_path)
        except OSError as exc:
            _showwarning("Rescan", f"Found sources but could not save config:\n{exc}")
        # If we had no active writable source yet, adopt the first one found.
        if self.active_source_id is None:
            writable = self._writable_sources()
            if writable:
                self.active_source_id = writable[0].id
                self.live_dir = Path(writable[0].path)
        self.refresh()
        self.organizer_view.sources_changed()
        self.organizer_view.rescan()
        _showinfo(
            "Rescan",
            f"Added {added} new live source(s).\n"
            f"Now tracking {len(self.state.live_sources)} live source(s).\n\n"
            "Tip: use Discover to also catalog copy-paste backups.",
        )

    def on_discover(self) -> None:
        # No live folder, enabled or not, is a backup (a copy-paste "st_... - Copy" still
        # is one); the vault itself is not searched.
        live = discover.live_folders(discover.all_live_paths(self.state))
        dirs = discover.discover_inplace_backups(live=live, exclude=[self.vault.root])
        known = {Path(e.path).resolve() for e in self.vault.entries}
        added = 0
        warns: list[str] = []
        failures: list[str] = []
        try:
            for d in dirs:
                try:
                    if d.resolve() in known:
                        continue
                    ops.import_backup(self.vault, d, label=d.name, copy_into_vault=False, warnings=warns)
                    added += 1
                except Exception as exc:  # noqa: BLE001 - one folder must not stop the rest
                    failures.append(f"{d}: {exc}")
        finally:
            self.refresh()
        lines = [f"Added {added} newly found backup(s) to the catalog."]
        if failures:
            lines.append("Could not add:\n - " + "\n - ".join(failures))
        # Entries an older version made of live folders are pointed out, never changed.
        vault_is_live, aliased = discover.entries_in_live_folders(self.vault, live)
        if vault_is_live:
            lines.append(
                f"Note: the vault ({self.vault.root}) is or lies inside a live save folder; "
                "its entries are left as they are."
            )
        lines += [
            f"Note: catalog entry '{e.id}' ({e.path}) is or lies inside a live save folder, "
            "not a backup; it is left as it is."
            for e in aliased
        ]
        _showinfo("Discover", _with_warnings("\n\n".join(lines), warns))

    def on_undo_slot(self, directory, slot: int) -> None:
        """Undo the last move into one live slot: the live operations' own ``plan_undo`` of
        the newest finished op that changed THAT slot as a slot operation, confirmed in the
        plan dialog. When the newest change was a whole-folder restore, that is said and the
        whole-folder Undo offered explicitly - other slots are never rolled back silently."""
        vault, directory = self.vault, Path(directory)

        def job():
            ctx, ref = classic.open_context(vault, directory)
            history = liveops.op_history(ctx, ref.target(slot))
            last = next((s for s in history if s.status == "done"
                         and s.kind not in ("keep", "undo")), None)
            plan = None if last is None or last.kind == "panic_restore"                 else liveops.plan_undo(ctx, last.op_id)
            return last, plan

        def planned(result) -> None:
            last, plan = result
            if last is None:
                _showinfo("Nothing to undo", f"No move into live slot {slot} can be undone.")
                return
            if last.kind == "panic_restore":
                if _askyesno("Undo the whole folder?",
                             f"The newest change to live slot {slot} was a whole-folder restore "
                             "(it changed every slot the backup held). Undoing it rolls back "
                             "all of those slots, not just this one.\n\n"
                             "Undo the whole-folder restore?"):
                    self.on_undo(directory)
                return
            if any(w.code == "UNDO_WOULD_EMPTY" for w in plan.blocked) and plan.targets:
                self._empty_steps(directory, slot)       # F9: a copy first, then the steps
                return
            self.plan_dialog = PlanDialog(
                self, worker=self.worker, themed=self.themed,
                make_context=lambda: classic.open_context(vault, directory)[0], plan=plan,
                title=f"Undo the last move into live slot {slot}",
                on_done=lambda o: (self.refresh(), self.organizer_view.rescan()))
        self.worker.submit(job, on_done=planned, on_error=lambda e: _showerror(
            "Cannot undo", str(e)))

    def on_undo(self, directory=None) -> None:
        """Undo the last change to a live folder: the one whose menu was used, else the
        active one. Names the change first, asks again over a damaged history, and offers
        the account data."""
        if directory is None:
            if not self._require_live():
                return
            directory = self.live_dir
        directory = Path(directory)
        others = self._live_paths()
        try:
            plan = ops.plan_undo(self.vault, directory, other_live_dirs=others)
        except ops.OperationError as exc:
            _showerror("Cannot undo", str(exc))
            return
        if plan.damaged_after and not _askyesno("Undo - damaged history", _undo_damage_question(plan)):
            return
        # When the change was a restore that put the account data back, undoing it does too:
        # the question says so, and offers no choice that would not be honoured.
        check = None if plan.with_accountdata or savedir.platform_of(directory) == "xbox" \
            else ACCOUNT_DATA_CHOICE
        ok, with_account = _confirm(self, "Undo", _undo_question(plan, directory), check=check)
        if not ok:
            return
        self._run(
            lambda force: ops.undo_last(
                self.vault, directory, allow_game_running=force, acknowledge_damage=plan.damaged_after,
                other_live_dirs=others, restore_accountdata=with_account, expected=plan.token,
            ),
            success="Undone.",
            message="Undoing - please wait...",
        )

    def on_playtime(self) -> None:
        """Total play time across every save the app can see, one row per playthrough."""
        PlaytimeDialog(self, self._playthrough_report())

    def on_accounts(self) -> None:
        try:
            AccountsDialog(self)
        except aliases.AliasConfigError as exc:
            messagebox.showerror(  # raw: the alias file is what failed to parse
                "Account display names",
                f"Could not read the account display names.\n\nFix or delete this file:\n{exc}",
            )

    def on_about(self) -> None:
        _showinfo(
            "About NMS Save Vault",
            f"NMS Save Vault {__version__}\n\n"
            "Backs up, catalogs and re-slots No Man's Sky saves (Steam and Xbox / Game "
            "Pass).\n\n"
            "GPL-3.0. Parts are a port of libNOM.io and NomNom; see the README for the "
            "credits.",
        )

    def on_help(self) -> None:
        win = tk.Toplevel(self)
        win.title("NMS Save Vault - Help")
        ttk.Button(win, text="Close", command=win.destroy).pack(side=tk.BOTTOM, pady=px(6))
        vsb = ttk.Scrollbar(win, orient="vertical")
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        p = theme.palette(self.state.theme)  # tk.Text is a classic widget: no ttk style
        win.configure(background=p["bg"])
        txt = tk.Text(
            win, wrap="word", padx=px(10), pady=px(10), yscrollcommand=vsb.set,
            background=p["field"], foreground=p["fg"], insertbackground=p["fg"],
            relief="flat",
        )
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.config(command=txt.yview)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")
        dpi.fit_to_content(win, 760, 620)


class PlaytimeDialog(tk.Toplevel):
    """One row per playthrough, with the headline total above it.

    Every copy of a save -- both members, every dated backup, and the same run on a second
    platform -- folds into one row, which is the whole point: the total under the headline
    is the time actually played, not the time on disk.
    """

    def __init__(self, master, report) -> None:
        super().__init__(master)
        self.title("NMS Save Vault - Play time")
        self.transient(master)

        total = ops.format_duration(report.total_play_time)
        ttk.Label(
            self, text=f"{len(report.playthroughs)} playthroughs, {total} of play time in total.",
            font=theme.HEADING_FONT, padding=pxs(12, 10, 12, 2),
        ).pack(side=tk.TOP, anchor="w")
        ttk.Label(
            self,
            text=(
                f"Counted once each across {report.saves} save files in {report.folders} "
                "folders (live saves + vault backups)."
            ),
            padding=pxs(12, 0, 12, 8),
        ).pack(side=tk.TOP, anchor="w")

        ttk.Button(self, text="Close", command=self.destroy).pack(side=tk.BOTTOM, pady=px(8))
        if report.unidentified:
            ttk.Label(
                self,
                text=(
                    f"{report.unidentified} save(s) predate the id the game now writes and are "
                    "counted by name, so two runs sharing a name would merge."
                ),
                padding=pxs(12, 0, 12, 6),
            ).pack(side=tk.BOTTOM, anchor="w")

        frame = ttk.Frame(self, padding=pxs(12, 0, 12, 0))
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(frame, orient="vertical")
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree = RedactingTreeview(
            frame, columns=("play", "platform", "copies", "name"), show="headings",
            yscrollcommand=vsb.set,
        )
        for cid, text, width, anchor in (
            ("play", "Play time", 90, "e"),
            ("platform", "Platform", 110, "w"),
            ("copies", "Copies", 60, "e"),
            ("name", "Save", 400, "w"),
        ):
            tree.heading(cid, text=text)
            tree.column(cid, width=px(width), anchor=anchor, stretch=(cid == "name"))
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.config(command=tree.yview)
        for row in report.playthroughs:
            name = row.name + ("   (counted by name)" if not row.identified else "")
            tree.insert(
                "", "end",
                values=(ops.format_duration(row.play_time), ", ".join(row.platforms), row.copies, name),
            )
        dpi.fit_to_content(self, 720, 460)


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
            padding=pxs(14, 12, 14, 8),
            justify="left",
            text=(
                "Show a name of your choosing instead of the real account id.\n"
                "Once a name is set, the app shows only that name -- in folder names,\n"
                "paths, labels and messages. This is the only place the real ids appear.\n"
                "Clear a name to go back to showing the real id."
            ),
        ).pack(anchor="w")

        grid = ttk.Frame(self, padding=pxs(14, 0, 14, 8))
        grid.pack(fill=tk.X)
        accounts = self._known_accounts()
        if not accounts:
            ttk.Label(grid, text="No accounts found yet -- use Rescan first.").grid(sticky="w")
        for column, heading in enumerate(("Platform", "Real account id", "Shows as")):
            ttk.Label(grid, text=heading, font=theme.BOLD_FONT).grid(
                row=0, column=column, sticky="w", padx=pxs(0, 10), pady=pxs(0, 4)
            )
        for row, (platform, account) in enumerate(accounts, start=1):
            ttk.Label(grid, text=platform).grid(row=row, column=0, sticky="w", padx=pxs(0, 10), pady=px(2))
            real = ttk.Entry(grid, width=52)  # fits a 49-char Xbox <xuid>_<titleid> whole
            real.insert(0, account)
            real.configure(state="readonly")  # selectable so it can be copied, not edited
            real.grid(row=row, column=1, sticky="w", padx=pxs(0, 10), pady=px(2))
            var = tk.StringVar(value=self.amap.alias_for(account))
            self._vars[account] = var
            ttk.Entry(grid, textvariable=var, width=26).grid(row=row, column=2, sticky="w", pady=px(2))

        buttons = ttk.Frame(self, padding=pxs(14, 0, 14, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", command=self._save).pack(side=tk.RIGHT, padx=px(6))

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


class ChooseSourceDialog(tk.Toplevel):
    """Pick which slot to copy from: any occupied slot, in a live folder or any backup."""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.result: SlotSource | None = None
        self.title("Choose the save to copy from")
        self.transient(app)

        ttk.Label(
            self, padding=pxs(14, 12, 14, 6), justify="left",
            text="Pick the slot you want to copy. Both of its saves (the Auto-Save and the\n"
                 "Restore-Point) are copied together.",
        ).pack(anchor="w")

        body = ttk.Frame(self, padding=pxs(14, 0, 14, 8))
        body.pack(fill=tk.BOTH, expand=True)
        self.tree = RedactingTreeview(body, columns=("name", "saved"), show="tree headings")
        self.tree.heading("#0", text="Folder / Slot")
        self.tree.column("#0", width=px(330), anchor="w")
        self.tree.heading("name", text="Save name")
        self.tree.column("name", width=px(230), anchor="w")
        self.tree.heading("saved", text="Saved")
        self.tree.column("saved", width=px(130), anchor="w")
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda _e: self._accept())

        self._sources: dict[str, SlotSource] = {}
        self._populate()

        buttons = ttk.Frame(self, padding=pxs(14, 0, 14, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Use this slot", command=self._accept).pack(side=tk.RIGHT, padx=px(6))

        dpi.fit_to_content(self, 760, 460)
        self.grab_set()
        self.wait_window()

    def _populate(self) -> None:
        for source in self.app.state.live_sources:
            if not Path(source.path).is_dir():
                continue
            view = savedir.scan_any(source.path)
            if not view.occupied_slots:
                continue
            caption = _source_caption(source)
            node = self.tree.insert("", "end", text=f"[live] {caption}", open=False)
            for sv in view.occupied_slots:
                newest = sv.newest
                row = self.tree.insert(
                    node, "end", text=f"Slot {sv.slot}",
                    values=(sv.display_name, _fmt_ts(newest.effective_timestamp if newest else 0)),
                )
                self._sources[row] = SlotSource(str(view.path), sv.slot, caption)

        for entry in self.app.vault.entries:
            if not entry.occupied_slots:
                continue
            node = self.tree.insert("", "end", text=f"[{entry.kind}] {entry.id}", open=False)
            for s in entry.occupied_slots:
                ts = max((m.timestamp for m in s.members if m.present), default=0)
                row = self.tree.insert(node, "end", text=f"Slot {s.slot}",
                                       values=(s.name, _fmt_ts(ts)))
                self._sources[row] = SlotSource(entry.path, s.slot, entry.id)

    def _accept(self) -> None:
        selection = self.tree.selection()
        source = self._sources.get(selection[0]) if selection else None
        if source is None:
            _showinfo("Choose a slot", "Expand a folder and pick one of its slots.")
            return
        self.result = source
        self.destroy()


class CopySlotDialog(tk.Toplevel):
    """Copy one slot's saves into a live slot, showing both ends before anything is written.

    The old flow asked only "Destination live slot (1-15):" -- a bare number, with no way
    to see what you were about to overwrite, and with the source implied by whatever
    happened to be selected. Here both ends are named, either can be changed, and every
    destination slot shows what is currently in it.
    """

    def __init__(self, app: App, source=None, dest_dir=None, dest_slot=None):
        super().__init__(app)
        self.app = app
        self.source = source
        self.result = None
        self.title("Copy a save into a live slot")
        self.transient(app)

        self._targets = [s for s in app.state.live_sources if s.writable and s.exists]
        if not self._targets:
            self.destroy()
            _showerror("No writable live folder", "There is no live save folder to copy into.")
            return

        frame = ttk.LabelFrame(self, text=" Copy from ", padding=pxs(12, 8))
        frame.pack(fill=tk.X, padx=px(14), pady=pxs(12, 6))
        self.source_var = tk.StringVar()
        # The button first: a long caption is then what gets cut, never the button.
        ttk.Button(frame, text="Change...", command=self._choose_source).pack(side=tk.RIGHT)
        ttk.Label(frame, textvariable=self.source_var, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        # The buttons and the rule go in from the bottom BEFORE the slot list: when the
        # window is short (a large font zoom), pack then shrinks the list, never them (P10).
        buttons = ttk.Frame(self, padding=pxs(14, 0, 14, 12))
        buttons.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Copy", command=self._accept).pack(side=tk.RIGHT, padx=px(6))
        rule = ttk.Label(self, padding=pxs(14, 0, 14, 6), justify="left", text=COPY_RULE.replace("\n", " "))
        rule.pack(side=tk.BOTTOM, fill=tk.X)
        # Wrapped to the window, however narrow: the caption grows taller, not wider.
        rule.bind("<Configure>", lambda e: rule.configure(wraplength=max(px(200), e.width - px(28))))
        rule.configure(wraplength=px(640))

        frame = ttk.LabelFrame(self, text=" Into this live slot ", padding=pxs(12, 8))
        frame.pack(fill=tk.BOTH, expand=True, padx=px(14), pady=px(6))
        picker = ttk.Frame(frame)
        picker.pack(fill=tk.X, pady=pxs(0, 6))
        ttk.Label(picker, text="Live folder:").pack(side=tk.LEFT)
        self._target_labels = {_source_caption(s): s for s in self._targets}
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(
            picker, textvariable=self.target_var, state="readonly",
            values=list(self._target_labels), width=40,
        )
        self.target_combo.pack(side=tk.LEFT, padx=px(6))
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_slots())
        if len(self._targets) == 1:
            self.target_combo.configure(state="disabled")

        holder = ttk.Frame(frame)
        holder.pack(fill=tk.BOTH, expand=True)
        self.slots = RedactingTreeview(
            holder, columns=("name", "saved"), show="tree headings", height=12
        )
        self.slots.heading("#0", text="Slot")
        self.slots.column("#0", width=px(70), anchor="w")
        self.slots.heading("name", text="What is in it now")
        self.slots.column("name", width=px(330), anchor="w")
        self.slots.heading("saved", text="Saved")
        self.slots.column("saved", width=px(130), anchor="w")
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.slots.yview)
        self.slots.configure(yscrollcommand=vsb.set)
        self.slots.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.slots.bind("<Double-1>", lambda _e: self._accept())

        chosen = next(
            (s for s in self._targets if dest_dir and Path(s.path) == Path(dest_dir)),
            self._targets[0],
        )
        self.target_var.set(_source_caption(chosen))
        self._reload_slots(select=dest_slot or (source.slot if source else None))
        self._show_source()

        # Big enough for what it holds at the current font size, at least the old 700x540,
        # and never wider or taller than the screen.
        dpi.fit_to_content(self, 700, 540)
        self.grab_set()
        self.wait_window()

    def _show_source(self) -> None:
        self.source_var.set(
            aliases.redact(self.source.caption) if self.source
            else "(nothing chosen yet - press Change...)"
        )

    def _choose_source(self) -> None:
        picked = ChooseSourceDialog(self.app).result
        if picked is not None:
            self.source = picked
            self._show_source()

    def _target_source(self):
        return self._target_labels.get(self.target_var.get(), self._targets[0])

    def _reload_slots(self, select: int | None = None) -> None:
        keep = select or self._selected_slot()
        self.slots.delete(*self.slots.get_children())
        view = savedir.scan_any(self._target_source().path)
        for number in sorted(view.slots):
            sv = view.slots[number]
            newest = sv.newest
            row = self.slots.insert(
                "", "end", iid=str(number), text=f"{number}",
                values=(
                    sv.display_name if sv.occupied else "empty",
                    _fmt_ts(newest.effective_timestamp if newest else 0),
                ),
            )
            if number == keep:
                self.slots.selection_set(row)
                self.slots.see(row)

    def _selected_slot(self) -> int | None:
        selection = self.slots.selection()
        return int(selection[0]) if selection else None

    def _accept(self) -> None:
        if self.source is None:
            _showinfo("Choose a source", "Press Change... and pick the slot you want to copy.")
            return
        slot = self._selected_slot()
        if slot is None:
            _showinfo("Choose a slot", "Pick the live slot to copy into.")
            return
        self.result = (self.source, Path(self._target_source().path), slot)
        self.destroy()


HELP_TEXT = """NMS Save Vault - Help

The window is split into two panes. Drag the divider to give either one more room.
  * LIVE SAVES (top)  -- every save folder found on this PC: each Steam account and each
     Xbox / Game Pass account. The ACTIVE one (green, bold) is the target of write
     actions; pick it from the "Active live" dropdown or right-click a folder to set it.
     Xbox folders are writable for same-platform actions (backup, restore, repopulate,
     promote within Xbox). Transferring a save between Steam and Xbox is not yet supported.
  # BACKUPS (bottom)  -- every backup in the catalog (full snapshots, extracts, imported
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
- Restore: Select a backup (a top-level row) and put it back. What that means depends on
  what you picked, and the menu says which before you commit:
    * a full backup / snapshot / imported folder is put back slot by slot -- live slots
      it lacks are left alone, nothing is removed;
    * a single-slot extract goes back into ITS OWN slot and nothing else is touched.
  Either way the current state is auto-snapshotted first, so it is reversible with Undo.
  A full restore keeps the live folder's own account data (unlocks, settings) unless you
  tick "Also put back the account data". A backup of another Xbox account is refused.
- Extract slot: Select a slot -- or either of the two saves inside it -- under LIVE or a
  backup, to copy just that one slot aside into the vault. You can free the slot now and
  bring it back later.
- Empty slot (steps): The app never empties a slot; it only replaces. This shows the
  steps to empty one yourself (they differ for Steam Cloud, Steam without Cloud and
  Xbox), and first offers to keep a copy of what the slot holds in the vault.
- Copy into live slot: Copies one slot's saves into a live slot. It opens a window
  showing BOTH ends: what is being copied, and every live slot with what is currently in
  it, so you can see what you are about to replace. Either end can be changed there, so
  it does not matter which way round you were thinking:
    * select a slot in a backup and it is the source -- pick where it lands;
    * select a live slot and it is the destination -- press Change... to pick what fills it.
  The save data is copied exactly and the small meta is re-keyed for the new slot number.
  A slot that holds only one save is refused when the live slot's other save is a
  different playthrough - the game could load that one instead; clear the slot first.
- Play time: How long you have actually played. Every copy of a save is folded onto one
  playthrough - its two members, every dated backup of it in the vault, and the same run
  on a second platform (an NMS cloud save keeps its identity across linked accounts) -
  and the highest play time of the copies is the one counted, because play time only ever
  goes up. Saves older than the id the game now writes are matched on their name instead,
  and the window says how many of those there were.
- Save file (above the backups): Narrows the backups to every copy of one save file,
  even where it was renamed or sat in another slot, so you can see when you backed it up.
  Each save is listed under its current name and the names it had before. Right-click a
  slot or save in either pane and choose "Show this save's history" to do the same.
- Promote: Select one of a live slot's two saves (Auto-Save or Restore-Point) to force it
  to be the newest, so the game loads it instead of the other -- e.g. to roll back to the
  Restore-Point.
- Import: Register an existing save folder you made yourself (or an Xbox / Game Pass
  save) into the catalog -- either in place or copied into the vault. You can also point
  Import at a whole copied Save Vault folder: it compares that vault's entries with yours
  and offers to copy the new ones in or index them in place (re-importing is harmless).
- Rescan: Re-detect live save folders (new Steam account, new Xbox account) and add them
  to the LIVE SAVES list. Your manual entries are left untouched.
- Discover: Scan the NMS folder for existing backups and add any new ones found. A live
  save folder is never added; a catalog entry that is (or lies inside) one is pointed
  out, and left as it is.
- Undo: Puts a live folder back as it was just before its last change, from the
  auto-snapshot taken then. It acts on the active live folder - or, from a folder's
  right-click menu, on that folder - and names the change before it asks. Each Undo steps
  one change further back. The folder keeps its own account data unless you tick the
  box. If the undo history has damaged lines, it says so and asks before going on.
  Only actions that WRITE into the live folder can be undone -- Backup, Extract and
  Import merely add to the vault, so after one of those there is nothing to put back.
- Refresh: Re-scan the live folder and the catalog.
- View > Theme: Light, Dark, or System, which follows your Windows light/dark
  setting. Your choice is remembered in the config.
- Accounts: Give each account a display name to show instead of its real id (the Steam
  st_<steamid64> folder, the Xbox <xuid>_<titleid> folder). Once a name is set, the app
  shows only that name everywhere -- folder names, paths, labels, messages -- and that
  dialog is the only place the real ids still appear. The names are stored in
  accounts.ini next to state.json; clearing a name shows the real id again.
- Help: This dialog.
- NO BACKUP YET (Organizer tab): this version exists only in the live save folder; no
  backup in the vault holds it, so overwriting the slot would lose it. Back up, or let a
  move keep it. The "No backup yet" chip lists such save files.

MENUS
Every button is also in the menu bar (File, Edit, View, Live, Tools, Help), so Alt and
the underlined letter reach it whatever the window size.

KEYBOARD
- Ctrl+plus / Ctrl+minus make every font in the window bigger or smaller, from 70% to
  200% of your system's own size; Ctrl+0 goes back to 100%. The tree rows grow with the
  text, and the size is remembered in the config just like the theme is.
- F1 opens this help, F5 refreshes.
- Ctrl+1 shows the Organizer tab, Ctrl+2 the Live saves and backups tab. The app opens
  on the tab you used last.

MARKERS AND WORDS
- Not linked, by date: no recorded move or backup links those versions, so they are
  listed by date. Running the organizer bootstrap links versions that backups saw.
- Only outside the vault: kept only in a folder you made yourself that the app
  catalogues in place - not copied into the vault.
- Pin: a pinned save is never suggested as the one to replace when a move needs a slot.
- * and moved (Live saves and backups tab): * = the save the game loads; moved = the file
  was moved here from another slot.

RIGHT-CLICK
Every row offers exactly what makes sense for it:
- a live save folder: back it up, make it the active write target, copy a save into one
  of its slots, or undo the last change;
- a live slot: replace it with a save from anywhere, copy it to another live slot,
  extract it to the vault, or clear it;
- one of the two saves in a live slot: the same, plus Promote to make that one the save
  the game loads;
- a backup: restore all of it (which replaces the live folder), and for a single-slot
  extract, also copy that slot into any live slot you choose;
- a slot inside a backup, or either save in it: copy it straight into the same-numbered
  live slot, copy it into a live slot you choose, or extract it to the vault.
Any slot, or either save in it, in either pane also offers "Show this save's history",
which narrows the backups to every copy of that save.

WORKFLOWS
- More than 15 slots: Extract the slots you are not using into the vault, empty them
  yourself (see Empty slot), then copy them back into a live slot whenever you want to play them
  again. Your library is unlimited; only 15 are live at a time.
- Safe experimenting: Backup live (or Extract the slot), make changes in-game, then
  Restore / copy back / Undo if you do not like the result.
- Move a save to another slot: right-click it and Copy into a live slot.
- Roll back within a slot: expand the live slot, right-click the save you want (usually
  the older one, or the Restore-Point), Promote it.
- Bring in an outside save: Import the folder, then copy the slot you want into live.
- See when you backed a save up: pick it under "Save file" above the backups. Renamed
  copies, and copies in other slots, are included.

UPDATES
The first time you run it, the app asks whether it may check for new versions. If you
say yes it asks GitHub once a day, on startup, whether a newer release exists, and shows
a bar at the top when there is one. Nothing about you or your saves is ever sent, and
nothing is downloaded until you ask for it. This is the only thing the app uses the
network for. Right-click anywhere and choose "Check for updates..." to check straight
away, or to turn the daily check back on.

"Install update" on that bar does the whole thing: it downloads the release from GitHub,
checks that the program inside it is a validly signed Python Software Foundation binary,
and only then closes the app, swaps the files and reopens it on the new version. Your
config, your vault and your saves are untouched, and if the swap fails the previous
version is put back. If it cannot install (you are running from source rather than the
packaged app), the button is not offered and "Open download page" is there instead.

SAFETY
- Writes are blocked while No Man's Sky is running -- close the game first.
- Every change auto-snapshots the live state first and can be reversed with Undo.
- Steam Cloud: operate with the game closed; if Steam shows a conflict on next launch,
  keep the local copy.
- Steam Cloud and deleting: Steam puts back a save that is only missing locally, so a
  cleared slot reappears at the next launch. Turn off "Keep game saves in the Steam
  Cloud" (game Properties > General), clear the slot, launch and quit the game, then
  turn Cloud back on. Xbox / Game Pass deletes propagate on their own.
"""


# --- what only the OS can tell the window (D6 3.10, 3.15) ---------------------------


def _enable_dpi_awareness() -> bool:
    """Make the process system-DPI-aware; must run before the first ``tk.Tk()``.

    Windows only, best effort. System awareness (1), not per-monitor (2): per-monitor needs
    ``WM_DPICHANGED`` handling that Tk 8.6 lacks, and system awareness degrades to Windows'
    bitmap scaling on a second monitor with another DPI. True when the call succeeded.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        return ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0
    except (AttributeError, OSError):
        pass
    try:  # no shcore (older Windows): the Vista-era switch, system-aware too
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


def _work_area(root) -> tuple[int, int]:
    """The usable desktop size in pixels: the screen without the taskbar on Windows."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        try:
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                width, height = rect.right - rect.left, rect.bottom - rect.top
                if width > 0 and height > 0:
                    return width, height
        except (AttributeError, OSError):
            pass
    return int(root.winfo_screenwidth()), int(root.winfo_screenheight())


def _window_chrome(root) -> tuple[int, int]:
    """What the window adds around the size given to ``geometry()``: the borders, the
    title bar and the menu bar. Windows asks the system; elsewhere a fair estimate."""
    if sys.platform == "win32":
        import ctypes

        metric = ctypes.windll.user32.GetSystemMetrics
        try:
            frame_x = metric(32) + metric(92)        # SM_CXSIZEFRAME + SM_CXPADDEDBORDER
            frame_y = metric(33) + metric(92)        # SM_CYSIZEFRAME + SM_CXPADDEDBORDER
            caption, menu = metric(4), metric(15)    # SM_CYCAPTION, SM_CYMENU
        except (AttributeError, OSError):
            pass
        else:
            if caption > 0:
                return 2 * frame_x, caption + menu + 2 * frame_y
    return dpi.screen_px(16), dpi.screen_px(80)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="nmsvault-gui", allow_abbrev=False)
    parser.add_argument("--vault", help="open this vault instead of the configured one")
    options, _unknown = parser.parse_known_args(argv)
    # First: under pythonw there is no console, so warnings and tracebacks go to a file.
    logfile.configure(host_platform.user_cache_dir() / "logs")
    # Before the first tk.Tk(): Tk reads the DPI once, when the root is made. Without this
    # Windows stretches a 96-dpi bitmap of the window, which is blurry at 150 %.
    _enable_dpi_awareness()
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
    App(**({"vault": options.vault} if options.vault else {})).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
