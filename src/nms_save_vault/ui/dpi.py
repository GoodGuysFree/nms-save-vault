"""DPI awareness and the ``px()`` helper (D6 3.10, 3.11).

Without awareness Windows draws the whole window as a bitmap and stretches it, which is
what made the app blurry at 150 %. With system awareness Tk sees the real DPI and scales
point-sized fonts by itself, but every constant given in pixels (column widths, paddings,
``wraplength``, canvas geometry, image sizes, dialog minimum sizes) stays the same number
of physical pixels and so shrinks next to the text. ``px(n)`` turns a size designed at
96 dpi and zoom 1.0 into pixels for this screen and the current font zoom.

One Tk root per process, so the factor is module state: :func:`configure` reads it from
the root once and again whenever the zoom changes. What only the OS can tell (making the
process DPI-aware, the work area, the window's frame) lives in ``gui``, which may branch
on the OS; this module stays platform-neutral.
"""
from __future__ import annotations

#: The DPI every pixel constant in the UI was designed at (Windows' 100 %).
BASE_DPI = 96.0

_pixels_per_inch = BASE_DPI
_zoom = 1.0


def scaled(n: float, pixels_per_inch: float, zoom: float) -> int:
    """``n`` design pixels at ``pixels_per_inch`` and ``zoom``, rounded to whole pixels."""
    return int(round(n * pixels_per_inch / BASE_DPI * zoom))


def configure(root=None, *, zoom: float | None = None) -> None:
    """Read the screen's pixels per inch from ``root`` and/or set the zoom factor."""
    global _pixels_per_inch, _zoom
    if root is not None:
        _pixels_per_inch = float(root.winfo_fpixels("1i"))
    if zoom is not None:
        _zoom = float(zoom)


def factor() -> float:
    """What one design pixel is worth now (screen scale x zoom)."""
    return _pixels_per_inch / BASE_DPI * _zoom


def zoom() -> float:
    return _zoom


def px(n: float) -> int:
    """``n`` design pixels (96 dpi, zoom 1.0) in pixels for this screen and zoom."""
    return scaled(n, _pixels_per_inch, _zoom)


def screen_px(n: float) -> int:
    """``n`` logical pixels for this screen, without the font zoom: window sizes, which
    the zoom must not push off the screen."""
    return scaled(n, _pixels_per_inch, 1.0)


def pxs(*values: float) -> tuple[int, ...]:
    """:func:`px` of each value: ``padding=pxs(14, 0, 14, 12)``."""
    return tuple(px(v) for v in values)


def first_start_size(area: tuple[int, int], minimum: tuple[int, int],
                     chrome: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """80 % of the work area, at least ``minimum``, and never so big that the window with
    its ``chrome`` leaves the work area (D6 3.15)."""
    out = []
    for have, least, frame in zip(area, minimum, chrome):
        room = max(1, have - frame)
        out.append(max(1, min(room, max(least, int(have * 0.8)))))
    return out[0], out[1]


def fit_to_content(win, min_width: float, min_height: float) -> None:
    """Size a dialog to what it holds at the current font size (R2 D6: fixed sizes clipped
    the buttons at a large zoom): at least the design minimum, at most 95 % x 90 % of the
    screen."""
    win.update_idletasks()
    width = min(max(px(min_width), win.winfo_reqwidth()), int(win.winfo_screenwidth() * 0.95))
    height = min(max(px(min_height), win.winfo_reqheight()), int(win.winfo_screenheight() * 0.9))
    win.geometry(f"{width}x{height}")
