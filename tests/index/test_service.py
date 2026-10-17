"""The service facade (``core/index/service.py``) and D1's FactsProvider contract (D4 2.6)."""
from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import threading
import time
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Protocol, runtime_checkable

import pytest

from nms_save_vault.core.index import IndexService, Priority
from nms_save_vault.core.index.cache import IndexCache
from nms_save_vault.core.index.changes import ChangeSet
from nms_save_vault.core.index.derive import FactsView
from nms_save_vault.core.index.pool import ExecutionPolicy, ProcessExecutor, ThreadExecutor
from nms_save_vault.core.index.schema import EXTRACTOR_VERSION
from nms_save_vault.core.index.worker import IndexJob, run_job

from ..conftest import xbox_meta
from .savebuilder import context_save, encode, pre_context_save


@runtime_checkable
class FactsProvider(Protocol):
    """D1 2.10's protocol, as D1-core-model.md declares it."""

    def view(self, vid: str) -> FactsView | None: ...
    def changes(self, child: str, parent: str, *, edge_kind: str | None = None) -> ChangeSet | None: ...
    def is_milestone(self, child: str, parent: str) -> bool | None: ...
    def request(self, vids: Iterable[str], priority: Priority) -> int: ...


class VersionUnavailable(Exception):
    pass


class SpyLocator:
    def __init__(self) -> None:
        self.versions: dict[str, SimpleNamespace] = {}
        self.threads: list[str] = []

    def add(self, data: bytes, *, platform: str = "steam", meta_plain: bytes = b"", vid: str | None = None) -> str:
        vid = vid or hashlib.sha256(data).hexdigest()
        self.versions[vid] = SimpleNamespace(data=data, meta_plain=meta_plain, platform=platform, meta_format=4,
                                             location=None)
        return vid

    def open(self, vid: str, prefer=None):
        self.threads.append(threading.current_thread().name)
        if vid not in self.versions:
            raise VersionUnavailable(vid)
        return self.versions[vid]


def save_bytes(units: int, play: int = 3600, glyphs: int = 0) -> bytes:
    return encode(context_save(base={"Units": units, "KnownPortalRunes": glyphs},
                               common={"SaveName": "Synth", "TotalPlayTime": play}))[0]


def wait_for(cond, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


@pytest.fixture
def make(tmp_path):
    made = []

    def build(locator, policy=ExecutionPolicy("thread", 1, False), **kw):
        svc = IndexService(cache_path=tmp_path / "cache" / "index-v1.sqlite", vault_id="v1", locator=locator,
                           policy=policy, game_running=lambda: False, **kw)
        made.append(svc)
        return svc

    yield build
    for svc in made:
        svc.close()


def settle(svc: IndexService) -> None:
    wait_for(svc.idle)
    assert svc.flush()


def test_the_service_is_d1s_facts_provider(make):
    loc = SpyLocator()
    parent, child = loc.add(save_bytes(1, 3600, 0b1)), loc.add(save_bytes(2, 9000, 0b111))
    svc = make(loc)
    assert isinstance(svc, FactsProvider)
    assert svc.view(child) is None
    assert svc.is_milestone(child, parent) is None
    assert svc.changes(child, parent) is None                        # and both are now queued at NEW
    settle(svc)
    assert isinstance(svc.view(child), FactsView)
    wait_for(lambda: svc.changes(child, parent, edge_kind="play") is not None)     # worked out off this thread
    cs = svc.changes(child, parent, edge_kind="play")
    assert isinstance(cs, ChangeSet) and cs.header == "+1.5 h played"
    assert [c.text for c in cs.items] == ["glyphs 1 -> 3 of 16", "units +1"]
    wait_for(lambda: svc.is_milestone(child, parent) is not None)       # worked out off this thread
    assert svc.is_milestone(child, parent) is False
    assert isinstance(svc.request([child], Priority.LIVE), int)


def test_requests_index_once_and_events_tell_the_ui(make):
    loc = SpyLocator()
    vids = [loc.add(save_bytes(i)) for i in range(3)]
    svc = make(loc)
    assert svc.status(vids[0]) == "not-indexed"
    assert svc.view(vids[0]) is None and svc.dispatcher.queued() == []   # a view asks for nothing
    assert svc.request(vids, Priority.LIVE) == 3
    settle(svc)
    assert {svc.status(v) for v in vids} == {"ok"}
    assert svc.request(vids, Priority.LIVE) == 0                      # indexed: never queued again
    events = svc.events_since(0)
    assert [e.kind for e in events if e.kind == "indexed"] == ["indexed"] * 3
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    assert events[-1].kind == "finished"
    assert svc.events_since(events[1].seq) == events[2:]
    p = svc.progress()
    assert (p.state, p.done, p.queued, p.running) == ("idle", 3, 0, 0)
    assert p.message.isascii()


def test_bytes_are_read_only_on_the_dispatcher_thread(make):
    loc = SpyLocator()
    vids = [loc.add(save_bytes(i)) for i in range(2)]
    svc = make(loc)
    svc.request(vids, Priority.INTERACTIVE)
    settle(svc)
    svc.view(vids[0]), svc.card(vids[0]), svc.changes(vids[1], vids[0]), svc.status(vids[0])
    assert loc.threads and set(loc.threads) == {"index-dispatch"}


def test_unreadable_and_mismatched_versions_are_failures_with_three_strikes(make):
    loc = SpyLocator()
    wrong = loc.add(save_bytes(1), vid="e" * 64)                      # bytes that do not hash to the vid
    svc = make(loc)
    svc.request(["d" * 64, wrong], Priority.LIVE)
    settle(svc)
    assert svc.status("d" * 64) == "unreadable" and svc.status(wrong) == "unreadable"
    assert svc.facts(wrong) is None
    for _ in range(5):
        svc.request(["d" * 64], Priority.LIVE)
        settle(svc)
    assert loc.threads.count("index-dispatch") == 4                   # 3 strikes for one vid, 1 for the other
    assert [e.status for e in svc.events_since(0) if e.kind == "failed"][:2] == ["unavailable", "mismatch"]


def test_stale_rows_are_served_and_refreshed_at_startup(make, tmp_path):
    loc = SpyLocator()
    vid = loc.add(save_bytes(7))
    old = run_job(IndexJob(vid, "facts", loc.versions[vid].data))
    old = dataclasses.replace(old, facts=dict(old.facts, extractor=EXTRACTOR_VERSION - 1))
    pre = IndexCache(tmp_path / "cache" / "index-v1.sqlite", vault_id="v1")
    pre.write_result(old)
    pre.close()
    svc = make(loc)
    settle(svc)
    assert svc.facts(vid)["extractor"] == EXTRACTOR_VERSION
    assert svc.status(vid) == "ok"


def test_off_means_nothing_is_indexed_or_read(make):
    loc = SpyLocator()
    vid = loc.add(save_bytes(1))
    svc = make(loc, ExecutionPolicy("off", 0, False))
    assert svc.request([vid], Priority.LIVE) == 0
    assert svc.view(vid) is None and svc.status(vid) == "off"
    assert svc.progress().state == "off" and loc.threads == []


def test_rebuild_indexes_everything_again(make):
    loc = SpyLocator()
    vids = [loc.add(save_bytes(i)) for i in range(2)]
    svc = make(loc)
    svc.request(vids, Priority.BACKFILL)
    settle(svc)
    svc.rebuild()
    settle(svc)
    assert "cleared" in [e.kind for e in svc.events_since(0)]
    assert all(svc.status(v) == "ok" for v in vids)
    assert loc.threads.count("index-dispatch") == 4


def test_an_old_unframed_xbox_save_gets_its_size_from_the_meta(make):
    loc = SpyLocator()
    data, hint = encode(pre_context_save({"SaveName": "Synth Old", "TotalPlayTime": 5}), unframed=True)
    vid = loc.add(data, platform="xbox", meta_plain=xbox_meta(size=hint, length=280))
    svc = make(loc)
    svc.request([vid], Priority.LIVE)
    settle(svc)
    assert svc.status(vid) == "ok" and svc.facts(vid)["save"]["save_name"] == "Synth Old"


def test_pause_and_a_cache_that_cannot_be_written(make, monkeypatch):
    loc = SpyLocator()
    vids = [loc.add(save_bytes(i)) for i in range(2)]
    svc = make(loc)
    svc.request(vids[:1], Priority.LIVE)
    settle(svc)
    svc.pause()
    svc.request(vids[1:], Priority.LIVE)
    assert svc.progress().state == "paused"

    def full(_result):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(svc.cache, "write_result", full)
    svc.resume()
    wait_for(lambda: svc.progress().state == "error")
    assert "OperationalError" in svc.progress().message
    assert svc.view(vids[0]) is not None                              # what is cached stays readable


def test_spawned_workers_end_to_end(make):
    loc = SpyLocator()
    vids = [loc.add(save_bytes(i, glyphs=i)) for i in range(3)]
    svc = make(loc, ExecutionPolicy("processes", 2, False), executor=ProcessExecutor(2))
    svc.request(vids, Priority.LIVE)
    wait_for(lambda: all(svc.status(v) == "ok" for v in vids), timeout=90)
    assert [svc.view(v).glyphs for v in vids] == [0, 1, 1]


def test_thread_and_process_modes_store_the_same_facts(make, tmp_path):
    loc = SpyLocator()
    vid = loc.add(save_bytes(3, glyphs=7))
    svc = make(loc, executor=ThreadExecutor())
    svc.request([vid], Priority.LIVE)
    settle(svc)
    assert svc.facts(vid) == run_job(IndexJob(vid, "facts", loc.versions[vid].data)).facts
