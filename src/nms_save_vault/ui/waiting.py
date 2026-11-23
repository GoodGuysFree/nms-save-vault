"""Show that the program is working (GGF 2026-11-23: "find all the places where delays could
reach more than 1 second, and implement a 'waiting' icon ... Consider that some people will be
running these 2 programs on a much slower machines. The very least we can do if they must wait,
or if did something that is going to change what they see on-screen and will only see it when
the action is complete - to show a waiting symbol").

Two pointers, as Windows draws them:

- :func:`wait_cursor` - work done on the window's own thread, which cannot answer meanwhile:
  the busy pointer (the hourglass) over the whole window before the work starts, and no
  clicks until it ends (Tk's busy window: a click made while waiting is not acted on later).
- :class:`Indicator` - work done away from the window (a worker job) whose result will change
  what it shows: the arrow with the hourglass ("app starting") while the window still answers.

:func:`time_callbacks` logs every handler of the window that took a second or more, by name,
so a slow machine's log says where it waited."""
from __future__ import annotations

import functools
import logging
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)

#: The pointer while the window cannot answer (Windows: the busy pointer).
WAIT = "watch"
#: The pointer while work goes on away from the window (Windows: "app starting"; elsewhere
#: Tk has no such pointer and the watch is used).
STARTING = "starting"
#: How often the indicator looks at the work (ms).
POLL_MS = 120
#: A handler at least this long is logged (s).
SLOW_SECONDS = 1.0

_held: dict[str, list] = {}     # toplevel path -> [depth, whether this module holds it]


def _top(widget) -> tk.Misc | None:
    """The window holding ``widget``; while that one is not shown yet (a dialog being
    built), the main window, which is what the user sees meanwhile. None for a widget not made
    yet (Tk would look the name up in its interpreter, which is not there)."""
    if "tk" not in getattr(widget, "__dict__", {}):
        return None
    try:
        top = widget.winfo_toplevel()
        if not top.winfo_viewable():
            root = widget._root()
            if root.winfo_viewable():
                return root
        return top
    except (tk.TclError, AttributeError):
        return None


def is_held(widget) -> bool:
    """Whether ``widget``'s window shows the busy pointer (Tk's busy window holds it)."""
    top = _top(widget)
    try:
        return top is not None and bool(int(top.tk.call("tk", "busy", "status", top._w)))
    except tk.TclError:
        return False


@contextmanager
def wait_cursor(widget) -> Iterator[None]:
    """Work on the window's thread that may take a while: the busy pointer over the window
    holding ``widget``, on screen before the work starts, and no clicks until it ends. Nested
    uses hold once; a window already held by someone else is left to them."""
    top = _top(widget)
    if top is None:
        yield
        return
    key = str(top)
    entry = _held.get(key)
    if entry is None:
        mine = False
        try:
            if not is_held(top):
                top.tk.call("tk", "busy", "hold", top._w, "-cursor", WAIT)
                mine = True
            top.update_idletasks()      # the busy window and its pointer reach the screen now
        except tk.TclError:             # the window is going
            pass
        entry = _held[key] = [0, mine]
    entry[0] += 1
    try:
        yield
    finally:
        entry[0] -= 1
        if entry[0] <= 0:
            _held.pop(key, None)
            if entry[1]:
                try:
                    top.tk.call("tk", "busy", "forget", top._w)
                except tk.TclError:
                    pass


def waits(method: Callable) -> Callable:
    """A method of a widget that may take a while: run under :func:`wait_cursor` of ``self``."""
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        with wait_cursor(self):
            return method(self, *args, **kwargs)
    return run


class Indicator:
    """The arrow with the hourglass over ``root`` while ``busy()`` says work away from the
    window is under way (looked at every :data:`POLL_MS`, and at once on :meth:`update`)."""

    def __init__(self, root: tk.Misc, busy: Callable[[], bool]) -> None:
        self.root, self.busy = root, busy
        self.shown = False
        self._after = None
        self._tick()

    def update(self) -> None:
        try:
            on = bool(self.busy())
        except Exception:  # noqa: BLE001 - an indicator never breaks the window
            on = False
        try:
            now = str(self.root.cget("cursor"))
        except tk.TclError:             # the window is going
            return
        if on == self.shown and (not on or now in (STARTING, WAIT)):
            return                      # as it should be (another hand may have reset it)
        self.shown = on
        try:
            try:
                self.root.configure(cursor=STARTING if on else "")
            except tk.TclError:         # "starting" is Windows' own
                self.root.configure(cursor=WAIT if on else "")
        except tk.TclError:             # the window is going
            pass

    def _tick(self) -> None:
        self.update()
        try:
            self._after = self.root.after(POLL_MS, self._tick)
        except tk.TclError:
            self._after = None

    def stop(self) -> None:
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None


def time_callbacks(threshold: float = SLOW_SECONDS, logger: logging.Logger = log) -> None:
    """Log every handler of the window (a command, a binding, an ``after``) that took
    ``threshold`` seconds or more, by name; once per process."""
    if getattr(tk.CallWrapper.__call__, "_timed", False):
        return
    real = tk.CallWrapper.__call__
    depth = [0]

    def timed(self, *args):
        start = time.perf_counter()
        depth[0] += 1
        try:
            return real(self, *args)
        finally:
            depth[0] -= 1
            took = time.perf_counter() - start
            if took >= threshold:
                func = getattr(self, "func", None)
                name = getattr(func, "__qualname__", None) or repr(func)
                logger.info("slow: %s took %.1f s%s", name, took,
                            " (inside another handler)" if depth[0] else "")
    timed._timed = True  # type: ignore[attr-defined]
    tk.CallWrapper.__call__ = timed
