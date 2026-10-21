"""The live area of the Organizer tab (D6 2.2, 2.9, 3.2): the live slots as vertical lists.

Two modes (owner review): **One** - a single wide list of the live folder picked in a
selector, with last played and the empty-slot warning in the row; **Two** - two lists side
by side, each with its own selector, any two of the configured live folders (several Steam
accounts and Xbox alike). Only configured live folders are offered, so nobody sees a column
for a platform they do not have; with one live folder there is no mode switch and no
selector. Mode and folders are kept in ``ui`` (``live_mode``, ``live_sources``).

Each list is drawn on a Canvas: per slot a chip (``S9`` in the platform colour; outlined
grey when empty), the name, ``+X8`` when the save is live on the other platform too, play
time, and markers - amber triangle: not in the vault; pin: pinned; clock: waiting for the
next launch; red: reverted; cloud: an empty slot that Steam Cloud or Xbox sync may fill
again (the app never empties a slot). The legend lists only the markers in use. The slots
of the selected save file are outlined in the accent colour. Read-only in phase 1: a click
selects that slot's save file; the right-click menu and Enter (Replace with...) are the
seams the live operations plug into (``on_context``, ``on_activate``).
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from types import SimpleNamespace
from tkinter import font as tkfont
from tkinter import ttk

from ... import theme
from ...core.organizer.model import platform_name
from ...core.organizer.readmodel import BoardSlot, LiveSource, ReadModel
from ..dpi import px, pxs
from ..fmt import local_time, play_time
from ..redact import ctext, shown
from ..themeaware import ThemeRegistry
from ..tooltip import Tooltip
from .words import NO_BACKUP_TIP, slot_words

ONE, TWO = "one", "two"
SLOT, GAME = "slot", "game"
GAME_ORDER_TIP = "As in game: newest save first, the order of the game's load screen."
NEVER_EMPTIES = "The app never empties a slot; this one was emptied outside the app."

#: What each marker means, in the legend and in words wherever it is drawn.
MARKERS = {
    "empty": "empty: Steam Cloud / Xbox sync may fill it again",
    "risk": "no backup yet",
    "pin": "pinned: never suggested",
    "pending": "waiting for the next game launch",
    "reverted": "reverted outside the app",
}


def text_on(colour: str) -> str:
    """White or near-black, whichever reads better on ``colour`` (the dark palette's
    platform colours are light)."""
    r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#ffffff" if luminance < 0.5 else "#1a1a1a"


def source_key(source: LiveSource) -> str:
    """A live folder's name in the preferences: stable across runs on this machine."""
    ref = source.ref
    return f"{ref.platform}:{ref.account_key}:{ref.instance}"


def refill_text(source: LiveSource) -> str | None:
    """D5's EMPTY_SLOT_MAY_REFILL for this folder: Steam Cloud (on, or not known to be
    off), GOG's cloud sync or Xbox sync may put a save back into an empty slot."""
    if source.platform == "xbox":
        return "While this slot is empty, Xbox sync may put a save back into it."
    if _is_gog(source):
        return ("While this slot is empty, cloud sync may put a save back into it at the next "
                "game start.")
    if source.steam_cloud is False:
        return None
    return ("While this slot is empty, Steam Cloud may put a save back into it at the next "
            "game start.")


def refill_inline(source: LiveSource) -> str:
    if source.platform == "xbox":
        return "Xbox sync may put a save back here"
    sync = "Cloud sync" if _is_gog(source) else "Steam Cloud"
    return f"{sync} may put a save back here at the next game start"


def _is_gog(source: LiveSource) -> bool:
    return platform_name(source.ref.platform, source.ref.account_key) == "GOG"


def _where(target, labels: dict) -> str:
    """``Steam slot 9``, or from its badge ``Steam 2 slot 9`` when there are two accounts."""
    if labels.get(target):
        return slot_words(labels[target])
    return f"{platform_name(target.platform, target.account_key)} slot {target.slot}"


def game_order(slots) -> list[BoardSlot]:
    """The game's load-screen order: newest save time first (the clock of "Last played");
    empty and unreadable slots last, in slot order."""
    def key(s: BoardSlot) -> tuple:
        if s.state in ("empty", "unreadable"):
            return (1, 0, s.target.slot)
        return (0, -s.saved_at, s.target.slot)
    return sorted(slots, key=key)


def slot_markers(slot: BoardSlot, source: LiveSource) -> list[str]:
    """The markers of one slot, in drawing order."""
    out = []
    if slot.state == "empty":
        if refill_text(source) is not None:
            out.append("empty")
        return out
    if slot.state in ("occupied", "partial") and slot.vault_copy != "durable":
        out.append("risk")
    if slot.pinned:
        out.append("pin")
    if slot.pending_op:
        out.append("pending")
    if slot.last_event is not None and slot.last_event.kind == "reverted":
        out.append("reverted")
    return out


def slot_tip(slot: BoardSlot, source: LiveSource, labels: dict) -> str:
    """The hover text of a slot: every marker in words (D6 3.2, accessibility)."""
    where = f"{platform_name(source.ref.platform, source.ref.account_key)} slot {slot.target.slot}"
    if slot.state == "empty":
        refill = refill_text(source)
        return f"{where} - empty.\n" + (f"{refill}\n{NEVER_EMPTIES}" if refill else NEVER_EMPTIES)
    if slot.state == "unreadable":
        return f"{where} - cannot be read right now (the game may be writing it)."
    out = [f"{where}: {slot.name or '(unnamed)'}",
           f"Played {play_time(slot.play_time) or '0h00'}, saved {local_time(slot.saved_at)}"]
    if slot.other_platform:
        out.append("Also live in " + ", ".join(_where(t, labels) for t in slot.other_platform)
                   + ".")
    if slot.twin:
        out.append("Also live in another slot of this folder.")
    for marker in slot_markers(slot, source):
        out.append(MARKERS[marker].capitalize())
        if marker == "risk":
            out.append(NO_BACKUP_TIP)
    if slot.state == "partial":
        out.append("Only one of the slot's two saves is there.")
    return "\n".join(out)


CONTEXT_KEYS = ("<Shift-F10>", "<App>")      # the keyboard's way to the right-click menu


@dataclass
class _Drawn:
    token: str
    source: LiveSource
    slot: BoardSlot
    box: tuple[int, int, int, int]
    bg: int = 0                 # the slot's background rectangle (hover tints only it)


class LiveArea(ttk.Frame):
    def __init__(self, master, *, themed: ThemeRegistry, prefs: MutableMapping,
                 save_prefs: Callable[[], None],
                 on_slot: Callable[[str, BoardSlot], None] | None = None,
                 on_activate: Callable[[str, BoardSlot], None] | None = None,
                 on_context: Callable[[str, BoardSlot, tk.Event], None] | None = None) -> None:
        super().__init__(master)
        self.prefs = prefs
        self.save_prefs = save_prefs
        self.on_slot = on_slot
        self.on_activate = on_activate
        self.on_context = on_context
        self.model: ReadModel | None = None
        self.sources: tuple[LiveSource, ...] = ()
        self.mode = prefs.get("live_mode") if prefs.get("live_mode") in (ONE, TWO) else None
        self.order = GAME if prefs.get("live_order") == GAME else SLOT
        self.chosen: list[str] = list(prefs.get("live_sources") or [])
        self.selected_pid: str | None = None
        self.focus_token: str | None = None
        self.hover_token: str | None = None
        self.drawn: dict[str, _Drawn] = {}
        self._p: dict = dict(theme.PALETTES[theme.LIGHT])
        self._build()
        themed.register(self)

    # --- building ---------------------------------------------------------------------

    def _build(self) -> None:
        self.bar = ttk.Frame(self)
        self.bar.pack(side=tk.TOP, fill=tk.X, pady=pxs(0, 4))
        self.title = ttk.Label(self.bar, text="Live:", style="Head.TLabel")
        self.mode_var = tk.StringVar(value=self.mode or TWO)
        self.mode_buttons = [
            tk.Radiobutton(self.bar, text=text, value=value, variable=self.mode_var,
                           indicatoron=False, command=lambda v=value: self.set_mode(v),
                           padx=px(10), pady=px(2), relief="flat", borderwidth=1,
                           offrelief="flat", overrelief="raised")
            for text, value in (("One", ONE), ("Two", TWO))]
        self.order_var = tk.StringVar(value=self.order)
        self.order_buttons = [
            tk.Radiobutton(self.bar, text=text, value=value, variable=self.order_var,
                           indicatoron=False, command=lambda v=value: self.set_order(v),
                           padx=px(10), pady=px(2), relief="flat", borderwidth=1,
                           offrelief="flat", overrelief="raised")
            for text, value in (("Slot order", SLOT), ("As in game", GAME))]
        self.pick_vars = [tk.StringVar(), tk.StringVar()]
        self.pickers = [ttk.Combobox(self.bar, textvariable=var, state="readonly", width=18)
                        for var in self.pick_vars]
        for n, picker in enumerate(self.pickers):
            picker.bind("<<ComboboxSelected>>", lambda _e, i=n: self._picked(i))
        self.order_tip = Tooltip(self.order_buttons[1], lambda _w, _k: GAME_ORDER_TIP,
                              key_at=lambda _e: "order")
        self.canvas = tk.Canvas(self, highlightthickness=px(1), borderwidth=0,
                                takefocus=True, height=px(200), width=px(460))
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Motion>", self._on_motion, add="+")
        self.canvas.bind("<Leave>", lambda _e: self._hover(None), add="+")
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        for key in CONTEXT_KEYS:
            try:
                self.canvas.bind(key, self._on_menu_key)
            except tk.TclError:                  # a keysym this platform does not have
                pass
        for key, step in (("<Up>", -1), ("<Down>", 1), ("<Left>", -15), ("<Right>", 15)):
            self.canvas.bind(key, lambda _e, s=step: self.move_focus(s))
        self.canvas.bind("<Return>", lambda _e: self._activate(self.focus_token))
        self.message = ttk.Label(self, text="", style="Muted.TLabel", wraplength=px(420))
        self.tooltip = Tooltip(self.canvas, lambda _w, token: self._tip(token),
                               key_at=lambda e: self.token_at(e.x, e.y))

    # --- theme ------------------------------------------------------------------------

    def apply_palette(self, p) -> None:
        self._p = dict(p)
        self.canvas.configure(background=p["field"], highlightbackground=p["border"],
                              highlightcolor=p["accent"])
        for button in (*self.mode_buttons, *self.order_buttons):
            button.configure(background=p["btn_bg"], foreground=p["fg"],
                             selectcolor=p["chip_on"], activebackground=p["hover_bg"],
                             activeforeground=p["fg"], highlightbackground=p["bg"])
        self.redraw()

    # --- the model and the choice of folders ---------------------------------------------

    def set_model(self, model: ReadModel | None) -> None:
        self.model = model
        self.sources = model.live_board().sources if model is not None else ()
        if self.mode is None:
            self.mode = TWO if len(self.sources) >= 2 else ONE
        self._settle_choice()
        self._layout_bar()
        self.redraw()

    def shown_sources(self) -> list[LiveSource]:
        """The lists drawn now: one, or two different folders."""
        by_key = {source_key(s): s for s in self.sources}
        count = 2 if self.mode == TWO and len(self.sources) >= 2 else 1
        return [by_key[k] for k in self.chosen[:count] if k in by_key]

    def _settle_choice(self) -> None:
        """Keep the chosen folders that still exist, fill up from the rest, never the same
        folder twice."""
        keys = [source_key(s) for s in self.sources]
        chosen = [k for k in dict.fromkeys(self.chosen) if k in keys]
        for k in keys:
            if len(chosen) >= 2:
                break
            if k not in chosen:
                chosen.append(k)
        self.chosen = chosen

    def _layout_bar(self) -> None:
        for widget in (self.title, *self.mode_buttons, *self.order_buttons, *self.pickers):
            widget.pack_forget()
        self.message.pack_forget()
        if not self.sources:
            self.message.configure(text="No live save folders found - Live > Accounts...")
            self.message.pack(side=tk.TOP, fill=tk.X, before=self.canvas)
            return
        self.title.pack(side=tk.LEFT, padx=pxs(0, 6))
        self.order_var.set(self.order)
        if len(self.sources) > 1:
            self.mode_var.set(self.mode)
            for button in self.mode_buttons:
                button.pack(side=tk.LEFT)
            tk.Frame(self.bar, width=px(10)).pack(side=tk.LEFT)
        for button in self.order_buttons:
            button.pack(side=tk.LEFT)
        if len(self.sources) == 1:
            return                               # one folder: no mode switch, no selector
        names = self.labels()
        labels = list(names.values())
        lists = 2 if self.mode == TWO else 1
        by_key = names
        for n in range(lists):
            self.pickers[n].configure(values=labels)
            self.pick_vars[n].set(by_key.get(self.chosen[n], "") if n < len(self.chosen) else "")
            self.pickers[n].pack(side=tk.LEFT, padx=pxs(6, 0))

    def set_mode(self, mode: str) -> None:
        if mode not in (ONE, TWO):
            return
        self.mode = mode
        self.prefs["live_mode"] = mode
        self.save_prefs()
        self._layout_bar()
        self.redraw()

    def set_order(self, order: str) -> None:
        if order not in (SLOT, GAME):
            return
        self.order = order
        self.prefs["live_order"] = order
        self.save_prefs()
        self.redraw()

    def set_source(self, index: int, key: str) -> None:
        """Show folder ``key`` in list ``index``; choosing the other list's folder swaps
        the two."""
        keys = [source_key(s) for s in self.sources]
        if key not in keys:
            return
        while len(self.chosen) <= index:
            self.chosen.append(next(k for k in keys if k not in self.chosen))
        other = 1 - index
        if other < len(self.chosen) and self.chosen[other] == key:
            self.chosen[other] = self.chosen[index]
        self.chosen[index] = key
        self.prefs["live_sources"] = list(self.chosen)
        self.save_prefs()
        self._layout_bar()
        self.redraw()

    def labels(self) -> dict[str, str]:
        """Each folder's name in the selectors, unique even when two share a label."""
        out: dict[str, str] = {}
        for source in self.sources:
            label, n = shown(self, source.label), 2
            unique = label
            while unique in out.values():
                unique, n = f"{label} #{n}", n + 1
            out[source_key(source)] = unique
        return out

    def _picked(self, index: int) -> None:
        label = self.pick_vars[index].get()
        key = next((k for k, v in self.labels().items() if v == label), None)
        if key is not None:
            self.set_source(index, key)

    # --- drawing --------------------------------------------------------------------------

    def _fonts(self):
        base = tkfont.nametofont("TkDefaultFont", self)
        return base, tkfont.nametofont(theme.BOLD_FONT, self), \
            tkfont.nametofont(theme.SMALL_FONT, self), tkfont.nametofont(theme.ITALIC_FONT, self)

    def redraw(self) -> None:
        canvas, p = self.canvas, self._p
        canvas.delete("all")
        self.drawn = {}
        lists = self.shown_sources()
        if not lists:
            return
        base, bold, small, italic = self._fonts()
        row_h = base.metrics("linespace") + px(8)
        head_h = bold.metrics("linespace") + px(10)
        # The canvas as it is: its real width once on screen, the asked-for one before.
        width = canvas.winfo_width() if canvas.winfo_ismapped() else int(canvas.cget("width"))
        gap = px(8)
        wide = len(lists) == 1
        col_w = (width - gap * (len(lists) - 1) - px(2)) // len(lists)
        used: set[str] = set()
        for n, source in enumerate(lists):
            x0 = px(1) + n * (col_w + gap)
            self._draw_list(source, x0, x0 + col_w, row_h, head_h, wide, used,
                            base, bold, small, italic)
        legend_y = head_h + 15 * (row_h + px(2)) + px(8)
        self._draw_legend(px(2), legend_y, used, small)
        canvas.configure(height=legend_y + small.metrics("linespace") + px(8))

    def _draw_list(self, source, x0, x1, row_h, head_h, wide, used, base, bold, small,
                   italic) -> None:
        canvas, p = self.canvas, self._p
        colour = p["xbox"] if source.platform == "xbox" else p["steam"]
        canvas.create_rectangle(x0, px(2), x0 + px(3), head_h - px(4), fill=colour, width=0)
        ctext(canvas, x0 + px(10), head_h / 2, text=f"{source.label} live", anchor="w",
              font=bold, fill=p["fg"])
        filled = sum(1 for s in source.slots if s.state != "empty")
        ctext(canvas, x1 - px(4), head_h / 2, text=f"{filled} of {len(source.slots)} used",
              anchor="e", font=small, fill=p["muted"])
        labels = {} if self.model is None else {
            b.target: b.label for line in self.model.lines() for b in line.live}
        slots = game_order(source.slots) if self.order == GAME else source.slots
        for i, slot in enumerate(slots):
            y0 = head_h + i * (row_h + px(2))
            token = self.model.token(slot.target) if self.model is not None else str(i)
            self._draw_slot(token, source, slot, x0, y0, x1, y0 + row_h, wide, used, labels,
                            base, bold, small, italic)

    def _draw_slot(self, token, source, slot, x0, y0, x1, y1, wide, used, labels, base,
                   bold, small, italic) -> None:
        canvas, p = self.canvas, self._p
        tag = f"slot:{token}"
        empty = slot.state == "empty"
        selected = self.selected_pid is not None and slot.pid == self.selected_pid
        fill = p["hl_bg"] if token == self.hover_token else p["field"]
        bg = canvas.create_rectangle(x0, y0, x1, y1, fill=fill,
                                     outline=p["accent"] if selected else p["border"],
                                     width=px(2) if selected else 1, tags=(tag, "bg"))
        self.drawn[token] = _Drawn(token, source, slot, (x0, y0, x1, y1), bg)
        colour = p["xbox"] if source.platform == "xbox" else p["steam"]
        chip_w = px(30)
        cy = (y0 + y1) / 2
        canvas.create_rectangle(x0 + px(3), y0 + px(3), x0 + px(3) + chip_w, y1 - px(3),
                                fill=p["slot_empty"] if empty else colour,
                                outline=p["muted"] if empty else colour, tags=(tag,))
        ctext(canvas, x0 + px(3) + chip_w / 2, cy, text=slot.label, font=bold,
              fill=p["muted"] if empty else text_on(colour), tags=(tag,))
        x = x0 + chip_w + px(10)
        right = x1 - px(6)
        markers = slot_markers(slot, source)
        used.update(markers)
        for marker in reversed(markers):
            right -= px(14)
            self._marker(marker, right, cy, tag)
            right -= px(4)
        if token == self.focus_token:
            canvas.create_rectangle(x0 + px(1), y0 + px(1), x1 - px(1), y1 - px(1),
                                    outline=p["accent"], dash=(2, 2), tags=(tag, "focus"))
        if empty:
            refill = refill_text(source)
            text = "(empty)" if not (wide and refill) else f"(empty)  {refill_inline(source)}"
            self._fit_text(text, x, cy, right - px(4), italic, p["muted"], tag)
            return
        if slot.state == "unreadable":
            self._fit_text("(cannot be read)", x, cy, right - px(4), italic, p["muted"], tag)
            return
        if wide:
            when = local_time(slot.saved_at)
            right -= px(4)
            ctext(canvas, right, cy, text=when, anchor="e", font=small, fill=p["muted"],
                  tags=(tag,))
            right -= small.measure(when) + px(10)
        play = play_time(slot.play_time)
        if play:
            ctext(canvas, right, cy, text=play, anchor="e", font=small, fill=p["muted"],
                  tags=(tag,))
            right -= small.measure(play) + px(8)
        others = " ".join(labels.get(t, "") for t in slot.other_platform).strip()
        if others:
            other = "+" + others.replace(" ", " +")
            colour = p["xbox"] if slot.other_platform[0].platform == "xbox" else p["steam"]
            ctext(canvas, right, cy, text=other, anchor="e", font=bold, fill=colour,
                  tags=(tag,))
            right -= bold.measure(other) + px(8)
        self._fit_text(slot.name or "(unnamed)", x, cy, right, base, p["fg"], tag)

    def _fit_text(self, text: str, x, cy, limit, font, colour, tag) -> None:
        """The text, cut with "..." to the room there is (the full text is in the tooltip)."""
        text = shown(self.canvas, text)
        room = max(0, limit - x)
        if font.measure(text) > room:
            while text and font.measure(text + "...") > room:
                text = text[:-1]
            text += "..."
        self.canvas.create_text(x, cy, text=text, anchor="w", font=font, fill=colour,
                                tags=(tag,))

    def _marker(self, kind: str, x: float, cy: float, tag: str) -> None:
        """The markers are drawn shapes, no image files (D6 3.2)."""
        c, p = self.canvas, self._p
        s = px(12)
        top, bottom = cy - s / 2, cy + s / 2
        tags = (tag, f"marker:{kind}")
        if kind == "risk":
            c.create_polygon(x, bottom, x + s / 2, top, x + s, bottom, fill=p["warn"],
                             outline=p["warn"], tags=tags)
            c.create_text(x + s / 2, cy + px(1.5), text="!", fill="#ffffff",
                          font=tkfont.nametofont(theme.BOLD_FONT, self), tags=tags)
        elif kind == "pin":
            c.create_line(x + s / 2, cy, x + s / 2, bottom, fill=p["danger"], width=px(2),
                          tags=tags)
            c.create_oval(x + px(2), top, x + s - px(2), cy + px(1), fill=p["danger"],
                          outline=p["danger"], tags=tags)
        elif kind == "empty":
            for dx, dy, r in ((3, 1, 3.2), (7, -1, 4), (10, 1.5, 2.8)):
                c.create_oval(x + px(dx) - px(r), cy + px(dy) - px(r), x + px(dx) + px(r),
                              cy + px(dy) + px(r), fill=p["steam"], outline=p["steam"],
                              tags=tags)
        elif kind == "pending":
            c.create_oval(x, top, x + s, bottom, outline=p["pending"], width=px(1.5),
                          tags=tags)
            c.create_line(x + s / 2, cy, x + s / 2, top + px(3), fill=p["pending"], tags=tags)
            c.create_line(x + s / 2, cy, x + s - px(3), cy, fill=p["pending"], tags=tags)
        elif kind == "reverted":
            c.create_polygon(x + s / 2, top, x + s, cy, x + s / 2, bottom, x, cy,
                             fill=p["danger"], outline=p["danger"], tags=tags)

    def _draw_legend(self, x: float, y: float, used: set[str], small) -> None:
        for kind in ("empty", "risk", "pin", "pending", "reverted"):
            if kind not in used:
                continue
            self._marker(kind, x, y, "legend")
            item = ctext(self.canvas, x + px(16), y, text=MARKERS[kind], anchor="w",
                         font=small, fill=self._p["muted"], tags=("legend",))
            x = self.canvas.bbox(item)[2] + px(14)

    # --- selection, hover and keys ----------------------------------------------------------

    def highlight(self, pid: str | None) -> None:
        """Outline the slots of the selected save file."""
        if pid != self.selected_pid:
            self.selected_pid = pid
            self.redraw()

    def token_at(self, x: float, y: float) -> str | None:
        for token, drawn in self.drawn.items():
            x0, y0, x1, y1 = drawn.box
            if x0 <= x <= x1 and y0 <= y <= y1:
                return token
        return None

    def _hover(self, token: str | None) -> None:
        """Tint the slot under the pointer: two rectangles change, nothing is redrawn."""
        if token == self.hover_token:
            return
        for old, fill in ((self.hover_token, self._p["field"]), (token, self._p["hl_bg"])):
            drawn = self.drawn.get(old) if old is not None else None
            if drawn is not None:
                self.canvas.itemconfigure(drawn.bg, fill=fill)
        self.hover_token = token

    def _on_motion(self, event) -> None:
        self._hover(self.token_at(event.x, event.y))

    def _on_click(self, event) -> None:
        self.canvas.focus_set()
        token = self.token_at(event.x, event.y)
        if token is not None:
            self.select_slot(token)

    def select_slot(self, token: str) -> None:
        self.focus_token = token
        drawn = self.drawn.get(token)
        self.redraw()
        if drawn is not None and self.on_slot is not None:
            self.on_slot(token, drawn.slot)

    def _on_right_click(self, event) -> None:
        token = self.token_at(event.x, event.y)
        if token is not None and self.on_context is not None:
            self.on_context(token, self.drawn[token].slot, event)

    def context_position(self, token: str | None = None) -> tuple[str, int, int] | None:
        """(token, x_root, y_root) where the keyboard's context menu opens: the focused slot
        (the first when none has the focus), just inside its left edge."""
        token = token if token in self.drawn else (
            self.focus_token if self.focus_token in self.drawn else next(iter(self.drawn), None))
        if token is None:
            return None
        x0, y0, x1, y1 = self.drawn[token].box
        return (token, self.canvas.winfo_rootx() + int(x0 + px(40)),
                self.canvas.winfo_rooty() + int((y0 + y1) / 2))

    def _on_menu_key(self, _event=None) -> str:
        """Shift+F10 / the Menu key: the same menu as the right click, at the focused slot."""
        where = self.context_position()
        if where is not None and self.on_context is not None:
            token, x, y = where
            self.on_context(token, self.drawn[token].slot,
                            SimpleNamespace(x_root=x, y_root=y, x=x - self.canvas.winfo_rootx(),
                                            y=y - self.canvas.winfo_rooty()))
        return "break"

    def _activate(self, token: str | None) -> None:
        if token is not None and token in self.drawn and self.on_activate is not None:
            self.on_activate(token, self.drawn[token].slot)

    def move_focus(self, step: int) -> None:
        """Arrow keys move the focus ring (Up / Down within a list, Left / Right across)."""
        order = list(self.drawn)
        if not order:
            return
        if self.focus_token not in self.drawn:
            index = 0
        else:
            index = min(max(0, order.index(self.focus_token) + step), len(order) - 1)
        self.select_slot(order[index])

    def _tip(self, token: str) -> str:
        drawn = self.drawn.get(token)
        if drawn is None:
            return ""
        labels = {} if self.model is None else {
            b.target: b.label for line in self.model.lines() for b in line.live}
        return slot_tip(drawn.slot, drawn.source, labels)
