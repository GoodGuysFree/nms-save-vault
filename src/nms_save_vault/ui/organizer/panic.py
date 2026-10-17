"""The whole-folder restore dialog (D5 4.3 "panic restore"; D6 U19): put a snapshot or a full
backup of one live folder back, slot by slot, byte for byte. It replaces the classic tab's
whole-folder restore: the plan, its warnings and the confirmations come from the live
operations (``plan_panic_restore``), the write is one ``panic_restore`` op in the worker, and the
dialog names the folder it will write into."""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ...core import aliases
from ...core import operations as ops
from ...core.liveops import OpContext, OpOutcome, PlacePlan, Warning
from ...core.liveops import classic, panic
from ...core.store import classic as store_classic
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


def _restore_job(vault, live_dir, snapshot_id: str, accountdata: bool):
    """The whole-folder restore exactly as before U19 (``classic.restore_full``): a damaged
    Xbox index is repaired from the backup first, a Steam folder with no account data gets the
    backup's, a missing Xbox AccountData record is filled in, and a kept account is said so."""
    def job() -> OpOutcome:
        try:
            entry = store_classic.find_entry(vault, snapshot_id)
            if entry is None:
                raise ops.OperationError(f"The backup '{snapshot_id}' is not in the vault.")
            result = classic.restore_full(vault, entry, live_dir, True, False,
                                          restore_accountdata=accountdata)
        except (ops.OperationError, OSError) as exc:
            return OpOutcome(None, "refused", str(exc), (), None, "n/a", str(exc))
        notes = tuple(Warning("RESTORE_NOTE", "info", str(aliases.scrub(w)), {})
                      for w in result.warnings)
        return OpOutcome(result.op_id, "done", result.detail or "Restored.", notes, None, "n/a",
                         None)
    return job


class PanicDialog(PlanDialog):
    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry,
                 open_context: Callable[[], tuple[OpContext, LiveRef]], folder_name: str,
                 vault=None, live_dir=None, snapshot: tuple[str, str] | None = None,
                 on_done: Callable[[OpOutcome], None] | None = None,
                 on_recover: Callable[[], None] | None = None) -> None:
        self.open_context = open_context
        self.vault, self.live_dir = vault, live_dir
        self.repair = False
        super().__init__(master, worker=worker, themed=themed,
                         make_context=lambda: open_context()[0], plan=None,
                         title=f"Restore the whole folder {folder_name}", on_done=on_done,
                         on_recover=on_recover)
        self.snapshot_id: str | None = snapshot[0] if snapshot else None
        self.repair_var = tk.BooleanVar(value=False)
        self.repair_check = ttk.Checkbutton(
            self.summary.master, variable=self.repair_var, command=self._update,
            text="The folder's save index is damaged. Repair it from this backup first (the "
                 "damaged file is kept in the vault), then restore.")
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
        found = [(sid, str(aliases.scrub(label))) for sid, label in found]
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
        self.repair = False
        self.repair_var.set(False)
        self.repair_check.pack_forget()
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
        if "BAD_INDEX" in str(error) or getattr(error, "code", "") == "BAD_INDEX":
            self.repair = True            # a restore is how a damaged Xbox folder is repaired
            self.repair_check.pack(side=tk.TOP, anchor="w", before=self.summary)
            set_label(self.summary, "The folder's save index does not read, so the slots "
                      "cannot be compared yet. Restoring repairs the index from this backup "
                      "first; nothing in the vault is changed.")
            self._update()
            return
        self.error.configure(text=shown(self, f"This could not be planned: {error}"))

    def _update(self) -> None:
        if self.repair and self.plan is None:
            ok = self.repair_var.get() and not self.busy and self.snapshot_id is not None
            self.apply_button.state(["!disabled"] if ok else ["disabled"])
            set_label(self.apply_button, "Repair the index and restore the whole folder")
            return
        super()._update()

    def apply(self) -> None:
        if self.repair and self.plan is None and not self.busy \
                and not self.apply_button.instate(["disabled"]):
            self.plan = _RepairPlan()
            try:
                super().apply()
            finally:
                self.plan = None
            return
        super().apply()

    def _make_job(self, confirmed, note):
        return _restore_job(self.vault, self.live_dir, self.snapshot_id or "",
                            self.account_var.get())


class _RepairPlan:
    """Stands in for the plan while the folder's index is being repaired (no plan can be made
    of a folder that does not read); ``restore_full`` plans after the repair."""
    blocked: tuple = ()
    confirmations: frozenset = frozenset()
    warnings: tuple = ()
    targets: tuple = ()
    outcome = "Repair the index and restore the whole folder"
