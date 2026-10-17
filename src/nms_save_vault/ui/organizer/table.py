"""The organizer's table of save files (D6 2.2, 3.1).

One row per save file (a D1 ``Line``) with a ``>`` / ``v`` marker drawn in code; opening a
line shows its versions under it and mutes every other line (owner review). Filter chips
(groups combine with AND, chips in a group with OR), a Find box, sortable headings with an
arrow and a column chooser; the sort, the chips and the columns are kept in the ``ui``
preferences.

A new read model updates the rows in place, keyed by save file and version: what is open,
selected and scrolled to stays (D6 3.1.2). Tk 8.6 allows images only in column #0, so every
status is a word (D6 4.3: one adapter to change when Tk 9 brings per-cell tags).
"""
from __future__ import annotations

import tkinter as tk
from collections import Counter
from collections.abc import Callable, Collection, Iterable, MutableMapping
from dataclasses import dataclass
from tkinter import ttk

from ...core.organizer.readmodel import (
    Line,
    LineFilter,
    ReadModel,
    VersionRow,
    filter_lines,
    sort_lines,
)
from ... import theme
from .. import dpi, raster
from ..dpi import px, pxs
from ..fmt import local_time, play_time
from ..redact import RedactingTreeview
from ..themeaware import ThemeRegistry
from ..tooltip import Tooltip
from .words import line_name, version_numbers

STYLE = "Organizer.Treeview"
FIND_DELAY_MS = 150
EMPTY_VAULT = "No save files yet - File > Back up live folder now, or Import a backup folder"


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    width: int              # design pixels
    anchor: str
    sort: str | None        # the read model's sort key; None: not sortable
    default: bool = True


NAME = Column("#0", "Save file", 250, "w", "name")
COLUMNS = (
    Column("live", "Live", 58, "w", "live"),
    Column("versions", "Vers.", 40, "e", "versions"),
    Column("play", "Play time", 76, "e", "play_time"),
    Column("last", "Last played", 112, "w", "last_played"),
    Column("mode", "Mode", 72, "w", "difficulty"),
    Column("state", "State", 126, "w", "risk"),
    Column("platforms", "Platforms", 90, "w", "platform", default=False),
    Column("branches", "Branches", 64, "e", None, default=False),
    Column("note", "Note", 280, "w", "note"),
)
BY_KEY = {c.key: c for c in (NAME, *COLUMNS)}
DEFAULT_COLUMNS = tuple(c.key for c in COLUMNS if c.default)


@dataclass(frozen=True)
class Chip:
    name: str
    group: str
    label: str
    filter: LineFilter


CHIPS = (
    Chip("live", "source", "Live", LineFilter(live=True)),
    Chip("vault", "source", "Vault only", LineFilter(live=False)),
    Chip("steam", "platform", "Steam", LineFilter(platform="steam")),
    Chip("xbox", "platform", "Xbox", LineFilter(platform="xbox")),
    Chip("both", "platform", "On both", LineFilter(cross_platform=True)),
    Chip("note", "flags", "Has note", LineFilter(has_note=True)),
    Chip("pinned", "flags", "Pinned", LineFilter(pinned=True)),
    Chip("risk", "flags", "Not in vault", LineFilter(at_risk=True)),
    Chip("expedition", "flags", "Expedition", LineFilter(expedition=True)),
)
CHIP = {c.name: c for c in CHIPS}


# --- what a row says (pure) ----------------------------------------------------------


def state_words(line: Line) -> str:
    """The State column (D6 2.2): words, because a Tk 8.6 cell cannot be coloured."""
    words = []
    if "at-risk" in line.flags:
        words.append("NOT IN VAULT")
    tips = {t.platform: t for t in line.platform_tips}
    if line.cross_platform == "xbox-behind" and "xbox" in tips:
        words.append(f"Xbox behind {play_time(tips['xbox'].behind_by)}")
    elif line.cross_platform == "steam-behind" and "steam" in tips:
        words.append(f"Steam behind {play_time(tips['steam'].behind_by)}")
    elif line.cross_platform == "diverged":
        words.append("two branches S/X")
    if not line.live:
        words.append("vault only")
    if "unreadable" in line.flags:
        words.append("unreadable")
    if "edited" in line.flags:
        words.append("edited")
    if line.pinned:
        words.append("pinned")
    return "; ".join(words)


def _mode(mode: str, difficulty: str) -> str:
    return mode.replace("Expedition", "Exped.") if mode else difficulty


def line_values(line: Line) -> dict[str, str]:
    return {
        "live": " ".join(b.label for b in line.live) or "vault",
        "versions": str(line.version_count),
        "play": play_time(line.play_time),
        "last": local_time(line.last_played),
        "mode": _mode(line.mode, line.difficulty),
        "state": state_words(line),
        "platforms": ", ".join("Steam" if p == "steam" else "Xbox" for p in line.platforms),
        "branches": str(line.branch_count) if line.branch_count else "",
        "note": line.note_preview,
    }


def version_values(row: VersionRow, live_labels: dict) -> dict[str, str]:
    """A version row's cells (D6 2.3); its own words in State."""
    words = []
    if row.durable_copies == 0 and "gone" not in row.flags:
        words.append("only outside the vault" if "outside-only" in row.flags
                     else "NOT IN VAULT")
    if "fork" in row.flags:
        words.append("branch point")
    elif "tip" in row.flags and "live" not in row.flags:
        words.append("branch tip")
    if "edited" in row.flags:
        words.append("edited outside game")
    if "unreadable" in row.flags:
        words.append("unreadable")
    if "gone" in row.flags:
        words.append("no copy left")
    if row.locked:
        words.append("locked")
    live = " ".join(live_labels.get(t, "") for t in row.live_in).strip()
    return {
        "live": live or ("vault" if row.durable_copies else ""),
        "versions": "",
        "play": play_time(row.play_time),
        "last": local_time(row.saved_at),
        "mode": _mode(row.mode, row.difficulty),
        "state": "; ".join(words),
        "platforms": "Steam" if row.platform == "steam" else "Xbox",
        "branches": "",
        "note": row.label or next(iter(row.comment.splitlines()), ""),
    }


@dataclass(frozen=True)
class RowSpec:
    """One row under an open line: ``key`` is stable across models (a version by its head),
    so a new model updates the row in place and a selected version stays selected."""
    key: tuple
    text: str
    values: tuple
    tags: tuple
    image: object = ""          # a key into the table's images
    head: str | None = None     # the version the row is


def _dot(row: VersionRow, linked: bool) -> tuple[str, str]:
    """A live version: a disc in its platform colour; a linked one: the main lane's colour;
    one listed by date: grey (lanes arrive with the history view, phase 4)."""
    if row.live_in:
        return "live", row.platform
    return "dot", "lane0" if linked else "muted"


def _version_spec(row: VersionRow, number: int, folded: int, live_labels: dict,
                  linked: bool) -> RowSpec:
    values = version_values(row, live_labels)
    if folded:
        marker = f"+{folded} re-save{'s' if folded > 1 else ''} folded"
        values["state"] = f"{values['state']}; {marker}" if values["state"] else marker
    return RowSpec(("v", row.head), f"#{number}", tuple(values[c.key] for c in COLUMNS),
                   ("ver",), _dot(row, linked), row.head)


def version_specs(model: ReadModel, pid: str, live_labels: dict) -> list[RowSpec]:
    """The rows of an open line (D6 2.3): first the linked history - versions joined by a
    move the app recorded, by the live journal or by consecutive app backups - newest
    first; then, under a separator, every other version by date, nothing guessed. Re-saves
    without play fold into the version they repeat ("+1 re-save folded")."""
    rows = model.versions(pid)
    if not rows:
        return [RowSpec(("none",), "(no versions)", (), ("muted",))]
    numbers = version_numbers(model, pid)
    folded = Counter(r.folded_into for r in rows if r.folded_into)
    shown = [r for r in rows if not r.folded_into]
    linked = [r for r in shown if r.in_tree]
    dated = [r for r in shown if not r.in_tree]
    out = [_version_spec(r, numbers[r.head], folded[r.head], live_labels, True)
           for r in linked]
    if dated:
        count = f"{len(dated)} version{'s' if len(dated) != 1 else ''}"
        note = {c.key: "" for c in COLUMNS}
        note["note"] = "no recorded move or app backup links them; no links are guessed"
        out.append(RowSpec(("sep",), f"Not linked, by date - {count}",
                           tuple(note[c.key] for c in COLUMNS), ("sep",)))
        out += [_version_spec(r, numbers[r.head], folded[r.head], live_labels, False)
                for r in dated]
    return out


def chip_counts(lines: Iterable[Line]) -> dict[str, int]:
    """Every chip's count, from the unfiltered lines (D6 2.2)."""
    lines = tuple(lines)
    counts = {c.name: len(filter_lines(lines, c.filter)) for c in CHIPS}
    counts["all"] = len(lines)
    return counts


def visible_lines(lines: Iterable[Line], chips: Collection[str], text: str,
                  sort_key: str = "last_played", descending: bool | None = None,
                  model: ReadModel | None = None) -> tuple[Line, ...]:
    """The lines the table shows: chips of one group OR'ed, groups AND'ed, then Find
    (name, former names, the note; with ``model`` the whole note and every version's label
    and comment too), then sorted (D1 ``sort_lines``)."""
    shown = tuple(lines)
    groups: dict[str, list[Chip]] = {}
    for name in chips:
        if name in CHIP:
            groups.setdefault(CHIP[name].group, []).append(CHIP[name])
    for members in groups.values():
        keep = {line.pid for chip in members for line in filter_lines(shown, chip.filter)}
        shown = tuple(line for line in shown if line.pid in keep)
    needle = text.strip()
    if needle:
        found = {line.pid for line in filter_lines(shown, LineFilter(text=needle))}
        if model is not None:
            found |= {line.pid for line in shown if _mentions(model, line.pid,
                                                              needle.casefold())}
        shown = tuple(line for line in shown if line.pid in found)
    return sort_lines(shown, sort_key, descending)


def _mentions(model: ReadModel, pid: str, needle: str) -> bool:
    note = model.notes.save_files.get(pid)
    if note is not None and needle in note.text.casefold():
        return True
    return any(needle in f"{row.label}\n{row.comment}".casefold()
               for row in model.versions(pid))


def line_tip(line: Line) -> str:
    """The hover text of a line, built when the pointer rests on it (D6 3.1.7)."""
    out = [line_name(line)]
    if line.former_names:
        out.append("Also called: " + ", ".join(line.former_names[:5]))
    if line.live:
        out.append("Live in: " + ", ".join(b.label for b in line.live))
    else:
        out.append("Not in any live slot: kept in the vault only.")
    if "at-risk" in line.flags:
        out.append("NOT IN VAULT: a live save with no copy in the vault yet.")
    state = state_words(line)
    if "behind" in state:
        out.append("The copy with less play time is behind; each platform's copy is its "
                   "own tip.")
    if line.note_preview:
        out.append("Note: " + line.note_preview)
    out.append(f"{line.version_count} versions, {play_time(line.play_time) or '0h00'} played")
    return "\n".join(out)


def version_tip(row: VersionRow) -> str:
    out = [f"{row.name or '(unnamed)'} - saved {local_time(row.saved_at)}"]
    out += [loc.label for loc in row.locations[:6]]
    out.append("Comes from: no recorded parent - listed by date" if row.parent is None
               else "Comes from an earlier version (linked)")
    if row.label:
        out.append(f"Label: {row.label}")
    return "\n".join(out)


# --- the widget ------------------------------------------------------------------------


class OrganizerTable(ttk.Frame):
    """``prefs`` is the ``ui`` preferences dict (kept by the caller, saved through
    ``save_prefs``); ``on_select(pid, head)`` reports the selected line (head None) or
    version."""

    def __init__(self, master, *, themed: ThemeRegistry, prefs: MutableMapping,
                 save_prefs: Callable[[], None],
                 on_select: Callable[[str | None, str | None], None] | None = None) -> None:
        super().__init__(master)
        self.prefs = prefs
        self.save_prefs = save_prefs
        self.on_select = on_select
        self.model: ReadModel | None = None
        self.lines: tuple[Line, ...] = ()
        self.shown: tuple[Line, ...] = ()
        self.open_pids: set[str] = set()
        self._iid_of: dict[str, str] = {}           # pid -> line iid
        self._pid_of: dict[str, str] = {}           # line iid -> pid
        self._version_of: dict[str, tuple[str, str]] = {}   # version iid -> (pid, head)
        self._values: dict[str, tuple] = {}
        self._next = 0
        self._find_after: str | None = None
        self._images: dict = {}
        self._palette: dict = {}
        self._child_iids: dict[str, dict[tuple, str]] = {}   # pid -> row key -> iid
        self._factor = dpi.factor()                  # the scale the widths were set at
        sort = prefs.get("table_sort") or ["last_played", True]
        self.sort_key, self.descending = str(sort[0]), bool(sort[1])
        self.chips: set[str] = {c for c in prefs.get("table_chips", []) if c in CHIP}
        self._build()
        themed.register(self)

    # --- building -----------------------------------------------------------------

    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side=tk.TOP, fill=tk.X, pady=pxs(0, 4))
        self.chip_vars: dict[str, tk.BooleanVar] = {}
        self.chip_buttons: dict[str, tk.Checkbutton] = {}
        self.all_var = tk.BooleanVar(value=not self.chips)
        self.chip_buttons["all"] = self._chip(bar, "all", self.all_var, self._chip_all)
        group = "source"
        for chip in CHIPS:
            if chip.group != group:
                ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y,
                                                            padx=px(4), pady=px(2))
                group = chip.group
            var = tk.BooleanVar(value=chip.name in self.chips)
            self.chip_vars[chip.name] = var
            self.chip_buttons[chip.name] = self._chip(
                bar, chip.name, var, lambda n=chip.name: self._chip_toggled(n))
        ttk.Button(bar, text="Columns...", command=self.choose_columns).pack(side=tk.RIGHT)
        self.find_var = tk.StringVar(value="")
        self.find = ttk.Entry(bar, textvariable=self.find_var, width=22)
        self.find.pack(side=tk.RIGHT, padx=pxs(8, 6))
        ttk.Label(bar, text="Find:").pack(side=tk.RIGHT)
        self.find_var.trace_add("write", lambda *_: self._find_changed())

        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)
        keys = [c.key for c in COLUMNS]
        self.tree = RedactingTreeview(body, columns=keys, show="tree headings",
                                      selectmode="extended", style=STYLE)
        self.tree.heading("#0", text=NAME.title, command=lambda: self.sort_by(NAME.key))
        widths = self.prefs.get("table_widths") or {}
        self.tree.column("#0", width=px(widths.get("#0", NAME.width)), anchor=NAME.anchor,
                         stretch=False)
        for col in COLUMNS:
            self.tree.heading(col.key, text=col.title,
                              command=(lambda c=col: self.sort_by(c.key)) if col.sort else "")
            self.tree.column(col.key, width=px(widths.get(col.key, col.width)),
                             anchor=col.anchor, stretch=col.key == "note")
        self.set_columns(self.prefs.get("table_columns") or DEFAULT_COLUMNS, save=False)
        vsb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(body, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.tree.bind("<<TreeviewOpen>>", lambda _e: self._opened(self.tree.focus(), True))
        self.tree.bind("<<TreeviewClose>>", lambda _e: self._opened(self.tree.focus(), False))
        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<ButtonRelease-1>", self._remember_widths, add="+")
        self._dragging = False
        self.tree.bind("<ButtonPress-1>", lambda e: setattr(
            self, "_dragging", self.tree.identify_region(e.x, e.y) == "separator"), add="+")
        self.tooltip = Tooltip(self.tree, self._tip)

    def _chip(self, parent, name: str, var: tk.BooleanVar, command) -> tk.Checkbutton:
        # A classic Checkbutton without its indicator: the vista Toolbutton style draws an
        # unselected chip as plain text (D6 3.12, verified).
        button = tk.Checkbutton(parent, text=name, variable=var, indicatoron=False,
                                command=command, relief="flat", overrelief="raised",
                                borderwidth=1, padx=px(8), pady=px(2), offrelief="flat")
        button.pack(side=tk.LEFT, padx=pxs(0, 3))
        return button

    # --- theme --------------------------------------------------------------------

    def apply_palette(self, p) -> None:
        """Re-colour the chips and the row tags; draw the markers again at this size."""
        self._palette = dict(p)
        style = ttk.Style(self)
        style.layout(f"{STYLE}.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.focus", {"side": "left", "sticky": "", "children": [
                ("Treeitem.text", {"side": "left", "sticky": ""})]})]})])
        for button in self.chip_buttons.values():
            button.configure(background=p["btn_bg"], foreground=p["fg"],
                             selectcolor=p["chip_on"], activebackground=p["hover_bg"],
                             activeforeground=p["fg"], highlightbackground=p["bg"],
                             disabledforeground=p["disabled_fg"])
        tree = self.tree
        tree.tag_configure("odd", background=p["zebra"])
        tree.tag_configure("ver", background=p["group_bg"])
        tree.tag_configure("dim", foreground=p["dim"])
        tree.tag_configure("sep", foreground=p["muted"], background=p["group_bg"],
                           font=theme.ITALIC_FONT)
        tree.tag_configure("muted", foreground=p["muted"])
        tree.tag_configure("risk", foreground=p["warn"])
        self._draw_images()

    def _row_height(self) -> int:
        try:
            return int(ttk.Style(self).lookup("Treeview", "rowheight")) or px(24)
        except (tk.TclError, ValueError):
            return px(24)

    def _draw_images(self) -> None:
        if not self._palette:
            return
        fg, h, scale = self._palette["fg"], self._row_height(), dpi.factor()
        w = px(18)
        self._images = {
            "closed": raster.chevron(w, h, open_=False, colour=fg, scale=scale).image(self),
            "open": raster.chevron(w, h, open_=True, colour=fg, scale=scale).image(self),
            "down": raster.sort_arrow(px(10), px(6), down=True, colour=fg).image(self),
            "up": raster.sort_arrow(px(10), px(6), down=False, colour=fg).image(self),
        }
        self._images.update(self._dot_images(h, scale))
        for pid, iid in self._iid_of.items():
            self.tree.item(iid, image=self._images["open" if pid in self.open_pids
                                                   else "closed"])
        self._show_sort_arrow()
        for children in self._child_iids.values():
            for iid in children.values():
                spec = self._values.get(iid)
                if isinstance(spec, RowSpec):
                    self.tree.item(iid, image=self._images.get(spec.image, ""))

    def _dot_images(self, h: int, scale: float) -> dict:
        p = self._palette
        out = {}
        for kind in raster.DOT_KINDS:
            for colour in ("muted", "steam", "xbox", "lane0"):
                out[(kind, colour)] = raster.version_dot(
                    px(18), h, kind, colour=p[colour], scale=scale).image(self)
        return out

    # --- the model ----------------------------------------------------------------

    def set_model(self, model: ReadModel | None) -> None:
        """Show a new read model, updating rows in place (open lines, selection and
        scroll position are kept)."""
        self.model = model
        self.lines = model.lines() if model is not None else ()
        self.open_pids &= {line.pid for line in self.lines}
        self._update_chip_labels()
        self.refresh()

    def refresh(self) -> None:
        """Apply the chips, Find and the sort to the current lines."""
        self.shown = visible_lines(self.lines, self.chips, self.find_var.get(),
                                   self.sort_key, self.descending, self.model)
        # A line hidden by the chips or Find is closed: the others are muted only while
        # the open line can be seen.
        self.open_pids &= {line.pid for line in self.shown}
        tree = self.tree
        wanted = []
        for n, line in enumerate(self.shown):
            iid = self._iid_of.get(line.pid)
            values = line_values(line)
            row = (line_name(line), tuple(values[c.key] for c in COLUMNS))
            if iid is None:
                iid = self._new_iid("L")
                self._iid_of[line.pid], self._pid_of[iid] = iid, line.pid
                tree.insert("", "end", iid, text=row[0], values=row[1],
                            image=self._images.get("closed", ""))
                tree.insert(iid, "end", self._new_iid("P"), text="")   # openable
                self._values[iid] = row
            elif self._values.get(iid) != row:
                tree.item(iid, text=row[0], values=row[1])
                self._values[iid] = row
            if tree.index(iid) != n or tree.parent(iid) != "":
                tree.move(iid, "", n)
            wanted.append(iid)
            if line.pid in self.open_pids:
                self._fill_versions(line.pid)
        for iid in set(tree.get_children("")) - set(wanted):
            pid = self._pid_of.pop(iid, None)
            if pid is not None:
                self._iid_of.pop(pid, None)
                self._forget_versions(pid)
            self._values.pop(iid, None)
            tree.delete(iid)
        if self.model is not None and not self.lines:
            tree.insert("", "end", "empty", text=EMPTY_VAULT, tags=("muted",))
        self._retag()

    def _new_iid(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}{self._next}"

    def _retag(self) -> None:
        """Zebra rows; every line but the open ones muted while any is open."""
        dimmed = bool(self.open_pids)
        for n, iid in enumerate(self.tree.get_children("")):
            pid = self._pid_of.get(iid)
            if pid is None:
                continue
            tags = ["line"]
            if n % 2:
                tags.append("odd")
            if dimmed and pid not in self.open_pids:
                tags.append("dim")
            self.tree.item(iid, tags=tags)

    # --- versions under an open line ------------------------------------------------

    def _fill_versions(self, pid: str) -> None:
        """Bring an open line's rows up to date in place: a row keeps its iid (and so its
        selection) for as long as its version is there (D6 3.1.2)."""
        tree, iid = self.tree, self._iid_of.get(pid)
        if iid is None or self.model is None:
            return
        labels = {b.target: b.label for line in self.lines for b in line.live}
        known = self._child_iids.setdefault(pid, {})
        wanted = []
        for n, spec in enumerate(version_specs(self.model, pid, labels)):
            viid = known.get(spec.key)
            if viid is None or not tree.exists(viid):
                viid = self._new_iid("V")
                known[spec.key] = viid
                tree.insert(iid, "end", viid, text=spec.text, values=spec.values,
                            tags=spec.tags, image=self._images.get(spec.image, ""))
            elif self._values.get(viid) != spec:
                tree.item(viid, text=spec.text, values=spec.values, tags=spec.tags,
                          image=self._images.get(spec.image, ""))
            self._values[viid] = spec
            if spec.head is not None:
                self._version_of[viid] = (pid, spec.head)
            else:
                self._version_of.pop(viid, None)
            if tree.parent(viid) != iid or tree.index(viid) != n:
                tree.move(viid, iid, n)
            wanted.append(viid)
        for child in tree.get_children(iid):
            if child not in wanted:
                self._drop_child(pid, child)
                tree.delete(child)

    def _drop_child(self, pid: str, iid: str) -> None:
        self._version_of.pop(iid, None)
        self._values.pop(iid, None)
        known = self._child_iids.get(pid, {})
        for key in [k for k, v in known.items() if v == iid]:
            del known[key]

    def _forget_versions(self, pid: str) -> None:
        for iid in self._child_iids.pop(pid, {}).values():
            self._version_of.pop(iid, None)
            self._values.pop(iid, None)

    # --- opening and closing ----------------------------------------------------------

    def _on_click(self, event) -> str | None:
        """A click on the marker opens or closes the line (the +/- indicator is gone)."""
        iid = self.tree.identify_row(event.y)
        if iid in self._pid_of and "image" in self.tree.identify_element(event.x, event.y):
            self.set_open(self._pid_of[iid], not self.tree.item(iid, "open"))
            return "break"
        return None

    def set_open(self, pid: str, is_open: bool) -> None:
        iid = self._iid_of.get(pid)
        if iid is None:
            return
        self.tree.item(iid, open=is_open)
        self._opened(iid, is_open)

    def _opened(self, iid: str, is_open: bool) -> None:
        pid = self._pid_of.get(iid)
        if pid is None:
            return
        if is_open:
            self.open_pids.add(pid)
            self._fill_versions(pid)
        else:
            self.open_pids.discard(pid)
        self.tree.item(iid, image=self._images.get("open" if is_open else "closed", ""))
        self._retag()

    # --- chips and Find ------------------------------------------------------------

    def _update_chip_labels(self) -> None:
        counts = chip_counts(self.lines)
        self.chip_buttons["all"].configure(text=f"All {counts['all']}")
        for chip in CHIPS:
            self.chip_buttons[chip.name].configure(text=f"{chip.label} {counts[chip.name]}")

    def _chip_all(self) -> None:
        self.set_chips(())

    def _chip_toggled(self, name: str) -> None:
        self.set_chips({n for n, var in self.chip_vars.items() if var.get()})

    def set_chips(self, chips: Iterable[str]) -> None:
        self.chips = {c for c in chips if c in CHIP}
        for name, var in self.chip_vars.items():
            var.set(name in self.chips)
        self.all_var.set(not self.chips)
        self.prefs["table_chips"] = sorted(self.chips)
        self.save_prefs()
        self.refresh()

    def _find_changed(self) -> None:
        if self._find_after is not None:
            self.after_cancel(self._find_after)
        self._find_after = self.after(FIND_DELAY_MS, self._find_now)

    def _find_now(self) -> None:
        self._find_after = None
        self.refresh()

    # --- sorting -------------------------------------------------------------------

    def sort_by(self, column: str) -> None:
        """Sort by a column's key; the same heading again reverses the order."""
        key = BY_KEY[column].sort
        if key is None:
            return
        if key == self.sort_key:
            self.descending = not self.descending
        else:
            self.sort_key = key
            self.descending = key in ("play_time", "last_played", "versions", "risk")
        self.prefs["table_sort"] = [self.sort_key, self.descending]
        self.save_prefs()
        self._show_sort_arrow()
        self.refresh()

    def _show_sort_arrow(self) -> None:
        arrow = self._images.get("down" if self.descending else "up", "")
        for col in (NAME, *COLUMNS):
            self.tree.heading(col.key, image=arrow if col.sort == self.sort_key else "")

    # --- columns ---------------------------------------------------------------------

    def set_columns(self, keys: Iterable[str], *, save: bool = True) -> None:
        """Which columns show, in order (#0 always)."""
        shown = [k for k in keys if k in BY_KEY and k != "#0"] or list(DEFAULT_COLUMNS)
        self.tree.configure(displaycolumns=shown)
        if save:
            self.prefs["table_columns"] = shown
            self.save_prefs()

    def displayed_columns(self) -> list[str]:
        shown = self.tree.cget("displaycolumns")
        if shown in ("#all", ("#all",)):
            return [c.key for c in COLUMNS]
        return list(shown)

    def rezoom(self, ratio: float) -> None:
        """The zoom changed: every column by the same ratio, so one the user dragged wider
        stays wider (as the classic panes do)."""
        for column in ("#0", *(c.key for c in COLUMNS)):
            width = int(self.tree.column(column, "width"))
            self.tree.column(column, width=max(1, int(round(width * ratio))))
        self._factor = dpi.factor()

    def _remember_widths(self, _event=None) -> None:
        """Keep column widths the user dragged, in design pixels: at the scale they are
        drawn at now."""
        if not self._dragging:
            return
        self._dragging = False
        factor = self._factor or 1.0
        widths = {c: int(round(int(self.tree.column(c, "width")) / factor))
                  for c in ("#0", *(col.key for col in COLUMNS))}
        if widths != self.prefs.get("table_widths"):
            self.prefs["table_widths"] = widths
            self.save_prefs()

    def choose_columns(self) -> None:
        ColumnChooser(self)

    # --- selection and hover -----------------------------------------------------------

    def selection_ref(self) -> tuple[str | None, str | None]:
        """(pid, head) of the focused row: head None for a line."""
        iid = self.tree.focus()
        if iid in self._pid_of:
            return self._pid_of[iid], None
        if iid in self._version_of:
            return self._version_of[iid]
        return None, None

    def select(self, pid: str, head: str | None = None) -> None:
        if head is not None:
            self.set_open(pid, True)
            iid = next((v for v, ref in self._version_of.items() if ref == (pid, head)), None)
        else:
            iid = self._iid_of.get(pid)
        if iid is not None:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)

    def _on_select(self, _event=None) -> None:
        if self.on_select is not None:
            self.on_select(*self.selection_ref())

    def _tip(self, _tree, iid: str) -> str:
        if iid in self._pid_of and self.model is not None:
            line = self.model.line(self._pid_of[iid])
            return line_tip(line) if line is not None else ""
        if iid in self._version_of and self.model is not None:
            pid, head = self._version_of[iid]
            row = next((r for r in self.model.versions(pid) if r.head == head), None)
            return version_tip(row) if row is not None else ""
        return ""



class ColumnChooser(tk.Toplevel):
    """Which columns show and in what order (D6 2.12): tick, move up or down, Reset."""

    def __init__(self, table: OrganizerTable) -> None:
        super().__init__(table)
        self.table = table
        self.title("Columns")
        self.transient(table.winfo_toplevel())
        shown = table.displayed_columns()
        self.order = shown + [c.key for c in COLUMNS if c.key not in shown]
        self.on = {key: tk.BooleanVar(value=key in shown) for key in self.order}
        self.body = ttk.Frame(self, padding=px(10))
        self.body.pack(fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(self, padding=pxs(10, 0, 10, 10))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Reset", command=self.reset).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="OK", command=self.ok).pack(side=tk.RIGHT, padx=pxs(0, 6))
        self._rows()
        dpi.fit_to_content(self, 280, 300)

    def _rows(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        ttk.Label(self.body, text="Save file (always shown)").grid(row=0, column=0, sticky="w")
        for n, key in enumerate(self.order, start=1):
            ttk.Checkbutton(self.body, text=BY_KEY[key].title,
                            variable=self.on[key]).grid(row=n, column=0, sticky="w")
            ttk.Button(self.body, text="Up", width=5,
                       command=lambda k=key: self.move(k, -1)).grid(row=n, column=1)
            ttk.Button(self.body, text="Down", width=5,
                       command=lambda k=key: self.move(k, 1)).grid(row=n, column=2)

    def move(self, key: str, step: int) -> None:
        i = self.order.index(key)
        j = min(max(0, i + step), len(self.order) - 1)
        self.order[i], self.order[j] = self.order[j], self.order[i]
        self._rows()

    def reset(self) -> None:
        self.order = [c.key for c in COLUMNS]
        for key, var in self.on.items():
            var.set(key in DEFAULT_COLUMNS)
        self._rows()

    def ok(self) -> None:
        self.table.set_columns([k for k in self.order if self.on[k].get()])
        self.destroy()
