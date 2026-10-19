"""The read model (D1 2.10): what the organizer view and the CLI show, built from one scan,
the journal's replay, lineage.json and the notes.

- :class:`Line` - one save file (playthrough), with where it is live, its Steam and Xbox
  copies compared, its note and flags;
- :class:`VersionRow` - one version (a pair of members), where its bytes are and its drawn
  parent (evidence only: nothing is inferred; without one it is listed by date);
- :class:`LiveBoard` - the 15 slots of every live folder;
- :class:`LineEvent` - what happened to a save file's slots, in plain words.

A pure function of its inputs: building it reads no file. Every label it makes is ASCII;
names, summaries and notes are the game's and the owner's own text. No account key or path
appears in ``to_dict()``: live slots travel as opaque tokens (``<platform>:<source>:<slot>``).
"""
from __future__ import annotations

import dataclasses
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from .. import aliases, formats, gameversion
from ..state import Source
from .journal import JournalState, ObsRecord, OpRecord
from .history import CrossLink, HistoryGraph, HNode, project
from .lineage import LineageGraph, LineageState
from .model import (
    EdgeKind,
    EdgeMethod,
    EdgeTier,
    EntryRef,
    JournalEvent,
    LiveRef,
    LiveTarget,
    Member,
    MemberObs,
    MetaSnap,
    PairKey,
    Pid,
    Platform,
    RawLocation,
    StoreRef,
    Vid,
    head_of,
    is_identified,
)
from .notes import NotesState
from .scanner import ScanResult, live_refs

CrossPlatform = Literal["n/a", "same", "steam-behind", "xbox-behind", "diverged"]

#: Operations that put a pair into one slot or took it out (whole-folder restores bind
#: slots in the journal but do not mark versions).
SLOT_OPS = ("place", "place_into_empty", "restore_older", "swap", "repopulate", "clear")
RECENT_EVENT_SECONDS = 30 * 86400
EXPEDITION_MODE = 6


# --- the content index, as the read model sees it (D1 2.10, D4 2.6) -----------


@runtime_checkable
class FactsProvider(Protocol):
    """The part of D4's ``IndexService`` the organizer uses. Every call is non-blocking: a
    memory read or a queue operation, safe on the UI thread. ``view`` is a D4 ``FactsView``
    (opaque here but for ``numbers``); None means "not indexed (or loaded) yet", and the read
    model shows meta-level columns. ``is_milestone`` answers from what is already computed:
    None means "not known yet", counted as no milestone. ``Priority`` holds the request tiers
    (``LIVE``, ``TIP``, ``NEW``, ``BACKFILL``), so the organizer never imports the index."""

    Priority: Any

    def view(self, vid: Vid) -> Any | None: ...
    def changes(self, child: Vid, parent: Vid, *, edge_kind: str | None = None) -> Any | None: ...
    def is_milestone(self, child: Vid, parent: Vid) -> bool | None: ...
    def request(self, vids: Iterable[Vid], priority: Any) -> int: ...


# --- the objects --------------------------------------------------------------


@dataclass(frozen=True)
class LiveBadge:
    target: LiveTarget
    label: str              # "S9", "X8"; "S2.7" when a platform has several live folders
    head: Vid
    member_loaded: Member
    is_tip: bool            # the live head has no drawn child
    behind_by: int          # play seconds behind the line's most-progressed live head


@dataclass(frozen=True)
class PlatformTip:
    platform: Platform
    target: LiveTarget
    head: Vid
    play_time: int
    saved_at: int
    behind_by: int          # play seconds behind the other platform's tip; 0 if not behind


@dataclass(frozen=True)
class Line:
    pid: Pid
    raw_pids: tuple[Pid, ...]
    identified: bool
    name: str
    former_names: tuple[str, ...]
    difficulty: str
    mode: str
    platforms: tuple[Platform, ...]
    live: tuple[LiveBadge, ...]
    platform_tips: tuple[PlatformTip, ...]
    cross_platform: CrossPlatform
    play_time: int
    last_played: int
    version_count: int
    gone_count: int
    branch_count: int
    note_preview: str
    has_note: bool
    pinned: bool
    locked_count: int
    flags: frozenset[str]
    representative: Vid
    facts: object | None = None


@dataclass(frozen=True)
class LocationSummary:
    kind: Literal["live", "vault", "external"]
    label: str
    location: RawLocation


@dataclass(frozen=True)
class VersionRow:
    pid: Pid
    pair: PairKey
    head: Vid
    head_member: Member
    partner: Vid | None
    name: str
    summary: str
    play_time: int
    saved_at: int
    difficulty: str
    mode: str
    platform: Platform
    locations: tuple[LocationSummary, ...]
    live_in: tuple[LiveTarget, ...]
    durable_copies: int
    flags: frozenset[str]
    parent: Vid | None              # drawn parent; None = "no recorded parent"
    parent_tier: EdgeTier | None
    parent_kind: EdgeKind | None
    parent_method: EdgeMethod | None
    children: int
    in_tree: bool                   # False: listed by date
    key: bool
    label: str
    comment: str
    locked: bool
    note_rev: int | None
    folded_into: Vid | None = None
    facts: object | None = None


@dataclass(frozen=True)
class BoardSlot:
    target: LiveTarget
    label: str
    state: Literal["empty", "occupied", "partial", "unreadable"]
    pid: Pid | None
    name: str
    head: Vid | None
    partner: Vid | None
    member_loaded: Member | None
    play_time: int
    saved_at: int
    vault_copy: Literal["durable", "partial", "outside-vault", "none"]
    pinned: bool
    twin: bool
    other_platform: tuple[LiveTarget, ...]
    cloud: str
    pending_op: str | None
    last_event: JournalEvent | None
    suggest_rank: int | None


@dataclass(frozen=True)
class LiveSource:
    ref: LiveRef
    label: str
    platform: Platform
    writable: bool
    steam_cloud: bool | None
    slots: tuple[BoardSlot, ...]


@dataclass(frozen=True)
class LiveBoard:
    sources: tuple[LiveSource, ...]


@dataclass(frozen=True)
class LineEvent:
    at: int
    kind: str
    target: LiveTarget
    op_id: str | None
    vids: tuple[Vid, ...]
    text: str


@dataclass(frozen=True)
class LineFilter:
    live: bool | None = None            # True: live somewhere; False: vault only
    platform: Platform | None = None
    cross_platform: bool | None = None
    has_note: bool | None = None
    pinned: bool | None = None
    at_risk: bool | None = None
    expedition: bool | None = None
    has_locked: bool | None = None
    text: str = ""


# --- building -----------------------------------------------------------------


@dataclass
class _Pair:
    pair: PairKey
    head: Vid
    head_member: Member
    partner: Vid | None


class ReadModel:
    def __init__(self, lines: Mapping[Pid, Line], versions: Mapping[Pid, tuple[VersionRow, ...]],
                 board: LiveBoard, events: Mapping[Pid, tuple[LineEvent, ...]],
                 tokens: Mapping[LiveRef, int], notes: NotesState, *,
                 trees: Mapping[Pid, tuple] | None = None, graph: LineageGraph | None = None,
                 vid_pid: Mapping[Vid, Pid] | None = None,
                 vid_raw: Mapping[Vid, Pid] | None = None,
                 problems: tuple[str, ...] = (),
                 line_problems: Mapping[Pid, tuple[str, ...]] | None = None) -> None:
        self.problems = problems                    # the whole vault's, in plain words
        self._line_problems = line_problems or {}
        self._vid_raw = vid_raw or {}
        self._trees = trees or {}
        self.graph = graph
        self._vid_pid = vid_pid or {}
        self._lines = lines
        self._versions = versions
        self._board = board
        self._events = events
        self._tokens = dict(tokens)
        self.notes = notes

    @classmethod
    def build(cls, scan: ScanResult, journal: JournalState, lineage: LineageState,
              notes: NotesState, sources: Sequence[Source], *, now: int,
              facts: FactsProvider | None = None) -> ReadModel:
        """``facts`` (D4's index) adds each line's and version's facts and the milestone
        rule of the key versions; it is only read, never waited for."""
        return _Builder(scan, journal, lineage, notes, sources, now, facts).build()

    def lines(self) -> tuple[Line, ...]:
        return tuple(self._lines.values())

    def line(self, pid: Pid) -> Line | None:
        return self._lines.get(pid)

    def versions(self, pid: Pid) -> tuple[VersionRow, ...]:
        """Newest first."""
        return self._versions.get(pid, ())

    def live_board(self) -> LiveBoard:
        return self._board

    def line_of(self, vid: Vid) -> Pid | None:
        """The save file a version (a head or a partner) belongs to."""
        return self._vid_pid.get(vid)

    def raw_pid_of(self, vid: Vid) -> Pid | None:
        """The save file id the game wrote into a version, before the owner's Combines."""
        return self._vid_raw.get(vid)

    def history(self, pid: Pid, mode: Literal["default", "all", "key"] = "default"
                ) -> HistoryGraph:
        """The line's history as a tree (D1 2.10.4, 2.10.5): only what was recorded or
        observed, lanes assigned; every other version listed by date. ``mode`` "default"
        folds re-saves without play, "all" folds nothing, "key" keeps the key versions."""
        nodes, links = self._trees.get(pid, ((), ()))
        graph = project(pid, mode, self._versions.get(pid, ()), nodes, links)
        return dataclasses.replace(graph, problems=self.problems_for(pid))

    def problems_for(self, pid: Pid) -> tuple[str, ...]:
        """The corrections left out and the conflicting evidence of one save file."""
        return self._line_problems.get(pid, ())

    def events(self, pid: Pid) -> tuple[LineEvent, ...]:
        """Newest first."""
        return self._events.get(pid, ())

    def token(self, target: LiveTarget) -> str:
        return f"{target.platform}:{self._tokens[target.ref]}:{target.slot}"

    def target_of(self, token: str) -> LiveTarget | None:
        try:
            platform, index, slot = token.split(":")
            ref = next(r for r, i in self._tokens.items() if i == int(index))
        except (ValueError, StopIteration):
            return None
        if ref.platform != platform or not 1 <= int(slot) <= formats.MAX_SAVE_SLOTS:
            return None
        return ref.target(int(slot))

    def to_dict(self, obj: Any) -> Any:
        """JSON-safe form of any read-model object (``--json``): tuples become lists, sets
        sorted lists, live slots their tokens; no account key or path is written."""
        return _jsonable(obj, self)


class _Builder:
    def __init__(self, scan: ScanResult, journal: JournalState, lineage: LineageState,
                 notes: NotesState, sources: Sequence[Source], now: int,
                 facts: FactsProvider | None = None) -> None:
        self.facts = facts
        self.scan = scan
        self.journal = journal
        self.lineage = lineage
        self.notes = notes
        self.sources = list(sources)
        self.now = now
        self._effective: dict[Vid, Pid] | None = None
        self.graph = LineageGraph.build(journal.edges, lineage)
        self.refs = live_refs(self.sources)
        self.tokens = {ref: i for i, ref in enumerate(self.refs)}
        per_platform = Counter(r.platform for r in self.refs)
        self.several = {p for p, n in per_platform.items() if n > 1}

    # --- per version --------------------------------------------------------------------

    def meta(self, vid: Vid) -> MetaSnap | None:
        info = self.scan.versions.get(vid)
        if info is not None:
            return info.meta
        obs = self.journal.metas.get(vid)
        return obs.meta if obs is not None else None

    def raw_pid(self, vid: Vid) -> Pid | None:
        info = self.scan.versions.get(vid)
        if info is not None:
            return info.raw_pid
        obs = self.journal.metas.get(vid)
        return obs.pid if obs is not None else None

    def pid(self, vid: Vid) -> Pid | None:
        """The save file ``vid`` belongs to: the owner's Combines and Splits applied."""
        if self._effective is None:
            known = {*self.scan.versions, *self.journal.metas}
            self._effective = self.graph.effective_pids(sorted(known), self.raw_pid, self.lineage)
        return self._effective.get(vid)

    def platform(self, vid: Vid) -> Platform:
        info = self.scan.versions.get(vid)
        if info is not None:
            return info.platform
        obs = self.journal.metas.get(vid)
        return obs.platform if obs is not None else "steam"

    def locations(self, vid: Vid) -> tuple[RawLocation, ...]:
        info = self.scan.versions.get(vid)
        return info.locations if info is not None else ()

    def order(self, vid: Vid) -> tuple:
        m = self.meta(vid)
        return (m.meta_ts if m else 0, m.play_time if m else 0,
                self.journal.first_seen.get(vid, (0, 0)), vid)

    # --- the whole build ------------------------------------------------------------------

    def build(self) -> ReadModel:
        pairs = _one_per_head(self._pairs())
        parent = self._parents()
        heads = {p.head for p in pairs.values()}
        children = Counter(parent.values())
        forks = {v for v, n in children.items() if n > 1}
        in_pair = {v for p in pairs.values() for v in p.pair if v}
        candidates = heads | (forks & in_pair)
        display = {h: self._display_parent(h, parent, candidates) for h in candidates}
        # An edge that leaves its save file (a Split, an unrelated line) is a link, not a branch.
        self.cross = {h: d for h, d in display.items()
                      if d is not None and self.pid(d) is not None
                      and self.pid(h) != self.pid(d)}
        for h in self.cross:
            display[h] = None
        self.cross_involved = set(self.cross) | set(self.cross.values())
        display_children = Counter(p for p in display.values() if p is not None)
        slot_ops = [r for r in self.journal.records
                    if isinstance(r, OpRecord) and r.status == "done" and r.kind in SLOT_OPS]
        self.placed = {v for r in slot_ops for v in r.pair if v}
        self.evicted = {v for r in slot_ops for v in (r.evicted or ()) if v}
        marked = self.placed | self.evicted
        live = self._live()
        by_line: dict[Pid, list[_Pair]] = defaultdict(list)
        for p in pairs.values():
            pid = self.pid(p.head)
            if pid is not None:
                by_line[pid].append(p)
        edited = {e.child for e in self.journal.edges if e.kind == "edit"}
        rows: dict[Pid, tuple[VersionRow, ...]] = {}
        lines: dict[Pid, Line] = {}
        live_by_pid: dict[Pid, list[tuple[LiveTarget, _Pair]]] = defaultdict(list)
        for target, p in live.items():
            pid = self.pid(p.head)
            if pid is not None:
                live_by_pid[pid].append((target, p))
        for pid, line_pairs in by_line.items():
            line_rows = [self._row(pid, p, pairs, parent, display, display_children, forks,
                                   marked, live, edited) for p in line_pairs]
            line_rows = _fold(self._mark_keys(sorted(line_rows,
                                                     key=lambda r: self.order(r.head),
                                                     reverse=True)))
            rows[pid] = tuple(line_rows)
            lines[pid] = self._line(pid, rows[pid], live_by_pid.get(pid, []), display_children)
        board = self._board(live, lines)
        events = self._events(lines)
        trees = self._trees(rows, display, candidates)
        by_pid, texts = self._problems(rows)
        self.problems = tuple(texts)
        return ReadModel(lines, rows, board, events, self.tokens, self.notes, trees=trees,
                         problems=self.problems, line_problems=by_pid,
                         graph=self.graph, vid_pid=dict(self._effective or {}),
                         vid_raw={v: self.raw_pid(v) for v in (self._effective or {})
                                  if self.raw_pid(v)})

    def _trees(self, rows: Mapping[Pid, tuple[VersionRow, ...]],
               display: Mapping[Vid, Vid | None], candidates: set[Vid]) -> dict[Pid, tuple]:
        """Per save file, the inputs of its HistoryGraph: the tree nodes (heads that are in the
        tree, and fork points that are no head, each in its own save file), and where edges
        cross to another save file."""
        by_vid = {v: r for line_rows in rows.values() for r in line_rows for v in r.pair if v}
        nodes: dict[Pid, list[HNode]] = defaultdict(list)
        for vid in sorted(candidates):
            row = by_vid.get(vid)
            pid = self.pid(vid)
            if row is None or pid is None:
                continue
            if vid == row.head:
                if not row.in_tree:
                    continue
                nodes[pid].append(HNode(vid, row.head, "version", display.get(vid),
                                        self.edge_of.get(vid), self.order(vid),
                                        self.platform(vid)))
            else:
                nodes[pid].append(HNode(vid, row.head, "fork-member", display.get(vid),
                                        self.edge_of.get(vid), self.order(vid),
                                        self.platform(vid), row))
        links: dict[Pid, list[CrossLink]] = defaultdict(list)
        for child, par in self.cross.items():
            cp, pp = self.pid(child), self.pid(par)
            links[cp].append(CrossLink(child, pp, par))
            links[pp].append(CrossLink(par, cp, child))
        return {pid: (tuple(nodes.get(pid, ())),
                      tuple(sorted(links.get(pid, ()), key=lambda k: (k.vid, k.other_vid))))
                for pid in {*nodes, *links, *rows}}

    def _pairs(self) -> dict[PairKey, _Pair]:
        """Every pair the app has seen: in this scan, or in the journal (gone or past)."""
        out = {key: _Pair(key, info.head, info.head_member, info.partner)
               for key, info in self.scan.pairs.items()}
        for rec in self.journal.records:
            if not isinstance(rec, ObsRecord) or rec.pair in out or rec.pair == (None, None):
                continue
            a, b = (self._obs(rec.pair[0], "A"), self._obs(rec.pair[1], "B"))
            if (rec.pair[0] and a is None) or (rec.pair[1] and b is None):
                continue
            head = head_of(a, b)
            if head is None:
                continue
            partner = rec.pair[1] if head is a else rec.pair[0]
            out[rec.pair] = _Pair(rec.pair, head.vid, "A" if head is a else "B", partner)
        return out

    def _obs(self, vid: Vid | None, member: Member) -> MemberObs | None:
        if vid is None:
            return None
        m = self.journal.metas.get(vid)
        if m is not None:
            return dataclasses.replace(m, member=member)
        info = self.scan.versions.get(vid)
        if info is None:
            return None
        return MemberObs(member, vid, info.raw_pid, info.pid_src, info.platform, info.meta)

    def _parents(self) -> dict[Vid, Vid]:
        """One parent per version (D1 2.7.1): the owner's word, else the highest tier of
        evidence, the later within a tier."""
        self.edge_of = dict(self.graph.edge_of)
        self.parent_of = dict(self.graph.parent)
        return dict(self.parent_of)

    def _view(self, vid: Vid) -> Any | None:
        return self.facts.view(vid) if self.facts is not None else None

    def _milestone(self, row: VersionRow) -> bool:
        """D4's key-version rule, on the step from the drawn parent (the row's ``parent``).
        Not known yet (None) is not a milestone."""
        if self.facts is None or row.parent is None:
            return False
        return self.facts.is_milestone(row.head, row.parent) is True

    def _problems(self, rows) -> tuple[dict[Pid, tuple[str, ...]], list[str]]:
        """Corrections that were left out and versions with conflicting evidence, in plain
        words, by the save file they belong to."""
        def stamp(unix) -> str:
            return datetime.fromtimestamp(unix, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        def saved(pid, vid) -> str:
            row = next((r for r in rows.get(pid, ()) if vid in r.pair), None)
            return f"the version saved {stamp(row.saved_at)}" if row else "a version"

        by_corr = {c.get("id"): c for c in self.lineage.corrections}
        found: list[tuple[Pid | None, str]] = []
        for cid, why in self.graph.dropped:
            c = by_corr.get(cid, {})
            child = c.get("child")
            pid = self.pid(child) if isinstance(child, str) else None
            when = f" of {stamp(c['at'])}" if type(c.get("at")) is int else ""
            found.append((pid, f"your correction{when} for {saved(pid, child)} was left out: "
                               f"{why}"))
        for child, _ in self.graph.conflicts:
            pid = self.pid(child)
            found.append((pid, f"{saved(pid, child)} has conflicting evidence of where it "
                               "comes from"))
        by_pid: dict[Pid, list[str]] = {}
        for pid, text in found:
            if pid is not None:
                by_pid.setdefault(pid, []).append(text)
        return {k: tuple(v) for k, v in by_pid.items()}, [t for _, t in found]

    @staticmethod
    def _display_parent(vid: Vid, parent: Mapping[Vid, Vid], candidates: set[Vid]) -> Vid | None:
        """The nearest ancestor that is itself shown (a pair's head or a fork point)."""
        seen = {vid}
        p = parent.get(vid)
        while p is not None and p not in candidates and p not in seen:
            seen.add(p)
            p = parent.get(p)
        return p if p is not None and p not in seen else None

    def _live(self) -> dict[LiveTarget, _Pair]:
        out = {}
        for ref, folder in self.scan.live.items():
            for capture in folder.captures:
                head = capture.head
                if head is None:
                    continue
                partner = capture.b if head.member == "A" else capture.a
                out[ref.target(capture.slot)] = _Pair(capture.pair, head.vid, head.member,
                                                      partner.vid if partner else None)
        return out

    def _row(self, pid, p, pairs, parent, display, display_children, forks, marked, live,
             edited) -> VersionRow:
        meta = self.meta(p.head)
        vids = [v for v in p.pair if v]
        locations = [loc for v in vids for loc in self.locations(v)]
        head_locations = self.locations(p.head)
        gone = not locations
        parent_vid = display.get(p.head)
        edge = self.edge_of.get(p.head)
        direct = bool(edge and parent_vid and edge.parent == parent_vid)   # else an ancestor, not that edge
        live_in = tuple(t for t, lp in live.items() if lp.head == p.head)
        note = self.notes.versions.get(p.head)
        flags = set()
        if live_in:
            flags.add("live")
        if any(lp.partner == p.head for lp in live.values()):
            flags.add("live-partner")
        n_children = display_children.get(p.head, 0)
        in_tree = (parent_vid is not None or n_children > 0 or p.head in marked
                   or p.head in self.cross_involved)
        if in_tree and n_children == 0:
            flags.add("tip")
        if n_children > 1 or p.head in forks:
            flags.add("fork")
        if in_tree and parent_vid is None:
            flags.add("root")
        if edge is not None and edge.kind == "touch":
            flags.add("touch")
        if any(v in edited for v in vids):
            flags.add("edited")
        if locations and all(loc.entry_kind == "live" for loc in locations):
            flags.add("only-live")
        outside = [loc for loc in locations if loc.entry_kind != "live"]
        if outside and all(loc.entry_kind == "inplace" for loc in outside):
            flags.add("outside-only")
        if gone:
            flags.add("gone")
        if note is not None and note.locked:
            flags.add("locked")
        if note is not None and (note.label or note.comment):
            flags.add("noted")
        if p.head in self.placed:
            flags.add("placed")
        if p.head in self.evicted:
            flags.add("evicted")
        if meta is not None and not meta.valid:
            flags.add("unreadable")
        if meta is not None and meta.valid and gameversion.newer(
                self.platform(p.head), meta.meta_format, meta.base_version):
            flags.add("newer")           # written by a newer game version: no live write (D5)
        return VersionRow(
            pid=pid, pair=p.pair, head=p.head, head_member=p.head_member, partner=p.partner,
            name=meta.name if meta else "", summary=meta.summary if meta else "",
            play_time=meta.play_time if meta else 0, saved_at=meta.meta_ts if meta else 0,
            difficulty=formats.difficulty_label(meta.difficulty, meta.game_mode) if meta else "",
            mode=_mode(meta), platform=self.platform(p.head),
            locations=_distinct(self._summary(loc) for loc in head_locations),
            live_in=live_in,
            durable_copies=sum(1 for loc in head_locations if _durable(loc)),
            flags=frozenset(flags), parent=parent_vid,
            parent_tier=edge.tier if direct else None,
            parent_kind=edge.kind if direct else None,
            parent_method=edge.method if direct else None,
            children=n_children, in_tree=in_tree, key=False,
            label=note.label if note else "", comment=note.comment if note else "",
            locked=bool(note and note.locked), note_rev=self.journal.note_rev.get(p.head),
            facts=self._view(p.head),
        )

    def _mark_keys(self, rows: list[VersionRow]) -> list[VersionRow]:
        """D1 2.10.4 rule 5, with the content index's milestones."""
        out = []
        dated = [r for r in rows if not r.in_tree]
        oldest_first = list(reversed(rows))
        new_name = set()
        last_name, last_platform = None, None
        platform_change = set()
        for r in oldest_first:
            if r.name and r.name != last_name:
                if last_name is not None:
                    new_name.add(r.head)
                last_name = r.name
            if last_platform is not None and r.platform != last_platform and r.in_tree:
                platform_change.add(r.head)
            if r.in_tree:
                last_platform = r.platform
        ends = {dated[0].head, dated[-1].head} if dated else set()
        for r in rows:
            special = bool(r.flags & {"live", "noted", "locked"}) or r.head in new_name
            if r.in_tree:
                key = special or bool(r.flags & {"tip", "fork", "root", "placed", "evicted"}) \
                    or r.head in platform_change or self._milestone(r)
            else:
                key = special or r.head in ends
            out.append(dataclasses.replace(r, key=key))
        return out

    def _summary(self, loc: RawLocation) -> LocationSummary:
        date = datetime.fromtimestamp(loc.seen_at, timezone.utc).strftime("%Y-%m-%d")
        if loc.entry_kind == "live":
            label = f"Live: {self._slot_words(loc.where, loc.slot)}"
            return LocationSummary("live", label, loc)
        if loc.entry_kind == "inplace":
            entry = self.scan.entries.get(loc.where.entry_id) if isinstance(loc.where, EntryRef) else None
            name = aliases.redact(entry.label) if entry and entry.label else "a folder of yours"
            return LocationSummary("external", f"Outside vault: {name}", loc)
        words = {"full": "backup", "snapshot": "safety snapshot", "extract": "copy of one slot",
                 "imported": "imported backup", "preserve": "kept when replaced"}
        return LocationSummary("vault", f"Vault: {words.get(loc.entry_kind, 'backup')} {date}", loc)

    def _slot_words(self, ref: Any, slot: int) -> str:
        if not isinstance(ref, LiveRef):
            return f"slot {slot}"
        name = "Steam" if ref.platform == "steam" else "Xbox"
        if ref.platform in self.several and ref in self.tokens:
            same = [r for r in self.refs if r.platform == ref.platform]
            name += f" {same.index(ref) + 1}"
        return f"{name} slot {slot}"

    def _badge(self, target: LiveTarget) -> str:
        letter = "S" if target.platform == "steam" else "X"
        if target.platform in self.several and target.ref in self.tokens:
            same = [r for r in self.refs if r.platform == target.platform]
            return f"{letter}{same.index(target.ref) + 1}.{target.slot}"
        return f"{letter}{target.slot}"

    # --- lines --------------------------------------------------------------------------

    def _line(self, pid: Pid, rows: tuple[VersionRow, ...],
              live: list[tuple[LiveTarget, _Pair]], display_children: Counter) -> Line:
        vids = {v for r in rows for v in r.pair if v}
        metas = {v: self.meta(v) for v in vids}
        ordered = sorted(vids, key=self.order, reverse=True)
        names: list[str] = []
        for v in ordered:
            m = metas[v]
            if m and m.name and m.name not in names:
                names.append(m.name)
        live_heads = [(t, p, self.meta(p.head)) for t, p in live]
        live_heads.sort(key=lambda x: (self._badge_order(x[0])))
        top = max((m.play_time for _, _, m in live_heads if m), default=0)
        badges = tuple(LiveBadge(t, self._badge(t), p.head, p.head_member,
                                 display_children.get(p.head, 0) == 0,
                                 max(0, top - (m.play_time if m else 0)))
                       for t, p, m in live_heads)
        tips, state = self._platforms(pid, live_heads)
        tip_rows = [r for r in rows if "tip" in r.flags]
        if live_heads:
            rep = max(live_heads, key=lambda x: (x[2].play_time, x[2].meta_ts) if x[2] else (0, 0))[1].head
        elif tip_rows:
            rep = max(tip_rows, key=lambda r: (r.saved_at, r.play_time)).head
        else:
            rep = rows[0].head
        rep_meta = self.meta(rep)
        note = self.notes.save_files.get(pid)
        text = note.text if note else ""
        preview = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:80]
        heads_in_tree = {r.head for r in rows if r.in_tree}
        branch_count = sum(1 for r in rows if "tip" in r.flags) + sum(
            1 for _, p, _ in live_heads if p.head not in heads_in_tree)
        locked_count = sum(1 for r in rows if r.locked)
        live_platforms = Counter(t.platform for t, _ in live)
        at_risk = any(self._vault_copy(p) != "durable" for _, p in live)
        flags = set()
        if live:
            flags.add("live")
        if len({t.platform for t, _ in live}) > 1:
            flags.add("cross-platform")
        if state == "diverged":
            flags.add("diverged")
        if any(n > 1 for n in live_platforms.values()):
            flags.add("twin")
        if at_risk:
            flags.add("at-risk")
        if any("edited" in r.flags for r in rows):
            flags.add("edited")
        if rep_meta is not None and rep_meta.game_mode == EXPEDITION_MODE:
            flags.add("expedition")
        if not is_identified(pid):
            flags.add("legacy-id")
        if note is not None and note.pinned:
            flags.add("pinned")
        if text.strip():
            flags.add("has-note")
        if any("unreadable" in r.flags for r in rows):
            flags.add("unreadable")
        if any("newer" in r.flags for r in rows):
            flags.add("newer")
        raw_pids = sorted({self.raw_pid(v) for v in vids if self.raw_pid(v)})
        return Line(
            pid=pid, raw_pids=tuple(raw_pids), identified=is_identified(pid),
            name=self._line_name(pid, names), former_names=tuple(self._former(pid, names)),
            difficulty=formats.difficulty_label(rep_meta.difficulty, rep_meta.game_mode)
            if rep_meta else "",
            mode=_mode(rep_meta),
            platforms=tuple(sorted({r.platform for r in rows}, key=_platform_order)),
            live=badges, platform_tips=tips, cross_platform=state,
            play_time=max((m.play_time for m in metas.values() if m), default=0),
            last_played=max((m.meta_ts for m in metas.values() if m), default=0),
            version_count=sum(1 for r in rows if "gone" not in r.flags),
            gone_count=sum(1 for r in rows if "gone" in r.flags),
            branch_count=branch_count, note_preview=preview, has_note=bool(text.strip()),
            pinned=bool(note and note.pinned), locked_count=locked_count,
            flags=frozenset(flags), representative=rep, facts=self._view(rep),
        )

    def _line_name(self, pid: Pid, names: list[str]) -> str:
        """A split line has the name the owner gave it, else the original's name and its number."""
        if not pid.startswith("split:"):
            return names[0] if names else ""
        given = self.lineage.split_names().get(pid)
        return given or f"{names[0] if names else 'Save'} (split {pid.rpartition(':')[2]})"

    def _former(self, pid: Pid, names: list[str]) -> list[str]:
        if pid.startswith("split:") and self.lineage.split_names().get(pid):
            return names
        return names[1:]

    def _badge_order(self, target: LiveTarget) -> tuple:
        return (_platform_order(target.platform), self.tokens.get(target.ref, 99), target.slot)

    def _platforms(self, pid: Pid, live_heads) -> tuple[tuple[PlatformTip, ...], CrossPlatform]:
        """D1 2.8: each platform's most-progressed live copy, compared without ancestry."""
        best: dict[str, tuple[LiveTarget, _Pair, MetaSnap]] = {}
        for t, p, m in live_heads:
            if m is None:
                continue
            if t.platform not in best or (m.play_time, m.meta_ts) > (
                    best[t.platform][2].play_time, best[t.platform][2].meta_ts):
                best[t.platform] = (t, p, m)
        if len(best) < 2:
            tips = tuple(PlatformTip(pl, t, p.head, m.play_time, m.meta_ts, 0)
                         for pl, (t, p, m) in sorted(best.items(), key=lambda x: _platform_order(x[0])))
            return tips, "n/a"
        s, x = best["steam"], best["xbox"]
        if s[2].play_time == x[2].play_time:
            state: CrossPlatform = "same"
        elif self._diverged(pid):
            state = "diverged"
        else:
            state = "xbox-behind" if x[2].play_time < s[2].play_time else "steam-behind"

        def behind(own, other) -> int:
            if state in ("same", "diverged"):
                return 0
            return max(0, other[2].play_time - own[2].play_time)

        tips = (PlatformTip("steam", s[0], s[1].head, s[2].play_time, s[2].meta_ts, behind(s, x)),
                PlatformTip("xbox", x[0], x[1].head, x[2].play_time, x[2].meta_ts, behind(x, s)))
        return tips, state

    def _diverged(self, pid: Pid) -> bool:
        """Both platforms played since they were last in step, as the journal saw it."""
        play_edges = {(e.target, e.created, e.child) for e in self.journal.edges
                      if e.kind == "play" and e.tier in ("recorded", "observed") and e.target}
        heads: dict[str, dict[LiveTarget, int]] = {"steam": {}, "xbox": {}}
        seen_pt: dict[str, set[int]] = {"steam": set(), "xbox": set()}
        sync, progressed = None, set()
        for rec in self.journal.records:
            vids = [v for v in rec.pair if v]
            if not vids or self.pid(vids[0]) != pid:
                continue
            here, there = rec.target.platform, "xbox" if rec.target.platform == "steam" else "steam"
            plays = [self.meta(v).play_time for v in vids if self.meta(v)]
            heads[here][rec.target] = max(plays, default=0)
            top = {p: max(h.values(), default=None) for p, h in heads.items()}
            if top["steam"] is not None and top["xbox"] is not None and top["steam"] == top["xbox"]:
                sync, progressed = rec.at, set()
            elif sync is not None and any(
                    (rec.target, rec.at, v) in play_edges and self.meta(v)
                    and self.meta(v).play_time not in seen_pt[there] for v in vids):
                progressed.add(here)
            seen_pt[here].update(plays)
        return sync is not None and progressed == {"steam", "xbox"}

    # --- the board ----------------------------------------------------------------------

    def _vault_copy(self, p: _Pair) -> str:
        vids = [v for v in p.pair if v]
        durable = [any(_durable(loc) for loc in self.locations(v)) for v in vids]
        if durable and all(durable):
            return "durable"
        if any(durable):
            return "partial"
        outside = [loc for v in vids for loc in self.locations(v) if loc.entry_kind != "live"]
        if outside and all(loc.entry_kind == "inplace" for loc in outside):
            return "outside-vault"
        return "none"

    def _board(self, live: Mapping[LiveTarget, _Pair], lines: Mapping[Pid, Line]) -> LiveBoard:
        pinned = self.notes.pinned_pids()
        recent = self.now - RECENT_EVENT_SECONDS
        last_event: dict[LiveTarget, JournalEvent] = {}
        for e in self.journal.events:
            if e.at >= recent and e.kind != "first-seen":
                last_event[e.target] = e
        pending: dict[LiveTarget, tuple[int, str]] = {}
        for (op_id, target), b in self.journal.ops.items():
            if b.outcome == "pending" and (target not in pending or b.at > pending[target][0]):
                pending[target] = (b.at, op_id)
        live_pids: dict[LiveTarget, Pid] = {t: self.pid(p.head) for t, p in live.items()}
        sources = []
        for source, ref in zip(self.sources, self.refs):
            folder = self.scan.live.get(ref)
            slots = []
            for slot in range(1, formats.MAX_SAVE_SLOTS + 1):
                target = ref.target(slot)
                slots.append(self._slot(target, folder, live.get(target), live_pids, pinned,
                                        last_event.get(target), pending.get(target)))
            ranked = self._ranks(slots)
            sources.append(LiveSource(
                ref, self._source_label(source, ref), ref.platform, source.writable,
                folder.steam_cloud if folder is not None and ref.platform == "steam" else None,
                tuple(ranked)))
        return LiveBoard(tuple(sources))

    def _source_label(self, source: Source, ref: LiveRef) -> str:
        """The folder's name in the app (the owner's aliases applied), never its account id."""
        label = str(aliases.redact(source.label)) if source.label else ""
        ids = {ref.account_key, ref.account_key.removeprefix("st_"), source.account} - {""}
        if not label or any(i in label for i in ids):
            label = "Steam" if ref.platform == "steam" else "Xbox"
            if ref.platform in self.several:
                same = [r for r in self.refs if r.platform == ref.platform]
                label += f" {same.index(ref) + 1}"
        return label

    def _slot(self, target, folder, p, live_pids, pinned, event, pending) -> BoardSlot:
        capture = None
        if folder is not None and folder.readable:
            capture = folder.captures[target.slot - 1]
        if capture is None or target.slot in folder.unstable:
            state = "unreadable"
        elif not capture.members:
            state = "empty"
        elif len(capture.members) == 1:
            state = "partial"
        else:
            state = "occupied"
        pid = live_pids.get(target) if p else None
        meta = self.meta(p.head) if p else None
        twin = pid is not None and any(
            t != target and t.platform == target.platform and q == pid for t, q in live_pids.items())
        others = tuple(t for t, q in live_pids.items()
                       if pid is not None and q == pid and t.platform != target.platform)
        if target.platform == "steam":
            cloud = "cloud on" if folder is not None and folder.steam_cloud else "cloud off"
        else:
            states = [m.xbox_sync for m in (capture.members if capture else ()) if m.xbox_sync is not None]
            worst = max(states, key=_sync_badness, default=None)
            cloud = formats.MS_SYNC_STATE_NAMES.get(worst, "") if worst is not None else ""
        return BoardSlot(
            target=target, label=self._badge(target), state=state, pid=pid,
            name=meta.name if meta else "", head=p.head if p else None,
            partner=p.partner if p else None, member_loaded=p.head_member if p else None,
            play_time=meta.play_time if meta else 0, saved_at=meta.meta_ts if meta else 0,
            vault_copy=self._vault_copy(p) if p else "none",
            pinned=pid in pinned if pid else False, twin=twin, other_platform=others,
            cloud=cloud, pending_op=pending[1] if pending else None, last_event=event,
            suggest_rank=None)

    @staticmethod
    def _ranks(slots: list[BoardSlot]) -> list[BoardSlot]:
        """The generic eviction order (C30): empty slots, then unpinned slots whose save is
        safe in the vault, least recently played first, then the other unpinned; never a
        pinned one."""
        def group(s: BoardSlot) -> tuple | None:
            if s.state == "unreadable" or s.pinned:
                return None
            if s.state == "empty":
                return (0, 0, s.target.slot)
            if s.vault_copy == "durable":
                return (1, s.saved_at, s.target.slot)
            return (2, s.saved_at, s.target.slot)

        order = sorted((g, i) for i, s in enumerate(slots) if (g := group(s)) is not None)
        rank = {i: n for n, (_, i) in enumerate(order)}
        return [dataclasses.replace(s, suggest_rank=rank.get(i)) for i, s in enumerate(slots)]

    # --- events -------------------------------------------------------------------------

    def _events(self, lines: Mapping[Pid, Line]) -> dict[Pid, tuple[LineEvent, ...]]:
        """Each line's operations and journal events, newest first (in journal order)."""
        out: dict[Pid, list[LineEvent]] = defaultdict(list)
        shown = ("reverted", "deleted", "returned", "resurrected", "replaced", "unexpected")
        for rec in self.journal.records:
            if isinstance(rec, OpRecord) and rec.status == "done":
                vids = tuple(v for pair in (rec.pair, rec.evicted or (None, None)) for v in pair if v)
                for pid in {self.pid(v) for v in vids} - {None}:
                    out[pid].append(LineEvent(rec.at, rec.kind, rec.target, rec.op_id, vids,
                                              self._op_text(rec)))
            for e in self.journal.events_by_seq.get(rec.seq, ()):
                if e.kind not in shown:
                    continue
                vids = tuple(dict.fromkeys(
                    v for v in (*e.vids, *(e.before or ()), *(e.after or ())) if v))
                for pid in {self.pid(v) for v in vids} - {None}:
                    out[pid].append(LineEvent(e.at, e.kind, e.target, e.op_id, vids,
                                              self._event_text(e)))
        return {pid: tuple(reversed(evs)) for pid, evs in out.items()}

    def _op_text(self, rec: OpRecord) -> str:
        where = self._slot_words(rec.target.ref, rec.target.slot)
        by = "the app" if rec.src == "app" else "the app (before recording began)"
        text = {
            "clear": f"Emptied by {by}: {where}",
            "promote": f"Made the save that loads by {by}: {where}",
            "restore_full": f"Whole folder restored by {by}: {where}",
            "panic_restore": f"Whole folder restored by {by}: {where}",
            "undo": f"Undone by {by}: {where}",
            "swap": f"Swapped by {by}: {where}",
        }.get(rec.kind, f"Put live by {by} in {where}")
        return str(aliases.redact(text))

    def _event_text(self, e: JournalEvent) -> str:
        where = self._slot_words(e.target.ref, e.target.slot)
        sync = "Steam Cloud" if e.target.platform == "steam" else "Xbox sync"
        text = {
            "reverted": f"{sync} put the replaced save back: {where}",
            "deleted": f"Deleted outside the app: {where}",
            "returned": f"An older version came back: {where}",
            "resurrected": f"{sync} brought a deleted save back: {where}",
            "replaced": f"Another save came in outside the app: {where}",
            "unexpected": f"Changed unexpectedly after an operation: {where}",
        }[e.kind]
        return str(aliases.redact(text))


# --- references (the CLI's <save> and <version>) -------------------------------

_BADGE = re.compile(r"[SX][0-9]+(?:\.[0-9]+)?", re.IGNORECASE)
_HEX = re.compile(r"[0-9a-fA-F]+")


class RefError(ValueError):
    """A reference names nothing, or more than one thing (``candidates`` lists them)."""

    def __init__(self, message: str, candidates: Sequence[str] = ()) -> None:
        super().__init__(message + "".join(f"\n  {c}" for c in candidates))
        self.candidates = tuple(candidates)


def resolve_save(model: ReadModel, ref: str) -> Pid:
    """A save file by live badge (``S9``, ``X4``, ``S2.7``), id prefix (6+ hex digits) or
    its name (case does not matter)."""
    ref = ref.strip()
    if _BADGE.fullmatch(ref):
        slot = _board_slot(model, ref)
        if slot.pid is None:
            raise RefError(f"live slot {slot.label} holds no save")
        return slot.pid
    lines = model.lines()
    if ref.lower().startswith("split:"):
        found = [line for line in lines if line.pid.lower().startswith(ref.lower())]
        if found:
            return _one(found, ref, _describe_line).pid
    if _HEX.fullmatch(ref) and len(ref) >= 6:
        found = [line for line in lines
                 if any(p.upper().startswith(ref.upper()) for p in (line.pid, *line.raw_pids))]
        if found:
            return _one(found, ref, _describe_line).pid
    folded = ref.casefold()
    found = [line for line in lines if line.name.casefold() == folded]
    if not found:
        raise RefError(f"no save file is called or numbered {ref!r}")
    return _one(found, ref, _describe_line).pid


def resolve_version(model: ReadModel, ref: str) -> VersionRow:
    """A version by live badge (the one loaded there), id prefix (8+ hex digits), or
    ``<save>@YYYY-MM-DD[THH:MM]`` (the newest saved at or before that local time)."""
    ref = ref.strip()
    if "@" in ref:
        save, _, when = ref.rpartition("@")
        pid = resolve_save(model, save)
        try:
            moment = datetime.fromisoformat(when)
        except ValueError:
            raise RefError(f"{when!r} is not a date (YYYY-MM-DD or YYYY-MM-DDTHH:MM)") from None
        if len(when) == 10:
            moment = moment.replace(hour=23, minute=59, second=59)
        limit = int(moment.timestamp())
        rows = [r for r in model.versions(pid) if r.saved_at <= limit]
        if not rows:
            raise RefError(f"{model.line(pid).name!r} has no version saved by {when}")
        return max(rows, key=lambda r: (r.saved_at, r.play_time))
    if _BADGE.fullmatch(ref):
        slot = _board_slot(model, ref)
        if slot.head is None:
            raise RefError(f"live slot {slot.label} holds no save")
        row = next((r for line in model.lines() for r in model.versions(line.pid)
                    if r.head == slot.head), None)
        if row is None:
            raise RefError(f"live slot {slot.label} holds a save no version row lists")
        return row
    if _HEX.fullmatch(ref) and len(ref) >= 8:
        prefix = ref.lower()
        found = [r for line in model.lines() for r in model.versions(line.pid)
                 if any(v and v.startswith(prefix) for v in r.pair)]
        if not found:
            raise RefError(f"no version id starts with {ref!r}")
        return _one(found, ref, _describe_row)
    raise RefError(f"{ref!r} is not a version: give 8+ hex digits of its id, a live slot "
                   "like S9, or <save>@YYYY-MM-DD")


def _board_slot(model: ReadModel, badge: str) -> BoardSlot:
    for source in model.live_board().sources:
        for slot in source.slots:
            if slot.label.upper() == badge.upper():
                return slot
    raise RefError(f"there is no live slot {badge.upper()}")


def _one(found: list, ref: str, describe):
    if len(found) > 1:
        raise RefError(f"{ref!r} could be several:", [describe(f) for f in found])
    return found[0]


def _describe_line(line: Line) -> str:
    return f"{line.name or '(no name)'}  [{line.pid[:16]}]"


def _describe_row(row: VersionRow) -> str:
    when = datetime.fromtimestamp(row.saved_at).strftime("%Y-%m-%d %H:%M") if row.saved_at else "-"
    return f"{row.head[:8]}  {row.name}  {when}"


# --- sorting and filtering ----------------------------------------------------

SORT_KEYS = ("name", "play_time", "last_played", "versions", "live", "platform", "difficulty",
             "risk", "note")


def sort_lines(lines: Iterable[Line], key: str = "last_played",
               descending: bool | None = None) -> tuple[Line, ...]:
    """Sort keys of D1 2.10.1. ``facts:<name>`` sorts by the representative's
    ``view.numbers[name]`` (largest first unless ``descending=False``); a line without facts
    or without that number sorts last either way, by name."""
    lines = list(lines)
    by_name = sorted(lines, key=lambda line: (line.name.casefold(), line.pid))
    if key.startswith("facts:"):
        name = key.split(":", 1)[1]

        def number(line: Line) -> float | None:
            value = (getattr(line.facts, "numbers", None) or {}).get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or math.isnan(value):
                return None                 # missing, not a number, or NaN: sorts last
            return value

        known = [line for line in by_name if number(line) is not None]
        missing = [line for line in by_name if number(line) is None]
        return tuple(sorted(known, key=number, reverse=descending is not False)) + tuple(missing)
    if key not in SORT_KEYS:
        raise ValueError(f"unknown sort key {key!r}; one of {', '.join(SORT_KEYS)}")
    if descending is None:
        descending = key in ("play_time", "last_played", "versions", "risk")
    getters = {
        "name": lambda line: line.name.casefold(),
        "play_time": lambda line: line.play_time,
        "last_played": lambda line: line.last_played,
        "versions": lambda line: line.version_count,
        "live": lambda line: (not line.live, [_platform_order(b.target.platform) for b in line.live],
                              [b.target.slot for b in line.live]),
        "platform": lambda line: [_platform_order(p) for p in line.platforms],
        "difficulty": lambda line: line.difficulty.casefold(),
        "risk": lambda line: ("at-risk" in line.flags, "live" in line.flags),
        "note": lambda line: (not line.has_note, line.note_preview.casefold()),
    }
    return tuple(sorted(by_name, key=getters[key], reverse=descending))   # stable: ties by name


def filter_lines(lines: Iterable[Line], f: LineFilter) -> tuple[Line, ...]:
    def keep(line: Line) -> bool:
        checks = [
            (f.live, bool(line.live)),
            (f.cross_platform, "cross-platform" in line.flags),
            (f.has_note, line.has_note),
            (f.pinned, line.pinned),
            (f.at_risk, "at-risk" in line.flags),
            (f.expedition, "expedition" in line.flags),
            (f.has_locked, line.locked_count > 0),
        ]
        if any(want is not None and want != have for want, have in checks):
            return False
        if f.platform is not None and f.platform not in line.platforms:
            return False
        if f.text:
            needle = f.text.casefold()
            hay = [line.name, *line.former_names, line.note_preview]
            return any(needle in h.casefold() for h in hay)
        return True

    return tuple(line for line in lines if keep(line))


# --- helpers ------------------------------------------------------------------


def _mode(meta: MetaSnap | None) -> str:
    if meta is None or meta.game_mode != EXPEDITION_MODE:
        return ""
    return f"Expedition {meta.season}" if meta.season else "Expedition"


#: What keeps a version from being folded away: it is worth a row of its own.
_UNFOLDABLE = frozenset({"live", "fork", "noted", "locked", "placed", "evicted"})


def _one_per_head(pairs: dict[PairKey, _Pair]) -> dict[PairKey, _Pair]:
    """One pair per head: a version is its head. The same head can sit in two pairs (a one-member
    source placed into a slot fills both members; the other platform's copy has no partner), and
    two rows for one version would list it twice."""
    out: dict[Vid, _Pair] = {}
    for p in pairs.values():
        out.setdefault(p.head, p)
    return {p.pair: p for p in out.values()}


def _distinct(locations) -> tuple[LocationSummary, ...]:
    """The same place once: both members of one slot hold the same save."""
    seen: dict[tuple, LocationSummary] = {}
    for loc in locations:
        seen.setdefault((loc.kind, loc.label), loc)
    return tuple(seen.values())


def _fold(rows: list[VersionRow]) -> list[VersionRow]:
    """D1 2.10.4 rule 4 (default mode, C5), on rows newest first: a tree node reached by a
    re-save without play (``touch``) or an edit outside the game, with at most one child and
    nothing else to say for itself, folds into its parent; a dated row folds into the next
    older dated row of the same platform with the same play time (a re-save without play -
    a marker only, never a link). ``folded_into`` names the row that stays: chains end at
    a row that is not folded."""
    heads = {r.head for r in rows}
    into: dict[Vid, Vid] = {}
    for r in rows:
        if (r.in_tree and r.parent in heads and r.parent_kind in ("touch", "edit")
                and r.children <= 1 and not r.flags & _UNFOLDABLE):
            into[r.head] = r.parent
    dated = [r for r in rows if not r.in_tree]
    for n, r in enumerate(dated):
        if r.flags & {"live", "noted", "locked"}:
            continue
        older = next((o for o in dated[n + 1:] if o.platform == r.platform), None)
        if older is not None and older.play_time == r.play_time:
            into[r.head] = older.head

    def final(vid: Vid) -> Vid:
        seen = set()
        while vid in into and vid not in seen:
            seen.add(vid)
            vid = into[vid]
        return vid

    return [dataclasses.replace(r, folded_into=final(r.head)) if r.head in into else r
            for r in rows]


def _durable(loc: RawLocation) -> bool:
    """A kept vault copy: a managed catalog entry (snapshots too, now that nothing is
    pruned) or a store entry; never a live folder or a folder of the owner's own."""
    return loc.managed and loc.entry_kind not in ("live", "inplace")


def _platform_order(platform: str) -> int:
    return 0 if platform == "steam" else 1


def _sync_badness(state: int) -> int:
    """Xbox sync states, the most worrying last: not yet uploaded beats synced."""
    return {1: 0, 0: 1, 4: 1, 5: 2, 2: 3, 3: 4}.get(state, 1)


def _jsonable(obj: Any, model: ReadModel) -> Any:
    if isinstance(obj, LiveTarget):
        return model.token(obj)
    if isinstance(obj, LiveRef):
        return f"{obj.platform}:{model._tokens.get(obj, '?')}"
    if isinstance(obj, EntryRef):
        return {"entry": obj.entry_id}
    if isinstance(obj, StoreRef):
        return {"store": obj.key}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name), model) for f in dataclasses.fields(obj)}
    if isinstance(obj, (frozenset, set)):
        return sorted(_jsonable(v, model) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, model) for v in obj]
    if isinstance(obj, Mapping):
        return {str(_jsonable(k, model)): _jsonable(v, model) for k, v in obj.items()}
    return obj
