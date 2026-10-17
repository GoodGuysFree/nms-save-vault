"""The one-time bootstrap of an existing vault (D1 2.12): history from before recording.

Two kinds of evidence, both observations, nothing guessed:

- "seen in backups": the app's own full backups and safety snapshots of a live folder are
  captures of its slots at the time they were made, so consecutive ones are journaled as
  ``backfill`` observations;
- "put live by the app": the old ``oplog.jsonl`` names each slot the app wrote and, through
  the backup it came from, what it put there; those become ``oplog`` operations.

Backups and log lines from after the journal began are left alone: the live journal and the
operation hook recorded those themselves. Running it again only adds what is new (old
backups imported later). A dry run computes everything and writes nothing.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import catalog, safety, savedir
from ..catalog import CatalogEntry, Vault
from ..state import Source
from .cache import ScanCache
from .journal import (
    Journal,
    ObsRecord,
    OpRecord,
    Record,
    decode_all,
    decode_record,
    obs_fields,
    op_fields,
    replay,
)
from .model import (
    EMPTY_PAIR,
    LiveRef,
    LiveTarget,
    MemberObs,
    PairKey,
    Vid,
    is_identified,
)
from .lineage import ensure_skeleton
from .scanner import EntryScan, ScanProblem, ScanResult, scan
from .recorder import LIVEOP_TIMEOUT
from .store import APP, LINEAGE, OrganizerStore, liveop_lock_path

log = logging.getLogger(__name__)

ALGO = 1
TIMELINE_KINDS = (catalog.KIND_FULL, catalog.KIND_SNAPSHOT)
HOOK_WINDOW = 300       # seconds between an oplog line and the hook's record of the same write
LIVE_SRCS = ("scan", "verify", "app")

_REPOPULATE = re.compile(r"(xbox )?(.+) slot (\d+) -> slot (\d+)", re.DOTALL)
_CLEAR = re.compile(r"(xbox )?slot (\d+)")


@dataclass(frozen=True)
class BootstrapReport:
    folders_scanned: int
    entries_skipped: tuple[tuple[str, str], ...]      # (entry id, reason)
    copies_hashed: int
    versions: int
    pairs: int
    lines: int
    legacy_lines: int
    journal_records: int                               # written (or, dry, that would be)
    ops_resolved: int
    ops_unresolved: tuple[tuple[str, str], ...]        # (oplog line, reason)
    edges_by_method: Mapping[str, int]
    roots: int
    forks: tuple[tuple[str, str], ...]                 # (save name, date of the fork version)
    events: Mapping[str, int]
    at_risk_live: int
    problems: tuple[ScanProblem, ...]
    seconds: float
    dry_run: bool


@dataclass(frozen=True)
class _Item:
    """One step on a slot's timeline: an entry's capture or an oplog operation."""

    time: int
    order: int              # 0 capture, 1 operation: at equal seconds the snapshot came first
    target: LiveTarget
    pair: PairKey
    members: Mapping[Vid, MemberObs]
    entry: str | None = None
    op_id: str | None = None
    kind: str = ""


def bootstrap(vault: Vault, store: OrganizerStore, sources: Sequence[Source],
              cache: ScanCache, *, game_running: bool | None, dry_run: bool = False,
              clock: Callable[[], float] = time.time,
              progress: Callable[[int, int], None] | None = None,
              cancel: threading.Event | None = None) -> BootstrapReport:
    started = time.perf_counter()
    begun = int(clock())
    result = scan(vault, sources, cache, game_running=game_running, deep=True, now=clock,
                  progress=progress, cancel=cancel)
    existing, problems = decode_all(store.journal_records())
    lineage = store.document(LINEAGE)
    done_at = (lineage.get("bootstrap") or {}).get("done_at")
    live_starts = [r.at for r in existing if isinstance(r, ObsRecord) and r.src in LIVE_SRCS]
    live_starts += [r.at for r in existing if isinstance(r, OpRecord) and r.src == "app"]
    # From here on the journal and the operation hook record what happens: older evidence
    # only is the bootstrap's to add, now and in every later run.
    cutoff = done_at if isinstance(done_at, int) else min(live_starts, default=begun)

    plan = _Plan(vault, result, existing, cutoff)
    plan.entries()
    plan.oplog()
    fields = plan.records(existing)

    if dry_run:
        new = _dry_records(existing, fields, result, clock)
        written = len(new)
        state = replay(existing + new, result.versions, problems)
    else:
        with store.transaction() as tx:
            for record, at in fields:
                tx.append(record, at=at)
        journal, observed = _observe_now(vault, store, sources, cache, result, game_running,
                                         clock)
        written = len(fields) + observed
        with store.transaction() as tx:
            doc = tx.doc(LINEAGE)
            ensure_skeleton(doc)
            block = dict(doc.get("bootstrap") or {})
            block.setdefault("done_at", cutoff)
            block.update(app=APP, algo=ALGO, report=_counts(plan, result, written))
            doc["bootstrap"] = block
        state = journal.replay()
    return _report(plan, result, state, written, time.perf_counter() - started, dry_run)


def _observe_now(vault: Vault, store: OrganizerStore, sources: Sequence[Source],
                 cache: ScanCache, deep: ScanResult, game_running: bool | None,
                 clock: Callable[[], float]) -> tuple[Journal, int]:
    """Journal the live slots as they are now, not as the deep scan found them seconds or
    minutes ago: a quick second look under the live-op lock, so no write is under way."""
    try:
        with safety.exclusive_lock(liveop_lock_path(vault.root), timeout=LIVEOP_TIMEOUT):
            fresh = scan(None, sources, cache, game_running=game_running, now=clock)
            journal = Journal(store, {**deep.versions, **fresh.versions})
            return journal, journal.observe(fresh, src="scan", skip=frozenset()).written
    except safety.LockTimeout:
        log.warning("bootstrap: a live write is under way; the live slots are journaled at "
                    "the next refresh")
        return Journal(store, deep.versions), 0


class _Plan:
    def __init__(self, vault: Vault, result: ScanResult, existing: tuple[Record, ...],
                 cutoff: int | None) -> None:
        self.vault = vault
        self.result = result
        self.cutoff = cutoff
        self.items: list[_Item] = []
        self.skipped: list[tuple[str, str]] = []
        self.unresolved: list[tuple[str, str]] = []
        self.resolved = 0
        self.represented = {r.entry for r in existing if isinstance(r, ObsRecord) and r.entry}
        self.known_ops = {(r.op_id, r.target) for r in existing if isinstance(r, OpRecord)}
        self.app_ops = [(r.target, r.pair, r.at) for r in existing
                        if isinstance(r, OpRecord) and r.src == "app"]
        self.catalog = {e.id: e for e in vault.entries}
        self.refs = list(result.live)

    def _before_cutoff(self, when: int) -> bool:
        return self.cutoff is None or when < self.cutoff

    # --- "seen in backups" ------------------------------------------------------------

    def entries(self) -> None:
        for entry_id, scanned in self.result.entries.items():
            if scanned.kind not in TIMELINE_KINDS or not scanned.managed:
                continue                # owner copies of unknown time: no timeline evidence
            if entry_id in self.represented or not self._before_cutoff(scanned.created):
                continue
            ref = self._live_for_entry(self.catalog[entry_id], scanned)
            if ref is None:
                continue
            for capture in scanned.captures:
                self.items.append(_Item(scanned.created, 0, ref.target(capture.slot),
                                        capture.pair, {m.vid: m for m in capture.members},
                                        entry=entry_id))

    def _live_for_entry(self, entry: CatalogEntry, scanned: EntryScan) -> LiveRef | None:
        platform = Vault.tag_of(entry.id) or savedir.platform_of(Path(entry.path))
        same_platform = [r for r in self.refs if r.platform == platform]
        named = [r for r in same_platform if r.account_key == scanned.source]
        if len(named) == 1:
            return named[0]
        if not named and len(same_platform) == 1:
            self.skipped.append((entry.id, f"taken of {scanned.source!r}; mapped to the only "
                                           f"{platform} live folder"))
            return same_platform[0]
        self.skipped.append((entry.id, "entry-unmapped: no single live folder it was taken of"))
        return None

    # --- "put live by the app" ----------------------------------------------------------

    def oplog(self) -> None:
        log = self.vault.read_oplog()
        records = list(zip(log.lines, log))
        for index, (line, record) in enumerate(records):
            op = record.get("op")
            when = _utc(record.get("ts"))
            if op not in ("repopulate_slot", "clear_slot", "restore_full", "undo"):
                continue                # backups, extracts, imports and promotes place nothing
            label = f"line {line}: {op}"
            if when is None:
                self.unresolved.append((label, "no readable time"))
                continue
            if not self._before_cutoff(when):
                continue
            if op == "restore_full" and _followed_by_its_undo(records, index):
                op = "undo"
            if op == "undo" and not record.get("entry_id"):
                continue                # the older undo format: its restore_full line placed it
            op_id = f"legacy-{line}"
            try:
                placed = self._placements(op, record)
            except _Unresolved as exc:
                self.unresolved.append((label, str(exc)))
                continue
            kind = {"repopulate_slot": "repopulate", "clear_slot": "clear"}.get(op, op)
            for target, pair, members in placed:
                if (op_id, target) not in self.known_ops and not self._hook_recorded(
                        target, pair, when):
                    self.items.append(_Item(when, 1, target, pair, members, op_id=op_id,
                                            kind=kind))
            self.resolved += 1

    def _hook_recorded(self, target: LiveTarget, pair: PairKey, when: int) -> bool:
        """The operation hook recorded this write already (its own id, the same content)."""
        return any(t == target and p == pair and abs(at - when) <= HOOK_WINDOW
                   for t, p, at in self.app_ops)

    def _placements(self, op: str, record: Mapping[str, Any]):
        detail = str(record.get("detail") or "")
        if op == "repopulate_slot":
            m = _REPOPULATE.fullmatch(detail)
            if not m:
                raise _Unresolved("detail not understood")
            ref = self._target_ref(record, "xbox" if m[1] else "steam")
            source = self._entry_named(m[2])
            capture = _capture_at(self.result.entries[source.id], int(m[3]))
            return [(ref.target(int(m[4])), capture[0], capture[1])]
        if op == "clear_slot":
            m = _CLEAR.fullmatch(detail)
            if not m:
                raise _Unresolved("detail not understood")
            ref = self._target_ref(record, "xbox" if m[1] else "steam")
            return [(ref.target(int(m[2])), EMPTY_PAIR, {})]
        entry_id = record.get("entry_id")
        scanned = self.result.entries.get(entry_id)
        if scanned is None:
            raise _Unresolved(f"the restored entry {entry_id} is not in the vault")
        entry = self.catalog[entry_id]
        ref = self._target_ref(record, Vault.tag_of(entry.id)
                               or savedir.platform_of(Path(entry.path)))
        return [(ref.target(c.slot), c.pair, {m.vid: m for m in c.members})
                for c in scanned.captures]

    def _target_ref(self, record: Mapping[str, Any], platform: str) -> LiveRef:
        """The live folder written: the record's own target when it has one, else the only
        live folder of the platform."""
        written = record.get("target")
        if written:
            for ref, folder in self.result.live.items():
                if _same_path(folder.source.path, written):
                    return ref
            raise _Unresolved("the folder it wrote is not a live folder here")
        candidates = [r for r in self.refs if r.platform == platform]
        if len(candidates) != 1:
            raise _Unresolved(f"{len(candidates)} {platform} live folders; the line names none")
        return candidates[0]

    def _entry_named(self, name: str) -> CatalogEntry:
        matches = [e for e in self.catalog.values() if e.id in self.result.entries and (
            (e.managed and e.id == name) or (not e.managed and Path(e.path).name == name))]
        if len(matches) != 1:
            raise _Unresolved(f"{len(matches)} vault entries match the source {name!r}")
        return matches[0]

    # --- records ----------------------------------------------------------------------

    def records(self, existing: tuple[Record, ...]) -> list[tuple[dict, int]]:
        """Journal records for the new timeline steps, merged per slot with what the journal
        holds already: an observation only where the slot's pair changed from the step
        before it, every operation; each vid described the first time it appears."""
        steps: list[tuple[int, int, int, LiveTarget, PairKey, _Item | None]] = []
        for rec in existing:
            if isinstance(rec, OpRecord) and rec.status != "done":
                continue
            steps.append((rec.at, 0 if isinstance(rec, ObsRecord) else 1, rec.seq, rec.target,
                          rec.pair, None))
        for n, item in enumerate(self.items):
            steps.append((item.time, item.order, -len(self.items) + n, item.target, item.pair,
                          item))
        steps.sort(key=lambda s: (s[3].platform, s[3].account_key, s[3].instance, s[3].slot,
                                  s[0], s[1], s[2]))
        described = {v for r in existing for v in r.members}
        prev: dict[LiveTarget, PairKey] = {}
        new: list[_Item] = []
        evicted: dict[int, PairKey | None] = {}
        for _, _, _, target, pair, item in steps:
            if item is not None and (item.order == 1 or prev.get(target, None) != pair
                                     or target not in prev):
                evicted[id(item)] = prev.get(target)
                new.append(item)
            prev[target] = pair
        out: list[tuple[dict, int]] = []
        for item in sorted(new, key=lambda i: (i.time, i.order)):
            members = {v: m for v, m in item.members.items() if v not in described}
            described.update(members)
            if item.order == 0:
                out.append((obs_fields("backfill", item.target, item.pair, members, {},
                                       entry=item.entry), item.time))
            else:
                out.append((op_fields("oplog", item.op_id, item.kind, item.target, item.pair,
                                      evicted[id(item)], app=APP, members=members), item.time))
        return out


class _Unresolved(Exception):
    pass


def _capture_at(scanned: EntryScan, slot: int) -> tuple[PairKey, dict]:
    for capture in scanned.captures:
        if capture.slot == slot:
            return capture.pair, {m.vid: m for m in capture.members}
    raise _Unresolved(f"the source has no slot {slot}")


def _followed_by_its_undo(records: list, index: int) -> bool:
    """An older undo logged a restore_full of the snapshot, then an undo naming it."""
    if index + 1 >= len(records):
        return False
    after = records[index + 1][1]
    return (after.get("op") == "undo" and not after.get("entry_id")
            and after.get("snapshot_id") == records[index][1].get("entry_id"))


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _utc(local: object) -> int | None:
    """Oplog times are local naive ISO; this machine's zone turns them into UTC."""
    try:
        return int(datetime.fromisoformat(str(local)).astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _dry_records(existing: tuple[Record, ...], fields: list[tuple[dict, int]],
                 result: ScanResult, clock: Callable[[], float]) -> tuple[Record, ...]:
    """The records a real run would write - backfill, operations and the live state now -
    numbered after the existing ones, in memory only."""
    seq = max((r.seq for r in existing), default=0)
    out: list[Record] = []
    for record, at in fields:
        seq += 1
        out.append(decode_record({**record, "id": f"dry-{seq}", "seq": seq, "at": at}))
    last: dict[LiveTarget, PairKey] = {}
    for rec in sorted(existing + tuple(out), key=lambda r: r.order):
        if not (isinstance(rec, OpRecord) and rec.status != "done"):
            last[rec.target] = rec.pair
    described = {v for r in existing + tuple(out) for v in r.members}
    now = int(clock())
    for ref, folder in result.live.items():
        if not folder.readable or result.game_running is True:
            continue
        for capture in folder.captures:
            target = ref.target(capture.slot)
            unchanged = target in last and last[target] == capture.pair
            if capture.slot in folder.unstable or unchanged:
                continue
            members = {m.vid: m for m in capture.members if m.vid not in described}
            described.update(members)
            seq += 1
            out.append(decode_record({**obs_fields("scan", target, capture.pair, members, {}),
                                      "id": f"dry-{seq}", "seq": seq, "at": now}))
    return tuple(out)


def _counts(plan: _Plan, result: ScanResult, written: int) -> dict:
    return {"versions": len(result.versions), "pairs": len(result.pairs),
            "journal_records": written, "ops_resolved": plan.resolved,
            "ops_unresolved": len(plan.unresolved), "entries_skipped": len(plan.skipped)}


def _report(plan: _Plan, result: ScanResult, state, written: int, seconds: float,
            dry_run: bool) -> BootstrapReport:
    pids = {info.raw_pid for info in result.versions.values()}
    parents = {e.parent for e in state.edges}
    children = Counter(e.child for e in state.edges)
    fan_out = Counter(e.parent for e in state.edges)
    forks = []
    for vid, n in sorted(fan_out.items()):
        if n > 1:
            meta = (state.metas[vid].meta if vid in state.metas
                    else result.versions[vid].meta if vid in result.versions else None)
            name = meta.name if meta else ""
            date = (datetime.fromtimestamp(meta.meta_ts, timezone.utc).strftime("%Y-%m-%d")
                    if meta else "")
            forks.append((name, date))
    at_risk = 0
    for folder in result.live.values():
        for capture in folder.captures:
            if capture.members and not all(
                    any(loc.managed for loc in result.versions[m.vid].locations)
                    for m in capture.members):
                at_risk += 1
    return BootstrapReport(
        folders_scanned=len(result.live) + len(result.entries),
        entries_skipped=tuple(plan.skipped),
        copies_hashed=sum(len(c.members) for c in result.captures),
        versions=len(result.versions), pairs=len(result.pairs), lines=len(pids),
        legacy_lines=sum(1 for p in pids if not is_identified(p)),
        journal_records=written, ops_resolved=plan.resolved,
        ops_unresolved=tuple(plan.unresolved),
        edges_by_method=dict(Counter(e.method for e in state.edges)),
        roots=len(parents - set(children)), forks=tuple(forks),
        events=dict(Counter(e.kind for e in state.events)), at_risk_live=at_risk,
        problems=result.problems, seconds=seconds, dry_run=dry_run,
    )
