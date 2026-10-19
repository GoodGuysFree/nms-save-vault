"""The content index in the Organizer tab (D6 U21-U22, D4): the progress strip, the facts cells of
the table, the version card and "what changed" text, and the first-run banner.

The index never blocks the Tk thread. Everything here reads what D4 keeps in memory
(``view``, ``progress``, ``events_since``, ``changes`` - a queue operation at worst); anything
that needs the cache (``card``) runs in the UI worker. Words stay honest: a version whose facts
are not read yet shows "...", an unreadable one says so, and a change is only ever described
against the drawn parent (nothing is guessed).
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..dpi import px, pxs
from ..fmt import local_time, play_time
from ..redact import set_label

POLL_MS = 500
#: The facts columns of the table: (column key, title, FactsView field).
FACTS_COLUMNS = (("glyphs", "Glyphs", "glyphs"), ("parts", "Parts", "base_parts"),
                 ("legacy", "Legacy", "legacy_placed"), ("ships", "Ships", "ships"))
NOT_READ = "..."
BANNER_TEXT = ("The organizer is reading what is inside your saves (story and Atlas progress, "
               "bases, ships, companions). Facts columns show \"...\" until a version has been "
               "read; the save file you select and the live saves go first. Nothing waits for it.")


def facts_cell(view, field: str, *, active: bool) -> str:
    """One facts cell: the number, "..." while not read, "unreadable" when the read failed."""
    if view is None:
        return NOT_READ if active else ""
    if getattr(view, "status", "ok") in ("failed", "unreadable"):
        return "unreadable"
    value = getattr(view, field, None)
    return "" if value is None else str(value)


def facts_values(view, *, active: bool) -> dict[str, str]:
    return {key: facts_cell(view, field, active=active) for key, _t, field in FACTS_COLUMNS}


def change_note(provider, row) -> str:
    """The Note cell of a version without a label: ``auto: `` + the top changes against the
    drawn parent; empty when there is no recorded parent or the comparison is not ready."""
    if provider is None or row.parent is None:
        return ""
    found = provider.changes(row.head, row.parent, edge_kind=row.parent_kind)
    if found is None or not found.items:
        return "" if found is None else f"auto: {found.header}"
    return "auto: " + "; ".join(c.text for c in found.top(3))


def _pair(value) -> str:
    return f"{value[0]}/{value[1]}" if value else ""


def view_lines(view) -> list[tuple[str, str]]:
    """(label, text) rows from a facts view for the version card and the Move dialog."""
    if view is None:
        return [("Contents", NOT_READ)]
    if getattr(view, "status", "ok") in ("failed", "unreadable"):
        return [("Contents", "unreadable")]
    rows: list[tuple[str, str]] = []
    if view.story:
        rows.append(("Story", _pair(view.story) + (f" - {view.story_ending}" if view.story_ending else "")))
    if view.atlas:
        rows.append(("Atlas", _pair(view.atlas)))
    expedition = view.expedition
    if expedition:
        rows.append(("Expedition", f"{expedition.get('number') or '?'} {expedition.get('name', '')} "
                                   f"{expedition.get('done', 0)}/{expedition.get('total', 0)}".strip()))
    if view.current_galaxy:
        rows.append(("Galaxy", str(view.current_galaxy)))
    if view.glyphs is not None:
        rows.append(("Glyphs", f"{view.glyphs} of 16"))
    if view.own_bases or view.base_parts:
        rows.append(("Bases", f"{view.own_bases} - {view.base_parts} parts"))
    if view.largest_base:
        rows.append(("Largest base", f"{view.largest_base[0]} ({view.largest_base[1]} parts)"))
    if view.legacy_placed:
        rows.append(("Legacy parts", str(view.legacy_placed)))
    if view.galaxies_with_bases:
        rows.append(("Galaxies with bases", ", ".join(f"{g} {n}" for g, n in view.galaxies_with_bases)))
    if view.ships or view.corvettes:
        rows.append(("Ships", f"{view.ships}" + (f" + {view.corvettes} corvettes" if view.corvettes else "")))
    if view.companions:
        rows.append(("Companions", ", ".join(view.companion_names) or str(view.companions)))
    if view.freighter:
        rows.append(("Freighter", str(view.freighter)))
    if view.journey_rank:
        rows.append(("Journey rank", str(view.journey_rank)))
    if view.systems_discovered is not None:
        rows.append(("Systems discovered", str(view.systems_discovered)))
    return rows or [("Contents", "nothing recorded yet")]


def card_lines(card) -> list[tuple[str, str]]:
    """The deep section of a version card (D4 2.10.4), flattened to label/text rows."""
    deep = getattr(card, "deep", None)
    if not deep:
        return []
    out: list[tuple[str, str]] = []
    for key, value in deep.items():
        if key == "vid":
            continue                                    # internal id, not something to read
        if key == "contexts" and isinstance(value, dict):
            out += [(f"Context {name}", _context_text(ctx)) for name, ctx in value.items()]
        else:
            out.append((str(key).replace("_", " ").capitalize(), _plain(value)))
    return out


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _context_text(ctx) -> str:
    if not isinstance(ctx, dict):
        return _plain(ctx)
    bases = ctx.get("bases") or []
    parts = sum(sum(b.get("parts_by_id", {}).values()) for b in bases if isinstance(b, dict))
    slots = sum(len(v) for v in (ctx.get("inventories") or {}).values() if isinstance(v, list))
    loose = sum(v for v in (ctx.get("loose_objects") or {}).values() if isinstance(v, int))
    return (f"{_count(len(bases), 'base')} ({parts:,} parts), {_count(slots, 'inventory slot')}, "
            f"{_count(loose, 'loose object')}")


def _plain(value) -> str:
    """Never a Python repr: lists and mappings become short readable text."""
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain(v) for v in value[:12]) + (" ..." if len(value) > 12 else "")
    if isinstance(value, dict):
        return ", ".join(f"{k} {_plain(v)}" for k, v in list(value.items())[:12])
    return str(value)


def change_lines(changes) -> list[str]:
    """"What changed since #n" for a version with a drawn parent, from D4's ChangeSet."""
    if changes is None:
        return [NOT_READ]
    return changes.lines()


def segments(rows: list[tuple[str, str]], *, indent: bool = True) -> list[tuple[str, tuple]]:
    """Label/text rows as text segments (``k`` key, plain value) for a read-only Text widget."""
    out: list[tuple[str, tuple]] = []
    for label, text in rows:
        out.append((f"{label}\t", ("k",)))
        out.append((f"{text}\n", ()))
    return out


class IndexStrip(ttk.Frame):
    """The progress strip (D6 2.8): a determinate bar and the index's own message, Pause /
    Resume and Cancel. Polled from the Tk loop (memory reads only); hidden while the index
    is idle or off. ``on_events`` gets what ``events_since`` returned, for the rows to refresh."""

    def __init__(self, master, *, provider: Callable[[], object | None],
                 on_events: Callable[[list], None]) -> None:
        super().__init__(master, padding=pxs(4, 2))
        self.provider, self.on_events = provider, on_events
        self.seq = 0
        self._service = None
        self._after: str | None = None
        self.state = "off"
        self.bar = ttk.Progressbar(self, mode="determinate", length=px(180))
        self.bar.pack(side=tk.LEFT)
        self.message = ttk.Label(self, text="", style="Muted.TLabel", anchor="w")
        self.message.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=px(8))
        self.cancel_button = ttk.Button(self, text="Cancel", command=self._cancel)
        self.cancel_button.pack(side=tk.RIGHT)
        self.pause_button = ttk.Button(self, text="Pause", command=self._pause)
        self.pause_button.pack(side=tk.RIGHT, padx=px(4))
        self.shown = False

    def start(self) -> None:
        if self._after is None:
            self._after = self.after(POLL_MS, self.poll)

    def stop(self) -> None:
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def poll(self) -> None:
        self._after = None
        service = self.provider()
        if service is not None and getattr(service, "active", False):
            if service is not self._service:        # a new index service numbers its events from 1
                self._service, self.seq = service, 0
            events = service.events_since(self.seq)
            if events:
                self.seq = events[-1].seq
                self.on_events(events)
            self.update_from(service.progress())
        else:
            self._show(False)
        self._after = self.after(POLL_MS, self.poll)

    def update_from(self, p) -> None:
        self.state = p.state
        busy = p.state in ("running", "throttled", "paused", "error")
        total = p.done + p.running + p.queued
        self.bar.configure(maximum=max(1, p.total_bytes or total or 1),
                           value=p.done_bytes if p.total_bytes else p.done)
        text = p.message
        if p.eta_s and p.state in ("running", "throttled"):
            text += f" - about {int(p.eta_s)} s left"
        set_label(self.message, text)
        set_label(self.pause_button, "Resume" if p.state == "paused" else "Pause")
        self._show(busy)

    def _show(self, show: bool) -> None:
        if show and not self.shown:
            self.pack(side=tk.TOP, fill=tk.X, padx=px(6))
            self.shown = True
        elif not show and self.shown:
            self.pack_forget()
            self.shown = False

    def _pause(self) -> None:
        service = self.provider()
        if service is None:
            return
        (service.resume if self.state == "paused" else service.pause)()
        self.update_from(service.progress())

    def _cancel(self) -> None:
        service = self.provider()
        if service is not None:
            service.cancel()
            self.update_from(service.progress())


def first_run_banner(master, on_dismiss: Callable[[], None]) -> ttk.Frame:
    """The dismissible first-run note (shown once; the owner's "do not show again" is the x)."""
    bar = ttk.Frame(master, style="Alert.TFrame", padding=pxs(8, 4))
    ttk.Label(bar, text=BANNER_TEXT, style="Alert.TLabel", anchor="w", wraplength=px(900),
              justify="left").pack(side=tk.LEFT, fill=tk.X, expand=True)
    ttk.Button(bar, text="Dismiss", command=on_dismiss).pack(side=tk.RIGHT)
    return bar
