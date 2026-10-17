"""Small images drawn in code at the current scale (D6 3.10): the ``>`` / ``v`` expand
marker, the sort arrow, radio marks, and the dots of version rows. Nothing loads a bitmap
file, so every image is sharp at any DPI and zoom and takes the theme's colours; callers
cache them and draw them again when the zoom or the theme changes.

Undrawn pixels stay transparent, so zebra rows, group tints and the selection show through.
Tk 8.6 does not anti-alias PhotoImage drawing; at 150 % that is acceptable (D6 8.1).
"""
from __future__ import annotations

import tkinter as tk


class Raster:
    """A sparse pixel buffer: draw shapes, then turn it into a ``tk.PhotoImage``."""

    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = max(1, int(width)), max(1, int(height))
        self.pixels: dict[tuple[int, int], str] = {}

    def dot(self, x: int, y: int, colour: str) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.pixels[(x, y)] = colour

    def rect(self, x0: float, y0: float, x1: float, y1: float, colour: str) -> None:
        for y in range(int(y0), int(y1)):
            for x in range(int(x0), int(x1)):
                self.dot(x, y, colour)

    def disc(self, cx: float, cy: float, r: float, colour: str) -> None:
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - r - 1), int(cx + r + 2)):
                if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r:
                    self.dot(x, y, colour)

    def ring(self, cx: float, cy: float, r: float, thickness: float, colour: str) -> None:
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - r - 1), int(cx + r + 2)):
                d = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5
                if r - thickness <= d <= r:
                    self.dot(x, y, colour)

    def line(self, x0: float, y0: float, x1: float, y1: float, thickness: float,
             colour: str) -> None:
        dx, dy = x1 - x0, y1 - y0
        length = max(1e-6, (dx * dx + dy * dy) ** 0.5)
        steps = int(length * 2) + 1
        r = thickness / 2
        for i in range(steps + 1):
            x, y = x0 + dx * i / steps, y0 + dy * i / steps
            for yy in range(int(y - r), int(y + r) + 1):
                for xx in range(int(x - r), int(x + r) + 1):
                    if (xx + 0.5 - x) ** 2 + (yy + 0.5 - y) ** 2 <= r * r + 0.25:
                        self.dot(xx, yy, colour)

    def poly(self, points: list[tuple[float, float]], colour: str) -> None:
        """A filled polygon (scanline, even-odd)."""
        ys = [p[1] for p in points]
        for y in range(int(min(ys)), int(max(ys)) + 1):
            yc = y + 0.5
            xs = []
            for (xa, ya), (xb, yb) in zip(points, points[1:] + points[:1]):
                if (ya <= yc < yb) or (yb <= yc < ya):
                    xs.append(xa + (yc - ya) * (xb - xa) / (yb - ya))
            xs.sort()
            for a, b in zip(xs[0::2], xs[1::2]):
                for x in range(int(round(a)), int(round(b))):
                    self.dot(x, y, colour)

    def image(self, master: tk.Misc) -> tk.PhotoImage:
        img = tk.PhotoImage(master=master, width=self.w, height=self.h)
        for y in range(self.h):
            x = 0
            while x < self.w:
                colour = self.pixels.get((x, y))
                if colour is None:
                    x += 1
                    continue
                end = x
                while end < self.w and self.pixels.get((end, y)) == colour:
                    end += 1
                img.put(colour, to=(x, y, end, y + 1))
                x = end
        return img


def chevron(width: int, height: int, *, open_: bool, colour: str, scale: float) -> Raster:
    """The expand marker of a save-file line: ``>`` closed, ``v`` open (owner review)."""
    r = Raster(width, height)
    t = max(2.0, 1.6 * scale)
    cy = height / 2
    if open_:
        r.line(4 * scale, cy - 2 * scale, 8.5 * scale, cy + 2.5 * scale, t, colour)
        r.line(8.5 * scale, cy + 2.5 * scale, 13 * scale, cy - 2 * scale, t, colour)
    else:
        r.line(6.5 * scale, cy - 4.5 * scale, 11 * scale, cy, t, colour)
        r.line(11 * scale, cy, 6.5 * scale, cy + 4.5 * scale, t, colour)
    return r


def sort_arrow(width: int, height: int, *, down: bool, colour: str) -> Raster:
    """The arrow in a sorted column's heading: down = descending."""
    r = Raster(width, height)
    w, h = r.w, r.h
    r.poly([(0, 0), (w, 0), (w / 2, h)] if down else [(0, h), (w, h), (w / 2, 0)], colour)
    return r


def radio(size: int, *, on: bool, ring: str, fill: str, scale: float) -> Raster:
    """A radio mark for a pickable row (images only in column #0 in Tk 8.6)."""
    r = Raster(size, size)
    c = size / 2
    r.ring(c, c, 6 * scale, max(1.5, 1.4 * scale), ring)
    if on:
        r.disc(c, c, 3.2 * scale, fill)
    return r


#: What a version row's dot says (D6 3.1.3, the dated list without lanes).
DOT_KINDS = ("dot", "live", "member", "run")


def version_dot(width: int, height: int, kind: str, *, colour: str, scale: float,
                centre: str = "#ffffff") -> Raster:
    """One version row's marker: a dot; a platform-coloured disc with a white centre when
    the version is live; a diamond for a restore-point-only fork member; three small dots
    for a folded run of versions."""
    r = Raster(width, height)
    cx, mid = 7 * scale, height / 2
    if kind == "run":
        for dy in (-5 * scale, 0, 5 * scale):
            r.disc(cx, mid + dy, 1.5 * scale, colour)
    elif kind == "live":
        r.disc(cx, mid, 5.2 * scale, colour)
        r.disc(cx, mid, 2 * scale, centre)
    elif kind == "member":
        s = 4.5 * scale
        r.poly([(cx, mid - s), (cx + s, mid), (cx, mid + s), (cx - s, mid)], colour)
    else:
        r.disc(cx, mid, 3.2 * scale, colour)
    return r
