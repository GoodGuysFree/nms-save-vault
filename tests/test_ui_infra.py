"""The UI's building blocks (D6 U6): the worker, the theme registry for classic widgets,
the redaction helpers and the images drawn in code."""
from __future__ import annotations

import gc
import threading
import time
import tkinter as tk

import pytest

from nms_save_vault import theme
from nms_save_vault.core import aliases
from nms_save_vault.ui import raster, redact
from nms_save_vault.ui.themeaware import ThemeRegistry
from nms_save_vault.ui.worker import UiWorker, on_tk_thread

ACCOUNT = "st_76561198000000001"


def _wait(app, condition, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        app.update()
        time.sleep(0.005)


# --- the worker -----------------------------------------------------------------------


def test_results_come_back_on_the_tk_thread(gui_app):
    worker = UiWorker(gui_app, drain_ms=5)
    seen = []
    try:
        worker.submit(lambda a, b: (threading.current_thread().name, a + b), 2, b=3,
                      on_done=lambda r: seen.append((r, on_tk_thread())))
        assert worker.busy
        _wait(gui_app, lambda: seen)
        (((thread, total), tk_thread),) = seen
        assert total == 5 and tk_thread and thread.startswith("nmsvault-ui")
        assert not worker.busy
    finally:
        worker.shutdown()


def test_jobs_run_one_at_a_time_in_order(gui_app):
    worker = UiWorker(gui_app, drain_ms=5)
    running, order, overlap = [], [], []

    def job(n):
        if running:
            overlap.append(n)
        running.append(n)
        time.sleep(0.02)
        running.remove(n)
        return n

    try:
        for n in range(4):
            worker.submit(job, n, on_done=order.append)
        _wait(gui_app, lambda: len(order) == 4)
        assert order == [0, 1, 2, 3] and overlap == []
    finally:
        worker.shutdown()


def test_a_failure_reaches_on_error_or_the_log(gui_app, caplog):
    worker = UiWorker(gui_app, drain_ms=5)
    errors = []

    def boom():
        raise ValueError("the vault went away")

    try:
        worker.submit(boom, on_error=errors.append)
        with caplog.at_level("ERROR"):
            worker.submit(boom)
            _wait(gui_app, lambda: not worker.busy)
        assert isinstance(errors[0], ValueError)
        assert "a background job failed" in caplog.text
    finally:
        worker.shutdown()


def test_a_callback_that_fails_does_not_stop_the_others(gui_app, caplog):
    worker = UiWorker(gui_app, drain_ms=5)
    got = []

    def bad(_):
        raise RuntimeError("widget gone")

    try:
        worker.submit(lambda: 1, on_done=bad)
        worker.submit(lambda: 2, on_done=got.append)
        _wait(gui_app, lambda: not worker.busy)
        assert got == [2]
    finally:
        worker.shutdown()


def test_submit_is_for_the_tk_thread_only(gui_app):
    worker = UiWorker(gui_app)
    failures = []

    def from_elsewhere():
        try:
            worker.submit(lambda: None)
        except AssertionError as exc:
            failures.append(exc)

    thread = threading.Thread(target=from_elsewhere)
    thread.start()
    thread.join()
    worker.shutdown()
    assert failures
    with pytest.raises(RuntimeError):
        worker.submit(lambda: None)


def test_the_app_has_a_worker_and_a_theme_registry(gui_app):
    assert isinstance(gui_app.worker, UiWorker) and isinstance(gui_app.themed, ThemeRegistry)


# --- the theme registry -----------------------------------------------------------------


class _Widget:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.palettes = []

    def apply_palette(self, p):
        if self.fail:
            raise RuntimeError("canvas destroyed")
        self.palettes.append(p["bg"])


def test_registered_widgets_follow_every_theme_change():
    registry = ThemeRegistry()
    broken = _Widget(fail=True)
    registry.register(broken)
    early = _Widget()
    registry.register(early)
    assert early.palettes == []                          # no palette known yet
    registry.apply(theme.PALETTES[theme.LIGHT])          # the broken one is skipped
    late = _Widget()
    registry.register(late)
    assert late.palettes == [theme.PALETTES[theme.LIGHT]["bg"]]      # coloured at once
    registry.apply(theme.PALETTES[theme.DARK])
    assert early.palettes[-1] == late.palettes[-1] == theme.PALETTES[theme.DARK]["bg"]


def test_a_widget_that_is_gone_drops_out():
    registry = ThemeRegistry()
    widget = _Widget()
    registry.register(widget)
    assert len(registry) == 1
    del widget
    gc.collect()
    assert len(registry) == 0


def test_the_app_recolours_registered_widgets_on_a_theme_switch(gui_app):
    widget = _Widget()
    gui_app.themed.register(widget)
    original = gui_app.state.theme
    try:
        gui_app.state.theme = theme.DARK
        gui_app._apply_theme()
        assert widget.palettes[-1] == theme.PALETTES[theme.DARK]["bg"]
    finally:
        gui_app.state.theme = original
        gui_app._apply_theme()


# --- redaction ---------------------------------------------------------------------------


@pytest.fixture
def alias(monkeypatch):
    names = aliases.AliasMap()
    names.set(ACCOUNT, "Steam main")
    monkeypatch.setattr(aliases, "_active", names)


def test_every_tree_call_is_filtered(gui_app, alias):
    tree = redact.RedactingTreeview(gui_app, columns=("a",))
    try:
        row = tree.insert("", "end", text=f"{ACCOUNT} slot 3", values=(ACCOUNT,))
        tree.item(row, text=f"now {ACCOUNT}")
        tree.set(row, "a", f"C:/x/{ACCOUNT}")
        tree.heading("a", text=ACCOUNT)
        texts = [tree.item(row, "text"), tree.set(row, "a"), tree.heading("a", "text")]
        assert all("76561198000000001" not in t and "Steam main" in t for t in texts)
    finally:
        tree.destroy()


def test_text_label_canvas_and_title_helpers_are_filtered(gui_app, alias):
    frame = tk.Frame(gui_app)
    try:
        text = tk.Text(frame)
        redact.rtext(text, "end", f"in {ACCOUNT}", "k")
        label = redact.rlabel(frame, text=f"{ACCOUNT} live")
        redact.set_label(label, f"{ACCOUNT} again")
        canvas = tk.Canvas(frame)
        item = redact.ctext(canvas, 1, 1, text=ACCOUNT)
        top = tk.Toplevel(frame)
        top.withdraw()
        redact.rtitle(top, f"History - {ACCOUNT}")
        shown = [text.get("1.0", "end"), label.cget("text"), canvas.itemcget(item, "text"),
                 top.title()]
        assert all("76561198000000001" not in s and "Steam main" in s for s in shown)
        assert "k" in text.tag_names("1.0")
    finally:
        frame.destroy()


def test_x11_shows_only_latin1(monkeypatch):
    class X11:
        tk = type("Tk", (), {"call": staticmethod(lambda *a: "x11")})()

    class Win:
        tk = type("Tk", (), {"call": staticmethod(lambda *a: "win32")})()

    text = "Settlement \u0394\u03c9 caf\u00e9"
    assert redact.shown(X11(), text) == "Settlement ?? caf\u00e9"
    assert redact.shown(Win(), text) == text
    assert redact.shown(Win(), 42) == 42


# --- images drawn in code ------------------------------------------------------------------


def _pixels(r: raster.Raster) -> set:
    return set(r.pixels)


def test_chevrons_closed_and_open_differ_and_fit_the_row(gui_app):
    closed = raster.chevron(18, 24, open_=False, colour="#101010", scale=1.0)
    opened = raster.chevron(18, 24, open_=True, colour="#101010", scale=1.0)
    assert _pixels(closed) and _pixels(opened) and _pixels(closed) != _pixels(opened)
    img = closed.image(gui_app)
    assert (img.width(), img.height()) == (18, 24)
    assert img.transparency_get(0, 0)                     # undrawn: the row shows through
    x, y = next(iter(closed.pixels))
    assert not img.transparency_get(x, y)


def test_the_chevron_scales_with_the_screen():
    small = raster.chevron(18, 24, open_=False, colour="#000000", scale=1.0)
    big = raster.chevron(36, 48, open_=False, colour="#000000", scale=2.0)
    assert len(big.pixels) > 2 * len(small.pixels)


def test_sort_arrows_point_their_way():
    down = raster.sort_arrow(10, 6, down=True, colour="#000000")
    up = raster.sort_arrow(10, 6, down=False, colour="#000000")
    assert (5, 0) in down.pixels and (5, 5) not in down.pixels
    assert (5, 5) in up.pixels and (5, 0) not in up.pixels


def test_radio_and_version_dots():
    off = raster.radio(16, on=False, ring="#000000", fill="#0000ff", scale=1.0)
    on = raster.radio(16, on=True, ring="#000000", fill="#0000ff", scale=1.0)
    assert "#0000ff" in on.pixels.values() and "#0000ff" not in off.pixels.values()
    shapes = {kind: _pixels(raster.version_dot(20, 24, kind, colour="#808080", scale=1.0))
              for kind in raster.DOT_KINDS}
    assert len({frozenset(s) for s in shapes.values()}) == len(raster.DOT_KINDS)
    live = raster.version_dot(20, 24, "live", colour="#1f5fa8", scale=1.0)
    assert {"#1f5fa8", "#ffffff"} <= set(live.pixels.values())
