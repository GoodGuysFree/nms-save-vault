"""The Organizer tab assembled (D6 2.2, U12): a slim toolbar, the alert bars, the save-file
table on the left and, on the right, the live area above the detail pane.

Selection is the hub: the table's selection fills the detail pane and outlines the save
file's live slots; a click on a live slot selects its save file (an empty slot shows its
card); a ``#n`` link in the detail pane selects that version; the notes window follows.

Every scan and every notes write runs in the UI worker; the window never waits. The first
scan starts with the tab, records what the live slots hold (the journal) and later ones
start from Rescan or on coming back to the tab; until a scan returns, the previous model
stays on screen. A background failure becomes an alert bar, never a modal.
"""
from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable, MutableMapping
from tkinter import ttk

from ...core import aliases
from ...core.liveops import OpContext
from ...core.organizer import Organizer
from ...core.organizer.readmodel import BoardSlot, LiveSource, ReadModel
from ..dpi import px, pxs
from ..redact import set_label
from ..themeaware import ThemeRegistry
from ..worker import UiWorker
from .detail import DetailPane
from .live import LiveArea
from .move import MoveDialog, source_for
from .notes import NotesWindow, toggle_pin
from .table import OrganizerTable

#: Coming back to the tab rescans when the last look is older than this (seconds).
STALE_AFTER = 5.0


def summary_text(model: ReadModel, organizer: Organizer | None) -> str:
    """The one line under the toolbar: counts, the live folders, the game."""
    lines = model.lines()
    versions = sum(len(model.versions(line.pid)) for line in lines)
    bits = [f"{len(lines)} save files, {versions} versions"]
    for source in model.live_board().sources:
        used = sum(1 for s in source.slots if s.state in ("occupied", "partial"))
        unreadable = sum(1 for s in source.slots if s.state == "unreadable")
        risky = sum(1 for s in source.slots if s.state in ("occupied", "partial")
                    and s.vault_copy != "durable")
        part = f"{source.label} {used}/{len(source.slots)} live"
        if risky:
            part += f", {risky} not in vault"
        if unreadable:
            part += f", {unreadable} unreadable"
        bits.append(part)
    running = organizer.scan.game_running if organizer and organizer.scan else False
    if running is True:
        bits.append("Game: RUNNING - live saves are not recorded")
    elif running is None:
        bits.append("Game: unknown")
    return " - ".join(bits)


def scan_alerts(organizer: Organizer) -> dict[str, str]:
    """What the user should know after a scan, by key: live folders that could not be
    read, and the organizer store's own state (read-only, damaged files)."""
    out = {}
    scan = organizer.scan
    if scan is not None:
        for ref, folder in scan.live.items():
            if not folder.readable:
                why = next((p.detail for p in scan.problems if p.where == ref), "")
                name = "Xbox" if ref.platform == "xbox" else "Steam"
                out[f"live:{ref.platform}:{ref.account_key}:{ref.instance}"] = (
                    f"A {name} live folder could not be read; its slots are shown as they "
                    f"were. {why}".strip())
    for n, problem in enumerate(organizer.problems):
        out[f"store:{n}"] = f"Organizer: {problem}"
    return {key: aliases.scrub(text) for key, text in out.items()}


def default_context(organizer: Organizer) -> OpContext:
    """The live-operations context of the app's own organizer (worker thread)."""
    from ... import __version__
    from ...core.liveops import context_for
    from ...core.liveops.opjournal import machine_id

    host = organizer.host
    return context_for(organizer, host=host, machine_id=machine_id(host),
                       app_version=f"nms-save-vault {__version__}")


class OrganizerView(ttk.Frame):
    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry,
                 prefs: MutableMapping, save_prefs: Callable[[], None],
                 make_organizer: Callable[[], Organizer],
                 make_context: Callable[[Organizer], OpContext] | None = None) -> None:
        super().__init__(master)
        self.worker = worker
        self.themed = themed
        self.prefs = prefs
        self.save_prefs = save_prefs
        self.make_organizer = make_organizer
        self.make_context = make_context or default_context
        self.move_dialog: MoveDialog | None = None
        self.organizer: Organizer | None = None
        self.model: ReadModel | None = None
        self.notes_window: NotesWindow | None = None
        self.scanning = False
        self.scanned_at = 0.0
        self._pending_scan: bool | None = None
        self.alerts: dict[str, ttk.Frame] = {}
        self._build()

    # --- building ---------------------------------------------------------------------------

    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side=tk.TOP, fill=tk.X, padx=px(6), pady=pxs(6, 2))
        self.notes_button = ttk.Button(bar, text="Notes...", command=self.open_notes)
        self.notes_button.pack(side=tk.LEFT)
        self.move_button = ttk.Button(bar, text="Move to live...", command=self.open_move)
        self.move_button.pack(side=tk.LEFT, padx=pxs(6, 0))
        self.rescan_button = ttk.Button(bar, text="Rescan", command=lambda: self.rescan())
        self.rescan_button.pack(side=tk.RIGHT)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=px(120))
        self.summary = ttk.Label(bar, text="", style="Muted.TLabel", anchor="w")
        self.summary.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=px(10))
        chips = ttk.Frame(self)                  # the table's chips, Find and Columns...
        chips.pack(side=tk.TOP, fill=tk.X, padx=px(6))
        self.alert_area = ttk.Frame(self)
        self.alert_area.pack(side=tk.TOP, fill=tk.X, padx=px(6))

        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=px(6), pady=pxs(2, 6))
        self.panes = panes
        self.table = OrganizerTable(panes, themed=self.themed, prefs=self.prefs,
                                    save_prefs=self.save_prefs, on_select=self._selected,
                                    bar_parent=chips, on_move=self.open_move_for)
        right = ttk.Frame(panes)
        panes.add(self.table, weight=3)
        panes.add(right, weight=2)
        # The live lists keep their full height (15 slots and the legend); the detail pane
        # takes what is left.
        self.live = LiveArea(right, themed=self.themed, prefs=self.prefs,
                             save_prefs=self.save_prefs, on_slot=self._slot_clicked,
                             on_activate=self._slot_activated)
        self.live.pack(side=tk.TOP, fill=tk.X)
        self.detail = DetailPane(right, themed=self.themed, on_link=self._link,
                                 on_notes=self.open_notes_for, on_pin=self._pin,
                                 on_move=self.open_move_for)
        self.detail.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=pxs(8, 0))
        self._drag = None
        self._dragging = False
        self._bind_drag()

    # --- scanning ---------------------------------------------------------------------------

    def rescan(self, *, record: bool = True) -> None:
        """Scan in the worker (``record``: journal the live slots, as Rescan and the
        start-up scan do); the old model stays until the new one is there."""
        if self.scanning:
            self._pending_scan = bool(self._pending_scan) or record
            return
        self.scanning = True
        self.rescan_button.state(["disabled"])
        self.progress.pack(side=tk.RIGHT, padx=pxs(0, 8))
        self.progress.start(15)
        set_label(self.summary, "Scanning live folders and the vault..."
                  if self.model is None else self.summary.cget("text"))
        self.worker.submit(self._scan_job, record, on_done=self._scanned,
                           on_error=self._scan_failed)

    def _scan_job(self, record: bool):
        """In the worker: the organizer is opened there too (it reads the catalog)."""
        if self.organizer is None:
            self.organizer = self.make_organizer()
        organizer = self.organizer
        model = organizer.refresh(record=record)
        return organizer, model

    def _scanned(self, result) -> None:
        organizer, model = result
        self._scan_over()
        self.clear_alert("scan-failed")
        self.set_model(model)
        alerts = scan_alerts(organizer)
        for key in [k for k in self.alerts if k.startswith(("live:", "store:"))]:
            if key not in alerts:
                self.clear_alert(key)
        for key, text in alerts.items():
            self.alert(key, text)
        set_label(self.summary, summary_text(model, organizer))

    def _scan_failed(self, error: BaseException) -> None:
        self._scan_over()
        self.alert("scan-failed", f"The organizer could not scan: {error}")

    def _scan_over(self) -> None:
        self.scanning = False
        self.scanned_at = time.monotonic()
        self.progress.stop()
        self.progress.pack_forget()
        self.rescan_button.state(["!disabled"])
        if self._pending_scan is not None:
            record, self._pending_scan = self._pending_scan, None
            self.after_idle(lambda: self.rescan(record=record))

    def reload(self) -> None:
        """A new model without journaling (after a note or a pin was saved)."""
        self.rescan(record=False)

    def shown(self) -> None:
        """The tab came to the front: look again unless the last look is fresh."""
        if not self.scanning and time.monotonic() - self.scanned_at > STALE_AFTER:
            self.rescan()

    def set_model(self, model: ReadModel) -> None:
        self.model = model
        self.table.set_model(model)
        self.live.set_model(model)
        pid, head = self.table.selection_ref()
        if pid is not None and model.line(pid) is not None:
            self._show(pid, head)
        if self.notes_window is not None and self.notes_window.winfo_exists():
            self.notes_window.set_model(model)

    # --- alerts --------------------------------------------------------------------------------

    def alert(self, key: str, text: str) -> None:
        """One bar per key, above the table, until dismissed (D6 3.8)."""
        bar = self.alerts.get(key)
        if bar is None:
            bar = ttk.Frame(self.alert_area, style="Alert.TFrame", padding=pxs(8, 4))
            bar.text = ttk.Label(bar, text="", style="Alert.TLabel", anchor="w")
            bar.text.pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Button(bar, text="Dismiss", command=lambda: self.clear_alert(key)).pack(
                side=tk.RIGHT)
            bar.pack(side=tk.TOP, fill=tk.X, pady=pxs(2, 0))
            self.alerts[key] = bar
        set_label(bar.text, text)

    def clear_alert(self, key: str) -> None:
        bar = self.alerts.pop(key, None)
        if bar is not None:
            bar.destroy()

    def confirm_close(self) -> bool:
        """False when the owner chose to keep editing an unsaved note (the main window's
        close asks through here)."""
        window = self.notes_window
        if window is not None and window.winfo_exists():
            return window.confirm_close()
        return True

    def _worker_error(self, what: str):
        return lambda error: self.alert(f"error:{what}", aliases.scrub(f"{what} failed: {error}"))

    # --- the selection hub -------------------------------------------------------------------

    def _selected(self, pid: str | None, head: str | None) -> None:
        if pid is None or self.model is None:
            return
        self._show(pid, head)
        if self.notes_window is not None and self.notes_window.winfo_exists():
            self.notes_window.show(pid, head)

    def _show(self, pid: str, head: str | None) -> None:
        if head is None:
            self.detail.show_line(self.model, pid)
        else:
            self.detail.show_version(self.model, pid, head)
        self.live.highlight(pid)

    def _slot_clicked(self, token: str, slot: BoardSlot) -> None:
        if self.model is None:
            return
        if slot.pid is not None and self.model.line(slot.pid) is not None:
            self.table.select(slot.pid)
            self._show(slot.pid, None)
            return
        source = self._source_of(slot)
        if source is not None:
            self.table.tree.selection_set(())
            self.live.highlight(None)
            self.detail.show_slot(self.model, source, slot)

    def _source_of(self, slot: BoardSlot) -> LiveSource | None:
        return next((s for s in self.model.live_board().sources
                     if slot in s.slots), None) if self.model is not None else None

    def _link(self, pid: str, head: str) -> None:
        self.table.select(pid, head)
        self._selected(pid, head)

    # --- moving to live -------------------------------------------------------------------------

    def open_move(self) -> None:
        pid, head = self.table.selection_ref()
        if pid is not None:
            self.open_move_for(pid, head)

    def open_move_for(self, pid: str, head: str | None = None, *, slot: int | None = None,
                      account=None) -> MoveDialog | None:
        """The Move to live dialog for a line (its newest version) or one version; the plan
        and the write run in the worker, and a finished move rescans and says so."""
        organizer = self.organizer
        if self.model is None or organizer is None or self.scanning:
            return None
        if self.move_dialog is not None and self.move_dialog.winfo_exists():
            self.move_dialog.lift()
            return self.move_dialog
        try:
            source, name, card = source_for(self.model, pid, head)
        except KeyError:
            return None
        labels = {s.ref: s.label for s in self.model.live_board().sources}
        self.move_dialog = MoveDialog(
            self.winfo_toplevel(), worker=self.worker, themed=self.themed,
            make_context=lambda: self.make_context(organizer), source=source, name=name,
            card=card, labels=labels, prefer_slot=slot, account=account, on_done=self._moved)
        return self.move_dialog

    def open_replace_for(self, slot: BoardSlot) -> MoveDialog | None:
        """Replace with...: the live slot is fixed, the version is picked in the dialog."""
        if self.model is None or self.organizer is None or self.scanning:
            return None
        if self.move_dialog is not None and self.move_dialog.winfo_exists():
            self.move_dialog.lift()
            return self.move_dialog
        organizer = self.organizer
        labels = {s.ref: s.label for s in self.model.live_board().sources}
        self.move_dialog = MoveDialog(
            self.winfo_toplevel(), worker=self.worker, themed=self.themed,
            make_context=lambda: self.make_context(organizer), source=None, name="",
            labels=labels, model=self.model, fixed=slot, on_done=self._moved)
        return self.move_dialog

    def _slot_activated(self, token: str, slot: BoardSlot) -> None:
        self.open_replace_for(slot)

    # --- dragging a version onto a live slot ------------------------------------------------

    def _bind_drag(self) -> None:
        tree = self.table.tree
        tree.bind("<ButtonPress-1>", self._drag_start, add="+")
        tree.bind("<B1-Motion>", self._drag_motion, add="+")
        tree.bind("<ButtonRelease-1>", self._drag_drop, add="+")

    def _drag_start(self, event) -> None:
        iid = self.table.tree.identify_row(event.y)
        self._drag = (event.x, event.y, iid) if iid else None
        self._dragging = False

    def _drag_motion(self, event) -> None:
        d = self._drag
        if d is not None and not self._dragging and abs(event.x - d[0]) + abs(event.y - d[1]) > 8:
            self._dragging = True
            self.table.tree.configure(cursor="hand2")

    def _drag_drop(self, event) -> None:
        d, self._drag = self._drag, None
        if not self._dragging or d is None:
            return
        self._dragging = False
        self.table.tree.configure(cursor="")
        self.drop_at(d[2], event.x_root, event.y_root)

    def drop_at(self, iid: str, x_root: int, y_root: int) -> bool:
        """A table row was dropped at a screen point: on a live slot it opens Move to live
        with that slot pre-picked."""
        canvas = self.live.canvas
        if self.winfo_containing(x_root, y_root) is not canvas:
            return False
        token = self.live.token_at(x_root - canvas.winfo_rootx(), y_root - canvas.winfo_rooty())
        pid, head = self.table.ref_of(iid)
        drawn = self.live.drawn.get(token) if token else None
        if drawn is None or pid is None:
            return False
        self.open_move_for(pid, head, slot=drawn.slot.target.slot, account=drawn.slot.target.ref)
        return True

    def _moved(self, outcome) -> None:
        undo = f"  Undo: {outcome.undo.label}" if outcome.undo else ""
        self.alert("moved", aliases.scrub(f"Done: {outcome.outcome}.{undo}"))
        self.rescan()

    # --- notes and pins -------------------------------------------------------------------------

    def open_notes(self) -> None:
        pid, head = self.table.selection_ref()
        if pid is not None:
            self.open_notes_for(pid, head)

    def open_notes_for(self, pid: str, head: str | None) -> None:
        if self.model is None or self.organizer is None:
            return
        if self.notes_window is not None and self.notes_window.winfo_exists():
            self.notes_window.show(pid, head)
            self.notes_window.deiconify()
            return
        self.notes_window = NotesWindow(self.winfo_toplevel(), notes=self.organizer.notes,
                                        model=self.model, worker=self.worker,
                                        themed=self.themed, pid=pid, head=head,
                                        on_saved=self.reload)

    def _pin(self, pid: str, pinned: bool) -> None:
        if self.organizer is not None:
            toggle_pin(self.worker, self.organizer.notes, pid, pinned, self.reload,
                       self._worker_error("Pin"))
