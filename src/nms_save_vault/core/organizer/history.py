"""The HistoryGraph (D1 2.10.4, 2.10.5): one save file's recorded history as a tree, in lanes
every UI draws the same way, with every other version listed by date.

Pure data: no file is read, nothing is drawn. The tree holds only what the app recorded or
observed (journal edges, "seen in backups" links, the owner's corrections); a version with no
such link is a dated row - nothing is guessed. The read model prepares the inputs
(:class:`HNode`); :func:`project` folds or collapses them by mode and assigns the lanes.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from .model import Edge, EdgeKind, EdgeMethod, EdgeTier, Pid, Platform, Vid

if TYPE_CHECKING:
    from .readmodel import VersionRow

MODES = ("default", "all", "key")


@dataclass(frozen=True)
class HNode:
    """One candidate node, before folding: a pair head that is in the tree, or a fork point
    that is no head (``kind`` "fork-member"; ``head`` names the pair it belongs to)."""

    vid: Vid
    head: Vid
    kind: Literal["version", "fork-member"]
    parent: Vid | None          # display parent: the nearest ancestor that is a candidate
    edge: Edge | None           # the member edge into ``vid``
    order: tuple                # newest sorts last
    platform: Platform


@dataclass(frozen=True)
class GraphNode:
    vid: Vid
    row: int                    # 0 = newest
    lane: int
    version: VersionRow         # for a non-head fork point: the row of a pair it belongs to
    kind: Literal["version", "fork-member"]
    flags: frozenset[str]
    folded: Mapping[str, int]   # {"touch": n, "edit": m} folded into this node


@dataclass(frozen=True)
class GraphEdge:
    parent: Vid
    child: Vid
    tier: EdgeTier
    kind: EdgeKind
    method: EdgeMethod
    op_id: str | None
    cross_platform: bool
    hidden: int                 # versions collapsed or folded on this path


@dataclass(frozen=True)
class CrossLink:                # an edge to or from another save file (a Split)
    vid: Vid
    other_pid: Pid
    other_vid: Vid


@dataclass(frozen=True)
class HistoryGraph:
    pid: Pid
    mode: str
    nodes: tuple[GraphNode, ...]                 # the drawn tree, in row order
    edges: tuple[GraphEdge, ...]
    lanes: int
    pass_through: tuple[tuple[int, ...], ...]    # per row: lanes with a line passing (no node)
    links: tuple[CrossLink, ...]
    dated: tuple[VersionRow, ...]                # every other version, newest first


def project(pid: Pid, mode: str, rows: Sequence[VersionRow], nodes: Sequence[HNode],
            links: Sequence[CrossLink] = ()) -> HistoryGraph:
    """The history of one save file. ``rows`` are its versions, newest first."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    by_head = {r.head: r for r in rows}
    by_vid = {n.vid: n for n in nodes if n.head in by_head}

    def row_of(n: HNode) -> VersionRow:
        return by_head[n.head]

    def is_key(n: HNode) -> bool:
        return n.kind == "fork-member" or row_of(n).key

    if mode == "all":
        kept = set(by_vid)
    elif mode == "key":
        kept = {v for v, n in by_vid.items() if is_key(n)}
    else:
        kept = {v for v, n in by_vid.items()
                if n.kind == "fork-member" or row_of(n).folded_into is None}

    def climb(n: HNode) -> tuple[Vid | None, int]:
        """The nearest kept ancestor and how many versions lie between."""
        hidden, p = 0, n.parent
        while p is not None and p not in kept:
            nxt = by_vid.get(p)
            hidden += 1
            p = nxt.parent if nxt is not None else None
        return (p if p in kept else None), hidden

    folded: dict[Vid, Counter] = {v: Counter() for v in kept}
    if mode == "default":
        for v, n in by_vid.items():
            if v in kept:
                continue
            into, _ = climb(n)
            kind = n.edge.kind if n.edge is not None else ""
            if into is not None and kind in ("touch", "edit"):
                folded[into][kind] += 1

    ordered = sorted((by_vid[v] for v in kept), key=lambda n: n.order, reverse=True)
    row_index = {n.vid: i for i, n in enumerate(ordered)}
    parent: dict[Vid, Vid | None] = {}
    hidden_of: dict[Vid, int] = {}
    for n in ordered:
        parent[n.vid], hidden_of[n.vid] = climb(n)

    lane_of, spans = _lanes(ordered, parent, row_index, by_head)
    lanes = max(lane_of.values(), default=-1) + 1
    pass_through = []
    for r in range(len(ordered)):
        own = lane_of[ordered[r].vid]
        pass_through.append(tuple(sorted(
            {lane for lane, lo, hi, parent_row in spans
             if lo <= r <= hi and lane != own and r != parent_row})))

    graph_nodes = []
    for n in ordered:
        row = row_of(n)
        flags = row.flags if n.kind == "version" else frozenset({"fork"})
        graph_nodes.append(GraphNode(n.vid, row_index[n.vid], lane_of[n.vid], row, n.kind,
                                     flags, dict(folded[n.vid])))
    edges = []
    for n in ordered:
        p = parent[n.vid]
        if p is None:
            continue
        e = n.edge
        edges.append(GraphEdge(
            p, n.vid, e.tier if e else "observed", e.kind if e else "unknown",
            e.method if e else "journal", e.op_id if e else None,
            by_vid[p].platform != n.platform, hidden_of[n.vid]))
    shown = tuple(k for k in links if k.vid in kept)
    dated = tuple(r for r in rows if not r.in_tree)
    return HistoryGraph(pid, mode, tuple(graph_nodes), tuple(edges), lanes,
                        tuple(pass_through), shown, dated)


def _lanes(ordered: Sequence[HNode], parent: Mapping[Vid, Vid | None],
           row_index: Mapping[Vid, int], by_head: Mapping[Vid, VersionRow]
           ) -> tuple[dict[Vid, int], list[tuple[int, int, int, int]]]:
    """D1 2.10.5 rules 2-6: each node's lane, and each branch's (lane, first row, last row,
    its parent's row or -1)."""
    kids: dict[Vid, list[HNode]] = {}
    for n in ordered:
        if parent[n.vid] is not None:
            kids.setdefault(parent[n.vid], []).append(n)
    by_vid = {n.vid: n for n in ordered}
    latest: dict[Vid, tuple] = {}
    for root in (n for n in ordered if parent[n.vid] is None):
        stack = [(root.vid, False)]
        while stack:
            vid, done = stack.pop()
            if not done:
                stack.append((vid, True))
                stack.extend((c.vid, False) for c in kids.get(vid, ()))
            else:
                latest[vid] = max([by_vid[vid].order] + [latest[c.vid] for c in kids.get(vid, ())])

    def live(n: HNode) -> bool:
        return n.kind == "version" and "live" in by_head[n.head].flags

    main: dict[Vid, Vid] = {}
    for vid, children in kids.items():
        main[vid] = max(children, key=lambda c: (latest[c.vid], live(c), c.vid)).vid
    branches = []
    for n in ordered:
        p = parent[n.vid]
        if p is None or main[p] != n.vid:
            chain = [n.vid]
            while chain[-1] in main:
                chain.append(main[chain[-1]])
            branches.append(chain)
    branches.sort(key=lambda c: c[0])
    branches.sort(key=lambda c: latest[c[0]], reverse=True)
    placed: list[tuple[int, int, int]] = []
    lane_of: dict[Vid, int] = {}
    spans: list[tuple[int, int, int, int]] = []
    for chain in branches:
        rows = [row_index[v] for v in chain]
        p = parent[chain[0]]
        if p is not None:
            rows.append(row_index[p])
        lo, hi = min(rows), max(rows)
        lane = 0
        while any(l == lane and lo <= h and a <= hi for a, h, l in placed):
            lane += 1
        placed.append((lo, hi, lane))
        spans.append((lane, lo, hi, row_index[p] if p is not None else -1))
        for v in chain:
            lane_of[v] = lane
    return lane_of, spans


# --- ASCII, as the CLI prints it ----------------------------------------------------

_METHOD_NOTE = {"capture-sequence": "seen in backups", "owner": "set by you"}


def format_history(graph: HistoryGraph, name: str, *,
                   live: Mapping[Vid, Sequence[str]] | None = None,
                   behind: Mapping[Vid, int] | None = None,
                   tz: timezone = timezone.utc) -> list[str]:
    """The history as text lines: ``*`` a node, ``|`` a lane passing, ``/`` the connector
    into its parent's lane, ``-`` a dated row (times in ``tz``, UTC by default)."""
    live = live or {}
    behind = behind or {}
    edge_into = {e.child: e for e in graph.edges}
    kids = Counter(e.parent for e in graph.edges)
    connect: dict[int, bool] = {}
    for e in graph.edges:
        child = next(n for n in graph.nodes if n.vid == e.child)
        par = next(n for n in graph.nodes if n.vid == e.parent)
        if child.lane > par.lane:
            connect[par.row] = True
    linked = sum(1 for _ in graph.nodes)
    out = [f'{name}, mode "{graph.mode}": {linked} linked versions, {graph.lanes} '
           f'lane{"s" if graph.lanes != 1 else ""}; {len(graph.dated)} listed by date']
    width = max(graph.lanes, 1)

    def cells(row: int, lane: int | None, mark: str) -> str:
        cs = [" "] * width
        for pl in graph.pass_through[row] if lane is not None else ():
            cs[pl] = "|"
        if lane is not None:
            cs[lane] = mark
        text = ""
        for i, c in enumerate(cs):
            text += c + ("/" if connect.get(row) and lane is not None and i == lane else " ")
        return text

    for node in graph.nodes:
        v = node.version
        notes = list(live.get(node.vid, ()) or ())
        notes = [f"live {', '.join(notes)}"] if notes else []
        for flag in ("tip", "fork", "root", "placed", "evicted", "locked"):
            if flag in node.flags and (flag != "tip" or kids[node.vid] == 0):
                notes.append(flag)
        e = edge_into.get(node.vid)
        if e is not None and e.method in _METHOD_NOTE:
            notes.append(_METHOD_NOTE[e.method])
        if e is not None and e.hidden and graph.mode == "key":
            notes.append(f"{e.hidden} versions hidden")
        for kind, count in sorted(node.folded.items()):
            notes.append(f"+{count} {'re-saved' if kind == 'touch' else 'edited'}")
        if node.kind == "fork-member":
            notes.append("fork point (partner)")
        if v.label:
            notes.append(f'"{v.label}"')
        out.append(f"{cells(node.row, node.lane, '*')} {_stamp(v, tz)}  "
                   f"{_hours(v.play_time):>6}  {_plat(v.platform)}  {node.vid[:8]}  "
                   f"{', '.join(notes)}".rstrip())
    if graph.dated:
        out.append("     no recorded link, by date:" if graph.nodes
                   else "     no recorded history, by date:")
        for v in graph.dated:
            notes = list(live.get(v.head, ()) or ())
            notes = [f"live {', '.join(notes)}"] if notes else []
            if behind.get(v.head):
                notes.append(f"copy {_hm(behind[v.head])} behind")
            if v.folded_into and graph.mode != "all":
                notes.append("re-save, no play")
            if v.label:
                notes.append(f'"{v.label}"')
            out.append(f"-{' ' * (2 * width - 1)} {_stamp(v, tz)}  {_hours(v.play_time):>6}  "
                       f"{_plat(v.platform)}  {v.head[:8]}  {', '.join(notes)}".rstrip())
    return out


def _stamp(v: VersionRow, tz: timezone) -> str:
    return datetime.fromtimestamp(v.saved_at, tz).strftime("%Y-%m-%d %H:%M")


def _hours(seconds: int) -> str:
    return f"{seconds / 3600:.1f}h"


def _hm(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60:02d}"


def _plat(platform: str) -> str:
    return "X" if platform == "xbox" else "S"
