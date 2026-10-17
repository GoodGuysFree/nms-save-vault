"""Worker targets for the real-pool tests; top level so spawned processes can import them."""
from __future__ import annotations

import time

from nms_save_vault.core.index.worker import IndexJob, IndexResult, run_job


def slow(job: IndexJob) -> IndexResult:
    time.sleep(60)
    return run_job(job)


def crash(job: IndexJob) -> IndexResult:
    raise RuntimeError("worker went away")
