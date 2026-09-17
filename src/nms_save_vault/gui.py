"""Tkinter desktop UI for NMS Save Vault.

A single tree shows the LIVE folder and every catalog backup; each entry expands to its
occupied slots, and each slot to its two saves -- the periodic Auto-Save and the
Restore-Point -- with the game-current one marked '*'. Toolbar actions cover all three
features plus promote and undo. Every write goes through the safety-wrapped core
(auto-snapshot + validate).
"""
from __future__ import annotations

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
from .core import savedir
from .core import state as appstate
from .core.catalog import Vault


def _icon_path() -> Path | None:
    """Locate nmsvault.ico - beside the packaged launcher, or in the repo's packaging/."""
    candidates = [
        Path(sys.executable).resolve().parent / "nmsvault.ico",
        Path(__file__).resolve().parents[2] / "packaging" / "nmsvault.ico",
    ]
    return next((c for c in candidates if c.is_file()), None)


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

# Tooltip colours; the theme swaps these so hover text stays readable in dark mode.
TOOLTIP_STYLE = {"background": "#ffffe0", "foreground": "#1a1a1a", "border": "#9a9a7a"}


class Tooltip:
    """Hover text for tree rows.

    ``text_for(tree, row_id)`` supplies the text (''  to show nothing). The text is built
    when the row is created, not on hover, so moving the mouse never re-scans anything.
    """

    DELAY_MS = 450

    def __init__(self, tree, text_for):
        self.tree = tree
        self.text_for = text_for
        self.window: tk.Toplevel | None = None
        self.row: str | None = None
        self._after: str | None = None
        tree.bind("<Motion>", self._on_motion, add="+")
        tree.bind("<Leave>", self.hide, add="+")
        tree.bind("<Button>", self.hide, add="+")

    def _on_motion(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row == self.row:
            return
        self.row = row
        self._close()
        self._cancel()
        if row:
            x, y = event.x_root, event.y_root
            self._after = self.tree.after(self.DELAY_MS, lambda: self._show(row, x, y))

    def _show(self, row: str, x: int, y: int) -> None:
        self._after = None
        text = self.text_for(self.tree, row)
        if not text or row != self.row:
            return
        self.window = tk.Toplevel(self.tree)
        self.window.wm_overrideredirect(True)  # no title bar / border
        tk.Label(
            self.window,
            text=text,
            justify="left",
            relief="solid",
            borderwidth=1,
            background=TOOLTIP_STYLE["background"],
            foreground=TOOLTIP_STYLE["foreground"],
            highlightbackground=TOOLTIP_STYLE["border"],
            padx=8,
            pady=6,
            font=theme.TIP_FONT,
        ).pack()
        self.window.wm_geometry(f"+{x + 16}+{y + 18}")

    def hide(self, _event=None) -> None:
        self.row = None
        self._cancel()
        self._close()

    def _cancel(self) -> None:
        if self._after is not None:
            self.tree.after_cancel(self._after)
            self._after = None

    def _close(self) -> None:
        if self.window is not None:
            self.window.destroy()
            self.window = None


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
    return f"Restore all of '{entry.id}' into live (replaces other slots)"


def _clear_question(plan, directory) -> str:
    """The confirmation text for clearing a slot: what goes, what the vault still has,
    and - when clearing would cost progress - the warning that earns the "are you sure"."""
    lines = [
        f"Delete both saves in live slot {plan.slot} of {Path(directory).name}?",
        "",
        f"Slot {plan.slot} holds: {plan.live_name}"
        f"   ({ops.format_duration(plan.live_play_time)} play time)",
    ]
    if plan.backed_up:
        lines.append(
            f"Newest copy in the vault: {plan.entry_id}"
            f"   ({ops.format_duration(plan.backup_play_time)} play time)"
        )
    if plan.warning:
        lines += ["", "WARNING: " + plan.warning]
    lines += ["", "The current state is auto-snapshotted first, so Undo can put it back."]
    return "\n".join(lines)


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


class RedactingTreeview(ttk.Treeview):
    """A tree whose row text and cell values pass through the account-alias filter.

    Filtering here rather than at each call site is what makes the guarantee structural:
    no row added now or later can leak a real account id once an alias is configured.
    """

    def insert(self, parent, index, iid=None, **kw):
        if "text" in kw:
            kw["text"] = aliases.redact(kw["text"])
        if "values" in kw:
            kw["values"] = tuple(aliases.redact(v) for v in kw["values"])
        return super().insert(parent, index, iid, **kw)


class App(tk.Tk):
    def __init__(self, live: str | Path | None = None, vault: str | Path | None = None):
        super().__init__()
        self.title(f"NMS Save Vault v{__version__}")
        self.geometry("1000x640")
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
        self._apply_theme()
        self.refresh()
        self.after(300, self._startup_update_check)  # after the window is on screen
        # Sweep up after a previous update. Deliberately late: when this run *is* the
        # update, the script that started it is still finishing its own cleanup.
        self.after(30_000, updates.clean_temp)

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

    def _build_widgets(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        for text, cmd in [
            ("Backup live", self.on_backup),
            ("Restore", self.on_restore),
            ("Extract slot", self.on_extract),
            ("Clear slot...", self.on_clear),
            ("Copy into live slot...", self.on_copy_slot),
            ("Promote", self.on_promote),
            ("Import...", self.on_import),
            ("Rescan", self.on_rescan),
            ("Discover", self.on_discover),
            ("Undo", self.on_undo),
            ("Refresh", self.refresh),
            ("Accounts...", self.on_accounts),
            ("Help", self.on_help),
        ]:
            ttk.Button(bar, text=text, command=cmd).pack(side=tk.LEFT, padx=2)

        controls = ttk.Frame(self)
        controls.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 4))

        # Active live (write target) selector -- the writable accounts (Steam + Xbox).
        self._source_choices: dict[str, str] = {}  # label -> source id
        self.active_var = tk.StringVar(value="")
        ttk.Label(controls, text="Active live:").pack(side=tk.LEFT)
        self.active_combo = ttk.Combobox(controls, textvariable=self.active_var, state="readonly", width=30)
        self.active_combo.pack(side=tk.LEFT, padx=(4, 16))
        self.active_combo.bind("<<ComboboxSelected>>", self._on_active_changed)

        self.theme_var = tk.StringVar(value=theme.CHOICE_LABELS.get(self.state.theme, "System"))
        self.theme_combo = ttk.Combobox(
            controls, textvariable=self.theme_var, state="readonly", width=8,
            values=[theme.CHOICE_LABELS[c] for c in theme.CHOICES],
        )
        self.theme_combo.pack(side=tk.RIGHT)
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_changed)
        ttk.Label(controls, text="Theme:").pack(side=tk.RIGHT, padx=(0, 4))

        self.status = tk.StringVar(value="")
        ttk.Label(
            self, textvariable=self.status, anchor="w", relief="sunken", style="Status.TLabel"
        ).pack(side=tk.BOTTOM, fill=tk.X)

        # Two independent trees, one per pane: live saves are browsed, backups are
        # searched and sorted, and mixing them in one tree made both worse.
        # Shown only when a newer release is found; packed above the panes at that point.
        self.banner = ttk.Frame(self, style="Banner.TFrame", padding=(8, 6))
        self.banner_var = tk.StringVar(value="")
        ttk.Label(self.banner, textvariable=self.banner_var, style="Banner.TLabel").pack(side=tk.LEFT)
        ttk.Button(self.banner, text="Dismiss", command=self._hide_banner).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(self.banner, text="Open download page", command=self._open_releases).pack(side=tk.RIGHT)
        # Packed only when this build can actually replace itself -- see _show_update_banner.
        self.install_button = ttk.Button(
            self.banner, text="Install update", command=self.on_install_update
        )

        panes = ttk.PanedWindow(self, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
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
        # Which tree the toolbar acts on: whichever the user last selected in.
        self._active_tree = self.live_tree
        # Backups start newest-first; clicking a heading re-sorts.
        self._sort_key: str = "saved"
        self._sort_reverse: bool = True

        for tree in (self.live_tree, self.backup_tree):
            tree.bind("<Button-3>", self._on_right_click)
            tree.bind("<<TreeviewSelect>>", self._on_tree_select)
            Tooltip(tree, self._tip_for_row)

        # Both spellings of each key: Ctrl+= and Ctrl+- are what those keys report
        # unshifted, Ctrl++ and Ctrl+_ what they report with Shift held, plus the keypad.
        for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            self.bind_all(seq, lambda _e: self._zoom(1))
        for seq in ("<Control-minus>", "<Control-underscore>", "<Control-KP_Subtract>"):
            self.bind_all(seq, lambda _e: self._zoom(-1))

    # --- theme ---------------------------------------------------------------

    def _apply_theme(self, *, persist: bool = False) -> None:
        """Restyle everything for the current choice and re-colour the row tags.

        Tag colours cannot live in the ttk style, so they are re-applied here: the
        light-mode green and amber are unreadable on a dark tree background.
        """
        self.state.font_scale = theme.apply_font_scale(self, self.state.font_scale)
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
        if persist:
            try:
                appstate.save(self.state)
            except OSError as exc:
                _showwarning("Theme", f"Could not save your theme choice:\n{exc}")

    def _zoom(self, steps: int) -> None:
        """Ctrl+/Ctrl-: step the font zoom; does nothing at the ends of the range.

        Saved quietly: a keystroke should not raise a modal because the install folder
        is read-only, and the theme dropdown already reports that failure loudly.
        """
        wanted = theme.clamp_font_scale(
            self.state.font_scale + steps * theme.FONT_SCALE_STEP
        )
        if wanted == self.state.font_scale:
            return
        self.state.font_scale = wanted
        self._apply_theme()
        self._save_state_quietly()

    # --- update checking -----------------------------------------------------

    def _save_state_quietly(self) -> None:
        """Persist preferences. A read-only install dir is not worth a modal at startup;
        the setting simply will not stick, and the user is told if they try to change it
        from the theme dropdown (which reports the same failure loudly)."""
        try:
            appstate.save(self.state)
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
            self.install_button.pack(side=tk.RIGHT, padx=(0, 6))
        else:
            self.install_button.pack_forget()
        self.banner.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 4), before=self._panes)

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
        label = ttk.Label(win, text=f"Contacting GitHub for version {version}...", padding=(24, 18, 24, 8))
        label.pack()
        bar = ttk.Progressbar(win, mode="indeterminate", length=320)
        bar.pack(padx=24, pady=(0, 20))
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
            side=tk.TOP, fill=tk.X, pady=(0, 2)
        )
        body = ttk.Frame(frame)
        body.pack(fill=tk.BOTH, expand=True)
        tree = RedactingTreeview(body, columns=[c.key for c in columns], show="tree headings")
        tree.heading("#0", text=tree_heading)
        tree.column("#0", width=300, anchor="w")
        for col in columns:
            if sortable:
                tree.heading(col.key, text=col.title,
                             command=lambda k=col.key: self._sort_backups(k))
            else:
                tree.heading(col.key, text=col.title)
            tree.column(col.key, width=col.width, anchor="w")
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

        # --- BACKUPS pane: catalog entries, sortable by any column ------------
        for e in self.vault.entries:
            node = self.backup_tree.insert(
                "", "end", text=e.id,
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
            self._remember(self.backup_tree, node, {"type": "entry", "entry": e, "tip": self._entry_tip(e)})
            self._add_entry(node, e)
        self._apply_backup_sort()

        self._refresh_source_combo()
        running = ops.safety.is_game_running()
        game = {True: "RUNNING (writes blocked)", False: "closed", None: "unknown"}[running]
        active = self._active_source()
        active_txt = active.label if active else (str(self.live_dir) if self.live_dir else "none")
        self.status.set(
            aliases.redact(
                f"Active live: {active_txt}   |   Sources: {len(sources)}   |   "
                f"Vault: {self.vault.root}   |   Game: {game}"
            )
        )

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

    def _add_entry(self, parent: str, entry) -> None:
        for s in entry.slots:
            if not s.occupied:
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
            menu.add_command(label="Undo the last change to live saves", command=self.on_undo)
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
                label=f"Clear live slot {slot} (delete its saves)...",
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

    # --- actions -------------------------------------------------------------

    def _run(self, fn, *, success: str, message: str = "Working - please wait...") -> None:
        """Run a core operation on a worker thread behind a modal wait dialog, prompting to
        override the game-running guard if the op reports the game is open."""
        holder = self._run_worker(fn, False, message)
        if isinstance(holder.get("error"), ops.GameRunningError):
            if not _askyesno("Game running", "No Man's Sky appears to be running.\nProceed anyway (risky)?"):
                return
            holder = self._run_worker(fn, True, message)

        err = holder.get("error")
        if isinstance(err, ops.FeatureNotYetAvailableError):
            _showinfo("Coming soon", str(err))
            return
        if isinstance(err, ops.OperationError):
            _showerror("Operation failed", str(err))
            return
        if err is not None:
            _showerror("Unexpected error", repr(err))
            return
        self.refresh()
        self._present_result(holder.get("result"), success)

    def _present_result(self, result, success: str) -> None:
        if isinstance(result, ops.OpResult):
            msg = result.detail + (f"\n\nUndo available (snapshot {result.snapshot_id})." if result.snapshot_id else "")
            if result.warnings:
                msg += "\n\nWarnings:\n - " + "\n - ".join(result.warnings)
            _showinfo("Done", msg)
        else:
            _showinfo("Done", success)

    def _busy(self, message: str) -> tk.Toplevel:
        """A small modal 'please wait' window with an animated indeterminate bar."""
        win = tk.Toplevel(self)
        win.title("Please wait")
        win.transient(self)
        win.resizable(False, False)
        ttk.Label(win, text=message, padding=(24, 18, 24, 8)).pack()
        pb = ttk.Progressbar(win, mode="indeterminate", length=260)
        pb.pack(padx=24, pady=(0, 20))
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
        self._run(
            lambda _force: ops.create_full_backup(self.vault, Path(directory), label=label),
            success="Backup created.",
            message="Creating backup - please wait...",
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
        if _extract_slot_number(entry) is not None:
            # An extract is one slot lifted aside, not a whole folder: it goes back where
            # it came from, and nothing else in the live folder is touched.
            question = (
                f"Put slot {slots[0]} back into live slot {slots[0]}, from '{entry.id}'?\n\n"
                f"Only slot {slots[0]} is overwritten - every other save is left alone.\n"
                "The current state is auto-snapshotted first."
            )
        else:
            question = (
                f"Replace the live saves with backup '{entry.id}'?\n\n"
                f"The backup holds {len(slots)} slot(s): {', '.join(map(str, slots)) or 'none'}.\n"
                "Any live save NOT in the backup is REMOVED.\n"
                "The current state is auto-snapshotted first."
            )
        if not _askyesno("Restore", question):
            return
        self._run(
            lambda force: ops.restore_entry(self.vault, entry, self.live_dir, allow_game_running=force),
            success="Restored.",
            message="Restoring - please wait...",
        )

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
        self._run(
            lambda _force: ops.extract_slot(self.vault, Path(sel["dir"]), sel["slot"], label=""),
            success=f"Extracted slot {sel['slot']}.",
            message=f"Extracting slot {sel['slot']} - please wait...",
        )

    def on_clear(self, target: dict | None = None) -> None:
        """Empty a live slot, after showing what the vault does (or does not) still hold."""
        sel = target or self._selected()
        if not sel or sel.get("type") not in ("slot", "member") or not sel.get("live"):
            _showinfo(
                "Select a live slot",
                "Clear empties one of your LIVE slots, so it needs a live one: select a "
                "slot - or either of the two saves inside it - in the LIVE SAVES pane.",
            )
            return
        directory, slot = Path(sel["dir"]), sel["slot"]
        plan = ops.plan_clear_slot(self.vault, directory, slot)
        if not plan.occupied:
            _showinfo("Already empty", f"Live slot {slot} holds no saves.")
            return
        if not _askyesno("Clear slot", _clear_question(plan, directory)):
            return
        self._run(
            lambda force: ops.clear_slot(self.vault, directory, slot, allow_game_running=force),
            success=f"Cleared slot {slot}.",
            message=f"Clearing slot {slot} - please wait...",
        )

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
                f"Live slot {dest_slot} currently holds: {occupant or 'nothing'}\n"
                "Both saves in the slot are copied, and the current state is "
                "auto-snapshotted first.",
            ):
                return

        src_folder, dest_folder, slot = Path(source.folder), Path(dest_dir), dest_slot
        self._run(
            lambda force: ops.repopulate_slot(
                self.vault, src_folder, source.slot, dest_folder, slot, allow_game_running=force
            ),
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
        self._run(
            lambda force: ops.promote_member(self.vault, self.live_dir, sel["slot"], sel["member"], allow_game_running=force),
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
        self._run(
            lambda _force: ops.import_backup(self.vault, directory, copy_into_vault=copy),
            success="Imported.",
            message="Importing - please wait...",
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
            appstate.save(self.state)
        except OSError as exc:
            _showwarning("Rescan", f"Found sources but could not save config:\n{exc}")
        # If we had no active writable source yet, adopt the first one found.
        if self.active_source_id is None:
            writable = self._writable_sources()
            if writable:
                self.active_source_id = writable[0].id
                self.live_dir = Path(writable[0].path)
        self.refresh()
        _showinfo(
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
        _showinfo("Discover", f"Added {added} newly found backup(s) to the catalog.")

    def on_undo(self) -> None:
        if not self._require_live():
            return
        if not _askyesno("Undo", "Undo the last operation by restoring its auto-snapshot?"):
            return
        self._run(
            lambda force: ops.undo_last(self.vault, self.live_dir, allow_game_running=force),
            success="Undone.",
            message="Undoing - please wait...",
        )

    def on_accounts(self) -> None:
        try:
            AccountsDialog(self)
        except aliases.AliasConfigError as exc:
            messagebox.showerror(  # raw: the alias file is what failed to parse
                "Account display names",
                f"Could not read the account display names.\n\nFix or delete this file:\n{exc}",
            )

    def on_help(self) -> None:
        win = tk.Toplevel(self)
        win.title("NMS Save Vault - Help")
        win.geometry("760x620")
        ttk.Button(win, text="Close", command=win.destroy).pack(side=tk.BOTTOM, pady=6)
        vsb = ttk.Scrollbar(win, orient="vertical")
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        p = theme.palette(self.state.theme)  # tk.Text is a classic widget: no ttk style
        win.configure(background=p["bg"])
        txt = tk.Text(
            win, wrap="word", padx=10, pady=10, yscrollcommand=vsb.set,
            background=p["field"], foreground=p["fg"], insertbackground=p["fg"],
            relief="flat",
        )
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.config(command=txt.yview)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")


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
            padding=(14, 12, 14, 8),
            justify="left",
            text=(
                "Show a name of your choosing instead of the real account id.\n"
                "Once a name is set, the app shows only that name -- in folder names,\n"
                "paths, labels and messages. This is the only place the real ids appear.\n"
                "Clear a name to go back to showing the real id."
            ),
        ).pack(anchor="w")

        grid = ttk.Frame(self, padding=(14, 0, 14, 8))
        grid.pack(fill=tk.X)
        accounts = self._known_accounts()
        if not accounts:
            ttk.Label(grid, text="No accounts found yet -- use Rescan first.").grid(sticky="w")
        for column, heading in enumerate(("Platform", "Real account id", "Shows as")):
            ttk.Label(grid, text=heading, font=theme.BOLD_FONT).grid(
                row=0, column=column, sticky="w", padx=(0, 10), pady=(0, 4)
            )
        for row, (platform, account) in enumerate(accounts, start=1):
            ttk.Label(grid, text=platform).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            real = ttk.Entry(grid, width=52)  # fits a 49-char Xbox <xuid>_<titleid> whole
            real.insert(0, account)
            real.configure(state="readonly")  # selectable so it can be copied, not edited
            real.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=2)
            var = tk.StringVar(value=self.amap.alias_for(account))
            self._vars[account] = var
            ttk.Entry(grid, textvariable=var, width=26).grid(row=row, column=2, sticky="w", pady=2)

        buttons = ttk.Frame(self, padding=(14, 0, 14, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Save", command=self._save).pack(side=tk.RIGHT, padx=6)

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
        self.geometry("760x460")

        ttk.Label(
            self, padding=(14, 12, 14, 6), justify="left",
            text="Pick the slot you want to copy. Both of its saves (the Auto-Save and the\n"
                 "Restore-Point) are copied together.",
        ).pack(anchor="w")

        body = ttk.Frame(self, padding=(14, 0, 14, 8))
        body.pack(fill=tk.BOTH, expand=True)
        self.tree = RedactingTreeview(body, columns=("name", "saved"), show="tree headings")
        self.tree.heading("#0", text="Folder / Slot")
        self.tree.column("#0", width=330, anchor="w")
        self.tree.heading("name", text="Save name")
        self.tree.column("name", width=230, anchor="w")
        self.tree.heading("saved", text="Saved")
        self.tree.column("saved", width=130, anchor="w")
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda _e: self._accept())

        self._sources: dict[str, SlotSource] = {}
        self._populate()

        buttons = ttk.Frame(self, padding=(14, 0, 14, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Use this slot", command=self._accept).pack(side=tk.RIGHT, padx=6)

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
        self.geometry("700x540")

        self._targets = [s for s in app.state.live_sources if s.writable and s.exists]
        if not self._targets:
            self.destroy()
            _showerror("No writable live folder", "There is no live save folder to copy into.")
            return

        frame = ttk.LabelFrame(self, text=" Copy from ", padding=(12, 8))
        frame.pack(fill=tk.X, padx=14, pady=(12, 6))
        self.source_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.source_var, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(frame, text="Change...", command=self._choose_source).pack(side=tk.RIGHT)

        frame = ttk.LabelFrame(self, text=" Into this live slot ", padding=(12, 8))
        frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=6)
        picker = ttk.Frame(frame)
        picker.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(picker, text="Live folder:").pack(side=tk.LEFT)
        self._target_labels = {_source_caption(s): s for s in self._targets}
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(
            picker, textvariable=self.target_var, state="readonly",
            values=list(self._target_labels), width=40,
        )
        self.target_combo.pack(side=tk.LEFT, padx=6)
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_slots())
        if len(self._targets) == 1:
            self.target_combo.configure(state="disabled")

        holder = ttk.Frame(frame)
        holder.pack(fill=tk.BOTH, expand=True)
        self.slots = RedactingTreeview(
            holder, columns=("name", "saved"), show="tree headings", height=12
        )
        self.slots.heading("#0", text="Slot")
        self.slots.column("#0", width=70, anchor="w")
        self.slots.heading("name", text="What is in it now")
        self.slots.column("name", width=330, anchor="w")
        self.slots.heading("saved", text="Saved")
        self.slots.column("saved", width=130, anchor="w")
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.slots.yview)
        self.slots.configure(yscrollcommand=vsb.set)
        self.slots.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.slots.bind("<Double-1>", lambda _e: self._accept())

        ttk.Label(
            self, padding=(14, 0, 14, 6), justify="left",
            text="Both saves in the slot are copied. Whatever is in the destination slot is\n"
                 "replaced - the live folder is auto-snapshotted first, so Undo puts it back.",
        ).pack(anchor="w")

        buttons = ttk.Frame(self, padding=(14, 0, 14, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Copy", command=self._accept).pack(side=tk.RIGHT, padx=6)

        chosen = next(
            (s for s in self._targets if dest_dir and Path(s.path) == Path(dest_dir)),
            self._targets[0],
        )
        self.target_var.set(_source_caption(chosen))
        self._reload_slots(select=dest_slot or (source.slot if source else None))
        self._show_source()

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
    * a full backup / snapshot / imported folder REPLACES the live folder -- any live
      save not in the backup is removed;
    * a single-slot extract goes back into ITS OWN slot and nothing else is touched.
  Either way the current state is auto-snapshotted first, so it is reversible with Undo.
- Extract slot: Select a slot -- or either of the two saves inside it -- under LIVE or a
  backup, to copy just that one slot aside into the vault. You can free the slot now and
  bring it back later.
- Clear slot: Empties a live slot by deleting both of its saves. Before it does, it
  shows the slot's play time next to the play time of the newest copy of that slot in
  the vault, and if the live save is further on - or if the vault has no copy at all -
  it says so and asks again, because that progress is what you would lose. The live
  state is auto-snapshotted first, so Undo brings the slot back.
- Copy into live slot: Copies one slot's saves into a live slot. It opens a window
  showing BOTH ends: what is being copied, and every live slot with what is currently in
  it, so you can see what you are about to replace. Either end can be changed there, so
  it does not matter which way round you were thinking:
    * select a slot in a backup and it is the source -- pick where it lands;
    * select a live slot and it is the destination -- press Change... to pick what fills it.
  The save data is copied exactly and the small meta is re-keyed for the new slot number.
- Promote: Select one of a live slot's two saves (Auto-Save or Restore-Point) to force it
  to be the newest, so the game loads it instead of the other -- e.g. to roll back to the
  Restore-Point.
- Import: Register an existing save folder you made yourself (or an Xbox / Game Pass
  save) into the catalog -- either in place or copied into the vault. You can also point
  Import at a whole copied Save Vault folder: it compares that vault's entries with yours
  and offers to copy the new ones in or index them in place (re-importing is harmless).
- Rescan: Re-detect live save folders (new Steam account, new Xbox account) and add them
  to the LIVE SAVES list. Your manual entries are left untouched.
- Discover: Scan the NMS folder for existing backups and add any new ones found.
- Undo: Restore the auto-snapshot taken just before the last change to your live saves.
  Only actions that WRITE into the live folder can be undone -- Backup, Extract and
  Import merely add to the vault, so after one of those there is nothing to put back.
- Refresh: Re-scan the live folder and the catalog.
- Theme (top right): Light, Dark, or System, which follows your Windows light/dark
  setting. Your choice is remembered in the config.
- Accounts: Give each account a display name to show instead of its real id (the Steam
  st_<steamid64> folder, the Xbox <xuid>_<titleid> folder). Once a name is set, the app
  shows only that name everywhere -- folder names, paths, labels, messages -- and that
  dialog is the only place the real ids still appear. The names are stored in
  accounts.ini next to state.json; clearing a name shows the real id again.
- Help: This dialog.

KEYBOARD
- Ctrl+plus / Ctrl+minus make every font in the window bigger or smaller, from 70% to
  200% of your system's own size. The tree rows grow with the text, and the size is
  remembered in the config just like the theme is.

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

WORKFLOWS
- More than 15 slots: Extract the slots you are not using into the vault, Clear them to
  free the live slot, then copy them back into a live slot whenever you want to play them
  again. Your library is unlimited; only 15 are live at a time.
- Safe experimenting: Backup live (or Extract the slot), make changes in-game, then
  Restore / copy back / Undo if you do not like the result.
- Move a save to another slot: right-click it and Copy into a live slot.
- Roll back within a slot: expand the live slot, right-click the save you want (usually
  the older one, or the Restore-Point), Promote it.
- Bring in an outside save: Import the folder, then copy the slot you want into live.

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
"""


def main(argv=None) -> int:
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
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
