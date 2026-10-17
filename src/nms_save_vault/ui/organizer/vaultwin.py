"""The vault upgrade window (D6 2.12 / row 20, D3 4.1-4.4): the one place where the vault becomes a
version store, the old backups are folded into it, hand-made folders are copied in, and the whole
upgrade is undone.

Nothing here starts by itself: the owner opens the window from the menu, reads the plan (what
will be copied aside, the free space) and presses the button. After an interrupted undo the next
start opens it asking "Finish the undo" or "Keep the upgrade". "Copy into the vault" is for
folders the owner made by hand and is OFF until ticked. Every job runs in the UI worker, in a
closure over plain data (never a widget); progress is polled, not pushed.
"""
from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk
from types import SimpleNamespace
from typing import Any, Callable

from ...core.store import (
    VersionStore, copy_in, not_copied_in, plan_upgrade, run_fold, run_upgrade, store_status,
    vault_lock, vault_mode)
from ...core.store import rollback, upgrade
from ...core.store.migrate import fold_pending
from ..dpi import px, pxs
from ..redact import set_label, shown
from ..themeaware import ThemeRegistry
from ..worker import UiWorker, weak
from .ops import _Dialog

GB = 1 << 30
CONFIRM_TEXT = ("I understand that older versions of NMS Save Vault must not be used on this "
                "vault afterwards.")


@dataclass
class VaultEnv:
    """What the window needs of the app: plain data, so no job closes over a widget."""

    vault_root: Path
    live_roots: Sequence[Path]
    app_state: Any
    app_version: str


def _gb(n: int) -> str:
    return f"{n / GB:.1f} GB"


def _open(env: VaultEnv, *, read_only: bool = False, for_rollback: bool = False) -> VersionStore:
    return VersionStore.open(env.vault_root, lock=vault_lock(env.vault_root),
                             app_version=env.app_version, live_roots=list(env.live_roots),
                             app_state=env.app_state, read_only=read_only,
                             for_rollback=for_rollback)


def needs_attention(vault_root: Path) -> bool:
    """At start-up: an undo of the upgrade was interrupted, so the owner is asked to finish it
    or keep the upgrade (D3 4.4). Reads only."""
    return rollback.rollback_interrupted(vault_root)


# --- the jobs (worker; plain data in, plain data out) --------------------------------------------


def read_state(env: VaultEnv, dest_parent: Path | None = None) -> SimpleNamespace:
    root = env.vault_root
    mode = vault_mode(root)
    out = SimpleNamespace(mode=mode, plan=None, status=None, fold_pending=False,
                          interrupted_undo=False, not_copied=[], eligible=upgrade.upgrade_needed(root))
    if mode == "legacy" and out.eligible:
        out.plan = plan_upgrade(root, dest_parent=dest_parent, live_roots=list(env.live_roots))
    elif mode == "upgrading":
        out.interrupted_undo = rollback.rollback_interrupted(root)
    elif mode == "store":
        store = _open(env, read_only=True)
        out.status = store_status(root, store, live_roots=list(env.live_roots),
                                  app_state=env.app_state)
        out.fold_pending = fold_pending(root)
        out.not_copied = [(e.id, e.label or e.id, Path(e.path).name)
                          for e in not_copied_in(store, live_roots=list(env.live_roots))]
    return out


def _fold_job(env: VaultEnv, box, cancel):
    store = _open(env)
    return run_fold(store, live_roots=list(env.live_roots),
                    progress=lambda p: setattr(box, "progress", p), cancel=cancel)


def upgrade_job(env: VaultEnv, dest_parent, box, cancel):
    plan = plan_upgrade(env.vault_root, dest_parent=dest_parent, live_roots=list(env.live_roots))
    result = run_upgrade(plan, progress=lambda p: setattr(box, "progress", p), cancel=cancel)
    box.progress = None
    report = _fold_job(env, box, cancel)
    return result, report


def fold_job(env: VaultEnv, box, cancel):
    return None, _fold_job(env, box, cancel)


def copy_in_job(env: VaultEnv, ids: list[str], cancel):
    return copy_in(_open(env), ids, live_roots=list(env.live_roots), cancel=cancel)


def rollback_plan_job(env: VaultEnv, interrupted: bool):
    store = _open(env, for_rollback=interrupted)
    return rollback.plan_rollback(env.vault_root, store)


def rollback_run_job(env: VaultEnv, interrupted: bool, cancel):
    store = _open(env, for_rollback=interrupted)
    plan = rollback.plan_rollback(env.vault_root, store, cancel=cancel)
    return rollback.run_rollback(plan, store, cancel=cancel)


def keep_job(env: VaultEnv):
    rollback.keep_upgrade(env.vault_root)


def cleanup_job(env: VaultEnv):
    return upgrade.cleanup_interrupted_upgrade(env.vault_root)


# --- the window --------------------------------------------------------------------------------


class VaultWindow(_Dialog):
    def __init__(self, master, *, worker: UiWorker, themed: ThemeRegistry, env: VaultEnv,
                 on_changed: Callable[[], None] | None = None) -> None:
        super().__init__(master, themed, "Vault upgrade", modal=False)
        self.worker, self.env, self.on_changed = worker, env, on_changed
        self.busy = False
        self.state: SimpleNamespace | None = None
        self._box = SimpleNamespace(progress=None)
        self._cancel = threading.Event()
        self._rollback_plan = None
        self.dest: Path | None = None
        self.confirm_var = tk.BooleanVar(value=False)
        self.dest_var = tk.StringVar(value="")
        self.copy_vars: dict[str, tk.BooleanVar] = {}
        pad = px(10)
        foot = ttk.Frame(self, padding=pad)
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(foot, text="Close", command=self.cancel).pack(side=tk.RIGHT)
        self.stop_button = ttk.Button(foot, text="Stop", command=self._stop)
        self.status = ttk.Label(foot, text="", style="Muted.TLabel")
        self.status.pack(side=tk.LEFT)
        self.bar = ttk.Progressbar(foot, mode="determinate", length=px(200))
        self.error = tk.Label(self, text="", anchor="w", justify="left", wraplength=px(640))
        self.error.pack(side=tk.BOTTOM, fill=tk.X, padx=pad)
        self.body = ttk.Frame(self, padding=pad)
        self.body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.minsize(px(600), px(320))
        self._ready()
        self.refresh()

    def _recolour(self) -> None:
        if self._pal is not None:
            self.error.configure(background=self._pal["bg"], foreground=self._pal["danger"])

    # --- reading the vault -----------------------------------------------------------------------

    def refresh(self) -> None:
        self.error.configure(text="")
        self._set_busy("Reading the vault...")
        env, dest = self.env, self.dest
        self.worker.submit(lambda: read_state(env, dest), on_done=weak(self._read),
                           on_error=weak(self._failed))

    def _read(self, state: SimpleNamespace) -> None:
        self._set_idle()
        self.state = state
        self._render()

    def _failed(self, error: BaseException) -> None:
        self._set_idle()
        self.error.configure(text=shown(self, str(error)))
        self._render()

    # --- rendering --------------------------------------------------------------------------------

    def _clear(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self.copy_vars.clear()
        self.upgrade_button = self.fold_button = self.copy_button = None
        self.undo_button = self.finish_button = self.keep_button = self.cleanup_button = None
        self.rollback_button = None

    def _text(self, text: str, *, style: str = "", wrap: int = 600) -> ttk.Label:
        label = ttk.Label(self.body, text=shown(self, text), wraplength=px(wrap), justify="left",
                          **({"style": style} if style else {}))
        label.pack(side=tk.TOP, anchor="w", pady=pxs(2, 2))
        return label

    def _render(self) -> None:
        self._clear()
        s = self.state
        ttk.Label(self.body, text="Vault upgrade", style="Title.TLabel").pack(anchor="w")
        if s is None:
            return
        if self._rollback_plan is not None:
            self._render_rollback(self._rollback_plan)
        elif s.mode == "legacy":
            self._render_legacy(s)
        elif s.mode == "upgrading":
            self._render_interrupted(s)
        else:
            self._render_store(s)

    def _render_legacy(self, s) -> None:
        if not s.eligible or s.plan is None:
            self._text("This folder is not a vault that can be upgraded.")
            return
        plan = s.plan
        self._text("The upgrade turns the vault into a version store: every save is kept once, "
                   "however many backups hold it. Nothing starts until you press the button. A "
                   "verified copy of the whole vault is made first, and it is yours to delete "
                   "when you are happy.")
        if plan.copy:
            self._text(f"Copy of the vault: {plan.files} files, {_gb(plan.bytes)}. It needs "
                       f"{_gb(plan.needed)} free; there are {_gb(plan.dest_free)}.")
            row = ttk.Frame(self.body)
            row.pack(anchor="w", fill=tk.X)
            ttk.Label(row, text="Make the copy in:").pack(side=tk.LEFT)
            self.dest_var.set(str(plan.dest.parent))
            ttk.Entry(row, textvariable=self.dest_var, width=44, state="readonly").pack(
                side=tk.LEFT, padx=px(6))
            ttk.Button(row, text="Choose...", command=self.choose_dest).pack(side=tk.LEFT)
        else:
            self._text("The vault has no backups yet, so no copy is needed.")
        for _code, text in plan.problems:
            tk.Label(self.body, text=shown(self, text), anchor="w", justify="left",
                     wraplength=px(600), foreground=self._pal["danger"] if self._pal else "red"
                     ).pack(anchor="w")
        ttk.Checkbutton(self.body, text=CONFIRM_TEXT, variable=self.confirm_var,
                        command=self._update_buttons).pack(anchor="w", pady=pxs(6, 2))
        self.confirm_var.set(False)
        self.upgrade_button = ttk.Button(self.body, text="Upgrade the vault", command=self.upgrade)
        self.upgrade_button.pack(anchor="w", pady=px(4))
        self._update_buttons()

    def _render_store(self, s) -> None:
        st = s.status
        self._text("The vault is a version store.")
        if st is not None:
            line = f"{st.entries} backups in the store, {_gb(st.store_bytes)}."
            if st.copy_aside is not None:
                line += f" The copy made before the upgrade is kept next to the vault."
            self._text(line)
            if st.fold_total:
                self._text(f"Old backups read into the store: {st.fold_done} of {st.fold_total}.")
            if st.fold_failed:
                self._text(f"{len(st.fold_failed)} old backup(s) could not be read in: "
                           + "; ".join(f"{k} ({why})" for k, why in st.fold_failed[:3]))
            if st.fold_waiting_for_space:
                self._text(f"The fold is waiting for {_gb(st.fold_waiting_for_space)} of free "
                           "space on the vault drive.")
            if st.damage:
                self._text(f"{len(st.damage)} damaged file(s) were found in the store.")
        if s.fold_pending:
            self.fold_button = ttk.Button(self.body, text="Resume reading in the old backups",
                                          command=self.fold)
            self.fold_button.pack(anchor="w", pady=px(4))
        if s.not_copied:
            box = ttk.LabelFrame(self.body, text="Copy into the vault (folders you made by hand)",
                                 padding=px(6))
            box.pack(fill=tk.X, pady=px(6))
            ttk.Label(box, text="These folders are kept where you put them. Tick the ones to "
                      "copy into the vault; the folders themselves are never changed.",
                      wraplength=px(560), justify="left").pack(anchor="w")
            for eid, label, folder in s.not_copied:
                var = tk.BooleanVar(value=False)                  # default OFF (owner decision)
                self.copy_vars[eid] = var
                ttk.Checkbutton(box, text=shown(self, f"{label}  ({folder})"), variable=var,
                                command=self._update_buttons).pack(anchor="w")
            self.copy_button = ttk.Button(box, text="Copy the ticked folders in",
                                          command=self.copy_selected)
            self.copy_button.pack(anchor="w", pady=px(4))
        self.undo_button = ttk.Button(self.body, text="Undo the vault upgrade...",
                                      command=self.plan_undo)
        self.undo_button.pack(anchor="w", pady=pxs(10, 2))
        self._update_buttons()

    def _render_interrupted(self, s) -> None:
        if s.interrupted_undo:
            self._text("Undoing the vault upgrade was interrupted. Finish the undo, or keep "
                       "the vault as an upgraded one. Nothing is lost either way.")
            self.finish_button = ttk.Button(self.body, text="Finish the undo",
                                            command=self.finish_undo)
            self.finish_button.pack(anchor="w", pady=px(2))
            self.keep_button = ttk.Button(self.body, text="Keep the upgrade", command=self.keep)
            self.keep_button.pack(anchor="w", pady=px(2))
        else:
            self._text("An upgrade did not finish. Cleaning up removes only what it made itself; "
                       "your vault is not changed.")
            self.cleanup_button = ttk.Button(self.body, text="Clean up", command=self.cleanup)
            self.cleanup_button.pack(anchor="w", pady=px(2))

    def _render_rollback(self, plan) -> None:
        self._text(f"Undoing the upgrade writes out {len(plan.export)} backup(s) made since as "
                   f"folders and puts back {len(plan.restore)} old folder(s) from the copy; the "
                   "store is then moved next to the vault, not deleted.")
        for _code, text in plan.problems:
            tk.Label(self.body, text=shown(self, text), anchor="w", justify="left",
                     wraplength=px(600), foreground=self._pal["danger"] if self._pal else "red"
                     ).pack(anchor="w")
        self.rollback_button = ttk.Button(self.body, text="Undo the upgrade now",
                                          command=self.run_undo)
        self.rollback_button.pack(anchor="w", pady=px(4))
        if plan.problems:
            self.rollback_button.state(["disabled"])
        ttk.Button(self.body, text="Back", command=self._back).pack(anchor="w")

    def _update_buttons(self) -> None:
        if getattr(self, "upgrade_button", None) is not None:
            plan = self.state.plan if self.state else None
            ok = (plan is not None and not plan.problems and self.confirm_var.get()
                  and not self.busy)
            self.upgrade_button.state(["!disabled"] if ok else ["disabled"])
        if getattr(self, "copy_button", None) is not None:
            ok = any(v.get() for v in self.copy_vars.values()) and not self.busy
            self.copy_button.state(["!disabled"] if ok else ["disabled"])

    # --- actions -----------------------------------------------------------------------------------

    def choose_dest(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Where to make the copy")
        if chosen:
            self.set_dest(Path(chosen))

    def set_dest(self, folder: Path) -> None:
        self.dest = folder
        self.refresh()

    def upgrade(self) -> None:
        if self.busy or self.upgrade_button.instate(["disabled"]):
            return
        dest = self.dest
        self._cancel = threading.Event()
        self._start(lambda env, box, cancel: upgrade_job(env, dest, box, cancel),
                    "Copying the vault aside...", self._upgraded)

    def fold(self) -> None:
        if not self.busy:
            self._start(lambda env, box, cancel: fold_job(env, box, cancel),
                        "Reading the old backups in...", self._upgraded)

    def copy_selected(self) -> None:
        ids = [i for i, v in self.copy_vars.items() if v.get()]
        if ids and not self.busy:
            self._start(lambda env, box, cancel: copy_in_job(env, ids, cancel),
                        "Copying the folders in...", self._copied)

    def plan_undo(self) -> None:
        if self.busy:
            return
        interrupted = bool(self.state and self.state.interrupted_undo)
        self._start(lambda env, box, cancel: rollback_plan_job(env, interrupted),
                    "Checking the copy made before the upgrade...", self._undo_planned)

    def run_undo(self) -> None:
        if self.busy or self.rollback_button.instate(["disabled"]):
            return
        interrupted = bool(self.state and self.state.interrupted_undo)
        self._start(lambda env, box, cancel: rollback_run_job(env, interrupted, cancel),
                    "Undoing the upgrade...", self._undone)

    def finish_undo(self) -> None:
        self.plan_undo()

    def keep(self) -> None:
        self._start(lambda env, box, cancel: keep_job(env), "Keeping the upgrade...", self._kept)

    def cleanup(self) -> None:
        self._start(lambda env, box, cancel: cleanup_job(env), "Cleaning up...", self._kept)

    def _back(self) -> None:
        self._rollback_plan = None
        self._render()

    def _start(self, job, text: str, done) -> None:
        """``job(env, box, cancel)`` in the worker: a closure over plain data only."""
        self._cancel = threading.Event()
        self._box.progress = None
        self._set_busy(text)
        self.error.configure(text="")
        self.stop_button.pack(side=tk.LEFT, padx=px(6))
        self.bar.pack(side=tk.LEFT, padx=px(6))
        self.bar.configure(value=0, maximum=1)
        self.after(150, self._poll)
        env, box, cancel = self.env, self._box, self._cancel
        self.worker.submit(lambda: job(env, box, cancel), on_done=weak(done),
                           on_error=weak(self._failed_run))

    def _stop(self) -> None:
        self._cancel.set()
        set_label(self.status, "Stopping...")

    def _poll(self) -> None:
        if not self.winfo_exists() or not self.busy:
            return
        p = self._box.progress
        if p is not None:
            if hasattr(p, "bytes_total"):          # an upgrade's copy
                self.bar.configure(maximum=max(1, p.bytes_total), value=p.bytes_done)
                set_label(self.status, {"copy": "Copying the vault aside...",
                                        "verify": "Checking the copy...",
                                        "commit": "Switching to the store..."}.get(p.stage, p.stage))
            else:                                   # the fold
                self.bar.configure(maximum=max(1, p.entries_total), value=p.entries_done)
                set_label(self.status, f"Reading old backups in: {p.entries_done} of "
                          f"{p.entries_total}")
        self.after(150, self._poll)

    def _set_busy(self, text: str) -> None:
        self.busy = True
        set_label(self.status, text)
        self._update_buttons()

    def _set_idle(self) -> None:
        self.busy = False
        self.stop_button.pack_forget()
        self.bar.pack_forget()
        set_label(self.status, "")
        self._update_buttons()

    # --- results -----------------------------------------------------------------------------------

    def _failed_run(self, error: BaseException) -> None:
        self._set_idle()
        self.error.configure(text=shown(self, str(error) or type(error).__name__))
        self._after()

    def _upgraded(self, result) -> None:
        up, report = result
        self._set_idle()
        notes = []
        if up is not None:
            notes.append(up.message)
        if report is not None:
            notes.append(report.message)
        self.error.configure(text="")
        self._changed()
        self.refresh()
        if notes:
            set_label(self.status, " ".join(n for n in notes if n)[:200])

    def _copied(self, report) -> None:
        self._set_idle()
        self._changed()
        self.refresh()
        set_label(self.status, report.message[:200])

    def _undo_planned(self, plan) -> None:
        self._set_idle()
        self._rollback_plan = plan
        self._render()

    def _undone(self, result) -> None:
        self._set_idle()
        self._rollback_plan = None
        self._changed()
        self.refresh()
        set_label(self.status, result.message[:200])

    def _kept(self, _result=None) -> None:
        self._set_idle()
        self._changed()
        self.refresh()

    def _after(self) -> None:
        self._render()

    def _changed(self) -> None:
        if self.on_changed is not None:
            self.on_changed()
