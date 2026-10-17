"""``lineage.json`` (D1 2.7.2): the owner's corrections to the history, Combine and Split
of save files, and the record of the bootstrap.

It also holds the **lineage graph** (D1 2.7.1): one parent per version, from the owner's
corrections first, then the journal's best evidence; nothing is guessed. Corrections, Combine
and Split are the owner's words about his history; they never touch a save file.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .model import Edge, Pid, Vid, is_pid, is_vid, pid_kind, split_pid
from .notes import NOTE_MAX
from .store import LINEAGE, NOTES, OrganizerStore

_TIER_RANK = {"owner": 3, "recorded": 2, "observed": 1}


@dataclass(frozen=True)
class BootstrapInfo:
    done_at: int | None
    app: str
    algo: int
    report: Mapping[str, Any]


@dataclass(frozen=True)
class LineageState:
    corrections: tuple[Mapping[str, Any], ...]
    aliases: tuple[Mapping[str, Any], ...]     # {"pid", "into", "at"}: an owner Combine
    splits: tuple[Mapping[str, Any], ...]
    bootstrap: BootstrapInfo | None

    def canonical(self, pid: Pid) -> Pid:
        """The save file ``pid`` belongs to after the owner's Combines (transitive; a cycle
        stops where it would repeat)."""
        into = {a["pid"]: a["into"] for a in self.aliases
                if isinstance(a.get("pid"), str) and isinstance(a.get("into"), str)}
        seen = {pid}
        while pid in into and into[pid] not in seen:
            pid = into[pid]
            seen.add(pid)
        return pid

    def split_sources(self) -> dict[Pid, Pid]:
        """Each split line's pid -> the raw pid it was split off."""
        return {sp["pid"]: sp["from"] for sp in self.splits
                if isinstance(sp.get("pid"), str) and isinstance(sp.get("from"), str)}

    def split_names(self) -> dict[Pid, str]:
        return {sp["pid"]: sp["name"] for sp in self.splits
                if isinstance(sp.get("pid"), str) and isinstance(sp.get("name"), str)
                and sp["name"].strip()}


def read_lineage(store: OrganizerStore) -> LineageState:
    return parse_lineage(store.document(LINEAGE))


def parse_lineage(doc: Mapping[str, Any]) -> LineageState:
    def items(key: str) -> tuple[Mapping[str, Any], ...]:
        value = doc.get(key)
        return tuple(v for v in value if isinstance(v, dict)) if isinstance(value, list) else ()

    block = doc.get("bootstrap")
    bootstrap = None
    if isinstance(block, dict):
        done_at = block.get("done_at")
        report = block.get("report")
        algo = block.get("algo")
        bootstrap = BootstrapInfo(done_at if type(done_at) is int else None,
                                  str(block.get("app", "")), algo if type(algo) is int else 0,
                                  report if isinstance(report, dict) else {})
    return LineageState(items("corrections"), items("aliases"), items("splits"), bootstrap)


def ensure_skeleton(doc: MutableMapping[str, Any]) -> None:
    """The lists every ``lineage.json`` carries, empty until the owner corrects anything."""
    for key in ("corrections", "aliases", "splits"):
        doc.setdefault(key, [])


# --- the lineage graph (D1 2.7.1) ----------------------------------------------


@dataclass(frozen=True)
class LineageGraph:
    """At most one parent per version.

    1. the owner's ``set_parent`` (``None``: "this version has no known parent");
    2. else the journal's edge of the highest tier (recorded over observed), the later
       evidence within a tier - every disagreement is listed in ``conflicts``;
    3. ``not_related`` drops any journal edge between the two versions;
    4. what is still without a parent is a root. Nothing is guessed for it.

    An owner edge that would close a cycle is left out and listed in ``dropped``."""

    parent: Mapping[Vid, Vid]
    edge_of: Mapping[Vid, Edge]
    owner_roots: frozenset[Vid]
    conflicts: tuple[tuple[Vid, tuple[Vid, ...]], ...]   # (child, the parents the evidence names)
    dropped: tuple[tuple[str, str], ...]                  # (correction id, why it was left out)

    @classmethod
    def build(cls, edges: Iterable[Edge], lineage: LineageState) -> LineageGraph:
        corrections = sorted((c for c in lineage.corrections if isinstance(c.get("id"), str)),
                             key=lambda c: (c["at"] if type(c.get("at")) is int else 0, c["id"]))
        unrelated = {frozenset((c["a"], c["b"])) for c in corrections
                     if c.get("kind") == "not_related" and isinstance(c.get("a"), str)
                     and isinstance(c.get("b"), str)}
        by_child: dict[Vid, list[Edge]] = {}
        for e in edges:
            if frozenset((e.parent, e.child)) not in unrelated:
                by_child.setdefault(e.child, []).append(e)
        parent: dict[Vid, Vid] = {}
        edge_of: dict[Vid, Edge] = {}
        conflicts = []
        for child, candidates in by_child.items():
            best = max(candidates, key=lambda e: (_TIER_RANK.get(e.tier, 0), e.created))
            parent[child], edge_of[child] = best.parent, best
            top = {e.parent for e in candidates
                   if _TIER_RANK.get(e.tier, 0) == _TIER_RANK.get(best.tier, 0)}
            if len(top) > 1:
                conflicts.append((child, tuple(sorted(top))))
        roots: set[Vid] = set()
        dropped = []
        for c in corrections:
            if c.get("kind") != "set_parent" or not isinstance(c.get("child"), str):
                continue
            child, new = c["child"], c.get("parent")
            if new is None:
                parent.pop(child, None)
                edge_of.pop(child, None)
                roots.add(child)
                continue
            if not isinstance(new, str) or _reaches(parent, new, child):
                dropped.append((c["id"], "it would close a cycle"))
                continue
            at = c["at"] if type(c.get("at")) is int else 0
            parent[child] = new
            edge_of[child] = Edge(new, child, "owner", "unknown", "owner", None, at)
            roots.discard(child)
        return cls(parent, edge_of, frozenset(roots), tuple(conflicts), tuple(dropped))

    def descends_from(self, vid: Vid, ancestor: Vid) -> bool:
        """``vid`` is ``ancestor`` or one of its descendants."""
        return _reaches(self.parent, vid, ancestor)

    def effective_pids(self, vids: Iterable[Vid], raw_pid: Callable[[Vid], Pid | None],
                       lineage: LineageState) -> dict[Vid, Pid]:
        """The save file each version belongs to (D1 2.7.4): the owner's seed of a split; else
        the split line its drawn parent belongs to when that is a split of the same raw pid
        (a split-off twin is carried forward); else the raw pid after the owner's Combines."""
        seed: dict[Vid, tuple[int, Pid]] = {}
        for sp in lineage.splits:
            at = sp["at"] if type(sp.get("at")) is int else 0
            for v in sp.get("vids") or ():
                if isinstance(sp.get("pid"), str) and isinstance(v, str) \
                        and (v not in seed or (at, sp["pid"]) >= seed[v]):
                    seed[v] = (at, sp["pid"])
        sources = lineage.split_sources()
        out: dict[Vid, Pid] = {}

        def own(v: Vid, above: Pid | None) -> Pid | None:
            raw = raw_pid(v)
            if raw is None:
                return None
            if v in seed:
                return seed[v][1]
            if above is not None and sources.get(above) == raw:
                return above
            return lineage.canonical(raw)

        for v in vids:
            if v in out:
                continue
            chain = [v]                     # v, its parent, ... up to a known or first version
            while (chain[-1] not in out and chain[-1] in self.parent
                   and chain[-1] not in seed and self.parent[chain[-1]] not in chain):
                chain.append(self.parent[chain[-1]])
            above: Pid | None = None
            for u in reversed(chain):
                if u in out:
                    above = out[u]
                    continue
                above = own(u, above)
                if above is not None:
                    out[u] = above
        return out


def _reaches(parent: Mapping[Vid, Vid], start: Vid, target: Vid) -> bool:
    """``target`` is ``start`` or one of its ancestors."""
    seen = set()
    node: Vid | None = start
    while node is not None and node not in seen:
        if node == target:
            return True
        seen.add(node)
        node = parent.get(node)
    return False


# --- the owner's corrections ---------------------------------------------------


class Corrections:
    """Writes ``lineage.json``: set parent, not related, Combine, Split. Each is validated
    first and refused with a ValueError saying why; none touches a save file."""

    def __init__(self, store: OrganizerStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def state(self) -> LineageState:
        return read_lineage(self.store)

    def _new_id(self) -> str:
        stamp = datetime.fromtimestamp(int(self.clock()), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"cor-{stamp}-{secrets.token_hex(2)}"

    def set_parent(self, child: Vid, parent: Vid | None, *, note: str = "",
                   graph: LineageGraph | None = None,
                   line_of: Callable[[Vid], Pid | None] | None = None) -> Mapping[str, Any]:
        """``parent`` None: "this version has no known parent". ``graph`` (to refuse a
        parent that comes after the child) and ``line_of`` (to refuse two save files) are
        what the caller knows; a parent saved later than the child is allowed."""
        _check_vid(child)
        if parent is not None:
            _check_vid(parent)
            if parent == child:
                raise ValueError("a version cannot be its own parent")
            if graph is not None and graph.descends_from(parent, child):
                raise ValueError("that version comes after this one; it cannot be its parent")
            if line_of is not None:
                a, b = line_of(child), line_of(parent)
                if a is None or b is None:
                    raise ValueError("a version is not known to this vault")
                if a != b:
                    raise ValueError("the two versions are in different save files; "
                                     "Combine the two save files first")
        record = {"id": self._new_id(), "kind": "set_parent", "child": child, "parent": parent,
                  "at": int(self.clock()), "note": str(note)}
        with self.store.transaction() as tx:
            tx.doc(LINEAGE).setdefault("corrections", []).append(record)
        return record

    def mark_unrelated(self, a: Vid, b: Vid, *, note: str = "") -> Mapping[str, Any]:
        _check_vid(a)
        _check_vid(b)
        if a == b:
            raise ValueError("a version is related to itself")
        record = {"id": self._new_id(), "kind": "not_related", "a": a, "b": b,
                  "at": int(self.clock()), "note": str(note)}
        with self.store.transaction() as tx:
            tx.doc(LINEAGE).setdefault("corrections", []).append(record)
        return record

    def remove_correction(self, correction_id: str) -> None:
        with self.store.transaction() as tx:
            items = tx.doc(LINEAGE).setdefault("corrections", [])
            kept = [c for c in items if c.get("id") != correction_id]
            if len(kept) == len(items):
                raise ValueError(f"no correction {correction_id!r}")
            items[:] = kept

    # --- Combine and Split ----------------------------------------------------------

    def combine(self, pids: Sequence[Pid], into: Pid, *,
                names: Mapping[Pid, str] | None = None) -> None:
        """Make ``pids`` one save file with ``into``. When several have a save-file note, the
        surviving one gets a new revision with the others appended under
        ``--- merged from <name> ---``."""
        names = names or {}
        for pid in (*pids, into):
            _check_pid(pid)
        with self.store.transaction() as tx:
            doc = tx.doc(LINEAGE)
            state = parse_lineage(doc)
            target = state.canonical(into)
            moving: list[Pid] = []
            for pid in pids:
                root = state.canonical(pid)
                if root != target and root not in moving:
                    moving.append(root)
            if not moving:
                raise ValueError("those are already one save file")
            at = int(self.clock())
            aliases = doc.setdefault("aliases", [])
            for root in moving:
                aliases[:] = [a for a in aliases if a.get("pid") != root]
                aliases.append({"pid": root, "into": target, "at": at})
            files = tx.doc(NOTES).setdefault("save_files", {})
            parts = [(names.get(m) or m[:8], files[m]["text"]) for m in moving
                     if m in files and files[m].get("text", "").strip()]
            if parts:
                _merge_note(files, target, parts, at)

    def uncombine(self, pid: Pid) -> None:
        """Undo a Combine of ``pid``. The merged note revision stays in the note's history."""
        with self.store.transaction() as tx:
            aliases = tx.doc(LINEAGE).setdefault("aliases", [])
            kept = [a for a in aliases if a.get("pid") != pid]
            if len(kept) == len(aliases):
                raise ValueError("that save file was not combined into another")
            aliases[:] = kept

    def split(self, raw_pid: Pid, vids: Sequence[Vid], name: str = "") -> Pid:
        """A new save file seeded with ``vids`` (the UI passes the branch: the node and its
        descendants); returns its pid, ``split:<raw pid>:<n>``."""
        _check_pid(raw_pid)
        if pid_kind(raw_pid) == "split":
            raise ValueError("split the original save file, not a split")
        if not vids:
            raise ValueError("a split needs at least one version")
        for v in vids:
            _check_vid(v)
        with self.store.transaction() as tx:
            splits = tx.doc(LINEAGE).setdefault("splits", [])
            used = [int(sp["pid"].rpartition(":")[2]) for sp in splits
                    if sp.get("from") == raw_pid and isinstance(sp.get("pid"), str)]
            pid = split_pid(raw_pid, max(used, default=0) + 1)
            splits.append({"pid": pid, "from": raw_pid, "vids": sorted(set(vids)),
                           "name": name.strip(), "at": int(self.clock())})
        return pid

    def unsplit(self, pid: Pid) -> None:
        with self.store.transaction() as tx:
            splits = tx.doc(LINEAGE).setdefault("splits", [])
            kept = [sp for sp in splits if sp.get("pid") != pid]
            if len(kept) == len(splits):
                raise ValueError("that is not a split save file")
            splits[:] = kept


def _merge_note(files: dict, into: Pid, parts: Sequence[tuple[str, str]], at: int) -> None:
    current = files.get(into)
    text = current["text"] if current else ""
    for name, other in parts:
        text = (text + "\n\n" if text.strip() else "") + f"--- merged from {name} ---\n{other}"
    if len(text) > NOTE_MAX:
        raise ValueError(f"the merged note would be {len(text)} characters "
                         f"(the most is {NOTE_MAX})")
    rev = current["rev"] if current else 0
    history = list(current["history"]) if current else []
    if rev:
        history.append({"rev": rev, "at": current["at"], "text": current["text"]})
    files[into] = {"text": text, "rev": rev + 1, "at": at,
                   "pinned": bool(current and current["pinned"]), "history": history}


def _check_vid(vid: object) -> None:
    if not is_vid(vid):
        raise ValueError(f"not a version id: {vid!r}")


def _check_pid(pid: object) -> None:
    if not is_pid(pid):
        raise ValueError(f"not a save file id: {pid!r}")
