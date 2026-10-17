"""The per-machine facts cache (``core/index/cache.py``)."""
from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import threading

import pytest

from nms_save_vault.core.index import cache as cache_mod
from nms_save_vault.core.index.cache import CacheLocationError, IndexCache
from nms_save_vault.core.index.schema import EXTRACTOR_VERSION
from nms_save_vault.core.index.worker import IndexJob, run_job

from ..conftest import chunked
from .savebuilder import context_save, encode
from .test_privacy import SENTINELS, sentinel_save


def result_for(readable_or_data, **kw):
    data = readable_or_data if isinstance(readable_or_data, bytes) else encode(readable_or_data)[0]
    return run_job(IndexJob(hashlib.sha256(data).hexdigest(), "facts", data))


def save(units: int = 5, name: str = "Synth"):
    return context_save(base={"Units": units}, common={"SaveName": name, "TotalPlayTime": units})


class Clock:
    def __init__(self, t: float = 1_800_000_000):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def path(tmp_path):
    return tmp_path / "cache" / "vaults" / "v1" / cache_mod.FILENAME


def test_results_survive_a_reopen(path):
    r = result_for(save())
    c = IndexCache(path, vault_id="v1")
    c.write_result(r)
    assert c.facts(r.vid) == r.facts
    c.close()
    again = IndexCache(path, vault_id="v1")
    assert again.facts(r.vid) == r.facts
    assert again.indexed() == {r.vid: ("ok", EXTRACTOR_VERSION)}
    assert again.counts() == {"ok": 1}
    row = again.row(r.vid)
    assert row["data_bytes"] == r.data_bytes and row["hash_ms"] is not None
    assert again.facts("f" * 64) is None
    again.close()


def test_failures_are_cached_as_statuses_and_mismatches_never(path):
    c = IndexCache(path, vault_id="v1")
    bad = result_for(chunked(b'{"a":'))
    c.write_result(bad)
    stored = c.facts(bad.vid)
    assert stored["status"] == "corrupt" and stored["errors"][0]["extractor"] == "decode"
    mismatch = dataclasses.replace(result_for(save()), status="mismatch", facts=None)
    c.write_result(mismatch)
    assert c.facts(mismatch.vid) is None
    assert c.failure(mismatch.vid)[:2] == (1, "mismatch")
    c.record_failure(mismatch.vid, "unavailable", "VersionUnavailable")
    assert c.failure(mismatch.vid)[:2] == (2, "unavailable")
    c.write_result(result_for(save()))                       # indexed at last: the failure row goes
    assert c.failure(mismatch.vid) is None
    c.close()


def test_one_transaction_per_result(path, monkeypatch):
    c = IndexCache(path, vault_id="v1")
    first = result_for(save(1))
    c.write_result(first)
    second = result_for(save(2))
    broken = dataclasses.replace(second, facts=dict(second.facts, contexts={"base": second.facts["contexts"]["base"],
                                                                            "weird": {}}))
    with pytest.raises(sqlite3.IntegrityError):
        c.write_result(broken)                               # the second context row violates a CHECK
    assert c.facts(second.vid) is None                       # nothing of it was kept
    assert c.facts(first.vid) == first.facts
    c.close()


def test_stale_rows_are_served_and_listed_for_refresh(path):
    c = IndexCache(path, vault_id="v1")
    r = result_for(save())
    old = dataclasses.replace(r, facts=dict(r.facts, extractor=EXTRACTOR_VERSION - 1))
    c.write_result(old)
    fresh = result_for(save(9))
    c.write_result(fresh)
    assert c.facts(old.vid)["extractor"] == EXTRACTOR_VERSION - 1
    assert c.stale_vids() == [old.vid]
    c.close()


def test_a_decoder_bump_retries_only_failed_rows(path, monkeypatch):
    c = IndexCache(path, vault_id="v1")
    good, bad = result_for(save()), result_for(chunked(b"[1]"))
    c.write_result(good)
    c.write_result(bad)
    assert c.retry_vids() == []
    monkeypatch.setattr(cache_mod, "DECODER_VERSION", cache_mod.DECODER_VERSION + 1)
    assert c.retry_vids() == [bad.vid]
    c.close()


def test_a_damaged_file_is_set_aside_and_rebuilt(path):
    path.parent.mkdir(parents=True)
    (path.parent / "index-v1.corrupt-20200101T000000Z.sqlite").write_bytes(b"older wreck")
    path.write_bytes(b"this is not a database at all" * 100)
    c = IndexCache(path, vault_id="v1", clock=Clock())
    assert c.rebuilt and "rebuilt" in c.warning
    wrecks = sorted(p.name for p in path.parent.glob("index-v1.corrupt-*.sqlite"))
    assert wrecks == ["index-v1.corrupt-20270115T080000Z.sqlite"]
    r = result_for(save())
    c.write_result(r)
    assert c.facts(r.vid) == r.facts
    c.close()


def test_the_cache_refuses_to_live_in_the_vault_or_a_save_folder(tmp_path):
    vault = tmp_path / "vault"
    with pytest.raises(CacheLocationError):
        IndexCache(vault / "organizer" / "index-v1.sqlite", vault_id="v1", forbidden=[vault])
    with pytest.raises(CacheLocationError):
        IndexCache(tmp_path / "st_1" / "index.sqlite", vault_id="v1", forbidden=[tmp_path / "st_1"])
    ok = IndexCache(tmp_path / "cache" / "index-v1.sqlite", vault_id="v1", forbidden=[vault])
    ok.close()


def test_an_unwritable_folder_gives_an_in_memory_cache(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    c = IndexCache(blocker / "index-v1.sqlite", vault_id="v1")
    assert c.in_memory and "memory" in c.warning
    r = result_for(save())
    c.write_result(r)
    assert c.facts(r.vid) == r.facts
    c.close()


def test_readers_read_while_the_writer_writes(path):
    c = IndexCache(path, vault_id="v1")
    results = [result_for(save(i)) for i in range(25)]
    errors: list[BaseException] = []
    done = threading.Event()

    def writer():
        c.adopt_writer()
        for r in results:
            c.write_result(r)
        done.set()

    def reader():
        try:
            while not done.is_set():
                for r in results:
                    f = c.facts(r.vid)
                    assert f is None or f["contexts"]["base"]["units"] == r.facts["contexts"]["base"]["units"]
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert all(c.facts(r.vid) == r.facts for r in results)
    c.close()


def test_only_one_thread_writes(path):
    c = IndexCache(path, vault_id="v1")
    c.write_result(result_for(save(1)))
    caught: list[BaseException] = []

    def other():
        try:
            c.write_result(result_for(save(2)))
        except RuntimeError as exc:
            caught.append(exc)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert len(caught) == 1
    c.close()


def test_rows_nobody_names_for_180_days_are_collected(path):
    clock = Clock()
    c = IndexCache(path, vault_id="v1", clock=clock)
    kept, dropped = result_for(save(1)), result_for(save(2))
    c.write_result(kept)
    c.write_result(dropped)
    clock.t += 170 * 86400
    c.touch([kept.vid])
    clock.t += 20 * 86400
    assert c.gc() == 1
    assert c.facts(dropped.vid) is None and c.facts(kept.vid) is not None
    c.clear()
    assert c.indexed() == {}
    c.close()


def test_no_ownership_value_reaches_the_cache_file(path):
    c = IndexCache(path, vault_id="v1")
    r = result_for(sentinel_save())
    assert r.status == "ok"
    c.write_result(r)
    c.close()
    blob = b"".join(p.read_bytes() for p in path.parent.iterdir() if p.is_file())
    assert [s for s in SENTINELS if s.encode() in blob] == []
