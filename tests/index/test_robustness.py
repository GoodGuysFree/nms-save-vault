"""Failure handling found in review: stuck builds, pauses during opens, broken and dead workers,
writer errors, locked cache files, the in-memory fallback, the facts LRU, a read-only vault."""
from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from nms_save_vault import index_cli
from nms_save_vault.core import safety
from nms_save_vault.core.index import cache as cache_mod
from nms_save_vault.core.index import pool, service as service_mod
from nms_save_vault.core.index.cache import IndexCache
from nms_save_vault.core.index.pool import (Dispatcher, ExecutionPolicy, ExecutorClosed, Priority, ProcessExecutor,
                                            ThreadExecutor)
from nms_save_vault.core.index.worker import IndexJob, IndexResult

from . import slowjob
from .test_cache import result_for, save
from .test_cli import run, world  # noqa: F401 - the CLI fixture
from .test_pool import Rig, jobs
from .test_service import SpyLocator, make, save_bytes, settle, wait_for  # noqa: F401 - fixtures


def in_thread(fn, timeout: float):
    """Run ``fn`` on a daemon thread; (finished, result)."""
    box: dict = {}

    def body():
        box["result"] = fn()

    t = threading.Thread(target=body, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive(), box.get("result")


# --- 1. index build stops with a reason instead of waiting forever ---------------------------


def test_build_stops_when_the_game_holds_older_versions_back(world, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(safety, "is_game_running", lambda: True)
    monkeypatch.setattr(index_cli, "STUCK_S", 0.5)
    finished, code = in_thread(lambda: run(world, "build", "--workers", "1"), 60)
    assert finished, "index build never returned"
    assert code == 2
    err = capsys.readouterr().err
    assert "the game is running" in err and "1 older versions wait" in err


def test_build_runs_when_the_game_state_is_unknown_with_a_warning(world, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(safety, "is_game_running", lambda: None)
    monkeypatch.setattr(index_cli, "_warned_unknown", False)
    finished, code = in_thread(lambda: run(world, "build", "--workers", "1"), 60)
    assert finished and code == 0
    captured = capsys.readouterr()
    assert "could not tell whether the game is running" in captured.err
    assert "2 ok" in captured.out


def test_build_stops_when_the_cache_fails(world, monkeypatch, capsys):  # noqa: F811
    def broken(self, result):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(IndexCache, "write_result", broken)
    finished, code = in_thread(lambda: run(world, "build", "--workers", "1"), 60)
    assert finished and code == 2
    assert "Content index stopped" in capsys.readouterr().err


# --- 2. pausing while bytes are read; a closed executor starts nothing ----------------------


def test_a_pause_during_an_open_keeps_the_job_unstarted():
    rig = Rig()
    original = rig.open

    def open_and_pause(vid):
        rig.d.pause()                                     # the owner pressed pause meanwhile
        return original(vid)

    rig.d.opener = open_and_pause
    rig.d.submit(["a"], Priority.LIVE)
    rig.settle()
    assert rig.ex.started == []
    rig.d.opener = original
    rig.d.resume()
    rig.settle()
    assert rig.ex.started == ["a"]


def test_a_closed_process_executor_builds_no_new_pool():
    ex = ProcessExecutor(1)
    ex.close()
    with pytest.raises(ExecutorClosed):
        ex.submit(jobs(1)[0], lambda r: None, lambda e: None)
    assert not ex.running


def test_a_dispatcher_job_after_close_goes_back_to_the_queue():
    rig = Rig()
    rig.d.submit(["a"], Priority.LIVE)

    def closed(*_a, **_k):
        raise ExecutorClosed("closed")

    rig.ex.submit = closed
    rig.d.step()
    assert rig.d.queued() == [("a", Priority.LIVE)] and rig.failures == []


# --- 3. the dispatcher survives errors; a broken pool is replaced ---------------------------


def test_the_dispatcher_thread_survives_an_error_in_a_step():
    todo = {j.vid: j for j in jobs(2)}
    results: list[IndexResult] = []
    calls = {"n": 0}

    def on_result(r):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("a bug in a callback")
        results.append(r)

    d = Dispatcher(ExecutionPolicy("thread", 1, False), ThreadExecutor(), opener=lambda v: (todo[v].data, None),
                   on_result=on_result, on_failure=lambda *a: None)
    d.start()
    try:
        first, second = list(todo)
        d.submit([first], Priority.LIVE)
        wait_for(lambda: calls["n"] >= 1, 30)
        d.submit([second], Priority.LIVE)
        wait_for(lambda: len(results) == 1, 30)
    finally:
        d.stop()
    assert results[0].vid == second


def test_a_job_that_cannot_be_started_is_retried_then_failed():
    rig = Rig()

    def broken(*_a, **_k):
        raise OSError("pipe broken")

    rig.ex.submit = broken
    rig.d.submit(["a"], Priority.LIVE)
    rig.settle()
    assert rig.failures == [("a", "worker-died")]


def test_a_pool_shut_down_under_the_executor_is_replaced():
    ex = ProcessExecutor(1)
    try:
        todo = jobs(2)
        got = []
        done = threading.Event()
        ex.submit(todo[0], lambda r: (got.append(r), done.set()), lambda e: done.set())
        assert done.wait(60)
        ex._pool.terminate()                               # broken from outside
        done.clear()
        ex.submit(todo[1], lambda r: (got.append(r), done.set()), lambda e: done.set())
        assert done.wait(60)
        assert [r.status for r in got] == ["ok", "ok"]
    finally:
        ex.close()


# --- 10. a worker process that dies is reported for its job only; the game gate -----------


def test_a_dead_worker_is_reported_for_its_job_only():
    good = jobs(1)[0]
    dead = IndexJob("d" * 64, "facts", b"DIE now")
    data = {good.vid: good.data, dead.vid: dead.data}
    results: list[IndexResult] = []
    failures: list[tuple[str, str]] = []
    d = Dispatcher(ExecutionPolicy("processes", 2, False), ProcessExecutor(2, target=slowjob.die_on_marker),
                   opener=lambda v: (data[v], None), on_result=results.append,
                   on_failure=lambda v, k, e: failures.append((v, k)))
    d.start()
    try:
        d.submit([dead.vid, good.vid], Priority.LIVE)
        wait_for(lambda: failures and results, 45)
    finally:
        d.stop()
    assert failures == [(dead.vid, "worker-died")]
    assert [r.vid for r in results] == [good.vid]


def test_a_held_backfill_job_waits_when_the_game_starts():
    rig = Rig(workers=1)
    rig.d.submit(["a", "b"], Priority.BACKFILL)
    rig.settle()
    assert rig.ex.running() == ["a"]                       # b is read and held, waiting for room
    rig.game = True
    rig.clock.t += pool.GAME_CHECK_S
    rig.ex.finish("a")
    rig.settle()
    assert rig.ex.running() == []


# --- 4 and 6. the writer keeps going; a locked cache is waited out --------------------------


def test_one_unstorable_result_does_not_stop_the_writer(make, monkeypatch):  # noqa: F811
    loc = SpyLocator()
    bad, good = loc.add(save_bytes(1)), loc.add(save_bytes(2))
    svc = make(loc)
    original = IndexCache.write_result

    def picky(self, result):
        if result.vid == bad:
            raise ValueError("cannot store this one")
        return original(self, result)

    monkeypatch.setattr(IndexCache, "write_result", picky)
    svc.request([bad], Priority.LIVE)
    settle(svc)
    svc.request([good], Priority.LIVE)
    settle(svc)
    assert svc.status(good) == "ok" and svc.status(bad) == "unreadable"
    assert svc.cache.failure(bad)[1] == "store-error"
    assert svc.progress().state == "idle"
    assert ("failed", bad) in [(e.kind, e.vid) for e in svc.events_since(0)]


def test_a_lock_held_by_another_instance_is_retried(make, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service_mod, "BUSY_BACKOFF_S", (0.01, 0.01, 0.01, 0.01))
    loc = SpyLocator()
    vid = loc.add(save_bytes(3))
    svc = make(loc)
    original = IndexCache.write_result
    calls = {"n": 0}

    def locked_twice(self, result):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return original(self, result)

    monkeypatch.setattr(IndexCache, "write_result", locked_twice)
    svc.request([vid], Priority.LIVE)
    settle(svc)
    assert svc.status(vid) == "ok" and svc.progress().state == "idle" and calls["n"] == 3


# --- 5, 7, 8. the cache file, the in-memory fallback, the LRU -------------------------------


def test_a_locked_cache_file_is_busy_not_damaged(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "OPEN_BUSY_TIMEOUT_S", 0.05)
    path = tmp_path / "index-v1.sqlite"
    holder = sqlite3.connect(path, timeout=0.05)
    holder.execute("CREATE TABLE other (x)")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")                       # another instance mid-write
    try:
        before = path.read_bytes()
        c = IndexCache(path, vault_id="v1")
        try:
            assert not c.rebuilt and not list(tmp_path.glob("*.corrupt-*"))
            assert c.in_memory and "busy" in c.warning
        finally:
            c.close()
    finally:
        holder.rollback()
        holder.close()
    assert path.read_bytes() == before


def test_in_memory_readers_and_the_writer_never_collide(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    c = IndexCache(blocker / "index-v1.sqlite", vault_id="v1")
    assert c.in_memory
    results = [result_for(save(i)) for i in range(40)]
    errors: list[BaseException] = []
    done = threading.Event()

    def writer():
        c.adopt_writer()
        try:
            for r in results:
                c.write_result(r)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        done.set()

    def reader():
        try:
            while not done.is_set():
                for r in results:
                    c.facts(r.vid)
                    c.counts()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    c.close()
    assert errors == []


def test_a_read_that_raced_a_write_is_not_kept(tmp_path):
    c = IndexCache(tmp_path / "index-v1.sqlite", vault_id="v1")
    r = result_for(save(4))
    original = c._load

    def load_then_write(vid):
        found = original(vid)                              # not there yet ...
        c.write_result(r)                                  # ... and stored meanwhile
        return found

    c._load = load_then_write
    assert c.facts(r.vid) is None
    c._load = original
    assert c.facts(r.vid) == r.facts                       # the stale "not indexed" was not cached
    c.close()


# --- 9. the index CLI never writes the vault ---------------------------------------------------


def test_the_index_cli_leaves_the_vault_untouched(world, capsys):  # noqa: F811
    from nms_save_vault.core import catalog

    stale = world["vault"] / f"{catalog.CATALOG_TEMP_PREFIX}old.part"
    stale.write_bytes(b"left by an interrupted save")
    old = time.time() - 10 * 86400
    os.utime(stale, (old, old))
    before = {p: p.stat().st_mtime_ns for p in world["vault"].rglob("*") if p.is_file()}
    assert run(world, "status") == 0
    after = {p: p.stat().st_mtime_ns for p in world["vault"].rglob("*") if p.is_file()}
    assert after == before


def test_the_organizer_the_index_opens_is_read_only(tmp_path):
    from nms_save_vault.core.index.vaultlink import read_only_organizer

    org = read_only_organizer(tmp_path / "vault", [])
    assert org.read_only and org.vault.read_only and org.store.read_only
    with pytest.raises(PermissionError):
        org.vault.save()
    org.refresh(record=True)                                   # asking to record records nothing
    assert not (tmp_path / "vault").exists()
