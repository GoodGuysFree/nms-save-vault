"""Execution (``core/index/pool.py`` and ``platform.index_execution_policy``).

Scheduling is tested through ``Dispatcher.step`` with a fake executor and clock; a few tests
run a real spawn pool on synthetic saves.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nms_save_vault.core import platform
from nms_save_vault.core.index import pool
from nms_save_vault.core.index.pool import Dispatcher, ExecutionPolicy, Priority, ProcessExecutor, ThreadExecutor
from nms_save_vault.core.index.worker import IndexJob, IndexResult, run_job

from . import slowjob
from .savebuilder import context_save, encode

MB = 1024 * 1024


class FakeExecutor:
    inline = False

    def __init__(self) -> None:
        self.jobs: list[tuple[IndexJob, object, object]] = []
        self.started: list[str] = []
        self.tokens: dict[str, int] = {}
        self.dead: set[str] = set()
        self.terminated = 0
        self.idled = 0

    def submit(self, job, done, failed, token=0):
        self.jobs.append((job, done, failed))
        self.started.append(job.vid)
        self.tokens[job.vid] = token

    def dead_tokens(self, live):
        gone = {self.tokens[v] for v in self.dead if v in self.tokens}
        self.jobs = [j for j in self.jobs if j[0].vid not in self.dead]
        self.dead.clear()
        return gone & set(live)

    def running(self) -> list[str]:
        return [j.vid for j, _d, _f in self.jobs]

    def finish(self, vid: str, status: str = "ok", error: str | None = None) -> None:
        for i, (job, done, _failed) in enumerate(self.jobs):
            if job.vid == vid:
                del self.jobs[i]
                done(IndexResult(vid, "facts", vid, status, len(job.data), error=error))
                return
        raise AssertionError(f"{vid} is not running")

    def crash(self, vid: str) -> None:
        for i, (job, _done, failed) in enumerate(self.jobs):
            if job.vid == vid:
                del self.jobs[i]
                failed(RuntimeError("worker died"))
                return
        raise AssertionError(f"{vid} is not running")

    def terminate(self) -> None:
        self.terminated += 1
        self.jobs.clear()

    def idle(self) -> None:
        self.idled += 1

    def close(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Rig:
    def __init__(self, workers=1, reserve=False, budget=pool.DEFAULT_BUDGET, sizes=None, when=None, game=False,
                 unavailable=()):
        self.ex = FakeExecutor()
        self.clock = Clock()
        self.results: list[IndexResult] = []
        self.failures: list[tuple[str, str]] = []
        self.sizes = sizes or {}
        self.game = game
        self.d = Dispatcher(
            ExecutionPolicy("processes", workers, reserve, budget), self.ex,
            opener=self.open, on_result=self.results.append,
            on_failure=lambda vid, kind, _err: self.failures.append((vid, kind)),
            size_hint=lambda vid: self.sizes.get(vid, MB), when=lambda vid: (when or {}).get(vid),
            game_running=lambda: self.game, clock=self.clock)
        self.unavailable = set(unavailable)

    def open(self, vid):
        if vid in self.unavailable:
            raise FileNotFoundError(vid)
        return b"x" * 10, None

    def settle(self) -> None:
        for _ in range(100):
            if not self.d.step():
                return
        raise AssertionError("the dispatcher never settled")


def test_tiers_run_best_first():
    r = Rig()
    r.d.submit(["a"], Priority.BACKFILL)
    r.d.submit(["b"], Priority.LIVE)
    r.d.submit(["c"], Priority.INTERACTIVE)
    r.d.submit(["d"], Priority.TIP)
    order = []
    for _ in range(4):
        r.settle()
        (vid,) = r.ex.running()
        order.append(vid)
        r.ex.finish(vid)
    r.settle()
    assert order == ["c", "b", "d", "a"]
    assert [x.vid for x in r.results] == order


def test_newest_first_in_live_tiers_and_largest_first_in_backfill():
    r = Rig(workers=1, sizes={"s": 1 * MB, "m": 5 * MB, "l": 9 * MB}, when={"old": 100, "new": 300, "mid": 200})
    r.d.submit(["old", "new", "mid"], Priority.LIVE)
    r.d.submit(["s", "l", "m"], Priority.BACKFILL)
    for _ in range(6):
        r.settle()
        r.ex.finish(r.ex.running()[0])
    assert r.ex.started == ["new", "mid", "old", "l", "m", "s"]


def test_a_rerequest_moves_up_and_nothing_is_queued_twice():
    r = Rig()
    assert r.d.submit(["a", "b"], Priority.BACKFILL) == 2
    assert r.d.submit(["a", "b"], Priority.BACKFILL) == 0
    assert r.d.submit(["b"], Priority.LIVE) == 1                  # moved up a tier: counted
    assert r.d.queued() == [("b", Priority.LIVE), ("a", Priority.BACKFILL)]
    r.settle()
    assert r.d.submit(["b"], Priority.INTERACTIVE) == 0           # already running
    assert r.d.counts().queued == 1 and r.d.counts().running == 1


def test_the_worker_count_and_the_reserved_interactive_worker():
    r = Rig(workers=3, reserve=True)
    r.d.submit(["a", "b", "c"], Priority.BACKFILL)
    r.settle()
    assert r.ex.running() == ["a", "b"]                           # the third worker is kept free
    r.d.submit(["i"], Priority.INTERACTIVE)
    r.settle()
    assert r.ex.running() == ["a", "b", "i"]
    r.ex.finish("a")
    r.settle()
    assert r.ex.running() == ["b", "i", "c"]


def test_the_heap_budget_and_oversized_saves_alone():
    budget = 1000 * MB
    # estimated heap = 9 x JSON: 40 MB of JSON = 360 MB; 60 MB = 540 MB > budget / 2
    r = Rig(workers=4, budget=budget, sizes={"a": 40 * MB, "b": 40 * MB, "c": 40 * MB, "big": 60 * MB})
    r.d.submit(["a", "b", "c"], Priority.LIVE)
    r.settle()
    assert r.ex.running() == ["a", "b"]                           # a third would pass the budget
    r.d.submit(["big"], Priority.INTERACTIVE)
    r.settle()
    assert r.ex.running() == ["a", "b"]                           # big waits until nothing runs
    r.ex.finish("a")
    r.ex.finish("b")
    r.settle()
    assert r.ex.running() == ["big"]                              # and runs alone
    r.ex.finish("big")
    r.settle()
    assert r.ex.running() == ["c"]


def test_the_watchdog_retries_a_hung_job_alone_once_then_gives_up():
    r = Rig(workers=2, sizes={"hung": 10 * MB, "ok": 1 * MB})
    r.d.submit(["hung"], Priority.LIVE)
    r.settle()
    r.clock.t += 30
    r.d.submit(["ok"], Priority.LIVE)
    r.settle()
    assert sorted(r.ex.running()) == ["hung", "ok"]
    r.clock.t += 31                                               # hung: 61 s; ok: 31 s
    r.settle()
    assert r.ex.terminated == 1
    assert r.ex.running() == ["ok"]                               # the other job came back first; hung waits to run alone
    r.ex.finish("ok")
    r.settle()
    assert r.ex.running() == ["hung"]
    r.clock.t += 61
    r.settle()
    assert r.failures == [("hung", "timeout")]
    assert [x.vid for x in r.results] == ["ok"]


def test_a_crashed_worker_is_retried_once_then_recorded():
    r = Rig()
    r.d.submit(["a"], Priority.LIVE)
    r.settle()
    r.ex.crash("a")
    r.settle()
    assert r.ex.running() == ["a"]
    r.ex.crash("a")
    r.settle()
    assert r.failures == [("a", "worker-died")] and r.results == []


def test_memory_errors_are_retried_alone_once():
    r = Rig(workers=2)
    r.d.submit(["a", "b"], Priority.LIVE)
    r.settle()
    r.ex.finish("a", "error", "MemoryError while decoding")
    r.settle()
    assert r.ex.running() == ["b"]                                # a waits to run alone
    r.ex.finish("b")
    r.settle()
    assert r.ex.running() == ["a"]
    r.ex.finish("a", "error", "MemoryError while decoding")
    r.settle()
    assert [(x.vid, x.status) for x in r.results] == [("b", "ok"), ("a", "error")]


def test_pause_resume_and_cancel():
    r = Rig(workers=2)
    r.d.pause()
    r.d.submit(["a", "b", "c"], Priority.LIVE)
    r.settle()
    assert r.ex.running() == []
    r.d.resume()
    r.settle()
    assert r.ex.running() == ["a", "b"]
    r.d.cancel()
    assert r.ex.terminated == 1 and r.d.paused
    assert sorted(v for v, _t in r.d.queued()) == ["a", "b", "c"]
    r.d.resume()
    r.settle()
    assert len(r.ex.running()) == 2


@pytest.mark.parametrize("state", [True, None])
def test_while_the_game_runs_one_worker_and_no_backfill(state):
    r = Rig(workers=4, game=state)
    r.d.submit(["back1", "back2"], Priority.BACKFILL)
    r.d.submit(["live1", "live2"], Priority.LIVE)
    r.settle()
    assert r.ex.running() == ["live1"]
    r.ex.finish("live1")
    r.settle()
    r.ex.finish("live2")
    r.settle()
    assert r.ex.running() == []                                  # backfill waits for the game to close
    r.game = False
    r.clock.t += pool.GAME_CHECK_S
    r.settle()
    assert r.ex.running() == ["back1", "back2"]


def test_the_pool_is_not_seen_idle_before_the_last_result_is_handed_on():
    r = Rig()
    seen = []

    def handed_on(res):
        reader = threading.Thread(target=lambda: seen.append(r.d.counts().running), daemon=True)
        reader.start()
        reader.join(0.3)
        seen.append(reader.is_alive())            # a reader on another thread waits for us
        r.results.append(res)

    r.d.on_result = handed_on
    r.d.submit(["a"], Priority.LIVE)
    r.settle()
    r.ex.finish("a")
    r.settle()
    assert seen[0] is True and r.d.counts().running == 0 and [x.vid for x in r.results] == ["a"]


def test_a_job_being_opened_still_counts_as_queued():
    r = Rig()
    seen = []

    def opening(vid):
        seen.append(r.d.counts())
        return b"x" * 10, None

    r.d.opener = opening
    r.d.submit(["a"], Priority.LIVE)
    r.settle()
    assert (seen[0].queued, seen[0].running) == (1, 0)
    assert (r.d.counts().queued, r.d.counts().running) == (0, 1)


def test_an_unreadable_version_is_a_failure_not_a_crash():
    r = Rig(unavailable={"gone"})
    r.d.submit(["gone", "fine"], Priority.LIVE)
    r.settle()
    assert r.failures == [("gone", "unavailable")] and r.ex.running() == ["fine"]


def test_an_urgent_job_overtakes_one_waiting_for_room():
    r = Rig(workers=1)
    r.d.submit(["a", "b"], Priority.BACKFILL)
    r.settle()
    r.d.submit(["now"], Priority.INTERACTIVE)
    r.settle()
    r.ex.finish(r.ex.running()[0])
    r.settle()
    assert r.ex.started[1] == "now"


def test_an_idle_pool_is_closed_after_a_minute():
    r = Rig()
    r.d.submit(["a"], Priority.LIVE)
    r.settle()
    r.ex.finish("a")
    r.settle()
    r.clock.t += pool.IDLE_CLOSE_S + 1
    r.d.step()
    assert r.ex.idled == 1


# --- policy ---------------------------------------------------------------------------------


def host(system: str) -> platform.Host:
    return platform.Host(system, Path("/home/u"), {})


@pytest.mark.parametrize(("system", "kit", "cpus", "setting", "expected"), [
    ("windows", True, 24, "auto", ExecutionPolicy("processes", 4, True)),
    ("windows", False, 4, "auto", ExecutionPolicy("processes", 3, True)),
    ("windows", True, 3, "auto", ExecutionPolicy("processes", 2, False)),
    ("windows", True, 2, "auto", ExecutionPolicy("processes", 1, False)),
    ("windows", True, None, "auto", ExecutionPolicy("processes", 1, False)),
    ("linux", True, 16, "auto", ExecutionPolicy("thread", 1, False)),
    ("macos", True, 16, "auto", ExecutionPolicy("thread", 1, False)),
    ("linux", False, 16, "auto", ExecutionPolicy("processes", 4, True)),
    ("windows", True, 16, 0, ExecutionPolicy("off", 0, False)),
    ("linux", True, 16, "off", ExecutionPolicy("off", 0, False)),
    ("windows", False, 16, 12, ExecutionPolicy("processes", 8, True)),
    ("windows", False, 16, 2, ExecutionPolicy("processes", 2, False)),
])
def test_the_execution_policy(system, kit, cpus, setting, expected):
    assert platform.index_execution_policy(host(system), portable_kit=kit, cpu_count=cpus, setting=setting) == expected


def test_executors_follow_the_policy_mode():
    assert isinstance(pool.make_executor(ExecutionPolicy("processes", 2, False)), ProcessExecutor)
    assert isinstance(pool.make_executor(ExecutionPolicy("thread", 1, False)), ThreadExecutor)
    assert pool.make_executor(ExecutionPolicy("off", 0, False)) is None


# --- real workers -----------------------------------------------------------------------------


def jobs(n: int) -> list[IndexJob]:
    out = []
    for i in range(n):
        data = encode(context_save(base={"Units": i, "SaveSummary": f"Synth {i}"}))[0]
        out.append(IndexJob(hashlib.sha256(data).hexdigest(), "facts", data))
    return out


def run_all(executor, todo: list[IndexJob], timeout: float = 60) -> dict[str, IndexResult]:
    got: dict[str, IndexResult] = {}
    finished = threading.Event()
    lock = threading.Lock()

    def done(result):
        with lock:
            got[result.vid] = result
            if len(got) == len(todo):
                finished.set()

    for job in todo:
        executor.submit(job, done, lambda exc: None)
    assert finished.wait(timeout), "the pool did not answer"
    return got


def test_a_spawn_pool_and_a_thread_give_the_same_facts():
    todo = jobs(3)
    processes = ProcessExecutor(2)
    try:
        got = run_all(processes, todo)
    finally:
        processes.close()
    inline = run_all(ThreadExecutor(), todo)
    assert {v: r.facts for v, r in got.items()} == {v: r.facts for v, r in inline.items()}
    assert all(r.status == "ok" for r in got.values())


def test_terminate_mid_run_reports_nothing_for_the_killed_jobs():
    ex = ProcessExecutor(1, target=slowjob.slow)
    reported = threading.Event()
    try:
        ex.submit(jobs(1)[0], lambda r: reported.set(), lambda e: reported.set())
        threading.Event().wait(1.0)
        ex.terminate()
        assert not reported.wait(1.5)
        assert not ex.running
    finally:
        ex.close()


def test_a_dispatcher_on_a_real_pool_indexes_and_records_a_crash():
    todo = {j.vid: j for j in jobs(2)}
    results: list[IndexResult] = []
    failures: list[tuple[str, str]] = []
    finished = threading.Event()

    def on_result(r):
        results.append(r)
        if len(results) == 2:
            finished.set()

    d = Dispatcher(ExecutionPolicy("processes", 2, False), ProcessExecutor(2), opener=lambda v: (todo[v].data, None),
                   on_result=on_result, on_failure=lambda v, k, e: failures.append((v, k)))
    d.start()
    try:
        d.submit(list(todo), Priority.LIVE)
        assert finished.wait(60)
    finally:
        d.stop()
    assert sorted(r.vid for r in results) == sorted(todo) and failures == []

    crashed = threading.Event()
    d = Dispatcher(ExecutionPolicy("processes", 1, False), ProcessExecutor(1, target=slowjob.crash),
                   opener=lambda v: (todo[v].data, None), on_result=lambda r: None,
                   on_failure=lambda v, k, e: (failures.append((v, k)), crashed.set()))
    d.start()
    try:
        d.submit([next(iter(todo))], Priority.LIVE)
        assert crashed.wait(60)
    finally:
        d.stop()
    assert failures == [(next(iter(todo)), "worker-died")]


def test_entry_modules_are_import_safe_for_spawn():
    """Spawn re-imports the main module: importing the front-ends must start nothing."""
    code = ("import threading, nms_save_vault.gui, nms_save_vault.cli, tkinter; "
            "print(threading.active_count(), tkinter._default_root is None)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", check=True)
    assert out.stdout.split() == ["1", "True"]


def _blocked_reader(r, seen):
    """Reads counts() on another thread while the dispatcher thread holds its lock mid-step."""
    reader = threading.Thread(target=lambda: seen.append(r.d.counts()), daemon=True)
    reader.start()
    reader.join(0.3)
    blocked = reader.is_alive()
    return blocked


def test_a_job_being_opened_is_queued_and_not_submitted_twice():
    r = Rig(workers=2)
    seen = {}

    def opener(vid):
        if not seen:
            seen.update(queued=r.d.is_queued("a"), again=r.d.submit(["a"], Priority.LIVE))
        return b"x" * 10, None

    r.d.opener = opener
    r.d.submit(["a"], Priority.LIVE)
    r.settle()
    assert seen == {"queued": True, "again": 0} and r.ex.started == ["a"]


def test_a_dead_worker_is_never_seen_between_running_and_requeued():
    r = Rig()
    r.d.submit(["b"], Priority.LIVE)
    r.settle()
    r.ex.dead.add("b")
    states = []
    orig = r.d._retry_or_fail

    def spy(run, kind, error):
        states.append(_blocked_reader(r, []))
        orig(run, kind, error)

    r.d._retry_or_fail = spy
    r.d.step()
    assert states == [True] and r.ex.started == ["b", "b"]


def test_a_timed_out_job_is_never_seen_between_running_and_requeued():
    r = Rig()
    r.d.submit(["a"], Priority.LIVE)
    r.settle()
    r.clock.t += pool.TIMEOUT_FLOOR_S + 1
    states = []
    orig = r.d._retry_or_fail

    def spy(run, kind, error):
        states.append(_blocked_reader(r, []))
        orig(run, kind, error)

    r.d._retry_or_fail = spy
    r.d.step()
    assert states == [True] and r.ex.terminated


def test_a_better_request_reaches_a_job_that_is_being_opened_or_held():
    r = Rig(workers=1)
    seen = {}

    def opener(vid):
        if vid == "a" and not seen:
            seen["opening"] = r.d.submit(["a"], Priority.INTERACTIVE)
        return b"x" * 10, None

    r.d.opener = opener
    r.d.submit(["a"], Priority.BACKFILL)
    r.settle()
    assert seen == {"opening": 1} and [x.tier for x in r.d._running.values()] == [Priority.INTERACTIVE]
    r.d.submit(["b"], Priority.BACKFILL)                    # waits for the one worker
    r.settle()
    assert r.d._held is not None and r.d._held.tier == Priority.BACKFILL
    assert r.d.submit(["b"], Priority.INTERACTIVE) == 1 and r.d._held.tier == Priority.INTERACTIVE
    assert r.d.submit(["b"], Priority.INTERACTIVE) == 0


def test_cancel_never_shows_the_pool_idle_while_it_requeues():
    r = Rig()
    r.d.submit(["a"], Priority.LIVE)
    r.settle()
    states = []
    orig = r.ex.terminate

    def terminate():
        reader = threading.Thread(target=lambda: states.append(r.d.counts()), daemon=True)
        reader.start()
        reader.join(0.3)
        states.append(reader.is_alive())
        orig()

    r.ex.terminate = terminate
    r.d.cancel()
    counts = r.d.counts()
    assert states[1] is False and (states[0].queued, states[0].running) == (1, 0)
    assert r.d.queued() == [("a", Priority.LIVE)] and counts.queued == 1


def test_not_ready_drops_the_job_without_a_failure():
    r = Rig()

    def opener(vid):
        raise pool.NotReady(vid)

    r.d.opener = opener
    r.d.submit(["a"], Priority.LIVE)
    r.settle()
    assert r.failures == [] and r.d.queued() == [] and r.d.counts().failed == 0
