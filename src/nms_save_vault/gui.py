"""Tkinter desktop UI for NMS Save Vault.

A single tree shows the LIVE folder and every catalog backup; each entry expands to its
occupied slots, and each slot to its two members (manual save + auto restore-point) with
the game-current one marked '*'. Toolbar actions cover all three features plus promote
and undo. Every write goes through the safety-wrapped core (auto-snapshot + validate).
"""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .core import discover, locations
from .core import operations as ops
from .core import savedir
from .core import state as appstate
from .core.catalog import Vault


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


class App(tk.Tk):
    COLUMNS = ("name", "mode", "play", "saved", "status")

    def __init__(self, live: str | Path | None = None, vault: str | Path | None = None):
        super().__init__()
        self.title("NMS Save Vault")
        self.geometry("1000x640")

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
        """Populate the active-live dropdown with the writable (Steam) sources."""
        writable = self._writable_sources()
        self._source_choices = {f"{s.label} ({Path(s.path).name})": s.id for s in writable}
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
            ("Help", self.on_help),
        ]:
            ttk.Button(bar, text=text, command=cmd).pack(side=tk.LEFT, padx=2)

        # Active live (write target) selector -- only writable Steam accounts.
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

        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self.tree = ttk.Treeview(frame, columns=self.COLUMNS, show="tree headings")
        self.tree.heading("#0", text="Backup / Slot / Save")
        self.tree.column("#0", width=320, anchor="w")
        for col, label, width in [
            ("name", "Save name", 230),
            ("mode", "Mode", 50),
            ("play", "Play", 70),
            ("saved", "Saved", 130),
            ("status", "Status", 150),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Button-3>", self._on_right_click)  # right-click context menu

        # Row styling: groups bold, active live emphasised, Xbox/read-only amber, backups grey.
        self.tree.tag_configure("group", font=("TkDefaultFont", 10, "bold"))
        self.tree.tag_configure("live", foreground="#0a6b2f")
        self.tree.tag_configure("active", foreground="#0a6b2f", font=("TkDefaultFont", 9, "bold"))
        self.tree.tag_configure("readonly", foreground="#7a5b00")
        self.tree.tag_configure("backup", foreground="#333333")

    # --- populate ------------------------------------------------------------

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._meta.clear()

        # --- LIVE SAVES group: every live source, grouped by account ----------
        live_group = self.tree.insert("", "end", text="● LIVE SAVES", open=True, tags=("group",))
        self._meta[live_group] = {"type": "group"}
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
            node = self.tree.insert(
                live_group, "end", open=active,
                text=f"{s.label}  ({Path(s.path).name}){badge}",
                values=("", "", "", "", "writable" if s.writable else "read-only"),
                tags=tags,
            )
            self._meta[node] = {"type": "live", "dir": s.path, "writable": s.writable, "source_id": s.id}
            self._add_view(node, savedir.scan_any(s.path), writable=s.writable)

        # --- BACKUPS group: catalog entries -----------------------------------
        backup_group = self.tree.insert("", "end", text="■ BACKUPS", open=True, tags=("group",))
        self._meta[backup_group] = {"type": "group"}
        for e in sorted(self.vault.entries, key=lambda e: e.id):
            node = self.tree.insert(backup_group, "end", text=f"{e.id}  [{e.kind}]",
                                    values=(e.label, "", "", "", ""), tags=("backup",))
            self._meta[node] = {"type": "entry", "entry": e}
            self._add_entry(node, e)

        self._refresh_source_combo()
        running = ops.safety.is_game_running()
        game = {True: "RUNNING (writes blocked)", False: "closed", None: "unknown"}[running]
        active = self._active_source()
        active_txt = active.label if active else (str(self.live_dir) if self.live_dir else "none")
        self.status.set(
            f"Active live: {active_txt}   |   Sources: {len(sources)}   |   "
            f"Vault: {self.vault.root}   |   Game: {game}"
        )

    def _add_view(self, parent: str, view: savedir.SaveDirView, writable: bool) -> None:
        for slot in sorted(view.slots):
            sv = view.slots[slot]
            if not sv.occupied:
                continue
            n = sv.newest
            node = self.tree.insert(
                parent,
                "end",
                text=f"Slot {slot}",
                values=(
                    sv.display_name,
                    (n.info.game_mode if n and n.info else ""),
                    _fmt_play(n.info.total_play_time if n and n.info else 0),
                    _fmt_ts(n.effective_timestamp if n else 0),
                    "",
                ),
            )
            self._meta[node] = {"type": "slot", "dir": str(view.path), "slot": slot, "live": writable}
            for m in sv.members:
                if not m.exists:
                    continue
                star = " *" if (n and m.label == n.label) else ""
                mid = self.tree.insert(
                    node,
                    "end",
                    text=f"   {m.label}{star}",
                    values=(
                        m.save_name,
                        (m.info.game_mode if m.info else ""),
                        _fmt_play(m.info.total_play_time if m.info else 0),
                        _fmt_ts(m.effective_timestamp),
                        ("valid" if m.valid else "INVALID") + (" / moved" if m.moved else ""),
                    ),
                )
                self._meta[mid] = {
                    "type": "member",
                    "dir": str(view.path),
                    "slot": slot,
                    "member": 0 if m.label == "A" else 1,
                    "live": writable,
                }

    def _add_entry(self, parent: str, entry) -> None:
        for s in entry.slots:
            if not s.occupied:
                continue
            ts = max((m.timestamp for m in s.members if m.present), default=0)
            node = self.tree.insert(
                parent, "end", text=f"Slot {s.slot}", values=(s.name, "", "", _fmt_ts(ts), "")
            )
            self._meta[node] = {"type": "slot", "dir": entry.path, "slot": s.slot, "live": False, "entry": entry}
            for m in s.members:
                if not m.present:
                    continue
                star = " *" if m.label == s.newest_label else ""
                self.tree.insert(
                    node,
                    "end",
                    text=f"   {m.label}{star}",
                    values=(
                        m.name,
                        m.game_mode,
                        _fmt_play(m.play_time),
                        _fmt_ts(m.timestamp),
                        ("valid" if m.valid else "INVALID") + (" / moved" if m.moved else ""),
                    ),
                )

    # --- selection helpers ---------------------------------------------------

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._meta.get(sel[0])

    def _on_right_click(self, event) -> None:
        """Build a context-sensitive menu for the right-clicked row."""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
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
                label = "A" if meta["member"] == 0 else "B"
                menu.add_command(label=f"Make save {label} the newest (promote)", command=lambda: self.on_promote(meta))
        menu.add_separator()
        menu.add_command(label="Refresh", command=self.refresh)
        menu.add_command(label="Help", command=self.on_help)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # --- actions -------------------------------------------------------------

    def _run(self, fn, *, success: str) -> None:
        """Run a core operation, prompting to override the game-running guard if needed."""
        try:
            try:
                result = fn(False)
            except ops.GameRunningError:
                if not messagebox.askyesno("Game running", "No Man's Sky appears to be running.\nProceed anyway (risky)?"):
                    return
                result = fn(True)
        except ops.OperationError as exc:
            messagebox.showerror("Operation failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors to the user
            messagebox.showerror("Unexpected error", repr(exc))
            return
        self.refresh()
        if isinstance(result, ops.OpResult):
            msg = result.detail + (f"\n\nUndo available (snapshot {result.snapshot_id})." if result.snapshot_id else "")
            if result.warnings:
                msg += "\n\nWarnings:\n - " + "\n - ".join(result.warnings)
            messagebox.showinfo("Done", msg)
        else:
            messagebox.showinfo("Done", success)

    def _require_live(self) -> bool:
        if not (self.live_dir and Path(self.live_dir).is_dir()):
            messagebox.showerror("No live folder", "Could not locate the live save folder.")
            return False
        return True

    def on_backup(self) -> None:
        if not self._require_live():
            return
        self._backup_dir(self.live_dir)

    def _backup_dir(self, directory) -> None:
        label = simpledialog.askstring("Backup", "Optional label:") or ""
        self._run(
            lambda _force: ops.create_full_backup(self.vault, Path(directory), label=label),
            success="Backup created.",
        )

    def on_restore(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "entry":
            messagebox.showinfo("Select a backup", "Select a catalog entry (top-level backup) to restore.")
            return
        if not self._require_live():
            return
        entry = sel["entry"]
        if not messagebox.askyesno("Restore", f"Replace the live saves with backup '{entry.id}'?\n(The current state is auto-snapshotted first.)"):
            return
        self._run(lambda force: ops.restore_full(self.vault, entry, self.live_dir, allow_game_running=force), success="Restored.")

    def on_extract(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "slot":
            messagebox.showinfo("Select a slot", "Select a slot (under LIVE or any backup) to extract.")
            return
        self._run(
            lambda _force: ops.extract_slot(self.vault, Path(sel["dir"]), sel["slot"], label=""),
            success=f"Extracted slot {sel['slot']}.",
        )

    def on_repopulate(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "slot":
            messagebox.showinfo("Select a source slot", "Select the source slot (under a backup or LIVE) first.")
            return
        if not self._require_live():
            return
        dest = simpledialog.askinteger("Repopulate", "Destination live slot (1-15):", minvalue=1, maxvalue=15)
        if not dest:
            return
        if not messagebox.askyesno(
            "Repopulate",
            f"Write slot {sel['slot']} from\n{sel['dir']}\ninto LIVE slot {dest}?\n(The current state is auto-snapshotted first.)",
        ):
            return
        self._run(
            lambda force: ops.repopulate_slot(self.vault, Path(sel["dir"]), sel["slot"], self.live_dir, dest, allow_game_running=force),
            success=f"Repopulated live slot {dest}.",
        )

    def on_promote(self, target: dict | None = None) -> None:
        sel = target or self._selected()
        if not sel or sel.get("type") != "member" or not sel.get("live"):
            messagebox.showinfo("Select a live save", "Select a save (A or B) under a LIVE slot to make it the newest.")
            return
        self._run(
            lambda force: ops.promote_member(self.vault, self.live_dir, sel["slot"], sel["member"], allow_game_running=force),
            success="Promoted.",
        )

    def on_import(self) -> None:
        folder = filedialog.askdirectory(title="Select a save-folder backup to import")
        if folder:
            self._import_dir(folder)

    def _import_dir(self, directory) -> None:
        copy = messagebox.askyesno("Import", "Copy the backup into the vault?\n(No = index it where it is.)")
        self._run(
            lambda _force: ops.import_backup(self.vault, Path(directory), copy_into_vault=copy),
            success="Imported.",
        )

    def on_rescan(self) -> None:
        """Re-scan AppData for live sources and merge any new accounts into the config."""
        added = discover.merge_live_sources(self.state)
        try:
            appstate.save(self.state)
        except OSError as exc:
            messagebox.showwarning("Rescan", f"Found sources but could not save config:\n{exc}")
        # If we had no active writable source yet, adopt the first one found.
        if self.active_source_id is None:
            writable = self._writable_sources()
            if writable:
                self.active_source_id = writable[0].id
                self.live_dir = Path(writable[0].path)
        self.refresh()
        messagebox.showinfo(
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
        messagebox.showinfo("Discover", f"Added {added} newly found backup(s) to the catalog.")

    def on_undo(self) -> None:
        if not self._require_live():
            return
        if not messagebox.askyesno("Undo", "Undo the last operation by restoring its auto-snapshot?"):
            return
        self._run(lambda force: ops.undo_last(self.vault, self.live_dir, allow_game_running=force), success="Undone.")

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


HELP_TEXT = """NMS Save Vault — Help

The tree is split into two groups:
  ● LIVE SAVES  -- every save folder found on this PC: each Steam account and, read-only,
     each Xbox / Game Pass account. The ACTIVE one (green, bold) is the target of write
     actions; pick it from the "Active live" dropdown or right-click a folder to set it.
     Xbox folders are read-only here -- you can browse and back them up, but not write to them.
  ■ BACKUPS  -- every backup in the catalog (full snapshots, extracts, imported and
     auto-discovered copy-paste backups).
Expand a folder to see its slots; expand a slot to see its two saves:
  - A and B are the slot's two saves: your manual save and the auto "restore point".
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
- Promote: Select one of a live slot's two saves (A or B) to force it to be the newest,
  so the game loads it instead of the other -- e.g. to roll back to the restore point.
- Import: Register an existing save folder you made yourself (or an Xbox / Game Pass
  save) into the catalog -- either in place or copied into the vault.
- Rescan: Re-detect live save folders (new Steam account, new Xbox account) and add them
  to the LIVE SAVES list. Your manual entries are left untouched.
- Discover: Scan the NMS folder for existing backups and add any new ones found.
- Undo: Restore the auto-snapshot taken just before the last operation.
- Refresh: Re-scan the live folder and the catalog.
- Help: This dialog.

WORKFLOWS
- More than 15 slots: Extract the slots you are not using into the vault, then Repopulate
  them into a live slot whenever you want to play them again. Your library is unlimited;
  only 15 are live at a time.
- Safe experimenting: Backup live (or Extract the slot), make changes in-game, then
  Restore / Repopulate / Undo if you do not like the result.
- Move a save to another slot: Repopulate -- pick the source slot and the destination.
- Roll back within a slot: expand the live slot, right-click the older save, Promote it.
- Bring in an outside save: Import the folder, then Repopulate the slot you want.

SAFETY
- Writes are blocked while No Man's Sky is running -- close the game first.
- Every change auto-snapshots the live state first and can be reversed with Undo.
- Steam Cloud: operate with the game closed; if Steam shows a conflict on next launch,
  keep the local copy.
"""


def main(argv=None) -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
