"""The content index as one object: ``IndexService`` (D4 2.12).

It implements D1's ``FactsProvider`` (``view``, ``changes``, ``is_milestone``, ``request``)
for the read model, plus cards, status, progress and events for the UI and the CLI. Every
method a UI calls is a cache read or a queue operation: decoding happens only in workers,
bytes are read only on the dispatcher thread, and the cache is written only by the writer
thread (invariants I6, I1). The index never blocks or delays an operation (I8): missing facts
mean meta-level display, nothing more.
"""
from __future__ import annotations

import logging
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
from .cache import IndexCache, is_busy
from .extract import describe
from .pool import Dispatcher, ExecutionPolicy, Executor, Priority, make_executor, split_key
from .schema import EXTRACTOR_VERSION
from .worker import IndexResult

EVENT_RING = 2000
BUSY_BACKOFF_S = (0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 4.0)   # waiting out another instance's lock
DEEP_LRU = 5                      # deep detail of the last few opened cards, in memory only
WRITER_JOIN_S = 30                # how long close() waits for the writer thread
MAX_STRIKES = 3                   # transient failures before a version stops being retried

log = logging.getLogger(__name__)

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
    kind: Literal["indexed", "failed", "stale", "cleared", "deep-ready", "finished", "milestone", "changes", "warmed"]
    vid: str | None
    status: str | None


def _default_game_running() -> bool | None:
    from .. import safety
    return safety.is_game_running()


class IndexService:
    #: The request tiers, reachable through the service so that the organizer (D1's
    #: ``FactsProvider`` user) never imports the index.
    Priority = Priority

    def __init__(self, *, cache_path: Path | None, vault_id: str, locator, policy: ExecutionPolicy,
                 defer_refresh: bool = False,
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
        self._closed = False
        self._abandon = False             # close() gave up waiting: the writer stops and closes the cache
        self._handoff = threading.Lock()  # who closes the cache: close() or, once abandoned, the writer
        self._writer_done = False
        self._cache_closed = False
        self._started_at: float | None = None
        self.cache: IndexCache | None = None
        self.dispatcher: Dispatcher | None = None
        self._writes: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None
        self._known: dict[str, tuple[str, int]] = {}
        self._strikes: dict[str, int] = {}
        self._deep: OrderedDict[str, dict] = OrderedDict()
        # What a UI thread may read without touching sqlite: the derived view of every
        # indexed version and the milestone answers computed so far (both filled on the
        # writer thread).
        self._views: dict[str, derive.FactsView] = {}
        self._milestones: dict[tuple[str, str], bool] = {}
        self._milestone_asked: set[tuple[str, str]] = set()
        self._changes: dict[tuple, changes_mod.ChangeSet | None] = {}
        self._changes_asked: set[tuple] = set()
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
        self._writes.put(("warm",))
        self.dispatcher.start()
        if not defer_refresh:
            self.queue_refresh()

    def queue_refresh(self) -> int:
        """Queue the stale and retryable versions at REFRESH. A service whose locator cannot
        open anything yet (the organizer has not scanned) is built with ``defer_refresh`` and
        refreshed after the scan, so a "not scanned yet" job never becomes a failed attempt."""
        if self.dispatcher is None or self._closed:
            return 0
        refresh = self.cache.stale_vids() + self.cache.retry_vids()
        return self.dispatcher.submit(refresh, Priority.REFRESH) if refresh else 0

    # --- D1's FactsProvider ----------------------------------------------------------------

    @property
    def active(self) -> bool:
        """False when the index is off: requests are dropped, so nothing should be marked as asked."""
        return self.dispatcher is not None and not self._closed

    def view(self, vid: str) -> derive.FactsView | None:
        """The version's facts view from memory: never a database read, never a wait. None
        until the version is indexed (or, just after start, until the cache is loaded)."""
        with self._lock:
            return self._views.get(vid)

    def changes(self, child: str, parent: str, *, edge_kind: str | None = None) -> changes_mod.ChangeSet | None:
        """The change set from what is computed so far: a memory read or a queue operation,
        never a database read. None means "not ready": the missing versions are queued at
        NEW, or the comparison is worked out on the writer thread (a ``changes`` event says
        when it is ready)."""
        if self._closed:
            return None
        key = (child, parent, edge_kind)
        with self._lock:
            if key in self._changes:
                return self._changes[key]
            missing = [v for v in (child, parent) if v not in self._views]
            if not missing:
                if key in self._changes_asked:
                    return None
                self._changes_asked.add(key)
        if missing:
            self.request(missing, Priority.NEW)
        else:
            self._writes.put(("changes", child, parent, edge_kind))
        return None

    def is_milestone(self, child: str, parent: str) -> bool | None:
        """Whether the step from ``parent`` reached a milestone, from what is computed so
        far. An unknown answer is None and is worked out on the writer thread (never on the
        caller's); a ``milestone`` event says when it is ready."""
        key = (child, parent)
        if self._closed:
            return None
        with self._lock:
            if key in self._milestones:
                return self._milestones[key]
            if child not in self._views or parent not in self._views or key in self._milestone_asked:
                return None
            self._milestone_asked.add(key)
        self._writes.put(("milestone", child, parent))
        return None

    def request(self, vids: Iterable[str], priority: Priority) -> int:
        """Queue versions that need (re)indexing; returns how many were newly queued."""
        if self.dispatcher is None or self._closed:
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
        return None if self.cache is None or self._closed else self.cache.facts(vid)

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
        if self.dispatcher is None or self._closed:
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
        if self.dispatcher is None or self._closed:
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
        if self.dispatcher is None or self._closed:
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
        """Stop for good. Afterwards every call is a no-op or an "off" answer (a worker job may
        still hold this service). A writer that outlives the wait stops before its next write
        and closes the cache itself."""
        if self._closed:
            return
        self._closed = True
        if self.dispatcher is not None:
            self.dispatcher.stop()
        writer, self._writer = self._writer, None
        if writer is not None:
            self._writes.put(None)
            writer.join(WRITER_JOIN_S)
            with self._handoff:
                if not self._writer_done:
                    self._abandon = True
                    return                        # the writer closes the cache as it leaves
        self._close_cache()

    def _close_cache(self) -> None:
        """Closes the cache exactly once, whichever side gets here first."""
        with self._handoff:
            if self._cache_closed or self.cache is None:
                return
            self._cache_closed = True
        self.cache.close()

    # --- progress and events -----------------------------------------------------------------

    def progress(self) -> IndexProgress:
        if self.dispatcher is None or self._closed:
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
        try:
            self._write_ops()
        finally:
            with self._handoff:
                self._writer_done = True
                abandoned = self._abandon
            if abandoned:                         # close() stopped waiting and left the cache to us
                self._close_cache()

    def _write_ops(self) -> None:
        self.cache.adopt_writer()
        unfinished = False
        while True:
            op = self._writes.get()
            if self._abandon or op is None:       # abandoned: no further writes
                return
            self._apply_safely(op)
            unfinished = unfinished or op[0] == "result"
            # Any later op (a "touch" queued behind the last result) may be the one that finds
            # the queue empty, so the check is not tied to the result op itself.
            if unfinished and self.dispatcher is not None and self.idle() and self._writes.empty():
                self._event("finished")
                unfinished = False

    def _apply_safely(self, op: tuple) -> None:
        """Apply one write. A lock held by another instance is waited out with backoff; a
        failing cache (disk full, I/O error, read-only) stops indexing for the session;
        anything else fails only this item (a failure row and an event) and the loop goes on."""
        for delay in BUSY_BACKOFF_S:
            try:
                self._apply(op)
                return
            except sqlite3.OperationalError as exc:
                if not is_busy(exc):
                    self._cache_failed(exc)          # disk full, I/O error, read-only file
                    return
                time.sleep(delay)
            except (sqlite3.IntegrityError, sqlite3.InterfaceError, sqlite3.ProgrammingError,
                    sqlite3.DataError) as exc:
                self._item_failed(op, f"store-error: {describe(exc)}")   # this item's data, not the cache
                return
            except sqlite3.Error as exc:
                self._cache_failed(exc)              # a damaged database
                return
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop the writer
                log.warning("content index could not store an item: %s", describe(exc))
                self._item_failed(op, f"store-error: {describe(exc)}")
                return
        # Still locked after the backoff: this item is dropped (it is requested again later),
        # indexing goes on.
        self._item_failed(op, "cache busy", record=False)

    def _cache_failed(self, exc: BaseException) -> None:
        # Disk full, I/O error, read-only: stop indexing, keep serving what is cached.
        self._error = f"{type(exc).__name__} writing the cache"
        if self.dispatcher is not None:
            self.dispatcher.pause()
        self._event("failed", None, "error")

    def _item_failed(self, op: tuple, reason: str, *, record: bool = True) -> None:
        vid = None
        if op[0] == "result":
            vid = op[1].vid
        elif op[0] == "failure":
            vid = split_key(op[1])[1]
        if vid is not None and record:
            try:
                self.cache.record_failure(vid, "store-error", reason)
            except Exception as exc:  # noqa: BLE001 - the event below still tells the UI
                log.warning("content index could not record a failure: %s", describe(exc))
            self._strike(vid)
        self._event("failed", vid, reason[:60])

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
            self._remember_view(result.vid)       # before the status flips: "ok" implies a view
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
                with self._lock:
                    self._views = {v: view for v, view in self._views.items() if v in self._known}
        elif kind == "warm":
            for vid in list(self._known):
                self._remember_view(vid, forget_milestones=False)
            self._event("warmed")
        elif kind == "milestone":
            _, child, parent = op
            c, p = self.cache.facts(child), self.cache.facts(parent)
            with self._lock:
                self._milestone_asked.discard((child, parent))
            if c is None or p is None:
                return
            found = changes_mod.is_milestone(changes_mod.changes(c, p, child_vid=child, parent_vid=parent))
            with self._lock:
                self._milestones[(child, parent)] = found
            self._event("milestone", child, "yes" if found else "no")
        elif kind == "changes":
            _, child, parent, edge_kind = op
            c, p = self.cache.facts(child), self.cache.facts(parent)
            with self._lock:
                self._changes_asked.discard((child, parent, edge_kind))
            if c is None or p is None:
                return
            found = changes_mod.changes(c, p, edge_kind=edge_kind, child_vid=child, parent_vid=parent)
            with self._lock:
                self._changes[(child, parent, edge_kind)] = found
            self._event("changes", child, "ready")
        elif kind == "clear":
            self.cache.clear()
            self._known.clear()
            with self._lock:
                self._views.clear()
                self._milestones.clear()
                self._milestone_asked.clear()
                self._changes.clear()
                self._changes_asked.clear()
            self._strikes.clear()
            self._event("cleared")
            op[1].set()
        elif kind == "sync":
            op[1].set()

    def _remember_view(self, vid: str, *, forget_milestones: bool = True) -> None:
        """Derive the version's view once, here on the writer thread, for :meth:`view`."""
        facts = self.cache.facts(vid)
        view = derive.view(vid, facts) if facts is not None else None
        with self._lock:
            if view is None:
                self._views.pop(vid, None)
            else:
                self._views[vid] = view
            if forget_milestones:
                for key in [k for k in self._milestones if vid in k]:
                    del self._milestones[key]
                for key in [k for k in self._changes if vid in k[:2]]:
                    del self._changes[key]

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
