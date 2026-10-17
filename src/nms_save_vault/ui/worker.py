"""The UI's one background worker (D6 3.9, C31).

Tk is single-threaded: no widget call may come from another thread. Everything slow the
organizer does - a scan and the read model, notes writes, later the live operations - runs
in one worker thread, one job at a time (so a plan never races an execute), and each
result comes back to its callback on the Tk thread through a queue the Tk loop drains every
50 ms. The window never waits on a job: the old model stays on screen until the new one
arrives.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

log = logging.getLogger(__name__)

DRAIN_MS = 50


def on_tk_thread() -> bool:
    """Tk runs on the main thread here; widget helpers assert this."""
    return threading.current_thread() is threading.main_thread()


class UiWorker:
    def __init__(self, root, *, drain_ms: int = DRAIN_MS) -> None:
        self.root = root
        self.drain_ms = drain_ms
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nmsvault-ui")
        self._done: queue.Queue = queue.Queue()
        self._pending = 0
        self._after: str | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        """A job is queued or running, or its result is not delivered yet."""
        return self._pending > 0

    def submit(self, fn: Callable[..., Any], *args,
               on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None, **kwargs) -> Future:
        """Run ``fn(*args, **kwargs)`` in the worker; ``on_done(result)`` or
        ``on_error(exception)`` is then called on the Tk thread. Without ``on_error`` a
        failure is logged."""
        assert on_tk_thread(), "UiWorker.submit is called from the Tk thread"
        if self._closed:
            raise RuntimeError("the UI worker is shut down")
        future = self._executor.submit(fn, *args, **kwargs)
        self._pending += 1
        future.add_done_callback(lambda f: self._done.put((f, on_done, on_error)))
        self._schedule()
        return future

    def _schedule(self) -> None:
        if self._after is None and not self._closed:
            self._after = self.root.after(self.drain_ms, self._drain)

    def _drain(self) -> None:
        """Deliver every finished job's result (on the Tk thread, from ``after``)."""
        self._after = None
        while True:
            try:
                future, on_done, on_error = self._done.get_nowait()
            except queue.Empty:
                break
            self._pending -= 1
            if future.cancelled():
                continue
            error = future.exception()
            try:
                if error is None:
                    if on_done is not None:
                        on_done(future.result())
                elif on_error is not None:
                    on_error(error)
                else:
                    log.error("a background job failed", exc_info=error)
            except Exception:  # noqa: BLE001 - one bad callback must not stop the others
                log.exception("a background job's result could not be shown")
        if self._pending > 0:
            self._schedule()

    def shutdown(self) -> None:
        """Stop delivering; a running job finishes in the background, queued ones are
        dropped."""
        self._closed = True
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except Exception:  # noqa: BLE001 - the root may be gone already
                pass
            self._after = None
        self._executor.shutdown(wait=False, cancel_futures=True)
