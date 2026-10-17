"""How times and play time read everywhere in the window (shared by every view)."""
from __future__ import annotations

from datetime import datetime


def local_time(unix: int) -> str:
    """``2026-10-15 23:39`` in this machine's time zone; empty for none."""
    if not unix:
        return ""
    try:
        return datetime.fromtimestamp(unix).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(unix)


def play_time(seconds: int) -> str:
    """``532h16``; empty for none."""
    if not seconds:
        return ""
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60:02d}"
