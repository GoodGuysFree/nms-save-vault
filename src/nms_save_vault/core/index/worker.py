"""The indexing job a worker runs: bytes in, facts out (D4 2.8.2).

``run_job`` is a top-level function in an import-safe module (no tkinter, no I/O at import),
so a spawned worker process can import and run it. It opens no files: the parent reads the
bytes through the version locator and sends them. It never raises: every outcome is a status.
The facts are stored under the sha256 of the exact bytes decoded, computed here (invariant
I2); bytes that do not hash to the requested version come back as ``mismatch``.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Literal

from .. import savejson
from . import extract
from .deep import deep_facts
from .schema import EXTRACTOR_VERSION

JobKind = Literal["facts", "deep"]          # deep: on-demand detail for a version card
ResultStatus = Literal["ok", "partial", "corrupt", "unsupported", "error", "mismatch"]


@dataclass(frozen=True)
class IndexJob:
    vid: str
    kind: JobKind
    data: bytes                         # verbatim data-file bytes from VersionLocator.open()
    bare_size_hint: int | None = None   # Xbox meta size, for a pre-Waypoint unframed block
    extractor_version: int = EXTRACTOR_VERSION


@dataclass(frozen=True)
class IndexResult:
    vid: str
    kind: JobKind
    actual_sha256: str
    status: ResultStatus
    data_bytes: int
    facts: dict | None = None           # VersionFacts
    deep: dict | None = None            # deep.DeepFacts, for a "deep" job
    timings_ms: dict[str, int] = field(default_factory=dict)
    error: str | None = None            # ours, never save text or a path


def run_job(job: IndexJob) -> IndexResult:
    timings: dict[str, int] = {}
    started = time.perf_counter()
    actual = hashlib.sha256(job.data).hexdigest()
    timings["hash"] = _ms(started)

    def result(status: ResultStatus, **kw) -> IndexResult:
        return IndexResult(job.vid, job.kind, actual, status, len(job.data), timings_ms=timings, **kw)

    if actual != job.vid:
        return result("mismatch", error="the bytes read do not hash to this version")
    try:
        decoded = savejson.decode_save(job.data, bare_size_hint=job.bare_size_hint, timings=timings)
    except savejson.SaveDecodeError as exc:
        return result("corrupt" if exc.kind == "corrupt" else "unsupported", error=str(exc)[:200])
    except MemoryError:
        return result("error", error="MemoryError while decoding")
    except Exception as exc:  # noqa: BLE001 - a worker never raises; the status says what happened
        return result("error", error=extract.describe(exc))
    try:
        t = time.perf_counter()
        if job.kind == "deep":
            found = deep_facts(job.vid, decoded)
            timings["extract"] = _ms(t)
            return result("ok", deep=found)
        facts = extract.run_extractors(decoded)
        timings["extract"] = _ms(t)
        return result(facts["status"], facts=facts)
    except MemoryError:
        return result("error", error="MemoryError while extracting")
    except Exception as exc:  # noqa: BLE001
        return result("error", error=extract.describe(exc))


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)
