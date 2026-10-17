"""The detail pane of the Organizer tab (D6 2.2, 2.3, 3.3): what the selection is.

A title and a one-line summary, the actions, and pages - **Summary** (the save file, the
version card, or the card of an empty live slot), **Where** (every place a version is kept
and what happened to the save file's slots) and **Notes** (the notes, read-only, with
Edit...). Facts arrive with the content index (phase 3) as their own page.

The pages are read-only Text widgets; ``#n`` references are links that select that
version. Every text is built from the read model alone (pure functions below) and shown
through the redaction filter. Nothing here is guessed: a version without a recorded parent
says so ("no recorded parent - listed by date").
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ... import theme
from ...core.organizer.readmodel import BoardSlot, LiveSource, ReadModel, VersionRow
from ..dpi import px, pxs
from ..fmt import local_time, play_time
from ..redact import rtext, set_label
from ..themeaware import ThemeRegistry
from .live import NEVER_EMPTIES, refill_text
from .words import METHOD_WORDS, find_row, line_name, slot_words, version_numbers

#: A segment of a page: its text and its tags. ``("link", head)`` makes a link to a version
#: of the shown save file, ``("link", head, pid)`` to one of another.
Segment = tuple[str, tuple]

NO_PARENT = "no recorded parent - not linked; listed by date, nothing to compare with"


def _row(key: str, *parts: Segment) -> list[Segment]:
    """A "Key<tab>value" line: the pages set one tab stop, so values line up in any font."""
    return [(f"{key}\t", ("k",)), *parts, ("\n", ())]


# --- what the pages say (pure) ------------------------------------------------------------


def line_title(model: ReadModel, pid: str) -> tuple[str, str]:
    line = model.line(pid)
    if line is None:
        return "", ""
    where = ", ".join(slot_words(b.label) for b in line.live) or "vault only"
    tips = sum(1 for r in model.versions(pid) if "tip" in r.flags)
    bits = [where, f"{line.version_count} versions"]
    if tips:
        bits.append(f"{tips} branch tip{'s' if tips != 1 else ''}")
    if line.pinned:
        bits.append("pinned")
    return line_name(line), " - ".join(bits)


def line_summary(model: ReadModel, pid: str) -> list[Segment]:
    line = model.line(pid)
    if line is None:
        return []
    numbers = version_numbers(model, pid)
    vault = {s.target: s.vault_copy for src in model.live_board().sources for s in src.slots}
    out: list[Segment] = []
    for badge in line.live:
        parts: list[Segment] = [(slot_words(badge.label), ("b",)), ("  ", ()),
                                (f"#{numbers.get(badge.head, '?')}", ("link", badge.head))]
        row = find_row(model, pid, badge.head)
        if row is not None:
            parts.append((f", saved {local_time(row.saved_at)}", ()))
        if vault.get(badge.target, "none") != "durable":
            parts.append(("  NOT IN VAULT", ("warn",)))
            parts.append((" - a backup keeps it", ()))
        if badge.behind_by:
            parts.append((f"  {play_time(badge.behind_by)} behind", ("warn",)))
        out += _row("Live", *parts)
    if not line.live:
        out += _row("Live", ("not in any live slot - kept in the vault", ()))
    played = [play_time(line.play_time) or "0h00", f"last {local_time(line.last_played)}"]
    if line.difficulty:
        played.append(line.difficulty)
    if line.mode:
        played.append(line.mode)
    out += _row("Played", (" - ".join(played), ()))
    platforms = _platform_sentence(line)
    if platforms:
        out += _row("Platforms", (platforms, ()))
    tips = [r for r in model.versions(pid) if "tip" in r.flags]
    versions = f"{line.version_count}"
    if tips:
        versions += "; tips: " + ", ".join(f"#{numbers[r.head]}" for r in tips[:6])
    if line.locked_count:
        versions += f"; {line.locked_count} locked"
    if line.gone_count:
        versions += f"; {line.gone_count} with no copy left"
    out += _row("Versions", (versions, ()))
    if line.former_names:
        out += _row("Also called", (", ".join(line.former_names), ()))
    if line.pinned:
        out += _row("Pinned", ("never suggested when a slot is needed for another save", ()))
    if line.note_preview:
        out += _row("Note", (line.note_preview, ("note",)))
    return out


def _platform_sentence(line) -> str:
    tips = {t.platform: t for t in line.platform_tips}
    if line.cross_platform == "xbox-behind":
        return (f"the Xbox copy is behind ({play_time(tips['xbox'].behind_by)} less play than "
                "the Steam copy); each platform's copy is its own tip")
    if line.cross_platform == "steam-behind":
        return (f"the Steam copy is behind ({play_time(tips['steam'].behind_by)} less play "
                "than the Xbox copy); each platform's copy is its own tip")
    if line.cross_platform == "diverged":
        return "both copies were played since they were last the same: two branches"
    if line.cross_platform == "same":
        return "the Steam and Xbox copies have the same play time"
    return ""


def version_title(model: ReadModel, pid: str, head: str) -> tuple[str, str]:
    row = find_row(model, pid, head)
    line = model.line(pid)
    if row is None or line is None:
        return "", ""
    number = version_numbers(model, pid)[head]
    bits = [f"saved {local_time(row.saved_at)}", f"{play_time(row.play_time) or '0h00'} played",
            "Steam" if row.platform == "steam" else "Xbox"]
    return f"{line_name(line)} #{number}", " - ".join(bits)


def version_card(model: ReadModel, pid: str, head: str) -> list[Segment]:
    row = find_row(model, pid, head)
    if row is None:
        return []
    numbers = version_numbers(model, pid)
    out: list[Segment] = []
    places = [loc.label for loc in row.locations] or ["no copy left anywhere"]
    out += _row("Where", (places[0], ()))
    for place in places[1:]:
        out += _row("", (place, ()))
    if row.parent is not None and row.parent in numbers:
        how = METHOD_WORDS.get(row.parent_method or "", "linked")
        out += _row("Comes from", (f"#{numbers[row.parent]}", ("link", row.parent)),
                    (f" ({how})", ()))
    else:
        out += _row("Comes from", (NO_PARENT, ("k",)))
    children = [r for r in model.versions(pid) if r.parent == head]
    if children:
        parts: list[Segment] = []
        for n, child in enumerate(sorted(children, key=lambda r: numbers[r.head],
                                         reverse=True)):
            if n:
                parts.append((", ", ()))
            parts.append((f"#{numbers[child.head]}", ("link", child.head)))
        if len(children) > 1:
            parts.append((" - a branch point", ()))
        out += _row("Led to", *parts)
    words = _version_state(row)
    if words:
        out += _row("State", (words, ("warn",) if "NOT IN VAULT" in words else ()))
    if row.label:
        out += _row("Label", (row.label, ("note",)))
    if row.comment:
        out += _row("Comment", (row.comment, ("note",)))
    if row.locked:
        out += _row("Locked", ("kept: never deleted", ()))
    events = [e for e in model.events(pid) if head in e.vids]
    for n, event in enumerate(events[:12]):
        out += _row("Events" if n == 0 else "",
                    (f"{local_time(event.at)}  {event.text}",
                     ("warn",) if event.kind == "reverted" else ("event",)))
    return out


def _version_state(row: VersionRow) -> str:
    words = []
    if "live" in row.flags:
        words.append("live")
    if row.durable_copies == 0 and "gone" not in row.flags:
        words.append("only outside the vault" if "outside-only" in row.flags
                     else "NOT IN VAULT")
    if "edited" in row.flags:
        words.append("edited outside the game")
    if "unreadable" in row.flags:
        words.append("unreadable - cannot be a source")
    if "gone" in row.flags:
        words.append("no copy left")
    return "; ".join(words)


def slot_title(source: LiveSource, slot: BoardSlot) -> tuple[str, str]:
    used = sum(1 for s in source.slots if s.state != "empty")
    state = {"empty": "empty", "unreadable": "cannot be read"}.get(slot.state, "")
    title = slot_words(slot.label) + (f"  -  {state}" if state else "")
    return title, f"{source.label} live folder - {used} of {len(source.slots)} slots used"


def slot_card(model: ReadModel, source: LiveSource, slot: BoardSlot) -> list[Segment]:
    """The card of an empty (or unreadable) live slot (D6 2.2, 08b)."""
    if slot.state == "unreadable":
        return [("This slot cannot be read right now.\n", ("h",)),
                ("The game may be writing it, or a file of the slot is missing. Its saves "
                 "are not recorded until it can be read again.\n", ())]
    out: list[Segment] = [("This slot is empty.\n", ("h",))]
    refill = refill_text(source)
    if refill:
        sync = "Xbox sync" if source.platform == "xbox" else "Steam Cloud"
        out += [(f"{sync} may fill it again: ", ("warn",)), (refill + " ", ()),
                (NEVER_EMPTIES + "\n", ())]
    else:
        out.append((NEVER_EMPTIES + " Steam Cloud is off for this folder, so it stays "
                    "empty.\n", ()))
    event = slot.last_event
    if event is not None and event.before and model is not None:
        vid = next((v for v in event.before if v), None)
        row = next((r for line in model.lines() for r in model.versions(line.pid)
                    if vid in r.pair), None) if vid else None
        if row is not None:
            number = version_numbers(model, row.pid)[row.head]
            out += _row("Last seen", (f"{row.name or '(unnamed)'} #{number}",
                                      ("link", row.head, row.pid)),
                        (f", until {local_time(event.at)}", ()))
    out += _row("To use it", ("copy a save into it from the Live saves and backups tab "
                              "(Copy into live slot...)", ()))
    return out


def where_text(model: ReadModel, pid: str, head: str | None = None) -> list[Segment]:
    """Every place each version is kept, newest first, then what happened to the slots."""
    numbers = version_numbers(model, pid)
    rows = [r for r in model.versions(pid) if head is None or r.head == head]
    out: list[Segment] = []
    for row in rows:
        out.append((f"#{numbers[row.head]}", ("link", row.head)))
        out.append((f"  saved {local_time(row.saved_at)}, {play_time(row.play_time) or '0h00'}"
                    "\n", ("k",)))
        for loc in row.locations or ():
            out.append((f"    {loc.label}\n", ()))
        if not row.locations:
            out.append(("    no copy left anywhere\n", ("warn",)))
    events = [e for e in model.events(pid) if head is None or head in e.vids]
    if events:
        out.append(("\nWhat happened\n", ("h",)))
        for event in events:
            out.append((f"{local_time(event.at)}  {event.text}\n",
                        ("warn",) if event.kind == "reverted" else ("event",)))
    return out


def notes_text(model: ReadModel, pid: str, head: str | None = None) -> list[Segment]:
    if head is not None:
        row = find_row(model, pid, head)
        if row is None or not (row.label or row.comment):
            return [("No label or comment on this version.\n", ("k",))]
        out: list[Segment] = []
        if row.label:
            out += _row("Label", (row.label, ("note",)))
        if row.comment:
            out += [("\n", ()), (row.comment + "\n", ("note",))]
        return out
    note = model.notes.save_files.get(pid)
    if note is None or not note.text.strip():
        return [("No note yet. Edit... writes one; notes are kept in the vault, never in "
                 "the save files.\n", ("k",))]
    return [(note.text.rstrip("\n") + "\n", ("note",)),
            (f"\nRevision {note.rev}, {local_time(note.at)}\n", ("small",))]


# --- the widget --------------------------------------------------------------------------


class DetailPane(ttk.Frame):
    def __init__(self, master, *, themed: ThemeRegistry,
                 on_link: Callable[[str, str], None] | None = None,
                 on_notes: Callable[[str, str | None], None] | None = None,
                 on_pin: Callable[[str, bool], None] | None = None,
                 on_move: Callable[[str, str | None], None] | None = None,
                 on_deep: Callable[[str], None] | None = None,
                 on_history: Callable[[str, str | None], None] | None = None) -> None:
        super().__init__(master)
        self.on_move = on_move
        self.on_deep = on_deep
        self.on_history = on_history
        self.facts_head: str | None = None
        self.on_link = on_link
        self.on_notes = on_notes
        self.on_pin = on_pin
        self.pid: str | None = None
        self.head: str | None = None
        self.pinned = False
        self._link_count = 0
        self.title = ttk.Label(self, text="", style="Title.TLabel", anchor="w")
        self.title.pack(side=tk.TOP, fill=tk.X)
        self.summary = ttk.Label(self, text="", style="Muted.TLabel", anchor="w")
        self.summary.pack(side=tk.TOP, fill=tk.X)
        self.actions = ttk.Frame(self)
        self.actions.pack(side=tk.TOP, fill=tk.X, pady=pxs(6, 6))
        self.notes_button = ttk.Button(self.actions, text="Notes...", command=self._notes)
        self.pin_button = ttk.Button(self.actions, text="Pin", command=self._pin)
        self.move_button = ttk.Button(self.actions, text="Move to live...", command=self._move)
        self.history_button = ttk.Button(self.actions, text="History...", command=self._history)
        self.pages = ttk.Notebook(self)
        self.pages.pack(fill=tk.BOTH, expand=True)
        self.summary_text = self._page("Summary")
        self.where_text = self._page("Where")
        facts = ttk.Frame(self.pages)
        self.pages.add(facts, text="Facts")
        self.deep_button = ttk.Button(facts, text="Read deeper detail", command=self._deep)
        self.deep_button.pack(side=tk.BOTTOM, anchor="e", pady=pxs(4, 2))
        self.facts_text = self._text(facts)
        notes = ttk.Frame(self.pages)
        self.pages.add(notes, text="Notes")
        self.edit_button = ttk.Button(notes, text="Edit...", command=self._notes)
        self.edit_button.pack(side=tk.BOTTOM, anchor="e", pady=pxs(4, 2))
        self.notes_text = self._text(notes)
        themed.register(self)
        self.clear()

    def _page(self, title: str) -> tk.Text:
        frame = ttk.Frame(self.pages)
        self.pages.add(frame, text=title)
        return self._text(frame)

    def _text(self, parent) -> tk.Text:
        text = tk.Text(parent, wrap="word", height=12, relief="flat", borderwidth=0,
                       highlightthickness=0, padx=px(10), pady=px(8), cursor="arrow",
                       font="TkDefaultFont", tabs=(px(96),), spacing1=px(1), spacing3=px(1))
        bar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=bar.set, state="disabled")
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return text

    # --- theme ---------------------------------------------------------------------------

    def apply_palette(self, p) -> None:
        for text in (self.summary_text, self.where_text, self.notes_text, self.facts_text):
            text.configure(background=p["card"], foreground=p["fg"],
                           selectbackground=p["sel_bg"], selectforeground=p["sel_fg"],
                           insertbackground=p["fg"])
            text.tag_configure("h", font=theme.HEADING_FONT, spacing1=px(4), spacing3=px(2))
            text.tag_configure("b", font=theme.BOLD_FONT)
            text.tag_configure("k", foreground=p["muted"])
            text.tag_configure("small", font=theme.SMALL_FONT, foreground=p["muted"])
            text.tag_configure("warn", foreground=p["warn"], font=theme.BOLD_FONT)
            text.tag_configure("ok", foreground=p["ok"])
            text.tag_configure("note", font=theme.ITALIC_FONT)
            text.tag_configure("event", foreground=p["event"])
            text.tag_configure("link", foreground=p["accent"], underline=True)
            text.tag_configure("hang", lmargin2=px(96))    # wrapped values stay indented

    # --- showing ----------------------------------------------------------------------------

    def clear(self, message: str = "Select a save file, a version or a live slot.") -> None:
        self.pid = self.head = None
        set_label(self.title, "")
        set_label(self.summary, message)
        self._buttons(line=False)
        for text in (self.summary_text, self.where_text, self.notes_text, self.facts_text):
            self._write(text, [])

    def show_line(self, model: ReadModel, pid: str) -> None:
        self.pid, self.head = pid, None
        self._write(self.facts_text, [])
        self.deep_button.state(["disabled"])
        title, summary = line_title(model, pid)
        set_label(self.title, title)
        set_label(self.summary, summary)
        line = model.line(pid)
        self.pinned = bool(line and line.pinned)
        self._buttons(line=True)
        self._write(self.summary_text, line_summary(model, pid))
        self._write(self.where_text, where_text(model, pid))
        self._write(self.notes_text, notes_text(model, pid))

    def show_version(self, model: ReadModel, pid: str, head: str) -> None:
        self.pid, self.head = pid, head
        self._write(self.facts_text, [])
        self.deep_button.state(["disabled"])
        title, summary = version_title(model, pid, head)
        set_label(self.title, title)
        set_label(self.summary, summary)
        self._buttons(line=False, notes=True)
        self._write(self.summary_text, version_card(model, pid, head))
        self._write(self.where_text, where_text(model, pid, head))
        self._write(self.notes_text, notes_text(model, pid, head))

    def show_slot(self, model: ReadModel, source: LiveSource, slot: BoardSlot) -> None:
        self.pid, self.head = None, None
        self._write(self.facts_text, [])
        self.deep_button.state(["disabled"])
        title, summary = slot_title(source, slot)
        set_label(self.title, title)
        set_label(self.summary, summary)
        self._buttons(line=False)
        self._write(self.summary_text, slot_card(model, source, slot))
        self._write(self.where_text, [])
        self._write(self.notes_text, [])
        self.pages.select(0)

    def _buttons(self, *, line: bool, notes: bool = False) -> None:
        for button in (self.notes_button, self.pin_button, self.move_button,
                       self.history_button):
            button.pack_forget()
        if line or notes:
            self.move_button.pack(side=tk.LEFT, padx=pxs(0, 6))
            self.history_button.pack(side=tk.LEFT, padx=pxs(0, 6))
            self.notes_button.pack(side=tk.LEFT)
            self.edit_button.state(["!disabled"])
        else:
            self.edit_button.state(["disabled"])
        if line:
            self.pin_button.configure(text="Unpin" if self.pinned else "Pin")
            self.pin_button.pack(side=tk.LEFT, padx=pxs(6, 0))

    def _write(self, text: tk.Text, segments: list[Segment]) -> None:
        text.configure(state="normal")
        text.delete("1.0", "end")
        for tag in [t for t in text.tag_names() if t.startswith("vlink")]:
            text.tag_delete(tag)
        for part, tags in segments:
            if tags and tags[0] == "link":
                self._link_count += 1
                name = f"vlink{self._link_count}"
                pid = tags[2] if len(tags) > 2 else self.pid
                text.tag_bind(name, "<Button-1>",
                              lambda _e, h=tags[1], p=pid: self._follow(p, h))
                rtext(text, "end", part, "link", name, "hang")
            else:
                rtext(text, "end", part, *tags, "hang")
        text.configure(state="disabled")

    # --- actions ----------------------------------------------------------------------------

    def _follow(self, pid: str | None, head: str) -> None:
        if self.on_link is not None and pid is not None:
            self.on_link(pid, head)

    def show_facts(self, segments: list[Segment], *, deep: bool = False,
                   head: str | None = None) -> None:
        """The Facts page (D4 card and "what changed") of ``head`` (the selected version, or
        the save file's newest); ``deep``: the deep section is loaded."""
        self.facts_head = head
        self._write(self.facts_text, segments)
        self.deep_button.state(["disabled"] if deep or head is None else ["!disabled"])
        set_label(self.deep_button, "Deeper detail read" if deep else "Read deeper detail")

    def _deep(self) -> None:
        if self.on_deep is not None and self.facts_head is not None:
            self.on_deep(self.facts_head)

    def _notes(self) -> None:
        if self.on_notes is not None and self.pid is not None:
            self.on_notes(self.pid, self.head)

    def _move(self) -> None:
        if self.on_move is not None and self.pid is not None:
            self.on_move(self.pid, self.head)

    def _history(self) -> None:
        if self.on_history is not None and self.pid is not None:
            self.on_history(self.pid, self.head)

    def _pin(self) -> None:
        if self.on_pin is not None and self.pid is not None:
            self.on_pin(self.pid, not self.pinned)

    def text_of(self, page: str) -> str:
        """A page's text (tests and the redaction scan)."""
        widget = {"summary": self.summary_text, "where": self.where_text,
                  "notes": self.notes_text, "facts": self.facts_text}[page]
        return widget.get("1.0", "end")
