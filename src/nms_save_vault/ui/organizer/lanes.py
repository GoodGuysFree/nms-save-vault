"""The lanes of the history, as cells (D6 3.1.3, U23): for every drawn version the lane it is
in, the lanes passing its row, whether its line goes up and down, and the branches that join
in at its row. Pure functions of D1's ``HistoryGraph`` - the table and the history window
draw the same cells, so a tree looks the same in both."""
from __future__ import annotations

from dataclasses import dataclass

from ...core.organizer.history import HistoryGraph

#: More lanes than this are drawn, but they repeat the colours of the first ones.
LANE_COLOURS = 4


@dataclass(frozen=True)
class LaneCell:
    lane: int
    lanes: int                      # how many lanes the whole tree has (the image's width)
    passing: tuple[int, ...]        # lanes with a line through this row (no node)
    up: bool                        # a child in the same lane above
    down: bool                      # a parent below (the line goes on to it)
    joins: tuple[int, ...]          # lanes of children that join this node from the side
    kind: str                       # "dot" | "live" | "member"
    platform: str = "steam"


def lane_cells(graph: HistoryGraph) -> dict[str, LaneCell]:
    """Cells by drawn node (``vid``)."""
    lane_of = {n.vid: n.lane for n in graph.nodes}
    children: dict[str, list[int]] = {}
    has_parent = set()
    for e in graph.edges:
        if e.parent in lane_of and e.child in lane_of:
            children.setdefault(e.parent, []).append(lane_of[e.child])
            has_parent.add(e.child)
    out = {}
    for n in graph.nodes:
        kids = children.get(n.vid, [])
        live = n.kind == "version" and bool(n.version.live_in)
        out[n.vid] = LaneCell(
            n.lane, max(graph.lanes, 1), graph.pass_through[n.row],
            n.lane in kids, n.vid in has_parent,
            tuple(sorted({k for k in kids if k != n.lane})),
            "member" if n.kind == "fork-member" else ("live" if live else "dot"),
            n.version.platform)
    return out


def lane_width(lanes: int) -> int:
    """Design pixels for ``lanes`` lanes (at least the width of a plain dot)."""
    return max(18, 12 * max(lanes, 1) + 6)
