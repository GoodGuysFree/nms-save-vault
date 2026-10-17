"""``lineage.json`` (D1 2.7.2): the owner's corrections to the history, Combine and Split
of save files, and the record of the bootstrap.

Phase 1 reads it and keeps its shape; the corrections themselves (set parent, not related,
Combine, Split) and the lineage graph that applies them arrive with the history view.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from .model import Pid
from .store import LINEAGE, OrganizerStore


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
