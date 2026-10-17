"""The history windows of the Organizer tab (D6 U24-U27), kept out of ``view.py``: the
History window, the Compare window and the correction dialogs, opened from the table or the
detail pane. The view owns one ``HistoryHooks``; it hands it every new read model.

Writes (a lock, a correction) run in the UI worker and end in a reload of the view."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.organizer.readmodel import ReadModel
from .compare import CompareWindow
from .history import HistoryWindow

if TYPE_CHECKING:
    from .view import OrganizerView


class HistoryHooks:
    def __init__(self, view: OrganizerView) -> None:
        self.view = view
        self.history: HistoryWindow | None = None
        self.compare: CompareWindow | None = None

    @staticmethod
    def _alive(window) -> bool:
        return window is not None and window.winfo_exists()

    def set_model(self, model: ReadModel) -> None:
        if self._alive(self.history):
            if model.line(self.history.pid) is None:
                self.history.destroy()
            else:
                self.history.set_model(model)
        if self._alive(self.compare):
            if model.line(self.compare.pid) is None:
                self.compare.destroy()
            else:
                self.compare.set_model(model)

    # --- History --------------------------------------------------------------------------

    def open_history(self, pid: str, head: str | None = None) -> HistoryWindow | None:
        view = self.view
        if view.model is None or view.model.line(pid) is None:
            return None
        if self._alive(self.history):
            self.history.set_model(view.model)
            self.history.show(pid, head)
            self.history.deiconify()
            self.history.lift()
            return self.history
        self.history = HistoryWindow(
            view.winfo_toplevel(), model=view.model, themed=view.themed, pid=pid, head=head,
            on_move=view.open_move_for, on_compare=self.open_compare, on_label=view.open_notes_for, on_lock=self.lock)
        return self.history

    def lock(self, pid: str, head: str, locked: bool) -> None:
        """Lock or unlock a version (never pruned, never deleted), in the worker."""
        view = self.view
        row = next((r for r in view.model.versions(pid) if r.head == head), None) \
            if view.model else None
        if row is None or view.organizer is None:
            return
        view.worker.submit(view.organizer.notes.set_version_note, head, pair=row.pair,
                           pid=row.pid, locked=locked, on_done=lambda _r: view.reload(),
                           on_error=view._worker_error("Lock"))

    # --- Compare --------------------------------------------------------------------------

    def open_compare(self, pid: str, a: str, b: str | None = None) -> CompareWindow | None:
        """Two versions side by side; ``b`` None: the save file's live version (the first
        live copy that is not ``a``)."""
        view = self.view
        model = view.model
        line = model.line(pid) if model else None
        if line is None:
            return None
        if b is None:
            b = next((x.head for x in line.live if x.head != a), None)
            if b is None:
                view.alert("compare", "Nothing to compare with: no other version is live. "
                                      "Ctrl+click a second version in the history.")
                return None
        view.clear_alert("compare")
        if self._alive(self.compare):
            self.compare.pid = pid
            self.compare.model = model
            self.compare.set_versions(a, b)
            self.compare.deiconify()
            self.compare.lift()
            return self.compare
        self.compare = CompareWindow(view.winfo_toplevel(), model=model, themed=view.themed,
                                     pid=pid, a=a, b=b)
        return self.compare
