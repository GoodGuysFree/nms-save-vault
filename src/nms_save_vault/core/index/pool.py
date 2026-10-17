"""Running index jobs off the UI thread: policy, priorities, dispatcher, executors (D4 2.8).

The :class:`Dispatcher` decides *what* runs next and *whether* it may start: priority tiers,
the worker count (one slot kept free for interactive jobs), a heap budget so a few huge
saves never run together, oversized saves alone, a watchdog for hung workers, and while the
game runs only live and new versions on one worker. An executor decides *how* a job runs:
spawned worker processes (the default; ``json.loads`` holds the GIL, so threads would freeze
Tk) or inline on the dispatcher thread (POSIX kits until spawn is verified there).

The dispatcher's decisions live in :meth:`Dispatcher.step`, which the dispatcher thread calls
in a loop and tests call directly with a fake executor and clock. Bytes are read through the
caller's ``opener`` on the dispatcher thread, never on a UI thread (C31).
"""
from __future__ import annotations

import heapq
import itertools
import logging
import multiprocessing
import multiprocessing.pool
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Literal, Protocol

from . import extract
from .worker import IndexJob, IndexResult, run_job

GIB = 1024 ** 3
DEFAULT_BUDGET = int(1.2 * GIB)
HEAP_PER_JSON_BYTE = 9            # R5: 151 MB of heap for 18.2 MB of JSON
JSON_PER_DATA_BYTE = 4            # compressed saves expand ~3.4x; 4 is the safe guess
TIMEOUT_FLOOR_S = 60.0
TIMEOUT_PER_JSON_MB_S = 1.0       # 10 s per 10 MB of JSON
IDLE_CLOSE_S = 60.0
GAME_CHECK_S = 5.0
DEAD_WORKER_GRACE_S = 3.0         # a worker gone this long without its job's result has died

log = logging.getLogger(__name__)


class Priority(IntEnum):
    INTERACTIVE = 0   # a version card or compare view is open
    LIVE = 1          # current live members
    TIP = 2           # each line's representative and branch tips
    NEW = 3           # first seen since the last run, or created by an operation
    BACKFILL = 4      # every other known version
    REFRESH = 5       # stale rows (older extractor / decoder)


NEWEST_FIRST = {Priority.LIVE, Priority.TIP, Priority.NEW}
LARGEST_FIRST = {Priority.BACKFILL, Priority.REFRESH}
PAUSED_WHILE_PLAYING = {Priority.BACKFILL, Priority.REFRESH}


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: Literal["processes", "thread", "off"]
    workers: int                      # processes: 1-8; thread: 1; off: 0
    reserve_interactive: bool         # keep one worker for on-demand jobs (workers >= 3)
    memory_budget: int = DEFAULT_BUDGET


# --- executors ---------------------------------------------------------------------------


Done = Callable[[IndexResult], None]
Failed = Callable[[BaseException], None]


class ExecutorClosed(RuntimeError):
    """The executor was closed: it starts no more work."""


class Executor(Protocol):
    inline: bool

    def submit(self, job: IndexJob, done: Done, failed: Failed, token: int = 0) -> None: ...
    def terminate(self) -> None: ...
    def idle(self) -> None: ...
    def close(self) -> None: ...
    def dead_tokens(self, live: Iterable[int]) -> set[int]: ...


class ThreadExecutor:
    """Runs each job inline on the dispatcher thread, one at a time (2.8.5)."""

    inline = True

    def __init__(self, target: Callable[[IndexJob], IndexResult] = run_job) -> None:
        self.target = target

    def submit(self, job: IndexJob, done: Done, failed: Failed, token: int = 0) -> None:
        try:
            result = self.target(job)
        except Exception as exc:  # noqa: BLE001 - reported like a crashed worker
            failed(exc)
            return
        done(result)

    def terminate(self) -> None:
        """An inline job cannot be interrupted; cancelling waits for it (at most one job)."""

    def idle(self) -> None:
        pass

    def close(self) -> None:
        pass

    def dead_tokens(self, live: Iterable[int]) -> set[int]:
        return set()


# Set in each worker process by the pool initializer: where a job announces that it started.
_STARTED = None


def _worker_init(started) -> None:
    global _STARTED
    _STARTED = started


def _tracked(token: int, target: Callable[[IndexJob], IndexResult], job: IndexJob) -> IndexResult:
    """Run ``target`` in a worker, first telling the parent which process took the job."""
    if _STARTED is not None:
        _STARTED.put((token, os.getpid()))
    return target(job)


class ProcessExecutor:
    """A spawn ``multiprocessing.Pool``: created on the first job, closed when idle, and
    terminated (18 ms in the kit) to cancel. ``maxtasksperchild`` bounds a worker's memory
    growth; Pool rather than ProcessPoolExecutor because it can terminate running work."""

    inline = False

    def __init__(self, workers: int, *, target: Callable[[IndexJob], IndexResult] = run_job,
                 maxtasksperchild: int = 25, clock: Callable[[], float] = time.monotonic) -> None:
        self.workers = workers
        self.target = target
        self.maxtasksperchild = maxtasksperchild
        self.clock = clock
        self._pool = None
        self._started = None
        self._closed = False
        self._lock = threading.Lock()
        self._pids: dict[int, int] = {}           # token -> worker pid that took it
        self._gone: dict[int, float] = {}         # token -> when its worker was first missing

    def _get(self):
        with self._lock:
            if self._closed:
                raise ExecutorClosed("the index workers were closed")
            if self._pool is not None and getattr(self._pool, "_state", None) != multiprocessing.pool.RUN:
                self._pool = None                 # broken or shut down under us: start a new one
            if self._pool is None:
                ctx = multiprocessing.get_context("spawn")   # fork with Tk/X11 state is unsafe
                self._started = ctx.SimpleQueue()
                self._pids.clear()
                self._gone.clear()
                self._pool = ctx.Pool(self.workers, initializer=_worker_init, initargs=(self._started,),
                                      maxtasksperchild=self.maxtasksperchild)
            return self._pool

    @property
    def running(self) -> bool:
        return self._pool is not None

    def submit(self, job: IndexJob, done: Done, failed: Failed, token: int = 0) -> None:
        self._get().apply_async(_tracked, (token, self.target, job), callback=done, error_callback=failed)

    def dead_tokens(self, live: Iterable[int]) -> set[int]:
        """Jobs whose worker process ended without a result (killed, crashed hard): a Pool
        replaces the process but never reports the lost job, so it is found here."""
        with self._lock:
            pool, started = self._pool, self._started
            if pool is None or started is None:
                return set()
            while not started.empty():
                token, pid = started.get()
                self._pids[token] = pid
            alive = {proc.pid for proc in list(getattr(pool, "_pool", [])) if proc.is_alive()}
            now = self.clock()
            dead = set()
            for token in live:
                pid = self._pids.get(token)
                if pid is None or pid in alive:
                    self._gone.pop(token, None)
                    continue
                first = self._gone.setdefault(token, now)
                if now - first >= DEAD_WORKER_GRACE_S:
                    dead.add(token)
            for token in dead:
                self._pids.pop(token, None)
                self._gone.pop(token, None)
            return dead

    def terminate(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
            self._pids.clear()
            self._gone.clear()
        if pool is not None:
            pool.terminate()
            pool.join()

    def idle(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()
            pool.join()

    def close(self) -> None:
        """Terminate the workers for good: nothing is started afterwards."""
        with self._lock:
            self._closed = True
        self.terminate()


def make_executor(policy: ExecutionPolicy) -> Executor | None:
    if policy.mode == "processes":
        return ProcessExecutor(policy.workers)
    if policy.mode == "thread":
        return ThreadExecutor()
    return None


# --- the dispatcher ---------------------------------------------------------------------


@dataclass
class _Running:
    vid: str
    tier: Priority
    est: int
    json_bytes: int
    started: float
    alone: bool


@dataclass
class DispatchStats:
    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    queued_bytes: int = 0
    done_bytes: int = 0


@dataclass
class _Held:
    """A job whose bytes are read but that is waiting for capacity."""
    vid: str
    tier: Priority
    job: IndexJob
    est: int
    json_bytes: int
    alone: bool


Opener = Callable[[str], "tuple[bytes, int | None]"]
DEEP_PREFIX = "deep:"


def job_key(vid: str, kind: str = "facts") -> str:
    """The dispatcher's queue key: the vid for facts, ``deep:<vid>`` for a deep decode."""
    return vid if kind == "facts" else DEEP_PREFIX + vid


def split_key(key: str) -> tuple[str, str]:
    return ("deep", key[len(DEEP_PREFIX):]) if key.startswith(DEEP_PREFIX) else ("facts", key)


class Dispatcher:
    def __init__(self, policy: ExecutionPolicy, executor: Executor, *, opener: Opener,
                 on_result: Callable[[IndexResult], None], on_failure: Callable[[str, str, str], None],
                 size_hint: Callable[[str], int | None] | None = None,
                 when: Callable[[str], float | None] | None = None,
                 game_running: Callable[[], bool | None] = lambda: False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.policy = policy
        self.executor = executor
        self.opener = opener
        self.on_result = on_result
        self.on_failure = on_failure
        self.size_hint = size_hint or (lambda _vid: None)
        self.when = when or (lambda _vid: None)
        self.game_running = game_running
        self.clock = clock
        self._lock = threading.Condition()
        self._heap: list[tuple] = []
        self._pending: dict[str, tuple] = {}            # vid -> its live heap entry
        self._alone: set[str] = set()                   # re-queued to run alone (retries)
        self._retried: set[str] = set()
        self._seq = itertools.count()
        self._tokens = itertools.count(1)
        self._running: dict[int, _Running] = {}
        self._completions: deque = deque()
        self._held: _Held | None = None
        self._paused = False
        self._stopped = False
        self._idle_since: float | None = None
        self._playing: bool | None = False
        self._game_checked = float("-inf")
        self.stats = DispatchStats()
        self._thread: threading.Thread | None = None

    # --- queueing (any thread) -----------------------------------------------------------

    def submit(self, vids: Iterable[str], priority: Priority, kind: str = "facts") -> int:
        """Queue ``vids`` at ``priority``; a vid already queued moves up to a better tier.
        Returns how many were newly queued or moved to a better tier (not counting running ones). A ``deep``
        job is queued under its own key (:func:`job_key`), so it never merges with facts."""
        added = 0
        with self._lock:
            running = {r.vid for r in self._running.values()} | ({self._held.vid} if self._held else set())
            for vid in (job_key(v, kind) for v in vids):
                if vid in running:
                    continue
                current = self._pending.get(vid)
                if current is not None and current[0] <= priority:
                    continue
                added += 1                                  # newly queued, or moved up a tier
                if current is None:
                    self.stats.queued_bytes += self._json_estimate(vid) or 0
                self._push(vid, Priority(priority))
            self._lock.notify_all()
        return added

    def _push(self, vid: str, tier: Priority) -> None:
        seq = next(self._seq)
        if tier in NEWEST_FIRST:
            key = -(self.when(split_key(vid)[1]) or 0)
        elif tier in LARGEST_FIRST:
            key = -(self._json_estimate(vid) or 0)
        else:
            key = 0
        entry = (int(tier), key, seq, vid)
        self._pending[vid] = entry
        heapq.heappush(self._heap, entry)

    def _pop(self, allowed: Callable[[Priority], bool]) -> tuple[str, Priority] | None:
        while self._heap:
            entry = self._heap[0]
            tier, _key, _seq, vid = entry
            if self._pending.get(vid) is not entry:
                heapq.heappop(self._heap)                   # superseded by a better tier
                continue
            if not allowed(Priority(tier)):
                return None                                 # every later entry is a worse tier
            heapq.heappop(self._heap)
            del self._pending[vid]
            return vid, Priority(tier)
        return None

    def queued(self) -> list[tuple[str, Priority]]:
        with self._lock:
            return [(e[3], Priority(e[0])) for e in sorted(self._pending.values())]

    def is_queued(self, vid: str) -> bool:
        with self._lock:
            return vid in self._pending or (self._held is not None and self._held.vid == vid)

    def is_running(self, vid: str) -> bool:
        with self._lock:
            return any(r.vid == vid for r in self._running.values())

    def counts(self) -> DispatchStats:
        with self._lock:
            self.stats.queued = len(self._pending) + (1 if self._held else 0)
            self.stats.running = len(self._running)
            return DispatchStats(**vars(self.stats))

    # --- control -------------------------------------------------------------------------

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._lock.notify_all()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def playing(self) -> bool:
        """The game was running (or unknown) at the last check: one worker, no backfill."""
        return self._playing is not False

    def cancel(self) -> None:
        """Stop what is running now and pause; running and held jobs go back to the queue."""
        with self._lock:
            self._paused = True
            back = [(r.vid, r.tier) for r in self._running.values()]
            if self._held is not None:
                back.append((self._held.vid, self._held.tier))
                self._held = None
            self._running.clear()
            self._completions.clear()
        self.executor.terminate()
        with self._lock:
            for vid, tier in back:
                if vid not in self._pending:
                    self._push(vid, tier)

    def clear(self) -> None:
        """Forget every queued job (a rebuild re-queues from scratch)."""
        with self._lock:
            self._heap.clear()
            self._pending.clear()
            self._held = None
            self.stats = DispatchStats()

    # --- the scheduling step -------------------------------------------------------------

    def _done(self, token: int) -> Done:
        def done(result: IndexResult) -> None:
            with self._lock:
                self._completions.append((token, result, None))
                self._lock.notify_all()
        return done

    def _failed(self, token: int) -> Failed:
        def failed(exc: BaseException) -> None:
            with self._lock:
                self._completions.append((token, None, exc))
                self._lock.notify_all()
        return failed

    def _json_estimate(self, key: str, data_len: int | None = None) -> int | None:
        hint = self.size_hint(split_key(key)[1])
        if hint:
            return hint
        return data_len * JSON_PER_DATA_BYTE if data_len else None

    def _workers(self) -> int:
        return 1 if self._playing is not False else max(1, self.policy.workers)

    def _check_game(self) -> None:
        now = self.clock()
        if now - self._game_checked >= GAME_CHECK_S:
            self._game_checked = now
            self._playing = self.game_running()

    def step(self) -> bool:
        """One round: collect finished jobs, enforce timeouts, start at most one job.
        Returns True when anything changed (the loop then runs again at once)."""
        changed = self._collect()
        changed |= self._reap_dead()
        changed |= self._watchdog()
        self._check_game()
        changed |= self._start_one()
        self._idle_tick()
        return changed

    def _collect(self) -> bool:
        with self._lock:
            items = list(self._completions)
            self._completions.clear()
        changed = False
        for token, result, exc in items:
            with self._lock:
                run = self._running.pop(token, None)
            if run is None:
                continue                                    # terminated before it reported
            changed = True
            if exc is not None:
                self._retry_or_fail(run, "worker-died", f"worker crashed: {extract.describe(exc)}")
                continue
            if result.status == "error" and (result.error or "").startswith("MemoryError") \
                    and run.vid not in self._retried:
                self._retried.add(run.vid)
                self._requeue(run.vid, run.tier, alone=True)
                continue
            self.stats.done += 1
            self.stats.done_bytes += run.json_bytes
            if result.status in ("error", "mismatch"):
                self.stats.failed += 1
            self.on_result(result)
        return changed

    def _reap_dead(self) -> bool:
        with self._lock:
            live = list(self._running)
        dead = self.executor.dead_tokens(live) if live else set()
        for token in dead:
            with self._lock:
                run = self._running.pop(token, None)
            if run is not None:
                self._retry_or_fail(run, "worker-died", "worker process ended without a result")
        return bool(dead)

    def _retry_or_fail(self, run: _Running, kind: str, error: str) -> None:
        if run.vid not in self._retried:
            self._retried.add(run.vid)
            self._requeue(run.vid, run.tier, alone=True)
            return
        self.stats.done += 1
        self.stats.failed += 1
        self.on_failure(run.vid, kind, error)

    def _requeue(self, vid: str, tier: Priority, *, alone: bool = False) -> None:
        with self._lock:
            if alone:
                self._alone.add(vid)
            if vid not in self._pending:
                self._push(vid, tier)
            self._lock.notify_all()

    def _watchdog(self) -> bool:
        if self.executor.inline:
            return False
        now = self.clock()
        with self._lock:
            late = [r for r in self._running.values()
                    if now - r.started > max(TIMEOUT_FLOOR_S, r.json_bytes / 1e6 * TIMEOUT_PER_JSON_MB_S)]
            if not late:
                return False
            others = [r for r in self._running.values() if r not in late]
            self._running.clear()
            self._completions.clear()
        self.executor.terminate()                           # the only way to stop a hung worker
        for r in others:
            self._requeue(r.vid, r.tier)
        for r in late:
            self._retry_or_fail(r, "timeout", "timed out")
        return True

    def _allowed(self, tier: Priority) -> bool:
        return not (self._playing is not False and tier in PAUSED_WHILE_PLAYING)

    def _start_one(self) -> bool:
        with self._lock:
            if self._paused or self._stopped:
                return False
            held = self._held
            if held is None:
                popped = self._pop(self._allowed)
                if popped is None:
                    return False
                vid, tier = popped
        if held is None:
            held = self._open(vid, tier)
            if held is None:
                return True
        with self._lock:
            if self._stopped:
                return False                                # opened while stopping: drop it
            if self._paused:
                self._held = held                           # opened while pausing: keep it for resume
                return False
            if not self._fits(held):
                better = self._heap and self._heap[0][0] < held.tier and self._pending.get(self._heap[0][3]) is self._heap[0]
                if better:
                    # Something more urgent arrived while this one waited for room: let it go first.
                    self._held = None
                    if held.alone:
                        self._alone.add(held.vid)
                    self._push(held.vid, held.tier)
                    return True
                self._held = held
                return False
            self._held = None
            token = next(self._tokens)
            run = _Running(held.vid, held.tier, held.est, held.json_bytes, self.clock(), held.alone)
            self._running[token] = run
            self._idle_since = None
        try:
            self.executor.submit(held.job, self._done(token), self._failed(token), token)
        except ExecutorClosed:
            with self._lock:
                self._running.pop(token, None)
                if held.vid not in self._pending:
                    self._push(held.vid, held.tier)
            return False
        except Exception as exc:  # noqa: BLE001 - a broken pool: this job fails, the next gets a new pool
            log.warning("index job %s could not be started: %s", held.vid[:12], extract.describe(exc))
            with self._lock:
                self._running.pop(token, None)
            self._retry_or_fail(run, "worker-died", f"could not start: {extract.describe(exc)}")
        return True

    def _open(self, key: str, tier: Priority) -> _Held | None:
        kind, vid = split_key(key)
        try:
            data, hint = self.opener(vid)
        except Exception as exc:  # noqa: BLE001 - VersionUnavailable and friends: a failure row
            self.stats.failed += 1
            self.on_failure(key, "unavailable", extract.describe(exc))
            return None
        json_bytes = self._json_estimate(key, len(data)) or 0
        est = json_bytes * HEAP_PER_JSON_BYTE
        alone = key in self._alone or est > self.policy.memory_budget // 2
        self._alone.discard(key)
        return _Held(key, tier, IndexJob(vid, kind, data, hint), est, json_bytes, alone)

    def _fits(self, held: _Held) -> bool:
        if not self._allowed(held.tier):
            return False                                    # the game started while it waited
        running = list(self._running.values())
        if any(r.alone for r in running):
            return False                                    # an oversized job runs alone
        if held.alone:
            return not running
        workers = self._workers()
        if len(running) >= workers:
            return False
        background = sum(1 for r in running if r.tier != Priority.INTERACTIVE)
        if (self.policy.reserve_interactive and held.tier != Priority.INTERACTIVE
                and background >= workers - 1 and self._playing is False):
            return False                                    # one worker is kept for the owner's jobs
        return sum(r.est for r in running) + held.est <= self.policy.memory_budget

    def _idle_tick(self) -> None:
        with self._lock:
            busy = bool(self._running or self._pending or self._held)
        now = self.clock()
        if busy:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= IDLE_CLOSE_S:
            self.executor.idle()
            self._idle_since = now

    # --- the thread ---------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="index-dispatch", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stopped:
            try:
                changed = self.step()
            except Exception as exc:  # noqa: BLE001 - one bad step must not end indexing
                log.exception("index dispatcher step failed")
                self._fail_held(exc)
                changed = False
            if not changed:
                with self._lock:
                    if not self._stopped and not self._completions:
                        self._lock.wait(timeout=1.0)

    def _fail_held(self, exc: Exception) -> None:
        """After an unexpected error, the job being started (if any) is reported failed."""
        with self._lock:
            held, self._held = self._held, None
        if held is not None:
            self.stats.failed += 1
            self.on_failure(held.vid, "worker-died", f"dispatcher error: {extract.describe(exc)}")

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._lock.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self.executor.close()
