"""What the organizer asks the content index for, and in what order (D1 step 13, D4 2.10.2).

The index's own priorities stay opaque here: they come through the provider
(``FactsProvider.Priority``), so the organizer never imports the index. Which versions were
known at the end of the last run is remembered per machine (``facts-seen.json`` beside the
scan cache, never in the vault), so the first refresh of a session can tell the versions that
are new since then from the ones only waiting for a backfill.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from .. import safety
from .model import Vid
from .readmodel import ReadModel
from .scanner import ScanResult

log = logging.getLogger(__name__)

SEEN_FILENAME = "facts-seen.json"
_PREFIX = 16                     # hex digits kept per version: collisions are negligible


def facts_targets(scan: ScanResult, model: ReadModel) -> tuple[list[Vid], list[Vid]]:
    """(live members, representatives and branch tips): what the view shows first. Both
    lists hold only versions whose bytes this scan found, in a stable order."""
    known = scan.versions
    live = [m.vid for f in scan.live.values() for c in f.captures for m in c.members if m.vid in known]
    lines = model.lines()
    tips = [line.representative for line in lines]
    tips += [r.head for line in lines for r in model.versions(line.pid) if "tip" in r.flags]
    live = list(dict.fromkeys(live))
    tips = [v for v in dict.fromkeys(tips) if v in known and v not in set(live)]
    return live, tips


class SeenMarker:
    """The versions known at the end of the last run, per machine. ``path`` None keeps
    nothing (an in-memory scan cache): every version is then "not new"."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def load(self) -> set[str] | None:
        """Prefixes of the versions known last time, or None when there was no last time."""
        if self.path is None or not self.path.is_file():
            return None
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(v) for v in doc["seen"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("facts marker not read: %s", type(exc).__name__)
            return None

    def save(self, vids: Iterable[Vid]) -> None:
        if self.path is None:
            return
        doc = {"schema": 1, "seen": sorted({v[:_PREFIX] for v in vids})}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            safety.atomic_write_bytes(self.path, json.dumps(doc, separators=(",", ":")).encode("ascii"))
        except OSError as exc:
            log.warning("facts marker not saved: %s", exc.strerror or type(exc).__name__)

    @staticmethod
    def contains(seen: set[str], vid: Vid) -> bool:
        return vid[:_PREFIX] in seen
