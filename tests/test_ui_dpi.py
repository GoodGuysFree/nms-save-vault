"""DPI awareness and px() (D6 3.10, 3.11)."""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from nms_save_vault.ui import dpi

SRC = Path(__file__).resolve().parents[1] / "src" / "nms_save_vault"


@pytest.fixture
def restore_dpi():
    """px() is module state; put back whatever the session's App configured."""
    saved = (dpi._pixels_per_inch, dpi._zoom)
    yield
    dpi._pixels_per_inch, dpi._zoom = saved


# --- the pure function ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "ppi", "zoom", "expected"),
    [
        (100, 96, 1.0, 100),      # 100 %: unchanged
        (100, 144, 1.0, 150),     # 150 %: what an aware process sees on the owner's screen
        (100, 192, 1.0, 200),     # 200 %
        (100, 96, 1.2, 120),      # zoom alone
        (100, 144, 1.2, 180),     # both multiply
        (14, 120, 1.0, 18),       # 125 %: 17.5 rounds to the even neighbour
        (0, 144, 2.0, 0),
        (-16, 144, 1.0, -24),     # offsets keep their sign
    ],
)
def test_scaled_multiplies_by_screen_scale_and_zoom(n, ppi, zoom, expected):
    assert dpi.scaled(n, ppi, zoom) == expected


def test_px_follows_configure(restore_dpi):
    class FakeRoot:
        def winfo_fpixels(self, what):
            assert what == "1i"
            return 144.0

    dpi.configure(FakeRoot(), zoom=1.0)
    assert dpi.px(10) == 15
    assert dpi.pxs(14, 0, 14, 12) == (21, 0, 21, 18)
    dpi.configure(zoom=2.0)
    assert dpi.px(10) == 30
    assert dpi.factor() == pytest.approx(3.0)
    assert dpi.zoom() == 2.0


def test_first_start_uses_80_percent_of_the_work_area_with_a_minimum():
    assert dpi.first_start_size((2000, 1000), (1000, 640)) == (1600, 800)
    assert dpi.first_start_size((1100, 700), (1000, 640)) == (1000, 640)   # minimum wins
    assert dpi.first_start_size((900, 600), (1000, 640)) == (900, 600)     # never off-screen


def test_the_first_window_with_its_chrome_fits_the_work_area():
    """The size given to geometry() is the inside: the title bar, menu bar and borders come
    on top, and must not push the window past the taskbar."""
    assert dpi.first_start_size((1100, 700), (1000, 640), (16, 90)) == (1000, 610)
    assert dpi.first_start_size((2000, 1000), (1000, 640), (16, 90)) == (1600, 800)
    assert dpi.first_start_size((900, 600), (1000, 640), (16, 90)) == (884, 510)


def test_the_minimum_start_size_follows_the_dpi_not_the_zoom(restore_dpi):
    class Root:
        def winfo_fpixels(self, _):
            return 144.0

    dpi.configure(Root(), zoom=2.0)
    assert dpi.screen_px(1000) == 1500 and dpi.px(1000) == 3000


def test_window_chrome_is_positive(gui_app):
    width, height = dpi.window_chrome(gui_app)
    assert width >= 0 and height > 0


# --- awareness ----------------------------------------------------------------------------------


def test_awareness_is_windows_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert dpi.enable_awareness() is False


def test_awareness_asks_for_system_awareness(monkeypatch):
    import ctypes

    calls = []
    shcore = types.SimpleNamespace(SetProcessDpiAwareness=lambda v: calls.append(v) or 0)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(shcore=shcore), raising=False)
    assert dpi.enable_awareness() is True
    assert calls == [1], "system-aware (1); per-monitor (2) needs WM_DPICHANGED that Tk lacks"


def test_awareness_falls_back_without_shcore(monkeypatch):
    import ctypes

    calls = []
    user32 = types.SimpleNamespace(SetProcessDPIAware=lambda: calls.append("vista") or 1)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=user32), raising=False)
    assert dpi.enable_awareness() is True
    assert calls == ["vista"]


def test_main_turns_awareness_on_before_the_first_root(monkeypatch):
    from nms_save_vault import gui

    order = []

    class FakeApp:
        def __init__(self):
            order.append("App")

        def mainloop(self):
            order.append("mainloop")

    monkeypatch.setattr(gui.logfile, "configure", lambda folder: order.append("log"))
    monkeypatch.setattr(gui.dpi, "enable_awareness", lambda: order.append("dpi"))
    monkeypatch.setattr(gui.aliases, "active", lambda: None)
    monkeypatch.setattr(gui, "App", FakeApp)
    assert gui.main() == 0
    assert order == ["log", "dpi", "App", "mainloop"]


# --- every pixel constant goes through px() -------------------------------------------------


_BARE = [
    re.compile(r"\b(padding|padx|pady|wraplength|length|sashthickness)=\(?\d"),
    re.compile(r"geometry\(\s*f?\"\d+x\d+"),
    re.compile(r"\.column\([^)]*width=\d"),
    re.compile(r"metrics\(\"linespace\"\)\s*\+\s*\d"),
]


@pytest.mark.parametrize("name", ["gui.py", "theme.py"])
def test_no_bare_pixel_constant_is_left(name):
    """Entry and Combobox widths are characters, not pixels, and are not matched."""
    found = []
    for number, line in enumerate((SRC / name).read_text(encoding="utf-8").splitlines(), 1):
        if any(p.search(line) for p in _BARE):
            found.append(f"{name}:{number}: {line.strip()}")
    assert not found, "\n".join(found)


# --- zoom re-derives the pixel sizes --------------------------------------------------------


def test_zoom_moves_px_and_stretches_the_columns_by_the_same_ratio(gui_app, monkeypatch):
    from tkinter import ttk

    from nms_save_vault import gui

    monkeypatch.setattr(gui.appstate, "save", lambda *a, **k: None)
    original = gui_app.state.font_scale
    try:
        gui_app.state.font_scale = 1.0
        gui_app._apply_theme()
        assert dpi.zoom() == 1.0
        gui_app.live_tree.column("name", width=dpi.px(400))   # as if dragged wider
        name_before = int(gui_app.live_tree.column("name", "width"))
        tree_before = int(gui_app.backup_tree.column("#0", "width"))
        pad_before = ttk.Style(gui_app).lookup("TButton", "padding")

        gui_app._zoom(1)
        assert dpi.zoom() == 1.1
        assert int(gui_app.live_tree.column("name", "width")) == round(name_before * 1.1)
        assert int(gui_app.backup_tree.column("#0", "width")) == round(tree_before * 1.1)
        assert ttk.Style(gui_app).lookup("TButton", "padding") != pad_before

        gui_app._zoom(-1)
        assert dpi.zoom() == 1.0
        assert int(gui_app.live_tree.column("name", "width")) == name_before
    finally:
        gui_app.state.font_scale = original
        gui_app._apply_theme()
        for column in gui.LIVE_COLUMNS:
            gui_app.live_tree.column(column.key, width=dpi.px(column.width))


class _FakeWindow:
    def __init__(self, req, screen=(3000, 2000)):
        self.req, self.screen, self.set = req, screen, None

    def update_idletasks(self):
        pass

    def winfo_reqwidth(self):
        return self.req[0]

    def winfo_reqheight(self):
        return self.req[1]

    def winfo_screenwidth(self):
        return self.screen[0]

    def winfo_screenheight(self):
        return self.screen[1]

    def geometry(self, spec):
        self.set = spec


def test_dialogs_size_from_content_with_a_scaled_minimum(restore_dpi):
    dpi._pixels_per_inch = 96.0
    dpi.configure(zoom=1.5)
    small = _FakeWindow((200, 100))
    dpi.fit_to_content(small, 300, 200)
    assert small.set == "450x300", "the design minimum, scaled"
    big = _FakeWindow((900, 700))
    dpi.fit_to_content(big, 300, 200)
    assert big.set == "900x700", "the content when it needs more (R2 D6: no clipped buttons)"
    huge = _FakeWindow((5000, 5000))
    dpi.fit_to_content(huge, 300, 200)
    assert huge.set == "2850x1800", "never larger than the screen"
