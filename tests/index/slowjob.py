"""Worker targets for the real-pool tests; top level so spawned processes can import them."""
from __future__ import annotations

import os
import time

from nms_save_vault.core.index.worker import IndexJob, IndexResult, run_job


def slow(job: IndexJob) -> IndexResult:
    time.sleep(60)
    return run_job(job)


def crash(job: IndexJob) -> IndexResult:
    raise RuntimeError("worker went away")


def die_on_marker(job: IndexJob) -> IndexResult:
    """The worker process itself ends (as if killed) for data starting with DIE."""
    if job.data.startswith(b"DIE"):
        os._exit(3)
    return run_job(job)
