"""The content index as one object: ``IndexService`` (D4 2.12).

It implements D1's ``FactsProvider`` (``view``, ``changes``, ``is_milestone``, ``request``)
for the read model, plus cards, status, progress and events for the UI and the CLI. Every
method a UI calls is a cache read or a queue operation: decoding happens only in workers,
bytes are read only on the dispatcher thread, and the cache is written only by the writer
thread (invariants I6, I1). The index never blocks or delays an operation (I8): missing facts
mean meta-level display, nothing more.
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import lz4_block, msstore
from . import changes as changes_mod
from . import derive
from .cache import IndexCache
from .pool import Dispatcher, ExecutionPolicy, Executor, Priority, make_executor, split_key
from .schema import EXTRACTOR_VERSION
from .worker import IndexResult

EVENT_RING = 2000
DEEP_LRU = 5                      # deep detail of the last few opened cards, in memory only
MAX_STRIKES = 3                   # transient failures before a version stops being retried

Status = Literal["not-indexed", "queued", "running", "ok", "partial", "corrupt", "unsupported", "error",
                 "unreadable", "stale", "off"]


@dataclass(frozen=True)
class IndexProgress:
    state: Literal["idle", "running", "paused", "throttled", "off", "error"]
    queued: int
    running: int
    done: int
    failed: int
    done_bytes: int
    total_bytes: int
    eta_s: float | None
    message: str                     # ASCII, e.g. "Indexing 12 of 195 versions"


@dataclass(frozen=True)
class IndexEvent:
    seq: int
    kind: Literal["indexed", "failed", "stale", "cleared", "deep-ready", "finished"]
    vid: str | None
    status: str | None


def _default_game_running() -> bool | None:
    from .. import safety
    return safety.is_game_running()


class IndexService:
    def __init__(self, *, cache_path: Path | None, vault_id: str, locator, policy: ExecutionPolicy,
                 size_hint: Callable[[str], int | None] | None = None,
                 when: Callable[[str], float | None] | None = None,
                 game_running: Callable[[], bool | None] = _default_game_running,
                 clock: Callable[[], float] = time.time, forbidden: Iterable[Path] = (),
                 app_version: str = "", executor: Executor | None = None) -> None:
        self.policy = policy
        self.locator = locator
        self.clock = clock
        self._lock = threading.Lock()
        self._events: deque[IndexEvent] = deque(maxlen=EVENT_RING)
        self._seq = 0
        self._requested: dict[str, Priority] = {}
        self._error: str | None = None
        self._started_at: float | None = None
        self.cache: IndexCache | None = None
        self.dispatcher: Dispatcher | None = None
        self._writes: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None
        self._known: dict[str, tuple[str, int]] = {}
        self._strikes: dict[str, int] = {}
        self._deep: OrderedDict[str, dict] = OrderedDict()
        if policy.mode == "off":
            return
        self.cache = IndexCache(cache_path, vault_id=vault_id, app_version=app_version, forbidden=forbidden,
                                clock=clock)
        self._known = self.cache.indexed()
        self._strikes = self.cache.failures()
        self.dispatcher = Dispatcher(policy, executor or make_executor(policy), opener=self._open,
                                     on_result=lambda r: self._writes.put(("result", r)),
                                     on_failure=lambda v, k, e: self._writes.put(("failure", v, k, e)),
                                     size_hint=size_hint, when=when, game_running=game_running)
        self._writer = threading.Thread(target=self._write_loop, name="index-write", daemon=True)
        self._writer.start()
        self._writes.put(("gc",))
        self.dispatcher.start()
        refresh = self.cache.stale_vids() + self.cache.retry_vids()
        if refresh:
            self.dispatcher.submit(refresh, Priority.REFRESH)

    # --- D1's FactsProvider ----------------------------------------------------------------

    def view(self, vid: str) -> derive.FactsView | None:
        facts = self.facts(vid)
        return None if facts is None else derive.view(vid, facts)

    def changes(self, child: str, parent: str, *, edge_kind: str | None = None) -> changes_mod.ChangeSet | None:
        c, p = self.facts(child), self.facts(parent)
        if c is None or p is None:
            self.request([v for v, f in ((child, c), (parent, p)) if f is None], Priority.NEW)
            return None
        return changes_mod.changes(c, p, edge_kind=edge_kind, child_vid=child, parent_vid=parent)

    def is_milestone(self, child: str, parent: str) -> bool | None:
        c, p = self.facts(child), self.facts(parent)
        if c is None or p is None:
            return None
        return changes_mod.is_milestone(changes_mod.changes(c, p, child_vid=child, parent_vid=parent))

    def request(self, vids: Iterable[str], priority: Priority) -> int:
        """Queue versions that need (re)indexing; returns how many were newly queued."""
        if self.dispatcher is None:
            return 0
        vids = list(dict.fromkeys(vids))
        with self._lock:
            for vid in vids:
                if vid not in self._requested or priority < self._requested[vid]:
                    self._requested[vid] = Priority(priority)
            wanted = [v for v in vids if self._needs_work(v)]
        if vids:
            self._writes.put(("touch", vids))
        if wanted and self._started_at is None:
            self._started_at = time.monotonic()
        return self.dispatcher.submit(wanted, priority)

    def _needs_work(self, vid: str) -> bool:
        known = self._known.get(vid)
        if known is not None and known[1] >= EXTRACTOR_VERSION:
            return False                      # indexed by this extractor: never again
        return self._strikes.get(vid, 0) < MAX_STRIKES

    # --- facts and cards ---------------------------------------------------------------------

    def facts(self, vid: str) -> dict | None:
        return None if self.cache is None else self.cache.facts(vid)

    def views(self, vids: Iterable[str]) -> dict[str, derive.FactsView]:
        out = {}
        for vid in vids:
            v = self.view(vid)
            if v is not None:
                out[vid] = v
        return out

    def card(self, vid: str) -> derive.FactsCard | None:
        """The version card; its ``deep`` section is filled once :meth:`request_deep` is done."""
        facts = self.facts(vid)
        return None if facts is None else derive.card(vid, facts, deep=self.deep(vid))

    # --- deep detail on demand (a version card) ----------------------------------------------

    def request_deep(self, vid: str) -> None:
        """A card opened: its facts if missing, and its deep detail, on the reserved worker.
        ``deep-ready`` is announced through :meth:`events_since`."""
        if self.dispatcher is None:
            return
        if self.status(vid) == "not-indexed":
            self.request([vid], Priority.INTERACTIVE)
        if self.deep(vid) is None:
            self.dispatcher.submit([vid], Priority.INTERACTIVE, kind="deep")

    def deep(self, vid: str) -> dict | None:
        with self._lock:
            found = self._deep.get(vid)
            if found is not None:
                self._deep.move_to_end(vid)
            return found

    def status(self, vid: str) -> Status:
        if self.dispatcher is None:
            return "off"
        if self.dispatcher.is_running(vid):
            return "running"
        known = self._known.get(vid)
        if known is not None:
            state, extractor = known
            if extractor < EXTRACTOR_VERSION and state in ("ok", "partial"):
                return "stale"
            return state  # type: ignore[return-value]
        if self.dispatcher.is_queued(vid):
            return "queued"
        if self._strikes.get(vid, 0):
            return "unreadable"
        return "not-indexed"

    # --- control -------------------------------------------------------------------------------

    def rebuild(self) -> None:
        """Delete every row and index again everything requested this session."""
        if self.dispatcher is None:
            return
        self.dispatcher.clear()
        done = threading.Event()
        self._writes.put(("clear", done))
        done.wait(30)
        with self._lock:
            requested = dict(self._requested)
        for tier in sorted(set(requested.values())):
            self.dispatcher.submit([v for v, t in requested.items() if t == tier], tier)

    def pause(self) -> None:
        if self.dispatcher is not None:
            self.dispatcher.pause()

    def resume(self) -> None:
        if self.dispatcher is not None:
            self.dispatcher.resume()

    def cancel(self) -> None:
        """Stop the running jobs now (the queue is kept) and pause; ``resume`` continues."""
        if self.dispatcher is not None:
            self.dispatcher.cancel()

    def flush(self, timeout: float = 30) -> bool:
        """Wait until every result handed to the writer so far is stored (CLI, tests)."""
        if self._writer is None:
            return True
        done = threading.Event()
        self._writes.put(("sync", done))
        return done.wait(timeout)

    def idle(self) -> bool:
        if self.dispatcher is None:
            return True
        c = self.dispatcher.counts()
        return c.queued == 0 and c.running == 0

    def close(self) -> None:
        if self.dispatcher is not None:
            self.dispatcher.stop()
        if self._writer is not None:
            self._writes.put(None)
            self._writer.join(30)
            self._writer = None
        if self.cache is not None:
            self.cache.close()

    # --- progress and events -----------------------------------------------------------------

    def progress(self) -> IndexProgress:
        if self.dispatcher is None:
            return IndexProgress("off", 0, 0, 0, 0, 0, 0, None, "Content index off")
        c = self.dispatcher.counts()
        total = c.done + c.running + c.queued
        if self._error:
            state, message = "error", f"Content index stopped: {self._error}"
        elif self.dispatcher.paused:
            state, message = "paused", "Content index paused"
        elif c.running or c.queued:
            playing = self.dispatcher.playing
            state = "throttled" if playing else "running"
            message = f"Indexing {c.done + 1} of {total} versions" + (" (game running)" if playing else "")
        else:
            state, message = "idle", "Content index up to date"
        eta = None
        if self._started_at and c.done_bytes and c.queued_bytes > c.done_bytes:
            rate = c.done_bytes / max(time.monotonic() - self._started_at, 1e-3)
            eta = (c.queued_bytes - c.done_bytes) / rate
        return IndexProgress(state, c.queued, c.running, c.done, c.failed, c.done_bytes,
                             max(c.queued_bytes, c.done_bytes), eta, message)

    def events_since(self, seq: int, limit: int = 500) -> list[IndexEvent]:
        with self._lock:
            return [e for e in self._events if e.seq > seq][:limit]

    def _event(self, kind: str, vid: str | None = None, status: str | None = None) -> None:
        with self._lock:
            self._seq += 1
            self._events.append(IndexEvent(self._seq, kind, vid, status))  # type: ignore[arg-type]

    # --- the dispatcher thread's opener ------------------------------------------------------

    def _open(self, vid: str) -> tuple[bytes, int | None]:
        opened = self.locator.open(vid)
        data = opened.data
        hint = None
        if getattr(opened, "platform", None) == "xbox" and not lz4_block.is_chunked(data):
            hint = msstore.parse_ms_meta(opened.meta_plain).get("size_field") or None
        return data, hint

    # --- the writer thread ---------------------------------------------------------------------

    def _write_loop(self) -> None:
        self.cache.adopt_writer()
        while True:
            op = self._writes.get()
            if op is None:
                return
            try:
                self._apply(op)
            except sqlite3.Error as exc:
                # Disk full, I/O error: stop indexing, keep serving what is cached.
                self._error = f"{type(exc).__name__} writing the cache"
                if self.dispatcher is not None:
                    self.dispatcher.pause()
                self._event("failed", None, "error")
            if self.dispatcher is not None and self.idle() and self._writes.empty() and op[0] == "result":
                self._event("finished")

    def _apply(self, op: tuple) -> None:
        kind = op[0]
        if kind == "result":
            result: IndexResult = op[1]
            if result.kind == "deep":
                self._deep_ready(result)
                return
            self.cache.write_result(result)
            if result.status == "mismatch":
                self._strike(result.vid)
                self._event("failed", result.vid, "mismatch")
                return
            stored = (result.facts or {}).get("extractor", EXTRACTOR_VERSION)
            self._known[result.vid] = (result.status, stored)
            self._strikes.pop(result.vid, None)
            self._event("indexed", result.vid, result.status)
        elif kind == "failure":
            _, key, failure, error = op
            job_kind, vid = split_key(key)
            if job_kind == "deep":
                self._event("failed", vid, f"deep {failure}")
                return
            self.cache.record_failure(vid, failure, error)
            self._strike(vid)
            self._event("failed", vid, failure)
        elif kind == "touch":
            self.cache.touch(op[1])
        elif kind == "gc":
            if self.cache.gc():
                self._known = self.cache.indexed()
        elif kind == "clear":
            self.cache.clear()
            self._known.clear()
            self._strikes.clear()
            self._event("cleared")
            op[1].set()
        elif kind == "sync":
            op[1].set()

    def _deep_ready(self, result: IndexResult) -> None:
        if result.status != "ok" or result.deep is None:
            self._event("failed", result.vid, f"deep {result.status}")
            return
        with self._lock:
            self._deep[result.vid] = result.deep
            self._deep.move_to_end(result.vid)
            while len(self._deep) > DEEP_LRU:
                self._deep.popitem(last=False)
        self._event("deep-ready", result.vid, "ok")

    def _strike(self, vid: str) -> None:
        self._strikes[vid] = self._strikes.get(vid, 0) + 1
