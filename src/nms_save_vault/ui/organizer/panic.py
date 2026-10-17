"""The whole-folder restore dialog (D5 4.3 "panic restore"; D6 U19): put a snapshot or a full
backup of one live folder back, slot by slot, byte for byte. It replaces the classic tab's
whole-folder restore: the plan, its warnings and the confirmations come from the live
operations (``plan_panic_restore``), the write is one ``panic_restore`` op in the worker, and the
dialog names the folder it will write into."""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ...core.liveops import OpContext, OpOutcome, PlacePlan
from ...core.liveops import panic
from ...core.organizer.model import LiveRef
from ..dpi import px, pxs
from ..redact import set_label, shown
from ..themeaware import ThemeRegistry
from ..worker import UiWorker, weak
from .ops import PlanDialog


def _load_job(open_context: Callable[[], tuple[OpContext, LiveRef]], extra: tuple[str, str] | None):
    """The worker's first job: the candidate snapshots and the folder's platform."""
    def job():
        ctx, ref = open_context()
        found = [(c.id, f"{c.label or c.id}  ({c.kind}, {c.created})")
                 for c in panic.candidates(ctx, ref, ("snapshot", "full"))]
        if extra is not None and extra[0] not in {i for i, _ in found}:
            found.insert(0, extra)
        return found, ref.platform
    return job


def _plan_job(open_context, snapshot_id: str, accountdata: bool):
    def job() -> PlacePlan:
        ctx, ref = open_context()
        return panic.plan_panic_restore(ctx, ref, snapshot_id, restore_accountdata=accountdata)
    return job


def _post_job(open_context, snapshot_id: str, accountdata: bool):
    """After a done restore: the account data goes back too when the owner asked."""
    if not accountdata:
        return None

    def post(ctx: OpContext, plan: PlacePlan, outcome: OpOutcome) -> None:
        _ctx, ref = open_context()
        panic._restore_accountdata(ctx, plan, ref, snapshot_id)
    return post


class PanicDialog(PlanDialog):
    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry,
                 open_context: Callable[[], tuple[OpContext, LiveRef]], folder_name: str,
                 snapshot: tuple[str, str] | None = None,
                 on_done: Callable[[OpOutcome], None] | None = None,
                 on_recover: Callable[[], None] | None = None) -> None:
        self.open_context = open_context
        super().__init__(master, worker=worker, themed=themed,
                         make_context=lambda: open_context()[0], plan=None,
                         title=f"Restore the whole folder {folder_name}", on_done=on_done,
                         on_recover=on_recover)
        self.snapshot_id: str | None = snapshot[0] if snapshot else None
        self.choices: dict[str, str] = {}
        self.platform = ""
        top = ttk.Frame(self.summary.master)
        top.pack(side=tk.TOP, fill=tk.X, before=self.summary)
        ttk.Label(top, text="Put back:").pack(side=tk.LEFT)
        self.choice_var = tk.StringVar()
        self.combo = ttk.Combobox(top, textvariable=self.choice_var, state="disabled", width=56)
        self.combo.pack(side=tk.LEFT, padx=px(6))
        self.combo.bind("<<ComboboxSelected>>", lambda _e: self._picked())
        self.account_var = tk.BooleanVar(value=False)
        self.account_check = ttk.Checkbutton(
            self.summary.master, variable=self.account_var, command=self._replan,
            text="Also put back the account data (unlocks, settings) - off keeps this folder's own")
        ttk.Label(self.summary.master, text="Slots the backup does not hold are left as they "
                  "are; nothing is deleted.", style="Muted.TLabel").pack(side=tk.TOP, anchor="w",
                                                                         before=self.summary)
        self._gen += 1
        gen = self._gen
        worker.submit(_load_job(open_context, snapshot), on_done=weak(self._loaded, gen),
                      on_error=weak(self._plan_failed, gen))

    def _loaded(self, gen: int, result) -> None:
        if gen != self._gen or not self.winfo_exists():
            return
        found, self.platform = result
        self.choices = {label: sid for sid, label in found}
        self.combo.configure(values=list(self.choices), state="readonly" if found else "disabled")
        if not found:
            set_label(self.summary, "There is no snapshot or backup of this folder to put back.")
            return
        wanted = next((label for sid, label in found if sid == self.snapshot_id), found[0][1])
        self.choice_var.set(wanted)
        if self.platform != "xbox":
            self.account_check.pack(side=tk.TOP, anchor="w", before=self.summary)
        self._picked()

    def _picked(self) -> None:
        self.snapshot_id = self.choices.get(self.choice_var.get())
        self._replan()

    def _replan(self) -> None:
        if self.snapshot_id is None or self.busy:
            return
        self._gen += 1
        gen = self._gen
        self.plan = None
        self.error.configure(text="")
        self._render()
        job = _plan_job(self.open_context, self.snapshot_id, self.account_var.get())
        self.worker.submit(job, on_done=weak(self._planned, gen), on_error=weak(self._plan_failed, gen))

    def _planned(self, gen: int, plan: PlacePlan) -> None:
        if gen != self._gen or not self.winfo_exists():
            return
        self.plan = plan
        self._render()

    def _plan_failed(self, gen: int, error: BaseException) -> None:
        if gen != self._gen or not self.winfo_exists():
            return
        self.busy = False
        self.error.configure(text=shown(self, f"This could not be planned: {error}"))

    def _post(self):
        return _post_job(self.open_context, self.snapshot_id or "", self.account_var.get())
