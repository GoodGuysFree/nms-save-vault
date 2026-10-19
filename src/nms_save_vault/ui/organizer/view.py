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

import logging
import time
import tkinter as tk
from collections.abc import Callable, MutableMapping
from pathlib import Path
from tkinter import ttk

from ...core import aliases
from ...core.liveops import (
    OpContext, OpOutcome, PlacePlan, dismiss_verification, empty_slot_guide, execute,
    pending_recovery, plan_keep, plan_promote, plan_swap, plan_undo, update_verification)
from ...core.organizer import Organizer
from ...core.organizer.readmodel import BoardSlot, LiveSource, ReadModel
from ..dialogs import grab_owner
from ..dpi import px, pxs
from ..menuhint import bind_menu_hints
from ..redact import set_label
from ..themeaware import ThemeRegistry
from ..tooltip import Tooltip
from ..worker import UiWorker
from .detail import DetailPane
from .histhooks import HistoryHooks
from .live import LiveArea
from .move import MoveDialog, source_for
from .notes import NotesWindow, toggle_pin
from . import factsui
from .ops import DoneBar, OpsLogWindow, PlanDialog, RecoveryDialog, StepsWindow
from .table import OrganizerTable
from .words import PROMOTE_TIP

#: Coming back to the tab rescans when the last look is older than this (seconds).
STALE_AFTER = 5.0

log = logging.getLogger(__name__)


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
            part += f", {risky} no backup yet"
        if unreadable:
            part += f", {unreadable} unreadable"
        bits.append(part)
    if organizer is not None and getattr(organizer, "store_error", None):
        bits.append("Vault store cannot be opened - backups are not shown")
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
    if getattr(organizer, "store_error", None):
        out["store:open"] = organizer.store_error
    for n, problem in enumerate(organizer.problems):
        out[f"store:{n}"] = f"Organizer: {problem}"
    return {key: aliases.scrub(text) for key, text in out.items()}


def last_change_text(outcome: OpOutcome) -> str:
    """The status bar's "Last change" line."""
    from datetime import datetime

    text = f"Last change {datetime.now():%H:%M}: {outcome.outcome.rstrip('.')}"
    if outcome.verify == "pending":
        text += " - checked after the next game launch"
    return aliases.scrub(text)


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
                 make_context: Callable[[Organizer], OpContext] | None = None,
                 on_last_change: Callable[[str], None] | None = None,
                 on_hint: Callable[[str], None] | None = None) -> None:
        super().__init__(master)
        self.worker = worker
        self.themed = themed
        self.prefs = prefs
        self.save_prefs = save_prefs
        self.make_organizer = make_organizer
        self.make_context = make_context or default_context
        self.on_last_change = on_last_change
        self.on_hint = on_hint
        self.move_dialog: MoveDialog | None = None
        self.plan_dialog: PlanDialog | None = None
        self.log_window: OpsLogWindow | None = None
        self.recovery_dialog: RecoveryDialog | None = None
        self._recovery_checked = False
        self._facts_refresh_at = 0.0
        self.banner: ttk.Frame | None = None
        self.organizer: Organizer | None = None
        self._reopen_organizer = False
        self.model: ReadModel | None = None
        self.notes_window: NotesWindow | None = None
        self.histories = HistoryHooks(self)
        self.scanning = False
        self.scanned_at = 0.0
        self._pending_scan: bool | None = None
        self.alerts: dict[str, ttk.Frame] = {}
        self._build()
        self.winfo_toplevel().bind("<Control-f>", self._ctrl_f, add="+")

    # --- building ---------------------------------------------------------------------------

    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side=tk.TOP, fill=tk.X, padx=px(6), pady=pxs(6, 2))
        self.notes_button = ttk.Button(bar, text="Notes...", command=self.open_notes)
        self.notes_button.pack(side=tk.LEFT)
        self.move_button = ttk.Button(bar, text="Move to live...", command=self.open_move)
        self.move_button.pack(side=tk.LEFT, padx=pxs(6, 0))
        self.history_button = ttk.Button(bar, text="History...", command=self.open_history)
        self.history_button.pack(side=tk.LEFT, padx=pxs(6, 0))
        self.action_why: dict[ttk.Button, str] = {}      # a disabled button's reason (its tooltip)
        for button in (self.notes_button, self.move_button, self.history_button):
            self.action_why[button] = ""
            Tooltip(button, lambda _w, _k, b=button: self.action_why.get(b, ""),
                    key_at=lambda _e: "button")
        self.log_button = ttk.Button(bar, text="Operations...", command=lambda: self.open_log())
        self.log_button.pack(side=tk.LEFT, padx=pxs(6, 0))
        self.rescan_button = ttk.Button(bar, text="Rescan", command=lambda: self.rescan())
        self.rescan_button.pack(side=tk.RIGHT)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=px(120))
        self.summary = ttk.Label(bar, text="", style="Muted.TLabel", anchor="w")
        self.summary.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=px(10))
        chips = ttk.Frame(self)                  # the table's chips, Find and Columns...
        chips.pack(side=tk.TOP, fill=tk.X, padx=px(6))
        self.alert_area = ttk.Frame(self)
        self.alert_area.pack(side=tk.TOP, fill=tk.X, padx=px(6))

        top_slot = ttk.Frame(self)                 # the index strip and the first-run banner
        top_slot.pack(side=tk.TOP, fill=tk.X)
        self.top_slot = top_slot
        self.index_strip = factsui.IndexStrip(top_slot, provider=lambda: self.facts,
                                              on_events=self._index_events)
        done_slot = ttk.Frame(self)
        done_slot.pack(side=tk.BOTTOM, fill=tk.X)
        self.done = DoneBar(done_slot, on_undo=self.undo_op, on_history=self.open_log)
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
                             on_activate=self._slot_activated, on_context=self._slot_context)
        self.live.pack(side=tk.TOP, fill=tk.X)
        self.detail = DetailPane(right, themed=self.themed, on_link=self._link,
                                 on_notes=self.open_notes_for, on_pin=self._pin,
                                 on_move=self.open_move_for, on_deep=self.read_deep,
                                 on_history=self.open_history_for)
        self.detail.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=pxs(8, 0))
        self._drag = None
        self._dragging = False
        self._bind_drag()
        self._update_actions()

    def _update_actions(self) -> None:
        """Notes, Move and History need a selected save file (Move also an idle scan): a button
        that cannot act is disabled, and its tooltip says why."""
        pid, _head = self.table.selection_ref()
        for button, needs_idle in ((self.notes_button, False), (self.move_button, True),
                                   (self.history_button, False)):
            if self.model is None or self.organizer is None:
                why = "The vault has not been read yet."
            elif pid is None:
                why = "Select a save file in the table first."
            elif needs_idle and self.scanning:
                why = "Wait for the scan to finish."
            else:
                why = ""
            self.action_why[button] = why
            button.state(["disabled" if why else "!disabled"])

    def focus_find(self) -> None:
        """Ctrl+F: the table's Find box."""
        self.table.find.focus_set()
        self.table.find.selection_range(0, tk.END)

    def _ctrl_f(self, _event=None) -> str | None:
        if not self.winfo_exists() or not self.winfo_ismapped():
            return None                          # another tab has the keyboard
        self.focus_find()
        return "break"

    # --- the content index (D4) -------------------------------------------------------------------

    @property
    def facts(self):
        """D4's IndexService when it runs, else None (memory reads only on the Tk thread)."""
        organizer = self.organizer
        service = organizer.facts if organizer is not None else None
        return service if service is not None and getattr(service, "active", False) else None

    def _attach_facts(self) -> None:
        service = self.facts
        self.table.facts = service
        if service is None:
            return
        self.index_strip.start()
        if self.banner is None and not self.prefs.get("index_banner_done"):
            def dismiss() -> None:
                self.prefs["index_banner_done"] = True
                self.save_prefs()
                if self.banner is not None:
                    self.banner.destroy()
                    self.banner = None
            self.banner = factsui.first_run_banner(self.top_slot, dismiss)
            self.banner.pack(side=tk.TOP, fill=tk.X, padx=px(6), pady=pxs(2, 0))

    def _index_events(self, events: list) -> None:
        """Rows update in place as facts arrive; the facts sort re-sorts at most every 3 s."""
        kinds = {e.kind for e in events}
        if kinds & {"indexed", "failed", "stale", "warmed", "changes", "finished", "cleared"}:
            self.table.refresh()           # values and the facts sort, in memory: no rescan
        history = self.histories.history
        if history is not None and history.winfo_exists():
            history.on_index_events(events)
        compare = self.histories.compare
        if compare is not None and compare.winfo_exists():
            compare.on_index_events(events)
        shown = self.detail.facts_head
        if shown is not None and any(e.vid == shown and e.kind in ("indexed", "deep-ready", "changes")
                                     for e in events):
            self.request_card(shown)

    def request_card(self, head: str) -> None:
        """The Facts page of ``head``: the card is read from the cache in the worker, never on
        the Tk thread; the version is moved to the front of the queue."""
        service = self.facts
        if service is None or self.model is None:
            return
        parent = self._parent_of(head)
        priority = service.Priority.INTERACTIVE

        def job():
            service.request([head], priority)
            card = service.card(head)
            changes = service.changes(head, parent[0], edge_kind=parent[1]) if parent else None
            return head, card, service.view(head), changes

        self.worker.submit(job, on_done=self._card_ready, on_error=self._worker_error("Facts"),
                           low=True)

    def _parent_of(self, head: str):
        for line in self.model.lines():
            for row in self.model.versions(line.pid):
                if row.head == head:
                    return (row.parent, row.parent_kind) if row.parent is not None else None
        return None

    def _card_ready(self, result) -> None:
        head, card, view, changes = result
        pid, shown = self.table.selection_ref()
        line = self.model.line(pid) if pid is not None and self.model is not None else None
        wanted = shown or (line.representative if line is not None else None)
        if head != wanted:
            return
        self.detail.show_facts(self._facts_segments(head, card, view, changes),
                               deep=bool(getattr(card, "deep", None)), head=head)

    def _facts_segments(self, head, card, view, changes) -> list:
        parent = self._parent_of(head)
        segs = factsui.segments(factsui.view_lines(view))
        if parent is not None:
            segs.append(("\nWhat changed since the version it comes from\n", ("h",)))
            segs += [(line + "\n", ()) for line in factsui.change_lines(changes)]
        deep = factsui.card_lines(card)
        if deep:
            segs.append(("\nDeeper detail\n", ("h",)))
            segs += factsui.segments(deep)
        return segs

    def read_deep(self, head: str) -> None:
        service = self.facts
        if service is not None:
            self.worker.submit(lambda: service.request_deep(head),
                               on_error=self._worker_error("Deeper detail"), low=True)

    # --- scanning ---------------------------------------------------------------------------

    def sources_changed(self) -> None:
        """The configured live folders changed: the next scan opens a new organizer over
        them (the old one keeps the list it was made with)."""
        self._reopen_organizer = True

    def vault_changed(self) -> None:
        """The vault changed mode (upgrade, fold, undo of the upgrade): the organizer and its
        context were made for the old mode, so the rescan opens new ones."""
        self.sources_changed()
        self._recovery_checked = False       # the new vault's interrupted moves are looked for
        self.rescan()

    def rescan(self, *, record: bool = True) -> None:
        """Scan in the worker (``record``: journal the live slots, as Rescan and the
        start-up scan do); the old model stays until the new one is there."""
        if self.scanning:
            self._pending_scan = bool(self._pending_scan) or record
            return
        self.scanning = True
        self._update_actions()
        self.rescan_button.state(["disabled"])
        self.progress.pack(side=tk.RIGHT, padx=pxs(0, 8))
        self.progress.start(15)
        set_label(self.summary, "Scanning live folders and the vault..."
                  if self.model is None else self.summary.cget("text"))
        self.worker.submit(self._scan_job, record, on_done=self._scanned,
                           on_error=self._scan_failed, low=True)

    def _scan_job(self, record: bool):
        """In the worker: the organizer is opened there too (it reads the catalog)."""
        if self.organizer is None or self._reopen_organizer:
            self._reopen_organizer = False
            old, self.organizer = self.organizer, self.make_organizer()
            if old is not None and old is not self.organizer and old.facts is not None:
                try:
                    old.facts.close()           # its pool and writer would otherwise run on
                except Exception:  # noqa: BLE001 - a stuck old index must not fail the scan
                    log.exception("the old content index did not close cleanly")
        organizer = self.organizer
        model = organizer.refresh(record=record)
        return organizer, model, self._extras(organizer, record)

    def _extras(self, organizer: Organizer, record: bool) -> dict:
        """In the worker after a scan: the verification of earlier moves (D5), the vault
        store's status (D3) and, once per session, the moves that were interrupted. A failure
        here never spoils the scan."""
        out: dict = {"verify": None, "store": None, "recovery": None}
        try:
            ctx = self.make_context(organizer)
            if record:
                out["verify"] = update_verification(ctx)
            if not self._recovery_checked:
                self._recovery_checked = True
                out["recovery"] = pending_recovery(ctx)
        except Exception:  # noqa: BLE001
            log.exception("the live operations' look-over failed")
        try:
            if organizer.facts is not None and getattr(organizer.facts, "active", False):
                organizer.facts.queue_refresh()      # only after a scan can bytes be opened
        except Exception:  # noqa: BLE001
            log.exception("the content index could not queue its refresh")
        try:
            if organizer.version_store is not None:
                from ...core.store import store_status
                out["store"] = store_status(organizer.vault_root, organizer.version_store,
                                            live_roots=[Path(s.path) for s in organizer.sources])
        except Exception:  # noqa: BLE001
            log.exception("the vault store's status could not be read")
        return out

    def _scanned(self, result) -> None:
        organizer, model, extras = result
        self._scan_over()
        self.clear_alert("scan-failed")
        self._attach_facts()                 # before the table fills: its facts cells read it
        self.set_model(model)
        alerts = scan_alerts(organizer)
        for key in [k for k in self.alerts if k.startswith(("live:", "store:"))]:
            if key not in alerts:
                self.clear_alert(key)
        for key, text in alerts.items():
            self.alert(key, text)
        set_label(self.summary, summary_text(model, organizer))
        self._extras_shown(extras)

    def _scan_failed(self, error: BaseException) -> None:
        self._scan_over()
        self.alert("scan-failed", f"The organizer could not scan: {error}")

    def _scan_over(self) -> None:
        self.scanning = False
        self.scanned_at = time.monotonic()
        self.progress.stop()
        self.progress.pack_forget()
        self.rescan_button.state(["!disabled"])
        self._update_actions()
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
        self._update_actions()
        pid, head = self.table.selection_ref()
        if pid is not None and model.line(pid) is not None:
            self._show(pid, head)
        if self.notes_window is not None and self.notes_window.winfo_exists():
            self.notes_window.set_model(model)
        self.histories.set_model(model)

    # --- alerts --------------------------------------------------------------------------------

    def alert(self, key: str, text: str, actions: tuple = (), dismiss=None) -> None:
        """One bar per key, above the table, until dismissed (D6 3.8). ``actions`` are
        (label, command) buttons; ``dismiss`` runs too when Dismiss is pressed."""
        bar = self.alerts.get(key)
        if bar is None:
            bar = ttk.Frame(self.alert_area, style="Alert.TFrame", padding=pxs(8, 4))
            bar.text = ttk.Label(bar, text="", style="Alert.TLabel", anchor="w")
            bar.text.pack(side=tk.LEFT, fill=tk.X, expand=True)

            def close() -> None:
                if dismiss is not None:
                    dismiss()
                self.clear_alert(key)
            ttk.Button(bar, text="Dismiss", command=close).pack(side=tk.RIGHT)
            for label, command in actions:
                ttk.Button(bar, text=label, command=command).pack(side=tk.RIGHT, padx=px(4))
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
        board = getattr(self, "live", None)
        aimed = board.drawn.get(board.focus_token) if board and board.focus_token else None
        if aimed is not None and aimed.slot.pid != pid:
            board.focus_token = None         # the selection moved off the slot menus aimed at
            board.redraw()
        self._update_actions()
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
        line = self.model.line(pid)
        target = head or (line.representative if line is not None else None)
        if target is not None:
            self.request_card(target)

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
        if self.model is None or organizer is None:
            return None
        if self.scanning:
            self.alert("busy", "The vault is being scanned - try again in a moment.")
            return None
        self.clear_alert("busy")
        if self.move_dialog is not None and self.move_dialog.winfo_exists():
            self.move_dialog.lift()
            return self.move_dialog
        try:
            source, name, card = source_for(self.model, pid, head)
        except KeyError:
            return None
        card = self._facts_card(pid, head, card)
        labels = {s.ref: s.label for s in self.model.live_board().sources}
        self.move_dialog = MoveDialog(
            self.winfo_toplevel(), worker=self.worker, themed=self.themed,
            make_context=lambda: self.make_context(organizer), source=source, name=name,
            card=card, labels=labels, prefer_slot=slot, account=account, on_done=self._moved,
            on_recover=self.open_recovery)
        return self.move_dialog

    def open_replace_for(self, slot: BoardSlot) -> MoveDialog | None:
        """Replace with...: the live slot is fixed, the version is picked in the dialog."""
        if self.model is None or self.organizer is None:
            return None
        if self.scanning:
            self.alert("busy", "The vault is being scanned - try again in a moment.")
            return None
        self.clear_alert("busy")
        if self.move_dialog is not None and self.move_dialog.winfo_exists():
            self.move_dialog.lift()
            return self.move_dialog
        organizer = self.organizer
        labels = {s.ref: s.label for s in self.model.live_board().sources}
        self.move_dialog = MoveDialog(
            self.winfo_toplevel(), worker=self.worker, themed=self.themed,
            make_context=lambda: self.make_context(organizer), source=None, name="",
            labels=labels, model=self.model, fixed=slot, on_done=self._moved,
            on_recover=self.open_recovery)
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

    def _facts_card(self, pid: str, head: str | None, card: list) -> list:
        """The facts of the version in "What you are putting in": what the index has read so
        far (memory only; "..." while not read), then what changed against its drawn parent."""
        service = self.facts
        if service is None:
            return card
        line = self.model.line(pid)
        target = head or (line.representative if line is not None else None)
        if target is None:
            return card
        parent = self._parent_of(target)
        out = factsui.segments(factsui.view_lines(service.view(target))) + [("\n", ())]
        out.append(("What changed in this version\n", ("h",)))
        if parent is None:
            out.append(("nothing shown - no recorded parent\n", ("k",)))
        else:
            found = service.changes(target, parent[0], edge_kind=parent[1])
            out += [(t + "\n", ()) for t in factsui.change_lines(found)]
        return out + card

    def _moved(self, outcome) -> None:
        self.done.show(outcome)
        if self.on_last_change is not None:
            self.on_last_change(last_change_text(outcome))
        self.rescan()

    def undo_last(self) -> bool:
        """Edit > Undo (Ctrl+Z): the same Undo as the Done bar's, for the last operation shown
        there. False (and a note) when there is nothing to undo."""
        outcome = self.done.outcome
        if outcome is None or outcome.undo is None or not outcome.op_id:
            self.alert("undo", "There is nothing to undo here.")
            return False
        self.undo_op(outcome.op_id)
        return True

    # --- undo, the log, plans, recovery, steps (U16-U18) ----------------------------------------

    def _job(self, fn, *args):
        """``fn(ctx, *args)`` in the worker, with a fresh context."""
        organizer = self.organizer
        return lambda: fn(self.make_context(organizer), *args)

    def undo_op(self, op_id: str) -> None:
        if self.organizer is None:
            return
        self.worker.submit(self._job(plan_undo, op_id),
                           on_done=lambda plan: self._undo_planned(plan),
                           on_error=self._worker_error("Undo"))

    def _undo_planned(self, plan: PlacePlan) -> None:
        if any(w.code == "UNDO_WOULD_EMPTY" for w in plan.blocked) and plan.targets:
            self.open_steps(plan.targets[0].target)         # the app never deletes (F8)
        elif plan.blocked or plan.warnings or plan.confirmations:
            self.show_plan("Undo", plan)
        else:
            self.worker.submit(self._job(execute, plan, frozenset()), on_done=self._undone,
                               on_error=self._worker_error("Undo"))

    def _undone(self, outcome: OpOutcome) -> None:
        if outcome.status == "done":
            self._moved(outcome)
        else:
            self.alert("undo", aliases.scrub(outcome.error or outcome.outcome))

    def show_plan(self, title: str, plan: PlacePlan) -> PlanDialog | None:
        if self.plan_dialog is not None and self.plan_dialog.winfo_exists():
            self.plan_dialog.destroy()
        organizer = self.organizer
        self.plan_dialog = PlanDialog(
            self.winfo_toplevel(), worker=self.worker, themed=self.themed,
            make_context=lambda: self.make_context(organizer), plan=plan, title=title,
            on_done=self._moved, on_recover=self.open_recovery)
        return self.plan_dialog

    def run_plan(self, title: str, planner, *args) -> None:
        """Plan in the worker, then show the plan for confirmation."""
        if self.organizer is None:
            return
        self.worker.submit(self._job(planner, *args),
                           on_done=lambda plan: self.show_plan(title, plan),
                           on_error=self._worker_error(title))

    def open_log(self, select: str | None = None, targets: list | None = None) -> None:
        if self.model is None or self.organizer is None:
            return
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.destroy()
        targets = targets or [s.target for src in self.model.live_board().sources
                              for s in src.slots]
        organizer = self.organizer
        self.log_window = OpsLogWindow(
            self.winfo_toplevel(), worker=self.worker, themed=self.themed,
            make_context=lambda: self.make_context(organizer), targets=targets,
            on_undo=self.undo_op, select=select)

    def open_steps(self, target) -> None:
        if self.organizer is None:
            return
        self.worker.submit(self._job(empty_slot_guide, target), on_done=self._steps,
                           on_error=self._worker_error("Empty this slot"))

    def _steps(self, guide) -> StepsWindow:
        return StepsWindow(self.winfo_toplevel(), themed=self.themed, guide=guide,
                           on_keep=lambda t: self.run_plan("Keep a copy in the vault",
                                                           plan_keep, t))

    def open_recovery(self, items=None) -> RecoveryDialog | None:
        """The interrupted moves (given, or asked of the live operations in the worker)."""
        if self.organizer is None:
            return None
        if items is None:
            self.worker.submit(self._job(pending_recovery), on_done=self.open_recovery,
                               on_error=self._worker_error("Recovery"))
            return None
        if self.recovery_dialog is not None and self.recovery_dialog.winfo_exists():
            self.recovery_dialog.destroy()
        if not items:
            self.alert("recovery", "No interrupted move is waiting.")
            return None
        organizer = self.organizer
        top = self.winfo_toplevel()
        self.recovery_dialog = RecoveryDialog(
            grab_owner(top) or top, worker=self.worker, themed=self.themed,
            make_context=lambda: self.make_context(organizer), items=items,
            on_changed=lambda: self.rescan())
        return self.recovery_dialog

    # --- the board menu ---------------------------------------------------------------------------

    def slot_menu(self, slot: BoardSlot) -> tk.Menu:
        """Right click on a live slot: Replace with..., Swap, Promote, Keep a copy, History,
        and the steps to empty it. A slot is only ever replaced or filled by the app (F8)."""
        menu = tk.Menu(self, tearoff=False)
        target = slot.target
        occupied = slot.state in ("occupied", "partial")
        menu.add_command(label="Replace with..." if occupied else "Put a save into it...",
                         command=lambda: self.open_replace_for(slot))
        if occupied and self.model is not None:
            others = [s for src in self.model.live_board().sources for s in src.slots
                      if s.target != target and s.target.platform == target.platform
                      and s.state in ("occupied", "partial")]
            swap = tk.Menu(menu, tearoff=False)
            for other in others:
                swap.add_command(
                    label=f"{other.label} - {other.name or '(unnamed)'}",
                    command=lambda o=other: self.run_plan(
                        "Swap two slots", plan_swap, target, o.target))
            if others:
                menu.add_cascade(label="Swap with", menu=swap)
            for pos, word in (("A", "auto-save"), ("B", "restore point")):
                if slot.member_loaded != pos:
                    menu.add_command(
                        label=f"Make the {word} load",
                        command=lambda q=pos: self.run_plan("Promote", plan_promote, target, q))
            menu.add_command(label="Keep a copy in the vault now",
                             command=lambda: self.run_plan("Keep a copy in the vault",
                                                           plan_keep, target))
        bind_menu_hints(menu, {f"Make the {w} load": PROMOTE_TIP
                               for w in ("auto-save", "restore point")},
                        lambda text: self.on_hint(text) if self.on_hint else None)
        menu.add_separator()
        menu.add_command(label="History of this slot...",
                         command=lambda: self.open_log(targets=[target]))
        menu.add_command(label="Empty this slot...", command=lambda: self.open_steps(target))
        return menu

    def _slot_context(self, token: str, slot: BoardSlot, event) -> None:
        self._post(self.slot_menu(slot), event.x_root, event.y_root)

    @staticmethod
    def _post(menu: tk.Menu, x_root: int, y_root: int) -> None:
        """Posting blocks (modal) until the menu closes; tests replace this."""
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    # --- verification, store status, recovery after a scan ------------------------------------------

    def _extras_shown(self, extras: dict) -> None:
        items = extras.get("verify")
        if items is not None:
            seen = set()
            for item in items:
                if item.state == "pending":
                    continue                      # the slot's clock shows it
                key = f"verify:{item.op_id}:{item.target.ref.account_key}:{item.target.slot}"
                seen.add(key)
                self.alert(key, aliases.scrub(item.text), actions=self._verify_actions(item),
                           dismiss=lambda i=item: self._dismiss_verify(i))
            for key in [k for k in self.alerts if k.startswith("verify:") and k not in seen]:
                self.clear_alert(key)
        status = extras.get("store")
        if status is not None:
            parts = []
            if status.damage:
                parts.append(f"{len(status.damage)} damaged file(s) in the vault store")
            if status.fold_failed:
                parts.append(f"{len(status.fold_failed)} backup(s) could not be folded into "
                             "the store")
            if status.fold_waiting_for_space:
                parts.append("the vault upgrade is waiting for free disk space")
            if parts:
                self.alert("store-status", aliases.scrub("Vault: " + "; ".join(parts) + "."))
            else:
                self.clear_alert("store-status")
        if extras.get("recovery"):
            self.open_recovery(extras["recovery"])

    def _verify_actions(self, item) -> tuple:
        slot = self._board_slot(item.target)
        out = []
        if "place_again" in item.actions and slot is not None:
            out.append(("Put it back", lambda: self.open_replace_for(slot)))
        if "show_steps" in item.actions:
            out.append(("Show steps", lambda: self.open_steps(item.target)))
        return tuple(out)

    def _board_slot(self, target) -> BoardSlot | None:
        if self.model is None:
            return None
        return next((s for src in self.model.live_board().sources for s in src.slots
                     if s.target == target), None)

    def _dismiss_verify(self, item) -> None:
        if self.organizer is not None:
            self.worker.submit(self._job(dismiss_verification, item.op_id, item.target),
                               on_error=self._worker_error("Dismiss"))

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

    def open_history(self) -> None:
        pid, head = self.table.selection_ref()
        if pid is not None:
            self.open_history_for(pid, head)

    def open_history_for(self, pid: str, head: str | None = None) -> None:
        self.histories.open_history(pid, head)

    def _pin(self, pid: str, pinned: bool) -> None:
        if self.organizer is not None:
            toggle_pin(self.worker, self.organizer.notes, pid, pinned, self.reload,
                       self._worker_error("Pin"))
