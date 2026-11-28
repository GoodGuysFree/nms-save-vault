"""The waiting symbol (GGF 2026-11-23: "if they must wait, or if did something that is going to
change what they see on-screen and will only see it when the action is complete - to show a
waiting symbol"): the busy pointer while the window's own thread works, the arrow with the
hourglass while a worker job runs, and slow handlers logged by name."""
from __future__ import annotations

import logging
import time
import tkinter as tk
from tkinter import ttk

import pytest

from nms_save_vault.ui import waiting


@pytest.fixture
def app(gui_app):
    yield gui_app
    for key in list(waiting._held):
        waiting._held.pop(key, None)
    try:
        gui_app.tk.call("tk", "busy", "forget", gui_app._w)
    except tk.TclError:
        pass


def test_work_on_the_window_s_thread_shows_the_busy_pointer_until_it_ends(app):
    assert not waiting.is_held(app)
    with waiting.wait_cursor(app):
        assert waiting.is_held(app)
        assert app.tk.call("tk", "busy", "cget", app._w, "-cursor") == waiting.WAIT
        with waiting.wait_cursor(app):          # nested: held once
            assert waiting.is_held(app)
        assert waiting.is_held(app)
    assert not waiting.is_held(app)


def test_a_window_already_held_by_someone_else_is_left_to_them(app):
    app.tk.call("tk", "busy", "hold", app._w)
    with waiting.wait_cursor(app):
        pass
    assert waiting.is_held(app)


def test_a_widget_inside_the_window_holds_its_whole_window(app):
    frame = ttk.Frame(app)
    try:
        with waiting.wait_cursor(frame):
            assert waiting.is_held(app)
    finally:
        frame.destroy()
    assert not waiting.is_held(app)


def test_a_method_marked_waits_runs_under_the_busy_pointer(app):
    seen = []

    class Box(ttk.Frame):
        @waiting.waits
        def slow(self):
            seen.append(waiting.is_held(self))
            return 7
    box = Box(app)
    try:
        assert box.slow() == 7 and seen == [True]
    finally:
        box.destroy()
    assert not waiting.is_held(app)


def test_the_indicator_shows_the_arrow_with_the_hourglass_while_work_runs(app):
    work = {"on": False}
    ind = waiting.Indicator(app, lambda: work["on"])
    try:
        work["on"] = True
        ind.update()
        assert str(app.cget("cursor")) in (waiting.STARTING, waiting.WAIT)
        work["on"] = False
        ind.update()
        assert str(app.cget("cursor")) == ""
    finally:
        ind.stop()


def test_the_app_shows_it_while_its_worker_runs_a_job(app):
    assert isinstance(app.indicator, waiting.Indicator)
    import threading
    gate = threading.Event()
    app.worker.submit(lambda: gate.wait(5))
    try:
        app.indicator.update()
        assert str(app.cget("cursor")) in (waiting.STARTING, waiting.WAIT)
    finally:
        gate.set()
    end = time.monotonic() + 10
    while app.worker.working and time.monotonic() < end:
        app.update()
        time.sleep(0.01)
    app.indicator.update()
    assert str(app.cget("cursor")) == ""


def test_a_slow_handler_is_logged_by_name(app, monkeypatch, caplog):
    monkeypatch.setattr(tk.CallWrapper, "__call__", tk.CallWrapper.__call__)
    waiting.time_callbacks(threshold=0.05)

    def sluggish():
        time.sleep(0.08)
    button = ttk.Button(app, command=sluggish)
    caplog.set_level(logging.INFO, logger=waiting.log.name)
    try:
        button.invoke()
    finally:
        button.destroy()
    assert "sluggish" in caplog.text and "slow:" in caplog.text


# --- the indicator over every window of the app (GGF 2026-11-28; cursor audit: a Toplevel's
# pointer is its own, so a dialog showed nothing while its worker job ran) ---------------------


def _cursor(w) -> str:
    return str(w.cget("cursor"))


def test_the_indicator_shows_over_a_dialog_and_one_opened_while_work_runs(app):
    work = {"on": False}
    first = tk.Toplevel(app)
    inner = ttk.Frame(first)
    inner.pack()
    ind = waiting.Indicator(app, lambda: work["on"])
    try:
        work["on"] = True
        ind.update()
        assert _cursor(first) in (waiting.STARTING, waiting.WAIT)
        second = tk.Toplevel(inner)                       # opened from inside a dialog
        ind.update()
        assert _cursor(second) in (waiting.STARTING, waiting.WAIT)
        work["on"] = False
        ind.update()
        assert _cursor(first) == "" and _cursor(second) == "" and _cursor(app) == ""
    finally:
        ind.stop()
        first.destroy()


def test_the_indicator_puts_back_a_dialog_s_own_pointer(app):
    work = {"on": True}
    own = tk.Toplevel(app, cursor="hand2")
    ind = waiting.Indicator(app, lambda: work["on"])
    try:
        ind.update()
        assert _cursor(own) in (waiting.STARTING, waiting.WAIT)
        work["on"] = False
        ind.update()
        assert _cursor(own) == "hand2"
    finally:
        ind.stop()
        own.destroy()


def test_a_dialog_closed_while_work_runs_breaks_nothing(app):
    work = {"on": True}
    gone = tk.Toplevel(app)
    ind = waiting.Indicator(app, lambda: work["on"])
    try:
        ind.update()
        gone.destroy()
        work["on"] = False
        ind.update()
        assert _cursor(app) == ""
    finally:
        ind.stop()
