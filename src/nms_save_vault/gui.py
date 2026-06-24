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

from .core import locations
from .core import operations as ops
from .core import savedir
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
        self.live_dir = Path(live) if live else locations.default_live_save_dir()
        vault_root = Path(vault) if vault else locations.default_vault_dir()
        self.vault = Vault(vault_root)
        self.vault.ensure()
        self.vault.load()
        self._meta: dict[str, dict] = {}
        self._build_widgets()
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
            ("Discover", self.on_discover),
            ("Undo", self.on_undo),
            ("Refresh", self.refresh),
        ]:
            ttk.Button(bar, text=text, command=cmd).pack(side=tk.LEFT, padx=2)

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

    # --- populate ------------------------------------------------------------

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._meta.clear()

        if self.live_dir and Path(self.live_dir).is_dir():
            node = self.tree.insert("", "end", text=f"LIVE  ({Path(self.live_dir).name})", open=True)
            self._meta[node] = {"type": "live"}
            self._add_view(node, savedir.scan(self.live_dir), live=True)

        for e in sorted(self.vault.entries, key=lambda e: e.id):
            node = self.tree.insert("", "end", text=f"{e.id}  [{e.kind}]", values=(e.label, "", "", "", ""))
            self._meta[node] = {"type": "entry", "entry": e}
            self._add_entry(node, e)

        running = ops.safety.is_game_running()
        game = {True: "RUNNING (writes blocked)", False: "closed", None: "unknown"}[running]
        self.status.set(f"Live: {self.live_dir}   |   Vault: {self.vault.root}   |   Game: {game}")

    def _add_view(self, parent: str, view: savedir.SaveDirView, live: bool) -> None:
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
                    _fmt_ts(n.timestamp if n else 0),
                    "",
                ),
            )
            self._meta[node] = {"type": "slot", "dir": str(view.path), "slot": slot, "live": live}
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
                        _fmt_ts(m.timestamp),
                        ("valid" if m.valid else "INVALID") + (" / moved" if m.moved else ""),
                    ),
                )
                self._meta[mid] = {
                    "type": "member",
                    "dir": str(view.path),
                    "slot": slot,
                    "member": 0 if m.label == "A" else 1,
                    "live": live,
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
        label = simpledialog.askstring("Backup", "Optional label:") or ""
        self._run(lambda _force: ops.create_full_backup(self.vault, self.live_dir, label=label), success="Backup created.")

    def on_restore(self) -> None:
        sel = self._selected()
        if not sel or sel.get("type") != "entry":
            messagebox.showinfo("Select a backup", "Select a catalog entry (top-level backup) to restore.")
            return
        if not self._require_live():
            return
        entry = sel["entry"]
        if not messagebox.askyesno("Restore", f"Replace the live saves with backup '{entry.id}'?\n(The current state is auto-snapshotted first.)"):
            return
        self._run(lambda force: ops.restore_full(self.vault, entry, self.live_dir, allow_game_running=force), success="Restored.")

    def on_extract(self) -> None:
        sel = self._selected()
        if not sel or sel.get("type") != "slot":
            messagebox.showinfo("Select a slot", "Select a slot (under LIVE or any backup) to extract.")
            return
        self._run(
            lambda _force: ops.extract_slot(self.vault, Path(sel["dir"]), sel["slot"], label=""),
            success=f"Extracted slot {sel['slot']}.",
        )

    def on_repopulate(self) -> None:
        sel = self._selected()
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

    def on_promote(self) -> None:
        sel = self._selected()
        if not sel or sel.get("type") != "member" or not sel.get("live"):
            messagebox.showinfo("Select a live save", "Select a save (A or B) under a LIVE slot to make it the newest.")
            return
        self._run(
            lambda force: ops.promote_member(self.vault, self.live_dir, sel["slot"], sel["member"], allow_game_running=force),
            success="Promoted.",
        )

    def on_import(self) -> None:
        folder = filedialog.askdirectory(title="Select a save-folder backup to import")
        if not folder:
            return
        copy = messagebox.askyesno("Import", "Copy the backup into the vault?\n(No = index it where it is.)")
        self._run(lambda _force: ops.import_backup(self.vault, Path(folder), copy_into_vault=copy), success="Imported.")

    def on_discover(self) -> None:
        root = locations.nms_root()
        if root is None:
            messagebox.showerror("Discover", "Could not locate the NMS root folder.")
            return
        from .core.catalog import discover_save_dirs

        exclude = [p for p in locations.find_live_save_dirs()] + [self.vault.root]
        known = {Path(e.path).resolve() for e in self.vault.entries}
        added = 0
        for d in discover_save_dirs(root, exclude=exclude):
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


def main(argv=None) -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
