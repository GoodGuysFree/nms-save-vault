"""The worker job (``core/index/worker.py``): bytes in, a status and facts out, never raising."""
from __future__ import annotations

import hashlib
import pickle
import subprocess
import sys

import pytest

from nms_save_vault.core import lz4_block
from nms_save_vault.core.index import extract, worker
from nms_save_vault.core.index.worker import IndexJob, run_job

from ..conftest import chunked
from .savebuilder import context_save, encode


def job_for(data: bytes, vid: str | None = None, hint: int | None = None) -> IndexJob:
    return IndexJob(vid or hashlib.sha256(data).hexdigest(), "facts", data, hint)


def synthetic() -> bytes:
    return encode(context_save(base={"Units": 5, "SaveSummary": "Synth"}))[0]


def test_a_good_save_gives_facts_timings_and_its_hash():
    data = synthetic()
    r = run_job(job_for(data))
    assert r.status == "ok" and r.error is None
    assert r.actual_sha256 == r.vid == hashlib.sha256(data).hexdigest()
    assert r.data_bytes == len(data)
    assert r.facts["contexts"]["base"]["units"] == 5
    assert set(r.timings_ms) == {"hash", "decode", "parse", "extract"}


def test_bytes_that_do_not_hash_to_the_vid_are_a_mismatch_without_facts():
    r = run_job(job_for(synthetic(), vid="0" * 64))
    assert r.status == "mismatch" and r.facts is None
    assert r.actual_sha256 != r.vid


@pytest.mark.parametrize(("data", "status"), [
    (chunked(b'{"a":'), "corrupt"),
    (b"\x00\x01garbage", "corrupt"),
    (lz4_block.HGSAVEV2_HEADER + b"\x00" * 32, "unsupported"),
])
def test_undecodable_data_maps_to_a_status(data, status):
    r = run_job(job_for(data))
    assert r.status == status and r.facts is None and r.error


def test_a_too_large_claim_is_unsupported():
    r = run_job(job_for(lz4_block.HGSAVEV2_HEADER[:0] + b"\x10x", hint=lz4_block.MAX_BARE_BLOCK + 1))
    assert r.status == "unsupported"


@pytest.mark.parametrize("exc", [RuntimeError("SENTINEL secret"), MemoryError()])
def test_run_job_never_raises(monkeypatch, exc):
    def boom(_decoded):
        raise exc

    monkeypatch.setattr(extract, "run_extractors", boom)
    r = run_job(job_for(synthetic()))
    assert r.status == "error" and r.facts is None
    assert "SENTINEL" not in (r.error or "")


def test_jobs_and_results_pickle_for_the_process_pool():
    job = job_for(synthetic())
    assert pickle.loads(pickle.dumps(job)) == job
    r = run_job(job)
    assert pickle.loads(pickle.dumps(r)) == r


def test_the_worker_module_imports_no_tkinter():
    code = "import sys, nms_save_vault.core.index.worker; print('tkinter' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", check=True)
    assert out.stdout.strip() == "False"
    assert callable(worker.run_job) and worker.run_job.__module__ == "nms_save_vault.core.index.worker"
