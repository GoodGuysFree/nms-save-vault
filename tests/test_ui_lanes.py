"""Lane images in the version rows (D6 U23): D1's lanes as cells, drawn the same in the table
and in the history window."""
from __future__ import annotations

import tkinter as tk

import pytest

from nms_save_vault.ui import raster
from nms_save_vault.ui.organizer import lanes as L
from nms_save_vault.ui.organizer import table as T

from .test_organizer_history import _forked, game_not_running  # noqa: F401
from .test_organizer_readmodel import PID


@pytest.fixture
def forked(tmp_path):
    w, vids, _ = _forked(tmp_path)
    return w.refresh(), vids


def test_cells_follow_the_lanes(forked):
    model, (a, b, c, d, e) = forked
    cells = L.lane_cells(model.history(PID, "all"))
    assert cells[d] == L.LaneCell(0, 2, (), False, True, (), cells[d].kind, "steam")
    assert cells[c].lane == 1 and cells[c].down and not cells[c].up
    assert cells[b].passing == (1,) and cells[b].up and cells[b].down
    assert cells[a].joins == (1,) and cells[a].up and not cells[a].down


def test_a_cell_is_drawn_in_its_lane_colours():
    cell = L.LaneCell(1, 2, (0,), False, True, (), "dot")
    img = raster.lane_row(40, 24, cell, ["#ff0000", "#00ff00"], scale=1.0)
    assert img.pixels[(6, 3)] == "#ff0000"           # lane 0 passes through
    assert img.pixels[(18, 12)] == "#00ff00"         # the node, in lane 1's colour
    assert (18, 3) not in img.pixels                 # nothing above: no child
    live = raster.lane_row(40, 24, L.LaneCell(0, 1, (), False, False, (), "live"),
                           ["#ff0000"], scale=1.0, live_colour="#0000ff")
    assert live.pixels[(6, 12)] == "#ffffff" and live.pixels[(6, 8)] == "#0000ff"


@pytest.fixture
def host(gui_app):
    top = tk.Toplevel(gui_app)
    top.withdraw()
    yield top
    top.destroy()


def test_the_table_draws_the_lanes_of_an_open_line(host, gui_app, forked):
    model, (a, b, c, d, e) = forked
    table = T.OrganizerTable(host, themed=gui_app.themed, prefs={}, save_prefs=lambda: None)
    table.pack()
    table.set_model(model)
    table.set_open(PID, True)
    gui_app.update()
    keys = {spec.head: spec.image for spec in
            T.version_specs(model, PID, {}) if spec.head is not None}
    assert [k[0] for k in (keys[d], keys[c], keys[b], keys[a])] == ["lane"] * 4
    assert keys[e] != ("lane", None) and keys[e][0] != "lane"      # dated: a grey dot
    assert keys[c][1].lane == 1 and keys[a][1].joins == (1,)
    viid = table._child_iids[PID][("v", c)]
    name = table.tree.item(viid, "image")
    name = name[0] if isinstance(name, (tuple, list)) else name
    assert str(name) == str(table._images[keys[c]])
    assert int(table._images[keys[c]].width()) > 18      # two lanes are wider than a dot
    before = table._images[keys[c]]
    table._draw_images()                                  # a zoom or theme change
    assert table._image(keys[c]) is not before
