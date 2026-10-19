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
import weakref
from collections.abc import Callable
import itertools
from concurrent.futures import Future
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
        # One thread, one job at a time; a priority queue so that a plan or a write the owner
        # is waiting on never sits behind a scan or an index read (``low=True``).
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread = threading.Thread(target=self._loop, name="nmsvault-ui-1", daemon=True)
        self._thread.start()
        self._done: queue.Queue = queue.Queue()
        self._pending = 0
        self._foreground = 0      # queued or running jobs that are not background ones
        self._after: str | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        """A job is queued or running, or its result is not delivered yet."""
        return self._pending > 0

    @property
    def working(self) -> bool:
        """A job that is not a background one (a scan, a refresh) is queued or running: what
        closing the window must not cut off."""
        return self._foreground > 0

    def _loop(self) -> None:
        while True:
            _prio, _n, item = self._jobs.get()
            if item is None:
                return
            future, fn, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - delivered through the future
                future.set_exception(exc)

    def submit(self, fn: Callable[..., Any], *args,
               on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None,
               low: bool = False, background: bool = False, **kwargs) -> Future:
        """Run ``fn(*args, **kwargs)`` in the worker; ``on_done(result)`` or
        ``on_error(exception)`` is then called on the Tk thread. Without ``on_error`` a
        failure is logged. A ``low`` or ``background`` job only reads and is not counted in
        :attr:`working`."""
        assert on_tk_thread(), "UiWorker.submit is called from the Tk thread"
        if self._closed:
            raise RuntimeError("the UI worker is shut down")
        future = Future()
        self._jobs.put((1 if low else 0, next(self._seq), (future, fn, args, kwargs)))
        self._pending += 1
        counted = not (low or background)
        self._foreground += counted
        future.add_done_callback(lambda f: self._done.put((f, on_done, on_error, counted)))
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
                future, on_done, on_error, counted = self._done.get_nowait()
            except queue.Empty:
                break
            self._pending -= 1
            self._foreground -= counted
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

    def shutdown(self, wait: bool = False, timeout: float | None = None) -> None:
        """Stop delivering; queued jobs are dropped. A running job finishes: in the background,
        or, with ``wait``, before this returns (at most ``timeout`` seconds)."""
        self._closed = True
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except Exception:  # noqa: BLE001 - the root may be gone already
                pass
            self._after = None
        while True:                                    # queued jobs are dropped
            try:
                _prio, _n, item = self._jobs.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[0].cancel()
        self._jobs.put((2, next(self._seq), None))     # the running job finishes, then it stops
        if wait and threading.current_thread() is not self._thread:
            self._thread.join(timeout)


def weak(method: Callable[..., Any], *lead) -> Callable[..., None]:
    """``method`` (a widget's bound method) for a job's callbacks, without keeping the widget
    alive: the worker may drop the last reference to a callback, and a widget (with its Tk
    variables) must only ever be released on the Tk thread. Calls nothing once it is gone."""
    ref = weakref.WeakMethod(method)

    def call(*args) -> None:
        target = ref()
        if target is not None:
            target(*lead, *args)
    return call


def finisher(ref, gen: int, on_done, on_recover) -> Callable[[Any], None]:
    """The callback of an executed plan: the dialog's own handler while the dialog object is
    there (it also copes with a closed one); otherwise the outcome still reaches what the
    dialog was given (the Done bar, the rescan, Recover), never the widget."""
    def call(outcome) -> None:
        dialog = ref()
        if dialog is not None:
            dialog._executed(gen, outcome)
        elif outcome.status == "done" and on_done is not None:
            on_done(outcome)
        elif outcome.status in ("failed", "rolled_back") and on_recover is not None:
            on_recover()
    return call


def release_tk_variables(*owners) -> None:
    """Drop every Tk variable an owner (a dialog, a picker) holds, on the Tk thread, as the
    owner is destroyed. A variable that outlives its window sits in a reference cycle, and the
    garbage collector may then free it on whichever thread happens to trigger a collection -
    the worker - where Tk refuses ("main thread is not in main loop")."""
    import tkinter as tk

    for owner in owners:
        if owner is None:
            continue
        for name, value in list(vars(owner).items()):
            if isinstance(value, tk.Variable):
                setattr(owner, name, None)
            elif isinstance(value, dict) and any(isinstance(v, tk.Variable) for v in value.values()):
                value.clear()
