"""Performance smoke of the organizer table (D6 6): 300 save files with 40 versions each
(12,000 version rows) fill the table quickly, and a new model that changes one line updates
it incrementally. Synthetic read-model objects, no files. Budgets are generous for a busy
machine; the design targets are 300 ms and 20 ms."""
from __future__ import annotations

import time
import tkinter as tk

import pytest

from nms_save_vault.core.organizer.notes import NotesState
from nms_save_vault.core.organizer.readmodel import Line, LiveBoard, ReadModel, VersionRow
from nms_save_vault.ui.organizer import table as T

LINES, VERSIONS = 300, 40


def _line(n: int, note: str = "") -> Line:
    pid = f"{n:016X}"
    return Line(pid=pid, raw_pids=(pid,), identified=True, name=f"Save {n:03d}",
                former_names=(), difficulty="Normal", mode="", platforms=("steam",), live=(),
                platform_tips=(), cross_platform="n/a", play_time=3600 * n, last_played=n,
                version_count=VERSIONS, gone_count=0, branch_count=1, note_preview=note,
                has_note=bool(note), pinned=False, locked_count=0, flags=frozenset(),
                representative=f"{n:08x}" + "0" * 56)


def _row(n: int, v: int) -> VersionRow:
    head = f"{n:08x}{v:08x}" + "0" * 48
    return VersionRow(pid=f"{n:016X}", pair=(head, None), head=head, head_member="A",
                      partner=None, name=f"Save {n:03d}", summary="", play_time=60 * v,
                      saved_at=v, difficulty="Normal", mode="", platform="steam",
                      locations=(), live_in=(), durable_copies=1, flags=frozenset(),
                      parent=None, parent_tier=None, parent_kind=None, parent_method=None,
                      children=0, in_tree=v % 2 == 0, key=False, label="", comment="",
                      locked=False, note_rev=None)


def _model(changed: int | None = None) -> ReadModel:
    lines = {f"{n:016X}": _line(n, "changed" if n == changed else "") for n in range(LINES)}
    versions = {f"{n:016X}": tuple(_row(n, v) for v in range(VERSIONS, 0, -1))
                for n in range(LINES)}
    return ReadModel(lines, versions, LiveBoard(()), {}, {}, NotesState({}, {}))


@pytest.fixture
def table(gui_app):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    widget = T.OrganizerTable(top, themed=gui_app.themed, prefs={}, save_prefs=lambda: None)
    widget.pack()
    yield widget
    top.destroy()


def test_300_lines_and_12000_version_rows_fill_quickly(table):
    model = _model()
    started = time.perf_counter()
    table.set_model(model)
    for pid in list(table._iid_of):
        table.set_open(pid, True)
    filled = time.perf_counter() - started
    rows = sum(len(table.tree.get_children(i)) for i in table.tree.get_children(""))
    assert rows >= LINES * VERSIONS
    assert filled < 8.0, f"{filled:.2f} s to fill"          # a hang, not a slow machine

    # One changed line must cost a fraction of a full fill. Measured against the fill taken a
    # moment before (so a busy machine slows both) and as the best of three (so one stall
    # during a parallel run does not count).
    incremental = float("inf")
    for changed in (7, 8, 9):
        started = time.perf_counter()
        table.set_model(_model(changed=changed))
        incremental = min(incremental, time.perf_counter() - started)
        assert table.tree.set(table._iid_of[f"{changed:016X}"], "note") == "changed"
    assert incremental < filled / 2, (
        f"{incremental * 1000:.0f} ms for one changed line, {filled * 1000:.0f} ms to fill")
    print(f"\n[perf] fill {filled * 1000:.0f} ms, incremental {incremental * 1000:.0f} ms")
