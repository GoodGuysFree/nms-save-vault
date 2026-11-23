"""The history windows of the Organizer tab (D6 U24-U27), kept out of ``view.py``: the
History window, the Compare window and the correction dialogs, opened from the table or the
detail pane. The view owns one ``HistoryHooks``; it hands it every new read model.

Writes (a lock, a correction) run in the UI worker and end in a reload of the view."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.organizer.readmodel import ReadModel
from ...core.organizer.lineage import Corrections
from ...core.store import thinning
from .. import waiting
from .compare import CompareWindow
from .correct import CorrectDialog
from .history import HistoryWindow

if TYPE_CHECKING:
    from .view import OrganizerView


class HistoryHooks:
    def __init__(self, view: OrganizerView) -> None:
        self.view = view
        self.history: HistoryWindow | None = None
        self.compare: CompareWindow | None = None
        self.correct: CorrectDialog | None = None

    @staticmethod
    def _alive(window) -> bool:
        return window is not None and window.winfo_exists()

    def set_model(self, model: ReadModel) -> None:
        if self._alive(self.history):
            if model.line(self.history.pid) is None:
                self.history.destroy()
            else:
                self.history.set_model(model)
        if self._alive(self.correct):
            self.correct.set_model(model)
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
        with waiting.wait_cursor(view):    # the graph of every version is drawn first
            self.history = self._new_history(view, pid, head)
        return self.history

    def _new_history(self, view, pid, head) -> HistoryWindow:
        return HistoryWindow(
            view.winfo_toplevel(), model=view.model, themed=view.themed, pid=pid, head=head,
            on_move=view.open_move_for, on_compare=self.open_compare, on_label=view.open_notes_for, on_lock=self.lock, on_correct=self.open_correct,
            facts=lambda: view.facts)

    def lock(self, pid: str, head: str, locked: bool) -> None:
        """Pin or unpin a version (the version note's lock: thinning never deletes it), in
        the worker, through the core's pin, which is refused while the vault is read-only;
        the refusal is said in the alert bar."""
        view = self.view
        row = next((r for r in view.model.versions(pid) if r.head == head), None) \
            if view.model else None
        if row is None or view.organizer is None:
            return
        root = view.organizer.vault_root
        if locked:
            view.worker.submit(thinning.pin_version, root, head, pair=row.pair, pid=row.pid,
                               on_done=lambda _r: view.reload(),
                               on_error=view._worker_error("Pin"))
        else:
            view.worker.submit(thinning.unpin_version, root, head,
                               on_done=lambda _r: view.reload(),
                               on_error=view._worker_error("Pin"))

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
                                     pid=pid, a=a, b=b, facts=lambda: view.facts)
        return self.compare

    # --- Correct --------------------------------------------------------------------------

    def open_correct(self, pid: str, head: str) -> CorrectDialog | None:
        """Comes from / Split here / Combine for one version, with undo for each."""
        view = self.view
        if view.model is None or view.organizer is None or view.scanning:
            return None
        if view.organizer.store.read_only:
            view.alert("correct", "The organizer store is read-only: corrections cannot be "
                                  "saved.")
            return None
        if self._alive(self.correct):
            self.correct.show(pid, head)
            self.correct.lift()
            return self.correct
        corrections = Corrections(view.organizer.store, view.organizer.clock)
        self.correct = CorrectDialog(
            view.winfo_toplevel(), model=view.model, corrections=corrections,
            worker=view.worker, themed=view.themed, pid=pid, head=head, on_done=view.reload)
        return self.correct
